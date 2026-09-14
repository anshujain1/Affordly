"""Typed dataset loading.

Everything is read with the standard library only.  Money is `Decimal`
throughout so that cents never drift.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional


def _d(value: str) -> Optional[Decimal]:
    value = (value or "").strip()
    return Decimal(value) if value else None


def _date(value: str) -> Optional[date]:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def _list(value: str) -> list[str]:
    return [p for p in (value or "").split("|") if p]


@dataclass(frozen=True)
class Profile:
    user_id: str
    home_currency: str
    balance: Decimal
    min_balance: Decimal
    priorities: list[str]
    protected: list[str]
    reducible: list[str]
    stoppable: list[str]
    methods: list[str]
    max_installments: Optional[int]


@dataclass(frozen=True)
class Event:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str            # debit | credit | non_cash
    amount: Optional[Decimal]  # None => must be read from an image
    currency: str
    event_date: date
    settlement_date: Optional[date]
    status: str
    linked_event_id: Optional[str]
    flexibility: str          # fixed | reducible | stoppable | reducible_or_stoppable
    min_allowed: Optional[Decimal]

    @property
    def cash_date(self) -> date:
        return self.settlement_date or self.event_date


@dataclass(frozen=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    amount: Decimal
    due_date: date
    allows_partial: bool
    text: str


@dataclass(frozen=True)
class PaymentOption:
    option_id: str
    request_id: str
    method: str
    payment_amount: Decimal
    n_payments: int
    first_date: date
    frequency_days: Optional[int]
    fee: Decimal
    total: Decimal


@dataclass(frozen=True)
class Message:
    message_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]
    sent_at: str
    source: str
    text: str


@dataclass(frozen=True)
class ImageRef:
    image_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]
    path: Path


@dataclass
class Dataset:
    root: Path
    profiles: dict[str, Profile] = field(default_factory=dict)
    events: dict[str, list[Event]] = field(default_factory=dict)       # by user
    requests: dict[str, Request] = field(default_factory=dict)
    options: dict[str, list[PaymentOption]] = field(default_factory=dict)  # by request
    messages: dict[str, list[Message]] = field(default_factory=dict)   # by user
    images: dict[str, list[ImageRef]] = field(default_factory=dict)    # by user
    rates: list[tuple[date, str, str, Decimal]] = field(default_factory=list)


def _rows(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


def load_requests(path: Path) -> dict[str, Request]:
    out = {}
    for r in _rows(path):
        out[r["request_id"]] = Request(
            request_id=r["request_id"], user_id=r["user_id"],
            request_date=_date(r["request_date"]), request_type=r["request_type"],
            amount=_d(r["requested_amount"]), due_date=_date(r["desired_completion_date"]),
            allows_partial=r["allows_partial_payment"].strip().lower() == "true",
            text=r.get("request_text", ""))
    return out


def load_dataset(root: Path, requests_file: str = "requests.csv") -> Dataset:
    root = Path(root)
    ds = Dataset(root=root)

    for r in _rows(root / "financial_profiles.csv"):
        mi = (r.get("max_installment_months") or "").strip()
        ds.profiles[r["user_id"]] = Profile(
            user_id=r["user_id"], home_currency=r["home_currency"],
            balance=_d(r["current_available_balance"]), min_balance=_d(r["minimum_balance_to_keep"]),
            priorities=_list(r["financial_priorities"]), protected=_list(r["expense_categories_to_protect"]),
            reducible=_list(r["expense_categories_user_is_willing_to_reduce"]),
            stoppable=_list(r["expense_categories_user_is_willing_to_stop"]),
            methods=_list(r["payment_methods_user_will_consider"]),
            max_installments=int(float(mi)) if mi else None)

    for r in _rows(root / "financial_events.csv"):
        ev = Event(
            event_id=r["event_id"], user_id=r["user_id"], event_type=r["event_type"],
            description=r["description"], category=r["category"], direction=r["direction"],
            amount=_d(r["amount"]), currency=r["currency"], event_date=_date(r["event_date"]),
            settlement_date=_date(r["settlement_date"]), status=r["status"],
            linked_event_id=(r["linked_event_id"] or None), flexibility=r["flexibility"],
            min_allowed=_d(r["minimum_allowed_amount"]))
        ds.events.setdefault(ev.user_id, []).append(ev)

    ds.requests = load_requests(root / requests_file)

    for r in _rows(root / "request_payment_options.csv"):
        ds.options.setdefault(r["request_id"], []).append(PaymentOption(
            option_id=r["payment_option_id"], request_id=r["request_id"], method=r["payment_method"],
            payment_amount=_d(r["payment_amount"]), n_payments=int(r["number_of_payments"]),
            first_date=_date(r["first_payment_date"]),
            frequency_days=int(float(r["payment_frequency_days"])) if r["payment_frequency_days"].strip() else None,
            fee=_d(r["financing_fee"]) or Decimal(0), total=_d(r["total_payable_amount"])))

    for r in _rows(root / "messages.csv"):
        ds.messages.setdefault(r["user_id"], []).append(Message(
            r["message_id"], r["user_id"], r["request_id"] or None, r["related_event_id"] or None,
            r["sent_at"], r["source_type"], r["message_text"]))

    for r in _rows(root / "images.csv"):
        ds.images.setdefault(r["user_id"], []).append(ImageRef(
            r["image_id"], r["user_id"], r["request_id"] or None, r["related_event_id"] or None,
            root / "media" / "images" / f"{r['image_id']}.png"))

    for r in _rows(root / "exchange_rates.csv"):
        ds.rates.append((_date(r["rate_date"]), r["from_currency"], r["to_currency"], _d(r["rate"])))
    ds.rates.sort(key=lambda t: t[0])
    return ds
