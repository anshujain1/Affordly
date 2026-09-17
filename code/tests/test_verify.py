"""Tests for the independent verifier (`affordly/verify.py`).

Three layers:
  1. unit tests of the naive simulator on tiny hand-checked ledgers
  2. differential test: the naive simulator and the engine's optimised `forecast.Path`
     must agree on hundreds of random ledgers
  3. every one of the 250 real output rows must verify, AND the verifier must
     catch deliberately corrupted rows (a verifier that never fails proves nothing)

Run with:  python3 -m unittest discover -s code/tests -v   (from the repo root)
"""
import random
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from affordly.data import load_dataset                                   # noqa: E402
from affordly.engine import HORIZON_DAYS, run                            # noqa: E402
from affordly.evidence import build_evidence                             # noqa: E402
from affordly.forecast import Path as BalancePath                        # noqa: E402
from affordly.fx import FxConverter                                      # noqa: E402
from affordly.ledger import Flow, Ledger, build_ledger                   # noqa: E402
from affordly.verify import (naive_earliest_full, naive_safe_amount,     # noqa: E402
                             parse_changes, parse_plan, simulate, verify_row, verify_rows)

DATA = ROOT / "dataset"
START = date(2025, 1, 1)


def toy_ledger(flows, days=10):
    led = Ledger("u", "EUR", START, START + timedelta(days=days - 1))
    led.flows = flows
    return led


class NaiveSimulator(unittest.TestCase):
    def test_interval_debit_hits_before_payday_credit(self):
        flows = [Flow(date(2025, 1, 3), D(-90), "series", "w", phase=0),
                 Flow(date(2025, 1, 3), D(200), "income", "pay", phase=1)]
        lows, closes = simulate(flows, D(100), START, START + timedelta(days=9))
        self.assertEqual(lows[2], D(10))        # 100 - 90 before the payday credit
        self.assertEqual(closes[2], D(210))

    def test_monthly_debit_posts_after_payday(self):
        flows = [Flow(date(2025, 1, 3), D(-90), "series", "m", phase=2),
                 Flow(date(2025, 1, 3), D(200), "income", "pay", phase=1)]
        lows, _ = simulate(flows, D(100), START, START + timedelta(days=9))
        self.assertEqual(min(lows), D(100))

    def test_safe_amount_and_earliest_date(self):
        flows = [Flow(date(2025, 1, 4), D(-60), "series", "x", phase=2),
                 Flow(date(2025, 1, 6), D(100), "income", "pay", phase=1)]
        lows, closes = simulate(flows, D(100), START, START + timedelta(days=9))
        self.assertEqual(naive_safe_amount(lows, closes, D(0), D(1000)), D(40))
        self.assertEqual(naive_earliest_full(lows, closes, D(0), D(100), START), date(2025, 1, 6))
        self.assertEqual(naive_earliest_full(lows, closes, D(0), D(30), START), START)
        self.assertIsNone(naive_earliest_full(lows, closes, D(0), D(10_000), START))

    def test_parsers(self):
        self.assertEqual(parse_plan("none"), [])
        self.assertEqual(parse_plan("2025-01-02:10|2025-02-02:5.50"),
                         [(date(2025, 1, 2), D(10)), (date(2025, 2, 2), D("5.50"))])
        self.assertEqual(parse_changes("stop:event_1|reduce_to:event_2:12.5"),
                         [("stop", "event_1", None), ("reduce_to", "event_2", D("12.5"))])


class DifferentialAgainstEngine(unittest.TestCase):
    """The engine's suffix-minimum shortcut must equal the naive loop on random ledgers."""

    def test_random_ledgers_agree(self):
        rng = random.Random(20261024)
        for trial in range(300):
            days = rng.randint(5, 40)
            flows = []
            for _ in range(rng.randint(2, 25)):
                on = START + timedelta(days=rng.randrange(days))
                if rng.random() < 0.3:
                    flows.append(Flow(on, D(rng.randint(50, 900)), "income", "pay", phase=1))
                else:
                    flows.append(Flow(on, D(-rng.randint(5, 400)), "series", "x", phase=rng.choice([0, 2])))
            opening, minimum = D(rng.randint(100, 2000)), D(rng.randint(0, 500))
            requested = D(rng.randint(10, 1500))
            led = toy_ledger(flows, days)
            path = BalancePath(led, opening)
            lows, closes = simulate(flows, opening, led.start, led.end)
            engine_safe = max(D(0), min(requested, path.safe_today(minimum))).quantize(D("0.01"))
            self.assertEqual(naive_safe_amount(lows, closes, minimum, requested), engine_safe, f"trial {trial}")
            self.assertEqual(naive_earliest_full(lows, closes, minimum, requested, led.start),
                             path.earliest_full(requested, minimum), f"trial {trial}")


class RealOutputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DATA)
        cls.fx = FxConverter(cls.ds.rates)
        cls.rows = run(DATA, "requests.csv", Path(tempfile.mkdtemp()) / "out.csv")
        cls.by_id = {r["request_id"]: r for r in cls.rows}

    def pick(self, predicate, what):
        for row in self.rows:
            if predicate(row):
                return dict(row)
        self.fail(f"no real row to mutate for: {what}")

    def errors_of(self, row):
        return verify_row(row, self.ds, self.fx)

    # -- the real thing --------------------------------------------------------
    def test_all_250_rows_verify(self):
        failures = verify_rows(self.rows, self.ds, self.fx)
        self.assertEqual(failures, {}, f"{len(failures)} rows failed independent verification")

    # -- the verifier must actually bite ------------------------------------------
    def test_catches_payment_pulled_too_early(self):
        row = self.pick(lambda r: r["recommended_payment_method"] == "wait", "wait row")
        rq = self.ds.requests[row["request_id"]]
        row["payment_plan"] = f"{rq.request_date.isoformat()}:{rq.amount}"
        self.assertTrue(any("breaks the minimum balance" in e for e in self.errors_of(row)))

    def test_catches_inflated_safe_amount(self):
        row = self.pick(lambda r: D(r["amount_safe_to_pay"]) > 0 and
                        D(r["amount_safe_to_pay"]) < self.ds.requests[r["request_id"]].amount * D("0.8"),
                        "row with 0 < safe < requested")
        row["amount_safe_to_pay"] = str(D(row["amount_safe_to_pay"]) * D("1.1") + 1)
        self.assertTrue(any("amount_safe_to_pay" in e for e in self.errors_of(row)))

    def test_catches_wrong_earliest_date(self):
        row = self.pick(lambda r: r["earliest_date_for_full_payment"] != "", "row with an earliest date")
        shifted = date.fromisoformat(row["earliest_date_for_full_payment"]) + timedelta(days=1)
        row["earliest_date_for_full_payment"] = shifted.isoformat()
        self.assertTrue(any("earliest_date" in e for e in self.errors_of(row)))

    def test_catches_change_to_a_fixed_expense(self):
        row = self.pick(lambda r: r["spending_changes_needed"] == "none", "row without changes")
        fixed = next(e for e in self.ds.events[self.ds.requests[row["request_id"]].user_id]
                     if e.flexibility == "fixed")
        row["spending_changes_needed"] = f"stop:{fixed.event_id}"
        self.assertTrue(any("does not allow stop" in e for e in self.errors_of(row)))

    def test_catches_change_to_another_users_event(self):
        row = self.pick(lambda r: r["spending_changes_needed"] == "none", "row without changes")
        other = next(e for uid, evs in self.ds.events.items()
                     if uid != self.ds.requests[row["request_id"]].user_id for e in evs)
        row["spending_changes_needed"] = f"stop:{other.event_id}"
        self.assertTrue(any("not an event of this user" in e for e in self.errors_of(row)))

    def test_catches_more_than_three_changes(self):
        row = self.pick(lambda r: r["spending_changes_needed"] == "none", "row without changes")
        evs = self.ds.events[self.ds.requests[row["request_id"]].user_id][:4]
        row["spending_changes_needed"] = "|".join(f"stop:{e.event_id}" for e in evs)
        self.assertTrue(any("more than 3" in e for e in self.errors_of(row)))

    def test_catches_reduction_below_minimum_allowed(self):
        row = self.pick(lambda r: "reduce_to" in r["spending_changes_needed"], "row with a reduce_to")
        parts = row["spending_changes_needed"].split("|")
        i = next(k for k, p in enumerate(parts) if p.startswith("reduce_to"))
        action, event_id, _ = parts[i].split(":")
        parts[i] = f"{action}:{event_id}:0.01"
        row["spending_changes_needed"] = "|".join(parts)
        self.assertTrue(any("below minimum_allowed_amount" in e for e in self.errors_of(row)))

    def test_catches_installment_plan_that_matches_no_option(self):
        row = self.pick(lambda r: r["recommended_payment_method"] == "installments", "installment row")
        d, a = row["payment_plan"].split("|")[0].split(":")
        row["payment_plan"] = "|".join([f"{d}:{D(a) + 1}"] + row["payment_plan"].split("|")[1:])
        self.assertTrue(any("does not match a supplied option" in e for e in self.errors_of(row)))


if __name__ == "__main__":
    unittest.main()
