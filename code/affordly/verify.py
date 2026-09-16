"""Independent verification of Affordly's output rows.

The engine answers "is this payment safe?" with an optimised suffix-minimum
trick (`forecast.Path`) and a search over spending changes (`planner.py`).
This module re-checks every answer with deliberately *naive* code: a plain
day-by-day loop that applies the plan's payments and spending changes to the
forecast flows and looks for any moment the balance drops below the user's
minimum.  If the clever code and the naive code ever disagree, one of them has
a bug.

What it does NOT check: whether the forecast flows themselves (the ledger built
in `ledger.py`) match reality.  That is the part only the labelled samples can
measure.  This verifier proves the *decision logic* is consistent with the
forecast it was given.

Checks per output row
  1. all hard constraints in `validate.py`
  2. the recommended plan (after its spending changes) never breaks the minimum balance
  3. every spending change targets a real, permitted, flexible expense
  4. `amount_safe_to_pay` equals the naive recomputation
  5. `earliest_date_for_full_payment` equals the naive recomputation
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .data import Dataset
from .engine import HORIZON_DAYS
from .evidence import build_evidence
from .fx import FxConverter
from .ledger import Flow, Ledger, build_ledger
from .validate import validate_row

CENT = Decimal("0.01")
ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# naive balance simulation
# ---------------------------------------------------------------------------
def _bucket(flow: Flow) -> int:
    """Intra-day posting order: 0 interval debits, 1 credits, 2 monthly/known debits and payments."""
    if flow.amount < 0:
        return min(flow.phase, 2)
    return 1 if flow.phase == 1 else 2


def simulate(flows: list[Flow], opening: Decimal, start: date, end: date):
    """Return (lows, closes): per day the lowest intra-day balance and the end-of-day balance."""
    by_day: dict[date, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO, ZERO])
    for f in flows:
        if start <= f.on <= end:
            by_day[f.on][_bucket(f)] += f.amount
    lows, closes = [], []
    balance = opening
    day = start
    while day <= end:
        d0, d1, d2 = by_day[day] if day in by_day else (ZERO, ZERO, ZERO)
        after_debits = balance + d0
        after_credits = after_debits + d1
        close = after_credits + d2
        lows.append(min(after_debits, after_credits, close))
        closes.append(close)
        balance = close
        day += timedelta(days=1)
    return lows, closes


def payment_flows(payments: list[tuple[date, Decimal]]) -> list[Flow]:
    return [Flow(d, -a, "payment", "plan payment", phase=2) for d, a in payments]


def apply_changes_naive(ledger: Ledger, changes: list[tuple[str, str, Decimal | None]]) -> list[Flow]:
    """changes: (action, stream_key, new_amount).  Returns the modified flow list."""
    by_key = {key: (action, amount) for action, key, amount in changes}
    out: list[Flow] = []
    for f in ledger.flows:
        change = by_key.get(f.series_key) if f.kind == "series" else None
        if change is None:
            out.append(f)
        elif change[0] == "reduce_to":
            out.append(Flow(f.on, -change[1], f.kind, f.label, f.category, f.series_key, f.phase))
        # "stop": drop the flow
    return out


def naive_safe_amount(lows, closes, min_balance: Decimal, requested: Decimal) -> Decimal:
    """Largest payment on day 0 (posted at end of day) that keeps every later low above the minimum."""
    worst = min([closes[0]] + lows[1:])
    return max(ZERO, min(requested, worst - min_balance)).quantize(CENT)


def naive_earliest_full(lows, closes, min_balance: Decimal, amount: Decimal, start: date):
    for i in range(len(closes)):
        if closes[i] - amount < min_balance:
            continue
        if all(low - amount >= min_balance for low in lows[i + 1:]):
            return start + timedelta(days=i)
    return None


# ---------------------------------------------------------------------------
# parsing of output cells
# ---------------------------------------------------------------------------
def parse_plan(text: str) -> list[tuple[date, Decimal]]:
    if text == "none":
        return []
    out = []
    for part in text.split("|"):
        d, a = part.split(":")
        out.append((date.fromisoformat(d), Decimal(a)))
    return out


def parse_changes(text: str) -> list[tuple[str, str, Decimal | None]]:
    """-> (action, event_id, new_amount_or_None)"""
    if text == "none":
        return []
    out = []
    for part in text.split("|"):
        bits = part.split(":")
        out.append((bits[0], bits[1], Decimal(bits[2]) if len(bits) > 2 else None))
    return out


# ---------------------------------------------------------------------------
# the verifier
# ---------------------------------------------------------------------------
def _stream_for_event(ledger: Ledger, event_id: str):
    for stream in ledger.streams.values():
        if stream.last_event_id == event_id:
            return stream
    return None


def verify_row(row: dict, ds: Dataset, fx: FxConverter, ledger: Ledger | None = None) -> list[str]:
    errors = [f"validate: {e}" for e in validate_row(row, ds)]
    request = ds.requests[row["request_id"]]
    profile = ds.profiles[request.user_id]
    if ledger is None:
        ledger = build_ledger(ds.events.get(request.user_id, []), profile, request.request_date,
                              HORIZON_DAYS, fx, build_evidence(ds, request))

    # -- 3. spending changes are real, permitted and flexible ------------------
    changes = parse_changes(row["spending_changes_needed"])
    events = {e.event_id: e for e in ds.events.get(request.user_id, [])}
    if len(changes) > 3:
        errors.append("more than 3 spending changes")
    if len({event_id for _, event_id, _ in changes}) != len(changes):
        errors.append("same event changed twice")
    resolved: list[tuple[str, str, Decimal | None]] = []
    for action, event_id, new_amount in changes:
        event = events.get(event_id)
        stream = _stream_for_event(ledger, event_id)
        if event is None:
            errors.append(f"{event_id}: not an event of this user")
            continue
        if event.category in profile.protected:
            errors.append(f"{event_id}: category {event.category} is protected")
        if action == "stop":
            if event.flexibility not in ("stoppable", "reducible_or_stoppable"):
                errors.append(f"{event_id}: flexibility {event.flexibility} does not allow stop")
            if event.category not in profile.stoppable:
                errors.append(f"{event_id}: user does not allow stopping {event.category}")
        elif action == "reduce_to":
            if event.flexibility not in ("reducible", "reducible_or_stoppable"):
                errors.append(f"{event_id}: flexibility {event.flexibility} does not allow reduce")
            if event.category not in profile.reducible:
                errors.append(f"{event_id}: user does not allow reducing {event.category}")
            if event.min_allowed is not None and new_amount is not None and new_amount < event.min_allowed:
                errors.append(f"{event_id}: reduced below minimum_allowed_amount {event.min_allowed}")
        else:
            errors.append(f"unknown action {action}")
        if stream is None:
            errors.append(f"{event_id}: does not belong to any recurring stream, so it cannot be changed")
        else:
            resolved.append((action, stream.key, new_amount))

    # -- 2. the recommended plan is safe after those changes -------------------
    payments = parse_plan(row["payment_plan"])
    if payments:
        flows = apply_changes_naive(ledger, resolved) + payment_flows(payments)
        if any(d > ledger.end for d, _ in payments):
            errors.append("a payment falls after the verified forecast horizon")
        lows, _ = simulate(flows, profile.balance, ledger.start, ledger.end)
        worst = min(lows)
        if worst < profile.min_balance:
            when = ledger.start + timedelta(days=lows.index(worst))
            errors.append(f"plan breaks the minimum balance on {when}: short by {profile.min_balance - worst:.6f}")

    # -- 4 + 5. safe amount and earliest date, recomputed from scratch ----------
    lows, closes = simulate(ledger.flows, profile.balance, ledger.start, ledger.end)
    expected_safe = naive_safe_amount(lows, closes, profile.min_balance, request.amount)
    if abs(Decimal(row["amount_safe_to_pay"]) - expected_safe) > CENT:
        errors.append(f"amount_safe_to_pay {row['amount_safe_to_pay']} != naive {expected_safe}")
    expected_date = naive_earliest_full(lows, closes, profile.min_balance, request.amount, ledger.start)
    expected_text = expected_date.isoformat() if expected_date else ""
    if row["earliest_date_for_full_payment"] != expected_text:
        errors.append(f"earliest_date {row['earliest_date_for_full_payment']!r} != naive {expected_text!r}")
    return errors


def verify_rows(rows: list[dict], ds: Dataset, fx: FxConverter) -> dict[str, list[str]]:
    """Return {request_id: [errors]} for every row that fails; empty dict means all rows verified."""
    failures = {}
    for row in rows:
        errs = verify_row(row, ds, fx)
        if errs:
            failures[row["request_id"]] = errs
    return failures
