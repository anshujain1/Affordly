"""Output formatting and grounded explanations."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .planner import Change, Decision, q

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def num(x: Decimal) -> str:
    """CSV number: integral amounts have no decimals, otherwise two."""
    x = q(x)
    return str(int(x)) if x == x.to_integral_value() else f"{x:.2f}"


def num_trim(x: Decimal) -> str:
    """amount_safe_to_pay style: trailing zeros trimmed (17229139.2)."""
    x = q(x)
    s = f"{x:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def money(cur: str, x: Decimal) -> str:
    x = q(x)
    body = f"{int(x):,}" if x == x.to_integral_value() else f"{x:,.2f}"
    return f"{cur} {body}"


def long_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def plan_string(payments) -> str:
    return "|".join(f"{d.isoformat()}:{num(a)}" for d, a in payments)


def changes_string(changes: list[Change]) -> str:
    if not changes:
        return "none"
    parts = []
    for c in changes:
        parts.append(f"stop:{c.event_id}" if c.kind == "stop" else f"reduce_to:{c.event_id}:{num(c.new_amount)}")
    return "|".join(parts)


def _noun(desc: str) -> str:
    return desc[:1].lower() + desc[1:] if desc else "expense"


def explanation(dec: Decision, request, profile, cur: str) -> str:
    mn = money(cur, profile.min_balance)
    plan = dec.plan
    if plan is None:
        if dec.explanation_kind == "partial_only":
            return (f"Do not proceed with the {money(cur, request.amount)} request. Although "
                    f"{money(cur, dec.safe_amount)} is available today, the full amount cannot be completed "
                    f"safely within 90 days.")
        return (f"Do not make this payment by {long_date(request.due_date)}. None of the available options "
                f"keeps the {mn} minimum protected.")
    lead = ""
    if plan.changes:
        bits = []
        for c in plan.changes:
            if c.kind == "stop":
                bits.append(f"stop the {_noun(c.description)}")
            else:
                bits.append(f"reduce the {_noun(c.description)} to {money(cur, c.new_amount)}")
        lead = " and ".join(bits)
        lead = lead[0].upper() + lead[1:]
    if plan.method == "full_payment":
        pay = f"pay {money(cur, plan.payments[0][1])} today"
        if lead:
            return f"{lead}, then {pay}. This leaves at least {mn} available."
        return f"{pay[0].upper() + pay[1:]}. This leaves at least {mn} available over the next 90 days."
    if plan.method == "wait":
        d, a = plan.payments[0]
        return (f"Pay {money(cur, a)} in full on {long_date(d)}. Paying earlier would take the balance "
                f"below the {mn} minimum.")
    if plan.method == "partial_payment":
        (d1, a1), (d2, a2) = plan.payments
        return (f"Pay {money(cur, a1)} today and the remaining {money(cur, a2)} on {long_date(d2)}. "
                f"This completes the full request and keeps the {mn} minimum protected.")
    n = len(plan.payments)
    first = f"Use {n} installments of {money(cur, plan.payments[0][1])}, starting {long_date(plan.payments[0][0])}."
    if lead:
        first = f"{lead}, then use {n} installments of {money(cur, plan.payments[0][1])}, starting {long_date(plan.payments[0][0])}."
    return f"{first} This leaves at least {mn} available."
