"""Amount extraction from receipt / bill / payslip images.

Order of preference:
1. `evidence_cache/image_amounts.json` (values verified by reading the image)
2. Tesseract OCR + a conservative "grand total / balance due" heuristic
   (only used when tesseract is installed)
3. Optional Gemini vision call (`GEMINI_API_KEY`) - see `llm_fallback.py`

Image content is treated as untrusted data: only a numeric amount is taken.
"""
from __future__ import annotations

import json
import re
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

CACHE = Path(__file__).resolve().parent.parent / "evidence_cache" / "image_amounts.json"
_TOTAL = re.compile(r"(grand\s*total|balance\s*due|net\s*pay|total\s*paid|amount\s*payable|net\s*amount|total)[^0-9\n]{0,15}([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)


def _cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except FileNotFoundError:
        return {}


def ocr_amount(path: Path) -> Optional[Decimal]:
    if not shutil.which("tesseract"):
        return None
    try:
        import pytesseract
        from PIL import Image
        text = pytesseract.image_to_string(Image.open(path))
    except Exception:
        return None
    best = None
    for _, num in _TOTAL.findall(text):
        try:
            best = Decimal(num.replace(",", ""))
        except InvalidOperation:
            continue
    return best


def image_amount(image_id: str, path: Path) -> Optional[tuple[Decimal, Optional[str], str]]:
    hit = _cache().get(image_id)
    if hit and not image_id.startswith("_"):
        return Decimal(hit["amount"]), hit.get("currency"), "cache"
    amt = ocr_amount(path)
    return (amt, None, "ocr") if amt is not None else None
