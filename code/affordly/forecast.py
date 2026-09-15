"""Daily balance path and safety queries.

Safety is judged on the *intra-day low*: on any day all debits are assumed to
post before credits (the financially safer interpretation).  A payment made on
day D is therefore tested against D's low point too.
"""
from __future__ import annotations
from datetime import timedelta
from decimal import Decimal
from .ledger import Ledger


SEMANTICS = "low"      # "low" (debits-first within a day) or "eod"


class Path:
    def __init__(self, ledger: Ledger, opening: Decimal, extra=None):
        self.start, self.end = ledger.start, ledger.end
        self.days = (self.end - self.start).days + 1
        ph = [[Decimal(0)] * self.days for _ in range(3)]
        for f in ledger.flows + list(extra or []):
            i = (f.on - self.start).days
            if 0 <= i < self.days:
                ph[min(f.phase, 2) if f.amount < 0 or f.phase == 1 else 2][i] += f.amount
        bal = opening
        self.low, self.bal = [], []
        for i in range(self.days):
            a = bal + ph[0][i]                 # after interval-stream debits
            b = a + ph[1][i]                   # after credits (payday)
            c = b + ph[2][i]                   # after monthly / known debits
            self.low.append(min(a, c))
            self.bal.append(c)
            bal = c
        # A payment made on day i is posted at the *end* of day i: it must clear
        # that day's closing balance and every later intra-day low.
        suf = [None] * self.days
        later = None                      # min of low[j] for j > i
        for i in range(self.days - 1, -1, -1):
            suf[i] = self.bal[i] if later is None else min(self.bal[i], later)
            later = self.low[i] if later is None else min(later, self.low[i])
        self.suffix_min = suf

    def date_at(self, i): return self.start + timedelta(days=i)

    def safe_today(self, min_balance: Decimal) -> Decimal:
        return self.suffix_min[0] - min_balance

    def earliest_full(self, amount: Decimal, min_balance: Decimal):
        for i in range(self.days):
            if self.suffix_min[i] - amount >= min_balance:
                return self.date_at(i)
        return None
