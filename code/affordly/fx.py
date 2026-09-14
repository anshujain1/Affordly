"""Deterministic dated FX conversion using only `exchange_rates.csv`.

Rates are published on a monthly grid.  A conversion on date D uses the most
recent rate dated on/before D (falling back to the earliest later one if the
date precedes the table).  Missing direct pairs are resolved through the
inverse rate or one intermediate currency.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional


class FxConverter:
    def __init__(self, rates):
        self._by_pair: dict[tuple[str, str], list[tuple[date, Decimal]]] = {}
        for d, a, b, r in rates:
            self._by_pair.setdefault((a, b), []).append((d, r))
        for v in self._by_pair.values():
            v.sort()
        self.currencies = sorted({c for pair in self._by_pair for c in pair})

    def _direct(self, a: str, b: str, on: date) -> Optional[Decimal]:
        series = self._by_pair.get((a, b))
        if series:
            chosen = None
            for d, r in series:
                if d <= on:
                    chosen = r
                else:
                    break
            return chosen if chosen is not None else series[0][1]
        inv = self._by_pair.get((b, a))
        if inv:
            chosen = None
            for d, r in inv:
                if d <= on:
                    chosen = r
                else:
                    break
            r = chosen if chosen is not None else inv[0][1]
            return Decimal(1) / r
        return None

    def rate(self, a: str, b: str, on: date) -> Decimal:
        if a == b:
            return Decimal(1)
        direct = self._direct(a, b, on)
        if direct is not None:
            return direct
        for mid in self.currencies:
            if mid in (a, b):
                continue
            r1, r2 = self._direct(a, mid, on), self._direct(mid, b, on)
            if r1 is not None and r2 is not None:
                return r1 * r2
        raise KeyError(f"no FX path {a}->{b}")

    def convert(self, amount: Decimal, a: str, b: str, on: date) -> Decimal:
        return amount * self.rate(a, b, on)
