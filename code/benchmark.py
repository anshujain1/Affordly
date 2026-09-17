#!/usr/bin/env python3
"""Score the engine on the 25 solved samples (dataset/sample_requests.csv).

    python3 code/benchmark.py            # summary
    python3 code/benchmark.py -v         # per-request diffs
"""
import argparse
import csv
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from affordly.engine import run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["affordability_status", "recommended_payment_method", "payment_plan",
          "earliest_date_for_full_payment", "spending_changes_needed"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--tol", type=float, default=0.05, help="relative tolerance for amount_safe_to_pay")
    args = ap.parse_args()

    out = Path(tempfile.gettempdir()) / "affordly_sample_out.csv"
    run(ROOT / "dataset", "sample_requests.csv", out)
    exp = {r["request_id"]: r for r in csv.DictReader(open(ROOT / "dataset" / "sample_requests.csv"))}
    got = {r["request_id"]: r for r in csv.DictReader(open(out))}

    hits = {f: 0 for f in FIELDS}
    amount_ok = 0
    full = 0
    for rid, e in exp.items():
        g = got[rid]
        row_ok = True
        for f in FIELDS:
            ok = e[f] == g[f]
            hits[f] += ok
            row_ok &= ok
            if args.v and not ok:
                print(f"{rid:11s} {f:32s} got={g[f]!r:55.55} exp={e[f]!r:.55}")
        ge, ee = Decimal(g["amount_safe_to_pay"]), Decimal(e["amount_safe_to_pay"])
        rel = abs(ge - ee) / max(ee, Decimal(1))
        a_ok = rel <= Decimal(str(args.tol))
        amount_ok += a_ok
        if args.v and not a_ok:
            print(f"{rid:11s} amount_safe_to_pay             got={ge} exp={ee} ({float(rel):.1%})")
        full += row_ok and a_ok
    n = len(exp)
    print(f"\nsamples: {n}")
    for f in FIELDS:
        print(f"  {f:34s} {hits[f]:2d}/{n}")
    print(f"  {'amount_safe_to_pay (±%d%%)' % int(args.tol * 100):34s} {amount_ok:2d}/{n}")
    print(f"  {'all fields correct':34s} {full:2d}/{n}")


if __name__ == "__main__":
    main()
