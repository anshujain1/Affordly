"""Financial-state reconstruction.

Turns a user's raw event history (+ evidence from messages/images) into a
dated list of *future cash flows* for the 90-day safety forecast.

The reconstruction is deliberately rule based and auditable:

* settled history  -> recurring streams (constant-gap or calendar-monthly)
* pending debits   -> reserved on their settlement date
* pending credits  -> ignored (not spendable yet)
* scheduled events -> known future flows
* failed / cancelled / unrealized / duplicate / one-off events -> never projected
* income           -> only *stable, confirmed* income is projected
"""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from .data import Event, Profile
from .evidence import Evidence
from .fx import FxConverter

ZERO = Decimal(0)

# --------------------------------------------------------------------------
# Salary taxonomy (descriptions emitted by the dataset generator)
# --------------------------------------------------------------------------
STABLE_SALARY = {
    "Payroll credit", "First-job payroll", "Base salary", "Primary household salary",
    "International employer payroll", "New employer payroll", "Previous employer payroll",
    "Payroll before leave", "Payroll after returning from leave", "Next confirmed salary",
    "Final employer payroll",
}
# Income that is real cash but never a repeating promise.
ONE_OFF_INCOME = {
    "Prorated first salary", "Promotion arrears payment", "Quarterly performance bonus",
    "Prize proceeds", "August 2019 net salary", "Investment sale proceeds",
    "Employer expense reimbursement",
}
# Contract-style pay that the generator uses for "contract ended" scenarios.
SEASONAL_INCOME = {"Seasonal contract payment", "Temporary assignment pay", "Peak-season wages"}
VARIABLE_INCOME = {
    "Performance commission", "Monthly sales commission", "Account commission payment",
    "Second household income", "Delivery platform payout", "Driver platform payout",
    "Task marketplace payout", "Weekly app earnings",
    "Website project payment", "Content contract payment", "Consulting invoice payment",
    "Freelance milestone payment", "Independent work payment", "Application project payment",
    "Client retainer payment", "Design contract payment",
}


def _median(v):
    v = sorted(v); n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


ESTIMATOR = "mean"
PENDING_RESERVED = True


SNAP_SIG = 0


def _snap(x: Decimal) -> Decimal:
    if not SNAP_SIG or x == 0:
        return x
    import math
    mag = 10 ** (math.floor(math.log10(abs(float(x)))) - SNAP_SIG + 1)
    mag = Decimal(str(mag)) if mag >= 1 else Decimal(1).scaleb(int(math.log10(mag)))
    return (x / mag).quantize(Decimal(1)) * mag


def estimate(vals: list) -> Decimal:
    """Typical amount of a recurring stream."""
    # fixed amounts (identical every time) are kept exactly
    if len(set(vals)) == 1:
        return vals[0]
    return _snap(_estimate(vals))


def _estimate(vals: list) -> Decimal:
    name = ESTIMATOR
    if name == "mean":
        return sum(vals) / len(vals)
    if name == "median":
        return _median(vals)
    if name == "last":
        return vals[-1]
    if name.startswith("last"):
        k = int(name[4:]); t = vals[-k:]
        return sum(t) / len(t)
    if name == "midrange":
        return (min(vals) + max(vals)) / 2
    if name == "max":
        return max(vals)
    if name == "p75":
        v = sorted(vals); return v[int(0.75 * (len(v) - 1))]
    if name == "trim":
        v = sorted(vals)
        if len(v) > 4:
            v = v[1:-1]
        return sum(v) / len(v)
    raise ValueError(name)


@dataclass(frozen=True)
class Flow:
    on: date
    amount: Decimal                 # signed: credit > 0, debit < 0
    kind: str                       # series | known | income | evidence
    label: str
    category: str = ""
    series_key: str = ""
    phase: int = 2                  # intra-day order: 0 interval debits, 1 credits, 2 monthly/known debits


@dataclass
class Stream:
    """A reconstructed recurring expense."""
    key: str
    category: str
    interval: str                   # "M" (calendar month) or "<days>"
    anchor: date                    # last regular cash date
    dom: int
    amount: Decimal
    n: int
    flexibility: str
    min_allowed: Optional[Decimal]
    last_event_id: str
    description: str = ""

    def occurrences(self, start: date, end: date) -> list[date]:
        out: list[date] = []
        cur = self.anchor
        if self.interval == "M":
            k = 0
            while True:
                k += 1
                cur = add_months(self.anchor, k, self.dom)
                if cur > end:
                    break
                if cur >= start:
                    out.append(cur)
        else:
            step = int(self.interval)
            while True:
                cur = cur + timedelta(days=step)
                if cur > end:
                    break
                if cur >= start:
                    out.append(cur)
        return out


def add_months(d: date, k: int, dom: Optional[int] = None) -> date:
    dom = dom or d.day
    y = d.year + (d.month - 1 + k) // 12
    m = (d.month - 1 + k) % 12 + 1
    return date(y, m, min(dom, calendar.monthrange(y, m)[1]))


@dataclass
class Ledger:
    user_id: str
    home: str
    start: date
    end: date
    flows: list[Flow] = field(default_factory=list)
    streams: dict[str, Stream] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    income_active: bool = True
    salary_amount: Optional[Decimal] = None

    def sorted_flows(self) -> list[Flow]:
        return sorted(self.flows, key=lambda f: (f.on, f.amount > 0))


# --------------------------------------------------------------------------
def _home_amount(ev: Event, fx: FxConverter, home: str, ev_amounts: dict[str, Decimal]) -> Optional[Decimal]:
    amt = ev.amount if ev.amount is not None else ev_amounts.get(ev.event_id)
    if amt is None:
        return None
    if ev.currency != home:
        amt = fx.convert(amt, ev.currency, home, ev.cash_date)
    return amt


def _detect_interval(dates: list[date]) -> Optional[tuple[str, int]]:
    """Return ("M", dom) or (str(days), 0) when the dates form a clean cadence."""
    if len(dates) < 3:
        return None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    if all(28 <= g <= 31 for g in gaps):
        dom = Counter(d.day for d in dates).most_common(1)[0][0]
        return "M", dom
    g = Counter(gaps).most_common(1)[0][0]
    if 3 <= g <= 27 and sum(1 for x in gaps if x == g) >= max(2, int(0.8 * len(gaps))):
        return str(g), 0
    return None


def build_ledger(
    events: list[Event],
    profile: Profile,
    request_date: date,
    horizon_days: int,
    fx: FxConverter,
    evidence: Evidence,
) -> Ledger:
    home = profile.home_currency
    end = request_date + timedelta(days=horizon_days)
    led = Ledger(profile.user_id, home, request_date, end)

    by_id = {e.event_id: e for e in events}
    desc_count = Counter()
    for e in events:
        desc_count[e.description] += 1

    amounts = evidence.event_amounts
    failed_by_cat: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        if e.status == "failed" and e.direction == "debit":
            failed_by_cat[e.category].append(e)
    settled_debits: list[Event] = []
    salary_events: list[Event] = []
    other_credits: list[Event] = []

    for e in events:
        if e.direction == "non_cash" or e.status in ("unrealized", "cancelled", "failed"):
            continue
        if e.status == "settled":
            if e.direction == "debit":
                settled_debits.append(e)
            elif e.category == "salary":
                salary_events.append(e)
            continue
        if e.status == "scheduled" and e.category == "salary" and e.direction == "credit":
            salary_events.append(e)
            continue

    # ---------------- recurring expense streams -----------------
    groups: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for e in settled_debits:
        if e.linked_event_id or e.event_id in evidence.not_recurring:
            continue
        if e.amount is None and e.event_id not in amounts:
            continue
        if e.cash_date >= request_date:
            continue
        groups[(e.category, "debit")].append(e)

    for (cat, _), evs in groups.items():
        evs = sorted(evs, key=lambda x: x.cash_date)
        # drop one-off descriptors (rare descriptions within the whole dataset are
        # encoded by the single-occurrence / amount-outlier tests below)
        main = _regular_core(evs, amounts, fx, home)
        if len(main) < 3:
            continue
        cad = _detect_interval([m.cash_date for m in main])
        if cad is None:
            continue
        interval, dom = cad
        vals = [_home_amount(m, fx, home, amounts) for m in main]
        vals = [v for v in vals if v is not None]
        amount = estimate(vals)
        last = main[-1]
        key = f"{cat}"
        anchor = main[-1].cash_date
        # A failed attempt consumes that billing cycle: it is not re-charged unless an
        # explicit scheduled retry exists (those are picked up as known flows below).
        for fe in failed_by_cat.get(cat, []):
            if anchor < fe.cash_date < request_date:
                anchor = fe.cash_date
        led.streams[key] = Stream(
            key=key, category=cat, interval=interval, anchor=anchor, dom=dom or main[-1].cash_date.day,
            amount=amount, n=len(main), flexibility=last.flexibility, min_allowed=last.min_allowed,
            last_event_id=last.event_id, description=last.description)

    # rent increases etc. -------------------------------------------------
    for s in led.streams.values():
        if s.category == "rent" and evidence.rent_increase_pct:
            s.amount = s.amount * (Decimal(1) + evidence.rent_increase_pct / Decimal(100))
            led.notes.append(f"rent +{evidence.rent_increase_pct}%")

    for s in led.streams.values():
        for d in s.occurrences(request_date, end):
            led.flows.append(Flow(d, -s.amount, "series", s.description or s.category, s.category, s.key,
                                  phase=2 if s.interval == "M" else 0))

    # ---------------- known future flows -----------------
    for e in events:
        if e.direction == "non_cash":
            continue
        if e.status == "pending" and e.direction == "debit":
            amt = _home_amount(e, fx, home, amounts)
            if not PENDING_RESERVED:
                continue
            if amt is None or e.event_id in evidence.ignore_events:
                continue
            on = max(e.cash_date, request_date)
            led.flows.append(Flow(on, -amt, "known", e.description, e.category))
        elif e.status == "scheduled" and e.direction == "debit":
            amt = _home_amount(e, fx, home, amounts)
            if amt is None:
                continue
            on = max(e.cash_date, request_date)
            led.flows.append(Flow(on, -amt, "known", e.description, e.category))

    # ---------------- income -----------------
    _income_flows(led, salary_events, request_date, end, fx, home, evidence, amounts)
    return led


def _regular_core(evs: list[Event], amounts, fx, home) -> list[Event]:
    """Strip amount outliers (single bulk purchases etc.) from a candidate series."""
    vals = [(e, _home_amount(e, fx, home, amounts)) for e in evs]
    vals = [(e, v) for e, v in vals if v is not None]
    if len(vals) < 4:
        return [e for e, _ in vals]
    sorted_vals = sorted(v for _, v in vals)
    median = sorted_vals[len(sorted_vals) // 2]
    return [e for e, v in vals if v <= median * Decimal(3) and v >= median / Decimal(3)]


# --------------------------------------------------------------------------
def _to_home(mo, on: date, fx: FxConverter, home: str) -> Decimal:
    return mo.amount if mo.currency == home else fx.convert(mo.amount, mo.currency, home, on)


def _income_flows(led, salary_events, request_date, end, fx, home, evidence, amounts):
    regular = sorted((e for e in salary_events if e.status == "settled" and e.description in STABLE_SALARY
                      and e.cash_date < request_date), key=lambda e: e.cash_date)
    scheduled = sorted((e for e in salary_events if e.status == "scheduled"), key=lambda e: e.cash_date)
    descs = {e.description for e in salary_events}
    ev = evidence

    ended = bool(ev.income_ended) or "Final employer payroll" in descs
    if descs & SEASONAL_INCOME and not (descs & STABLE_SALARY):
        ended = True

    # ---- base monthly salary ------------------------------------------------
    base = anchor = None
    if regular:
        vals = [_home_amount(e, fx, home, amounts) for e in regular]
        last_amt = vals[-1]
        reduced = ev.next_salary_amount is not None or ev.unpaid_leave_amount is not None
        if reduced:
            cnt = Counter(vals)
            top = max(cnt.values())
            base = max(v for v, c in cnt.items() if c == top)
        else:
            base = last_amt
        anchor = regular[-1].cash_date
    # "confirmed base salary" messages only say commission is NOT counted; the
    # settled Base-salary events already hold the amount that really arrives.
    # "household income ended / remaining salary": the message quotes the household total;
    # the settled primary-salary events hold the part that really arrives, so the stream is
    # kept as-is and the (variable) second income is simply never projected.
    if ev.remaining_salary is not None:
        ended = False
    if scheduled and base is None:
        base = _home_amount(scheduled[0], fx, home, amounts)
    if scheduled and anchor is None:
        anchor = scheduled[0].cash_date

    occ: dict[date, Decimal] = {}            # explicit, dated occurrences
    monthly_from: Optional[date] = None     # start of the projected monthly series

    if ev.first_salary:
        d, mo = ev.first_salary
        base, anchor, monthly_from = _to_home(mo, d, fx, home), d, d
        ended = False
    if ev.resume:
        d, mo = ev.resume
        base, anchor, monthly_from = _to_home(mo, d, fx, home), d, d
        ended = False
    if ev.fx_salary:
        d, mo = ev.fx_salary
        occ[d] = _to_home(mo, d, fx, home)
        if anchor is None or d > anchor:
            anchor = d
        if base is None:
            base = occ[d]
        ended = False

    for s in scheduled:
        a = _home_amount(s, fx, home, amounts)
        if a is not None and request_date <= s.cash_date <= end:
            occ[s.cash_date] = a
            anchor = max(anchor, s.cash_date) if anchor else s.cash_date
            if base is None:
                base = a

    led.salary_amount = base
    led.income_active = not ended

    # ---- one-time confirmed invoices (freelancers) ------------------------------
    for d, mo in ev.invoices:
        if request_date <= d <= end:
            led.flows.append(Flow(d, _to_home(mo, d, fx, home), "income", "approved invoice", "income", "invoice", phase=1))

    # ---- generate payroll dates ---------------------------------------------------
    dates: list[date] = []
    if not ended and base is not None and anchor is not None:
        if ev.next_salary_date:
            anchor = ev.next_salary_date
            first = ev.next_salary_date
        elif monthly_from:
            first = monthly_from
        else:
            first = add_months(anchor, 1)
        k = 0
        d = first
        while d <= end:
            if d >= request_date:
                dates.append(d)
            k += 1
            d = add_months(first, k, first.day)
    for d in occ:
        if d not in dates:
            dates.append(d)
    dates = sorted(set(dates))
    if ended:
        dates = sorted(d for d in occ)

    first_pending = True
    for d in dates:
        if d in occ:
            amt = occ[d]
        else:
            amt = base
            if ev.salary_raise and d >= ev.salary_raise[0]:
                amt = _to_home(ev.salary_raise[1], d, fx, home)
            if first_pending:
                if ev.next_salary_amount is not None:
                    amt = _to_home(ev.next_salary_amount, d, fx, home)
                elif ev.unpaid_leave_amount is not None:
                    amt = _to_home(ev.unpaid_leave_amount, d, fx, home)
                # "regular salary + one-time arrears": the arrears credit is already in the
                # settled history as a one-off, so it is never added or projected again.
        first_pending = False
        led.flows.append(Flow(d, amt, "income", "salary", "salary", "salary", phase=1))
