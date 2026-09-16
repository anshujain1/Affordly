#!/usr/bin/env python3
"""Affordly - run the Buy-or-Wait decision engine.

    python3 code/main.py                      # dataset/requests.csv -> output.csv
    python3 code/main.py --requests sample_requests.csv --out /tmp/sample_out.csv
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from affordly.engine import run  # noqa: E402


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(root / "dataset"))
    ap.add_argument("--requests", default="requests.csv", help="file inside --data")
    ap.add_argument("--out", default=str(root / "output.csv"))
    args = ap.parse_args()
    import time
    from affordly.engine import write_usage_report
    t = time.time()
    rows = run(args.data, args.requests, args.out)
    print(f"wrote {len(rows)} rows -> {args.out}")
    if args.requests == "requests.csv":
        write_usage_report(Path(__file__).resolve().parent / "evaluation" / "usage_report.md", rows, time.time() - t, args.requests)


if __name__ == "__main__":
    main()
