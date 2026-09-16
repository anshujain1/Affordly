"""Hard-constraint validation of output rows against the problem statement."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from .data import Dataset

STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
PAIR = re.compile(r"^(\d{4}-\d{2}-\d{2}):(\d+(?:\.\d{1,2})?)$")


def validate_row(row: dict, ds: Dataset) -> list[str]:
    errs: list[str] = []
    rq = ds.requests[row["request_id"]]
    prof = ds.profiles[rq.user_id]
    safe = Decimal(row["amount_safe_to_pay"])
    if not (0 <= safe <= rq.amount):
        errs.append("amount_safe_to_pay outside [0, requested]")
    st, me = row["affordability_status"], row["recommended_payment_method"]
    if st not in STATUSES:
        errs.append(f"bad status {st}")
    if me not in METHODS:
        errs.append(f"bad method {me}")
    pairs = []
    if row["payment_plan"] != "none":
        for part in row["payment_plan"].split("|"):
            m = PAIR.match(part)
            if not m:
                errs.append(f"bad plan token {part}")
                continue
            pairs.append((date.fromisoformat(m.group(1)), Decimal(m.group(2))))
        if [d for d, _ in pairs] != sorted(d for d, _ in pairs):
            errs.append("plan not chronological")
        if pairs and pairs[0][0] < rq.request_date:
            errs.append("plan starts before request_date")
        if pairs and pairs[-1][0] > rq.due_date:
            errs.append("plan completes after desired date")
    if st == "not_affordable" and (me != "not_recommended" or row["payment_plan"] != "none"):
        errs.append("not_affordable must be not_recommended/none")
    if st != "not_affordable":
        if me not in prof.methods and me != "wait":
            errs.append("method not accepted by user")
        if me == "wait" and "full_payment" not in prof.methods:
            errs.append("wait requires full_payment")
        total = sum((a for _, a in pairs), Decimal(0))
        if me in ("full_payment", "wait", "partial_payment") and abs(total - rq.amount) > Decimal("0.01"):
            errs.append("plan total != requested")
        if me == "installments":
            opts = [o for o in ds.options.get(rq.request_id, []) if o.method == "installments"]
            ok = any(o.n_payments == len(pairs) and all(a == o.payment_amount for _, a in pairs)
                     and pairs[0][0] == o.first_date for o in opts)
            if not ok:
                errs.append("installment plan does not match a supplied option")
        if me == "partial_payment":
            if not rq.allows_partial or len(pairs) != 2:
                errs.append("partial payment not allowed / not two payments")
            elif abs(pairs[0][1] - safe) > Decimal("0.01"):
                errs.append("first partial payment != amount_safe_to_pay")
    if st == "affordable_now" and row["earliest_date_for_full_payment"] != rq.request_date.isoformat():
        errs.append("affordable_now requires earliest == request_date")
    ch = row["spending_changes_needed"]
    if ch != "none":
        items = ch.split("|")
        if len(items) > 3:
            errs.append("more than 3 spending changes")
        ids = [i.split(":")[1] for i in items]
        if len(set(ids)) != len(ids):
            errs.append("same event stopped and reduced")
    if not row["decision_explanation"].strip():
        errs.append("empty explanation")
    return errs
