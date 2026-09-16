"""Decision logic: choose the safest eligible plan for one request.

Ranking (from the problem statement) among *safe, eligible* plans:
  1. completes the request by `desired_completion_date`
  2. needs no spending changes
  3. minimises the total amount paid
  4. starts earlier
  5. uses fewer payments
  6. lowest payment_option_id
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from itertools import combinations, product
from typing import Optional

from .data import PaymentOption, Profile, Request
from .forecast import Path
from .ledger import Flow, Ledger, Stream

CENT = Decimal("0.01")
ZERO = Decimal(0)


def q(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Change:
    kind: str                       # stop | reduce
    stream_key: str
    event_id: str
    description: str
    new_amount: Optional[Decimal]   # for reduce
    saving: Decimal                 # total saved across the horizon


@dataclass
class Plan:
    status: str
    method: str
    payments: list[tuple[date, Decimal]]
    changes: list[Change] = field(default_factory=list)
    option_id: str = ""
    total: Decimal = ZERO
    completes_by_due: bool = True
    note: str = ""

    @property
    def sort_key(self):
        return (
            0 if self.completes_by_due else 1,
            len(self.changes) > 0,
            self.total,
            self.payments[0][0] if self.payments else date.max,
            len(self.payments),
            self.option_id or "",
        )


@dataclass
class Decision:
    safe_amount: Decimal
    earliest: Optional[date]
    plan: Optional[Plan]
    status: str
    method: str
    explanation_kind: str = ""


# --------------------------------------------------------------------------
def payment_flows(payments: list[tuple[date, Decimal]]) -> list[Flow]:
    # A payment posts at the end of its day (phase 2, after payday credits).
    return [Flow(d, -a, "payment", "requested payment", phase=2) for d, a in payments]


def safe_with(ledger: Ledger, opening: Decimal, min_bal: Decimal, payments) -> bool:
    inside = [(d, a) for d, a in payments if ledger.start <= d <= ledger.end]
    path = Path(ledger, opening, extra=payment_flows(inside))
    return min(path.low) >= min_bal and min(path.bal) >= min_bal


def installment_dates(opt: PaymentOption) -> list[tuple[date, Decimal]]:
    step = opt.frequency_days or 30
    return [(opt.first_date + timedelta(days=step * k), opt.payment_amount) for k in range(opt.n_payments)]


# --------------------------------------------------------------------------
def candidate_changes(ledger: Ledger, profile: Profile) -> list[list[Change]]:
    """Per-stream list of allowed single actions."""
    out: list[list[Change]] = []
    for key in sorted(ledger.streams):
        s = ledger.streams[key]
        opts: list[Change] = []
        total = sum((-f.amount for f in ledger.flows if f.series_key == key), ZERO)
        can_reduce = s.flexibility in ("reducible", "reducible_or_stoppable") and s.category in profile.reducible
        can_stop = s.flexibility in ("stoppable", "reducible_or_stoppable") and s.category in profile.stoppable
        if can_reduce and s.min_allowed is not None and s.min_allowed < s.amount:
            n = sum(1 for f in ledger.flows if f.series_key == key)
            opts.append(Change("reduce", key, s.last_event_id, s.description, s.min_allowed, (s.amount - s.min_allowed) * n))
        if can_stop and total > 0:
            opts.append(Change("stop", key, s.last_event_id, s.description, None, total))
        if opts:
            out.append(opts)
    return out


def apply_changes(ledger: Ledger, changes: list[Change]) -> Ledger:
    by_key = {c.stream_key: c for c in changes}
    new = Ledger(ledger.user_id, ledger.home, ledger.start, ledger.end, streams=ledger.streams,
                 notes=ledger.notes, income_active=ledger.income_active, salary_amount=ledger.salary_amount)
    for f in ledger.flows:
        c = by_key.get(f.series_key) if f.kind == "series" else None
        if c is None:
            new.flows.append(f)
        elif c.kind == "reduce":
            new.flows.append(Flow(f.on, -c.new_amount, f.kind, f.label, f.category, f.series_key, f.phase))
        # stop: drop the flow
    return new


def cheapest_changes(ledger: Ledger, profile: Profile, check, max_changes: int = 3) -> Optional[list[Change]]:
    """Smallest total sacrifice (<= 3 flexible events) that makes `check(ledger)` pass."""
    groups = candidate_changes(ledger, profile)
    best: Optional[tuple] = None
    for k in range(1, min(max_changes, len(groups)) + 1):
        for combo in combinations(range(len(groups)), k):
            for picks in product(*[groups[i] for i in combo]):
                saving = sum((c.saving for c in picks), ZERO)
                if best is not None and (saving, len(picks)) >= (best[0], len(best[1])):
                    continue
                if check(apply_changes(ledger, list(picks))):
                    best = (saving, list(picks))
    return best[1] if best else None


# --------------------------------------------------------------------------
def decide(request: Request, profile: Profile, ledger: Ledger, options: list[PaymentOption]) -> Decision:
    opening, min_bal = profile.balance, profile.min_balance
    req_amt = request.amount
    rd, due = request.request_date, request.due_date
    methods = set(profile.methods)

    base = Path(ledger, opening)
    safe_raw = base.safe_today(min_bal)
    safe_amt = max(ZERO, min(req_amt, safe_raw)).quantize(CENT, rounding=ROUND_DOWN)   # round DOWN: never overshoot the safe amount
    earliest = base.earliest_full(req_amt, min_bal)

    plans: list[Plan] = []

    # ---- full payment today / wait -------------------------------------------
    if "full_payment" in methods:
        if safe_raw >= req_amt:
            plans.append(Plan("affordable_now", "full_payment", [(rd, req_amt)], total=req_amt))
        elif earliest is not None and earliest > rd:
            plans.append(Plan("affordable_later", "wait", [(earliest, req_amt)], total=req_amt,
                              completes_by_due=earliest <= due))

    # ---- partial payment --------------------------------------------------------
    if ("partial_payment" in methods and request.allows_partial and ZERO < safe_amt < req_amt
            and earliest is not None and earliest <= due):
        plans.append(Plan("affordable_with_plan", "partial_payment",
                          [(rd, safe_amt), (earliest, q(req_amt - safe_amt))], total=req_amt))

    # ---- installments ---------------------------------------------------------------
    inst_opts = []
    if "installments" in methods:
        for opt in sorted(options, key=lambda o: o.option_id):
            if opt.method != "installments":
                continue
            if profile.max_installments is not None and opt.n_payments > profile.max_installments:
                continue
            pays = installment_dates(opt)
            inst_opts.append((opt, pays))
            if safe_with(ledger, opening, min_bal, pays):
                plans.append(Plan("affordable_with_plan", "installments", pays, option_id=opt.option_id,
                                  total=opt.total, completes_by_due=pays[-1][0] <= due))

    plans = [p for p in plans if p.completes_by_due]
    plans.sort(key=lambda p: p.sort_key)

    if plans:
        best = plans[0]
        return Decision(safe_amt, earliest, best, best.status, best.method)

    # ---- plans that need spending changes ---------------------------------------------
    change_plans: list[Plan] = []
    if "full_payment" in methods:
        chg = cheapest_changes(ledger, profile,
                               lambda L: safe_with(L, opening, min_bal, [(rd, req_amt)]))
        if chg:
            change_plans.append(Plan("affordable_with_plan", "full_payment", [(rd, req_amt)], changes=chg, total=req_amt))
    for opt, pays in inst_opts:
        if pays[-1][0] > due:
            continue
        chg = cheapest_changes(ledger, profile, lambda L, p=pays: safe_with(L, opening, min_bal, p))
        if chg:
            change_plans.append(Plan("affordable_with_plan", "installments", pays, changes=chg,
                                     option_id=opt.option_id, total=opt.total))
    if change_plans:
        change_plans.sort(key=lambda p: p.sort_key)
        best = change_plans[0]
        return Decision(safe_amt, earliest, best, best.status, best.method)

    kind = "partial_only" if (profile.methods == ["partial_payment"] and request.allows_partial and safe_amt > 0) else "none"
    return Decision(safe_amt, earliest, None, "not_affordable", "not_recommended", kind)
