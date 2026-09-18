# Affordly: "Buy or Wait?" payment-decision engine

[![CI](https://github.com/anshujain1/Affordly/actions/workflows/ci.yml/badge.svg)](https://github.com/anshujain1/Affordly/actions/workflows/ci.yml)

Given a user's ledger, preferences and a payment request (a purchase, a bill, a transfer, an investment), Affordly answers:
**can I pay this safely, how should I pay it, and if not now, when?** It forecasts the user's balance, checks it against the
minimum balance they want to keep, and picks the safest plan: full payment, wait, partial payment, one of the supplied
installment options, or a plan that needs a small spending change.

Built for the HackerRank Orchestrate "Buy or Wait?" challenge (spec: [`docs/PROBLEM.md`](docs/PROBLEM.md), original
README: [`docs/CHALLENGE.md`](docs/CHALLENGE.md)). The deadline has passed, so this is a portfolio project, not a live entry.

**Pure Python standard library, deterministic, no API calls, all 250 requests in under a second.**

## Results

### What is guaranteed: all 250 outputs are internally consistent

Every row of `output.csv` passes two independent layers of checks, run on every commit in CI:

1. **Hard constraints** from the spec (`affordly/validate.py`): bounds, plan arithmetic, installments match a supplied option, partial-payment rules, at most 3 changes.
2. **Independent re-simulation** (`affordly/verify.py`): a deliberately naive day-by-day loop re-applies each plan and its spending changes to the forecast and confirms the balance never drops below the user's minimum. It also recomputes `amount_safe_to_pay` and `earliest_date_for_full_payment` from scratch, and checks every spending change targets a real, permitted, flexible expense.

The verifier is itself tested: 8 deliberately corrupted rows (payment pulled too early, inflated safe amount, wrong date, change to a
fixed expense, someone else's event, 4 changes, reduction below the allowed minimum, installment that matches no option) must all be caught,
and on 300 random ledgers the naive simulator and the engine's optimised forecast must agree exactly.

> The verifier found a real bug in this project: `amount_safe_to_pay` was rounded to the *nearest* cent, so three partial-payment plans
> overshot the safe amount by up to half a cent. "The largest safe amount" must be rounded **down**. Fixed, and covered by the test.

What this does **not** prove: that the forecast flows themselves (the ledger built in `ledger.py`) match reality. Only labelled data can measure that.

### What is measured: the 25 labelled samples (in-sample)

| Field | Correct |
|---|---|
| `affordability_status` | 23 / 25 |
| `recommended_payment_method` | 24 / 25 |
| `payment_plan` | 23 / 25 |
| `earliest_date_for_full_payment` | 21 / 25 |
| `spending_changes_needed` | 22 / 25 |
| `amount_safe_to_pay` (within ±5 %) | 14 / 25 |
| every field | 11 / 25 |

**These are in-sample numbers.** The calibration choices below were made while looking at these same 25 samples, so expect the hidden
set to score lower. 11/25 on every field is a modest result; the decision logic is strong and the forecast amounts are the weak part.

### Calibration choices, and what they cost

Two knobs were fitted on the samples: the forecast window (84 days; the spec says 90) and the estimator for variable spending
(`mean`). Reproduce with `python3 code/sensitivity.py`:

| window | estimator | status | method | plan | date | changes | amount ±5 % | all fields |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| **84** | **mean** (shipped) | 23 | 24 | 23 | 21 | 22 | 14 | **11** |
| 84 | median | 22 | 23 | 22 | 23 | 21 | 13 | 10 |
| 84 | midrange | 22 | 23 | 22 | 22 | 22 | 14 | 12 |
| 84 | p75 | 20 | 21 | 20 | 18 | 18 | 11 | 6 |
| 84 | max | 15 | 15 | 15 | 13 | 19 | 6 | 3 |
| 90 | mean | 20 | 21 | 20 | 17 | 21 | 12 | 7 |
| 90 | median | 19 | 20 | 19 | 19 | 20 | 11 | 6 |
| 90 | midrange | 19 | 20 | 19 | 18 | 21 | 12 | 8 |
| 90 | p75 | 17 | 18 | 17 | 15 | 19 | 10 | 6 |
| 90 | max | 13 | 13 | 13 | 12 | 20 | 5 | 3 |

How to read it:
* **More conservative estimators (p75, max) are much worse**, and the amount errors are balanced (6 samples too high, 5 too low), so this is not a bias that a safety margin can fix.
* **84 days is an empirical choice, not a rule.** Sample 12 stops fitting beyond an 86-day window and sample 09 beyond 89, so 84 sits in the consistent band, but the spec says 90 and 90 scores worse here. Treat 84 as a calibration to revisit, not as truth.
* Mean vs midrange (11 vs 12 all-fields) is within noise on 25 samples.

## Known failures

Eleven samples miss on `amount_safe_to_pay`; four also miss on the decision or date.

| Symptom | Samples |
|---|---|
| Safe amount too high (not cautious enough) | 02, 03, 04, 05, 10, 20 |
| Safe amount too low (too cautious) | 06, 15, 19, 23, 25 |
| Wrong decision or date | 06, 11 (a month early), 17 (a month late), 21; plan wrong on 19 |

What they have in common:
* **Nine of the eleven (02, 03, 04, 06, 15, 19, 20, 23, 25) hit their lowest balance in the first 12 days**, before the first payday. So the error is in near-term cash flows, not in the forecast window or the spending estimator.
* Among those, 20, 23 and 25 involve a pending or failed charge, and 19 and 20 involve an amount that had to be read from an image. These are the prime suspects, **not confirmed**. Samples 02, 03, 04, 06 and 15 have not been investigated yet.
* The other two (05, 10) bottom out at day 82-84, but no window length reproduces them either (sample 05's safe amount jumps from 1,856 straight to 0 between an 87- and an 88-day window; the expected value is 737).

## How it works

```
CSVs ──► data.py ──► evidence.py (messages + images) ──► ledger.py ──► forecast.py ──► planner.py ──► explain.py ──► output.csv
                                                                                                           │
                                                                              validate.py + verify.py ◄────┘ (independent checks)
```

1. **Ledger reconstruction** (`ledger.py`): settled history becomes recurring *streams* (constant-gap such as every 5/7/10/14/21 days, or calendar-monthly). One-offs, cancelled/failed/unrealised events, refunds, outliers and image-documented purchases never become recurring. Pending debits are reserved, pending credits ignored, scheduled items are known flows, and foreign-currency items use the rate valid on the settlement date.
2. **Income model**: only stable, confirmed income is projected (regular payroll, a confirmed next salary, raises from their effective date, a moved payday, resumed or first salary, approved invoices). Gig payouts, freelance streams, commissions, bonuses, seasonal contracts and a final payroll are not.
3. **Evidence** (`evidence.py`, `images.py`): about 20 English/Indonesian intent patterns turn messages into structured findings; receipts, bills and payslips supply blank amounts. Message and image text is untrusted data: only numbers, dates and intents are read, and a scam message is flagged, never obeyed.
4. **Safety forecast** (`forecast.py`): daily balance path. Within a day, interval debits post before payday and monthly debits after it; a payment posts at the end of its day. `amount_safe_to_pay` is the lowest forecast balance minus the minimum, rounded down and capped to `[0, requested]`; `earliest_date_for_full_payment` is the first day the full amount keeps every later low point above the minimum.
5. **Decision** (`planner.py`): eligible plans must use methods the user accepts and finish by the desired date, ranked by *no spending changes, lowest total paid, earliest start, fewest payments, option id*. Spending changes (at most 3, flexible streams only) are searched for the smallest total sacrifice that makes the plan safe.
6. **Explanations** (`explain.py`) are generated from the same numbers that drive the decision.

### Why deterministic

Money decisions need to be reproducible and auditable, so the core has no model calls: the same input always gives the same output (tested), and every
number in an explanation traces back to a forecast flow. An earlier version used an LLM to read messages and images; the shipped version reads
messages with patterns and uses image amounts read once offline and cached in `code/evidence_cache/image_amounts.json`.

## Rules inferred from the solved samples

These are not stated in the task; they were derived by fitting the labelled examples, so treat them as hypotheses:

* a failed bill attempt consumes its billing cycle (no phantom re-charge) unless a retry is scheduled;
* "confirmed base salary", "household income ended" and arrears notices quote the *total* package, while the settled salary events carry the part that really arrives, and an arrears credit already in history is not added again;
* a reduced "next salary" message applies to the next payroll only; a moved payday shifts the whole monthly series;
* the cited event for a spending change is the *latest* occurrence of the flexible stream.

## Run

```bash
python3 code/main.py                                  # dataset/requests.csv -> output.csv (+ code/evaluation/usage_report.md)
python3 code/benchmark.py -v                          # score the 25 labelled samples with per-field diffs
python3 code/sensitivity.py                           # window / estimator sensitivity table
python3 -m unittest discover -s code/tests -v         # 30 tests: units, end-to-end, independent verifier
```

## Repository layout

```
code/main.py             CLI entry point
code/affordly/           data · fx · evidence · images · ledger · forecast · planner · explain · validate · verify · engine
code/evidence_cache/     image_amounts.json (amounts read from the 16 images; OCR is a fallback)
code/evaluation/         usage_report.md (generated by the final run)
code/tests/              unit, end-to-end and verifier tests
code/benchmark.py        score on the labelled samples
code/sensitivity.py      calibration sweep
docs/                    problem statement and original challenge README
output.csv               predictions for the 250 requests
```

## Limitations

* **Forecast amounts are the weak part** (see Known failures). Spending is estimated from noisy history; each stream carries about 3 % error, which dominates borderline cases.
* **Calibrated on 25 samples.** There is no held-out set, so reported accuracy is optimistic.
* **Message patterns are fitted to this dataset.** A test proves every message in the dataset is recognised, which says nothing about phrasing the patterns have not seen.
* **Images.** The 16 amounts were read once and cached; an unseen image falls back to OCR (needs `tesseract`) and is otherwise left unresolved rather than guessed.
* **Unobserved rules.** The hidden reference may treat some scenarios differently (for example whether a temporary pay cut reverts after one payroll).

## Roadmap (not built yet)

- [ ] Fix the near-term cash-flow misses, one rule at a time, with a before/after score for each
- [ ] HTTP API and a small demo page showing the balance forecast against the minimum
- [ ] Deploy on AWS (Lambda + API Gateway + S3/CloudFront) with infrastructure as code and CI deploys
- [ ] LLM fallback for messages the patterns do not recognise, with an evaluation on unseen phrasings
