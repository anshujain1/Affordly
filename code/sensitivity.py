#!/usr/bin/env python3
"""How sensitive is the score on the 25 labelled samples to the two main calibration knobs?

    python3 code/sensitivity.py

The forecast window (HORIZON_DAYS) and the estimator used for variable spending
(ledger.ESTIMATOR) were chosen by looking at these same 25 samples, so every
number here is IN-SAMPLE.  This script exists so the choice can be inspected and
reproduced instead of taken on faith.  The problem statement says 90 days; the
sweep shows what that costs.
"""
import csv
import itertools
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from affordly import engine, ledger  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "dataset"
FIELDS = ["affordability_status", "recommended_payment_method", "payment_plan",
          "earliest_date_for_full_payment", "spending_changes_needed"]
TOLERANCE = Decimal("0.05")


def score(horizon: int, estimator: str, expected: dict) -> dict:
    engine.HORIZON_DAYS = horizon
    ledger.ESTIMATOR = estimator
    out = Path(tempfile.mkdtemp()) / "sample_out.csv"
    engine.run(DATA, "sample_requests.csv", out)
    with open(out, newline="") as fh:
        got = {r["request_id"]: r for r in csv.DictReader(fh)}

    def amount_ok(rid):
        e, g = Decimal(expected[rid]["amount_safe_to_pay"]), Decimal(got[rid]["amount_safe_to_pay"])
        return abs(g - e) / max(e, Decimal(1)) <= TOLERANCE

    hits = {f: sum(expected[k][f] == got[k][f] for k in expected) for f in FIELDS}
    hits["amount"] = sum(amount_ok(k) for k in expected)
    hits["all"] = sum(amount_ok(k) and all(expected[k][f] == got[k][f] for f in FIELDS) for k in expected)
    return hits


def main() -> None:
    with open(DATA / "sample_requests.csv", newline="") as fh:
        expected = {r["request_id"]: r for r in csv.DictReader(fh)}
    shipped = (engine.HORIZON_DAYS, ledger.ESTIMATOR)
    print(f"{len(expected)} labelled samples (in-sample: these also guided the calibration)\n")
    print("window  estimator   status method plan  date  changes | amount(±5%)  all-fields")
    for horizon, estimator in itertools.product([84, 90], ["mean", "median", "midrange", "p75", "max"]):
        h = score(horizon, estimator, expected)
        marker = "  <- shipped" if (horizon, estimator) == shipped else ""
        print(f"{horizon:>5}   {estimator:<10} {h[FIELDS[0]]:>6} {h[FIELDS[1]]:>6} {h[FIELDS[2]]:>4} {h[FIELDS[3]]:>5} "
              f"{h[FIELDS[4]]:>7} | {h['amount']:>10}  {h['all']:>10}{marker}")
    engine.HORIZON_DAYS, ledger.ESTIMATOR = shipped


if __name__ == "__main__":
    main()
