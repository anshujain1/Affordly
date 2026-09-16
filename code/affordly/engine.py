"""End-to-end pipeline: dataset + requests -> output rows."""
from __future__ import annotations

import csv
from pathlib import Path as FsPath

from .data import Dataset, load_dataset
from .evidence import build_evidence
from .explain import changes_string, explanation, num, num_trim, plan_string
from .forecast import Path
from .fx import FxConverter
from .ledger import build_ledger
from .planner import decide

HORIZON_DAYS = 84       # calibrated on the solved samples (see README)

COLUMNS = ["request_id", "amount_safe_to_pay", "affordability_status", "recommended_payment_method",
           "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed", "decision_explanation"]


def solve_request(ds: Dataset, fx: FxConverter, request_id: str) -> dict:
    rq = ds.requests[request_id]
    profile = ds.profiles[rq.user_id]
    evidence = build_evidence(ds, rq)
    ledger = build_ledger(ds.events.get(rq.user_id, []), profile, rq.request_date, HORIZON_DAYS, fx, evidence)
    dec = decide(rq, profile, ledger, ds.options.get(request_id, []))
    plan = dec.plan
    return {
        "request_id": request_id,
        "amount_safe_to_pay": num_trim(dec.safe_amount),
        "affordability_status": dec.status,
        "recommended_payment_method": dec.method,
        "payment_plan": plan_string(plan.payments) if plan else "none",
        "earliest_date_for_full_payment": dec.earliest.isoformat() if dec.earliest else "",
        "spending_changes_needed": changes_string(plan.changes) if plan else "none",
        "decision_explanation": explanation(dec, rq, profile, profile.home_currency),
    }


def write_usage_report(path, rows, seconds, requests_file) -> None:
    from .evidence import STATS
    n = max(len(rows), 1)
    lines = [
        "# Token usage and cost report", "",
        f"Final full-dataset run: `{requests_file}` -> `output.csv` ({len(rows)} requests, {seconds:.2f} s wall clock).", "",
        "| Item | Value |", "|---|---|",
        "| Model providers / names | none - the shipped pipeline is fully deterministic |",
        f"| Model calls | {STATS['llm_calls']} |",
        "| Input tokens | 0 |", "| Output tokens | 0 |",
        "| Total tokens | 0 |", f"| Average tokens per request | 0 |",
        "| Estimated total cost (USD) | 0.00 |", "| Estimated cost per request (USD) | 0.00 |", "",
        "## How evidence was handled in this run", "",
        f"- Messages read: {STATS['messages']} ({STATS['messages_recognised']} matched an intent pattern, "
        f"{STATS['messages_unrecognised']} unmatched and therefore ignored).",
        f"- Images needed: {STATS['images_cache'] + STATS['images_ocr'] + STATS['images_missing']} "
        f"(cache hits: {STATS['images_cache']}, OCR fallback: {STATS['images_ocr']}, unresolved: {STATS['images_missing']}).",
        "- `evidence_cache/image_amounts.json` holds the 16 amounts that were read from the images by Claude's vision "
        "during development (cross-checked with the *amount in words* line or OCR). That one-off, offline step is not "
        "part of the run above and is not counted in the token totals.",
        "- Message and image content is treated as untrusted data: only dates, amounts and intents are extracted; "
        "no instruction inside a message can change the rules.", ""]
    from pathlib import Path as _P
    _P(path).parent.mkdir(parents=True, exist_ok=True)
    _P(path).write_text("\n".join(lines), encoding="utf-8")


def run(data_dir, requests_file="requests.csv", out_path="output.csv") -> list[dict]:
    ds = load_dataset(FsPath(data_dir), requests_file)
    fx = FxConverter(ds.rates)
    rows = [solve_request(ds, fx, rid) for rid in ds.requests]
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return rows
