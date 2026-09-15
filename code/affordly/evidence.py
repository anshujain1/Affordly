"""Evidence extraction from messages and images.

Messages in this dataset are short, templated notices in English or
Indonesian.  They are parsed with *intent patterns* (never executed as
instructions - they are untrusted data) into a small structured `Evidence`
object that the ledger consumes.

Anything the parser does not recognise is simply ignored, which is the
conservative behaviour required by "do not invent unsupported information".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

CUR = r"(IDR|INR|USD|EUR|ZAR)"
MONEY = CUR + r"\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
ISO = r"(\d{4}-\d{2}-\d{2})"


@dataclass
class Money:
    currency: str
    amount: Decimal


@dataclass
class Evidence:
    # ----- salary / income findings -------------------------------------
    salary_raise: Optional[tuple[date, Money]] = None       # from date, new monthly salary
    next_salary_date: Optional[date] = None                 # confirmed payday moved
    next_salary_amount: Optional[Money] = None              # applies to next payroll only
    unpaid_leave_amount: Optional[Money] = None             # message amount for next payroll
    base_salary: Optional[Money] = None                     # confirmed base (commission excluded)
    remaining_salary: Optional[Money] = None                # after a household income ended
    income_ended: bool = False
    resume: Optional[tuple[date, Money]] = None
    first_salary: Optional[tuple[date, Money]] = None
    invoices: list = field(default_factory=list)
    fx_salary: Optional[tuple[date, Money]] = None
    arrears: Optional[Money] = None                         # one-time, same payroll
    regular_next: Optional[Money] = None                    # "regular salary for next payroll"
    # ----- expense findings -----------------------------------------------
    rent_increase_pct: Optional[Decimal] = None
    # ----- plumbing --------------------------------------------------------
    event_amounts: dict = field(default_factory=dict)       # event_id -> Decimal (from images)
    not_recurring: set = field(default_factory=set)
    ignore_events: set = field(default_factory=set)
    flags: set = field(default_factory=set)                 # informational intents (no cash effect)
    recognised: int = 0
    unrecognised: list = field(default_factory=list)


def _money(text: str, n: int = 0) -> Optional[Money]:
    found = re.findall(MONEY, text)
    if len(found) <= n:
        return None
    cur, amt = found[n]
    return Money(cur, Decimal(amt.replace(",", "").rstrip(".")))


def _date(text: str, n: int = 0) -> Optional[date]:
    found = re.findall(ISO, text)
    return date.fromisoformat(found[n]) if len(found) > n else None


# (flag, [regexes]) -- first matching intent wins; patterns cover EN + ID
INTENTS = [
    ("scam", [r"release charge", r"processing charge now", r"biaya pencairan", r"biaya pemrosesan sekarang"]),
    ("fx_salary", [r"salary of " + MONEY + r" is confirmed for " + ISO, r"Gaji sebesar " + MONEY + r" dikonfirmasi untuk " + ISO,
                   r"employer has confirmed a " + MONEY + r" salary credit for " + ISO]),
    ("raise", [r"monthly salary has increased to", r"naik menjadi"]),
    ("date_shift", [r"confirmed salary is now expected on", r"dikonfirmasi kini diperkirakan masuk pada"]),
    ("unpaid_leave", [r"next salary is reduced to", r"[Gg]aji berikutnya (?:dikurangi|berkurang)"]),
    ("temp_pay", [r"temporary monthly pay is", r"[Gg]aji bulanan sementara"]),
    ("resume", [r"[Rr]egular salary of .{0,20} resumes on", r"[Gg]aji rutin sebesar .* (?:kembali|dilanjutkan)"]),
    ("first_salary", [r"first salary", r"[Gg]aji pertama"]),
    ("household_ended", [r"household employment record has ended", r"pendapatan kerja rumah tangga telah berakhir"]),
    ("employment_ended", [r"Your employment has ended", r"Hubungan kerja Anda telah berakhir"]),
    ("seasonal_ended", [r"seasonal contract has ended", r"[Kk]ontrak musiman saat ini telah berakhir"]),
    ("base_salary", [r"confirmed base salary is", r"[Gg]aji pokok yang dikonfirmasi"]),
    ("arrears", [r"one-time arrears adjustment", r"penyesuaian tunggakan satu kali"]),
    ("bonus_pending", [r"bonus.*(?:final performance review|penilaian kinerja)", r"[Bb]onus kuartalan"]),
    ("payout_pending", [r"payout is still pending", r"masih tertunda"]),
    ("invoice", [r"approved an invoice payment of", r"menyetujui pembayaran faktur sebesar"]),
    ("rent_increase", [r"increases monthly rent by", r"menaikkan biaya sewa bulanan sebesar"]),
    ("refund_pending", [r"refund has been initiated", r"foreign-currency refund is still processing",
                        r"[Pp]engembalian dana sudah diproses", r"refund.*still processing"]),
    ("prize_processing", [r"prize claim has been verified", r"[Kk]laim hadiah Anda sudah diverifikasi"]),
    ("prize_received", [r"prize proceeds have reached your account", r"[Hh]asil hadiah.*masuk"]),
    ("portfolio_value", [r"displayed (?:market )?value", r"[Nn]ilai investasi yang ditampilkan", r"portfolio.s displayed"]),
    ("investment_sale", [r"proceeds from your investment sale have settled", r"[Hh]asil penjualan investasi"]),
    ("reimbursement", [r"reimbursement for your earlier work expense", r"penggantian atas biaya kerja"]),
    ("transfer_own", [r"transfer between your two accounts", r"transfer antara dua rekening",
                      r"Debit dan kredit dengan jumlah yang sama"]),
    ("duplicate_charge", [r"extra card charge is still being investigated", r"[Tt]agihan kartu tambahan masih dalam penyelidikan"]),
    ("failed_debit", [r"previous debit attempt failed", r"[Dd]ebit sebelumnya gagal"]),
    ("two_cards", [r"minimum payments due on two separate card accounts", r"pembayaran minimum.*dua kartu"]),
    ("fx_bill", [r"bill was charged in a foreign currency", r"[Tt]agihan dikenakan dalam mata uang asing"]),
    ("regular_confirmed", [r"Gaji rutin untuk penggajian berikutnya sudah dikonfirmasi", r"regular salary for the next payroll is confirmed"]),
    ("receipt", [r"receipt has the final", r"receipt contains the final", r"The receipt has"]),
]


def classify(text: str) -> Optional[str]:
    for flag, pats in INTENTS:
        for p in pats:
            if re.search(p, text):
                return flag
    return None


def parse_messages(messages, evidence: Optional[Evidence] = None) -> Evidence:
    ev = evidence or Evidence()
    for m in sorted(messages, key=lambda x: x.sent_at):
        text = m.text
        intent = classify(text)
        if intent is None:
            ev.unrecognised.append(m.message_id)
            continue
        ev.recognised += 1
        ev.flags.add(intent)
        if intent == "raise":
            d, mo = _date(text), _money(text)
            if d and mo:
                ev.salary_raise = (d, mo)
        elif intent == "date_shift":
            ev.next_salary_date = _date(text)
        elif intent == "unpaid_leave":
            ev.unpaid_leave_amount = _money(text)
        elif intent == "temp_pay":
            ev.next_salary_amount = _money(text)
        elif intent == "resume":
            d, mo = _date(text), _money(text)
            if d and mo:
                ev.resume = (d, mo)
        elif intent == "first_salary":
            d, mo = _date(text), _money(text)
            if d and mo:
                ev.first_salary = (d, mo)
        elif intent == "household_ended":
            ev.remaining_salary = _money(text)
        elif intent in ("employment_ended", "seasonal_ended"):
            ev.income_ended = True
        elif intent == "base_salary":
            ev.base_salary = _money(text)
        elif intent == "arrears":
            ev.regular_next, ev.arrears = _money(text, 0), _money(text, 1)
        elif intent == "invoice":
            d, mo = _date(text), _money(text)
            if d and mo:
                ev.invoices.append((d, mo))
        elif intent == "fx_salary":
            d, mo = _date(text), _money(text)
            if d and mo:
                ev.fx_salary = (d, mo)
        elif intent == "rent_increase":
            pct = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
            if pct:
                ev.rent_increase_pct = Decimal(pct.group(1))
    return ev


STATS = {"messages": 0, "messages_recognised": 0, "messages_unrecognised": 0,
         "images_cache": 0, "images_ocr": 0, "images_missing": 0, "llm_calls": 0}


def build_evidence(ds, request) -> "Evidence":
    """Collect all message + image evidence relevant to one request."""
    from .images import image_amount
    msgs = [m for m in ds.messages.get(request.user_id, []) if m.request_id in (None, request.request_id)]
    ev = parse_messages(msgs)
    STATS["messages"] += len(msgs)
    STATS["messages_recognised"] += ev.recognised
    STATS["messages_unrecognised"] += len(ev.unrecognised)
    blank = {e.event_id for e in ds.events.get(request.user_id, []) if e.amount is None}
    for img in ds.images.get(request.user_id, []):
        if img.request_id not in (None, request.request_id) or img.related_event_id not in blank:
            continue
        got = image_amount(img.image_id, img.path)
        if got is None:
            STATS["images_missing"] += 1
        else:
            STATS["images_cache" if got[2] == "cache" else "images_ocr"] += 1
        if got is not None:
            ev.event_amounts[img.related_event_id] = got[0]
            ev.not_recurring.add(img.related_event_id)     # a document, not a cadence
    return ev
