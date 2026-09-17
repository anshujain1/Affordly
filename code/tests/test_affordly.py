
"""Run with: python3 -m unittest discover -s code/tests -v (from the repo root)"""
import csv
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from affordly.data import load_dataset  # noqa: E402
from affordly.engine import run  # noqa: E402
from affordly.evidence import classify, parse_messages  # noqa: E402
from affordly.explain import long_date, money, num, num_trim, plan_string  # noqa: E402
from affordly.forecast import Path as BalancePath  # noqa: E402
from affordly.fx import FxConverter  # noqa: E402
from affordly.ledger import Flow, Ledger, Stream, add_months  # noqa: E402
from affordly.planner import Change, apply_changes  # noqa: E402
from affordly.validate import validate_row  # noqa: E402

DATA = ROOT / "dataset"


def mini_ledger(flows, days=10):
    start = date(2025, 1, 1)
    led = Ledger("u", "EUR", start, date(2025, 1, days))
    led.flows = flows
    return led


class Formatting(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(num(D("25256.00")), "25256")
        self.assertEqual(num(D("620.4")), "620.40")
        self.assertEqual(num_trim(D("17229139.20")), "17229139.2")
        self.assertEqual(num_trim(D("166.61")), "166.61")
        self.assertEqual(money("IDR", D("15952906.67")), "IDR 15,952,906.67")
        self.assertEqual(money("ZAR", D("25256")), "ZAR 25,256")

    def test_dates_and_plan(self):
        self.assertEqual(long_date(date(2025, 8, 8)), "8 August 2025")
        self.assertEqual(
            plan_string([
                (date(2025, 1, 2), D("10")),
                (date(2025, 2, 2), D("5.5")),
            ]),
            "2025-01-02:10|2025-02-02:5.50",
        )

    def test_add_months_clamps(self):
        self.assertEqual(
            add_months(date(2025, 1, 31), 1, 31),
            date(2025, 2, 28),
        )


class Fx(unittest.TestCase):
    def test_dated_and_inverse(self):
        fx = FxConverter([
            (date(2025, 1, 15), "USD", "IDR", D(16000)),
            (date(2025, 2, 15), "USD", "IDR", D(17000)),
        ])
        self.assertEqual(
            fx.convert(D(10), "USD", "IDR", date(2025, 2, 1)),
            D(160000),
        )
        self.assertEqual(
            fx.convert(D(10), "USD", "IDR", date(2025, 3, 1)),
            D(170000),
        )
        self.assertAlmostEqual(
            float(fx.rate("IDR", "USD", date(2025, 2, 20))),
            1 / 17000,
        )


class Messages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DATA)

    def test_every_message_is_recognised(self):
        unmatched = [
            m.message_id
            for ms in self.ds.messages.values()
            for m in ms
            if classify(m.text) is None
        ]
        self.assertEqual(unmatched, [])

    def test_english_and_indonesian_raise(self):
        en = type("M", (), dict(
            message_id="a",
            sent_at="1",
            text="Your monthly salary has increased to USD 2988. The change applies from 2026-07-15.",
        ))
        idn = type("M", (), dict(
            message_id="b",
            sent_at="1",
            text="Gaji bulanan Anda naik menjadi IDR 42750000. Perubahan ini berlaku mulai 2025-08-15.",
        ))

        for m, cur, amt in (
            (en, "USD", D(2988)),
            (idn, "IDR", D(42750000)),
        ):
            ev = parse_messages([m])
            self.assertEqual(ev.salary_raise[1].currency, cur)
            self.assertEqual(ev.salary_raise[1].amount, amt)

    def test_scam_is_flagged_not_executed(self):
        m = type("M", (), dict(
            message_id="s",
            sent_at="1",
            text="Congratulations! Pay the release charge today to receive the funds.",
        ))
        ev = parse_messages([m])
        self.assertIn("scam", ev.flags)
        self.assertIsNone(ev.salary_raise)


class Forecast(unittest.TestCase):
    def test_payday_debits_interval_before_credit(self):
        flows = [
            Flow(date(2025, 1, 3), D(-90), "series", "w", phase=0),
            Flow(date(2025, 1, 3), D(200), "income", "pay", phase=1),
        ]
        p = BalancePath(mini_ledger(flows), D(100))
        self.assertEqual(p.low[2], D(10))
        self.assertEqual(p.bal[2], D(210))

    def test_monthly_debit_after_payday(self):
        flows = [
            Flow(date(2025, 1, 3), D(-90), "series", "m", phase=2),
            Flow(date(2025, 1, 3), D(200), "income", "pay", phase=1),
        ]
        p = BalancePath(mini_ledger(flows), D(100))
        self.assertEqual(min(p.low), D(100))

    def test_safe_and_earliest_date(self):
        flows = [
            Flow(date(2025, 1, 4), D(-60), "series", "x", phase=2),
            Flow(date(2025, 1, 6), D(100), "income", "pay", phase=1),
        ]
        p = BalancePath(mini_ledger(flows), D(100))
        self.assertEqual(p.safe_today(D(0)), D(40))
        self.assertEqual(
            p.earliest_full(D(100), D(0)),
            date(2025, 1, 6),
        )
        self.assertEqual(
            p.earliest_full(D(30), D(0)),
            date(2025, 1, 1),
        )

    def test_apply_changes_stop_and_reduce(self):
        led = mini_ledger([
            Flow(date(2025, 1, 2), D(-50), "series", "s", "sub", "sub"),
            Flow(date(2025, 1, 3), D(-30), "series", "d", "dining", "dine"),
        ])
        out = apply_changes(led, [
            Change("stop", "sub", "e1", "Sub", None, D(50)),
            Change("reduce", "dine", "e2", "Dine", D(10), D(20)),
        ])
        self.assertEqual([f.amount for f in out.flows], [D(-10)])


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.rows = run(DATA, "requests.csv", cls.tmp / "out.csv")
        cls.ds = load_dataset(DATA)

    def test_row_count_and_ids(self):
        self.assertEqual(
            [r["request_id"] for r in self.rows],
            list(self.ds.requests),
        )
        self.assertEqual(len(self.rows), 250)

    def test_no_constraint_violations(self):
        bad = {
            r["request_id"]: validate_row(r, self.ds)
            for r in self.rows
            if validate_row(r, self.ds)
        }
        self.assertEqual(bad, {})

    def test_column_order_matches_template(self):
        with open(DATA / "output.csv", newline="") as f:
            header = next(csv.reader(f))
        self.assertEqual(list(self.rows[0].keys()), header)

    def test_deterministic(self):
        again = run(DATA, "requests.csv", self.tmp / "out2.csv")
        self.assertEqual(again, self.rows)

    def test_sample_regression_floor(self):
        out = self.tmp / "s.csv"
        run(DATA, "sample_requests.csv", out)

        with open(DATA / "sample_requests.csv", newline="") as f:
            exp = {
                r["request_id"]: r
                for r in csv.DictReader(f)
            }

        with open(out, newline="") as f:
            got = {
                r["request_id"]: r
                for r in csv.DictReader(f)
            }

        hits = lambda field: sum(
            exp[k][field] == got[k][field]
            for k in exp
        )

        self.assertGreaterEqual(hits("affordability_status"), 22)
        self.assertGreaterEqual(hits("recommended_payment_method"), 23)
        self.assertGreaterEqual(hits("payment_plan"), 21)
        self.assertGreaterEqual(hits("earliest_date_for_full_payment"), 20)


if __name__ == "__main__":
    unittest.main()
