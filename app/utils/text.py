"""Text and number normalisation for scraped SEC HTML."""
from __future__ import annotations

import re
from typing import Optional

# The portal is full of &nbsp; (U+00A0) and doubled spaces inside Thai names.
_WS_RE = re.compile(r"[\s ​]+")

# Values the portal uses to mean "nothing here".
_EMPTY_TOKENS = {"", "-", "--", "n/a", "N/A", "na", "NA", "ไม่มี", "ไม่มีข้อมูล", "ไม่ระบุ"}

_NUMBER_CLEAN_RE = re.compile(r"[,\s ]")


def clean_text(value: Optional[str]) -> str:
    """Collapse whitespace/NBSP and strip. Never returns ``None``."""
    if value is None:
        return ""
    return _WS_RE.sub(" ", str(value)).strip()


def clean_optional(value: Optional[str]) -> Optional[str]:
    """Like :func:`clean_text` but maps placeholder values to ``None``."""
    text = clean_text(value)
    if text in _EMPTY_TOKENS or text.lower() in _EMPTY_TOKENS:
        return None
    return text


def is_empty_token(value: Optional[str]) -> bool:
    text = clean_text(value)
    return text in _EMPTY_TOKENS or text.lower() in _EMPTY_TOKENS


def parse_int(value: Optional[str]) -> Optional[int]:
    """Parse ``"1,000,000"`` into ``1000000``; ``None`` when not a number."""
    text = clean_optional(value)
    if text is None:
        return None
    negative = text.startswith("(") and text.endswith(")")  # accounting negatives
    text = _NUMBER_CLEAN_RE.sub("", text.strip("()"))
    if not re.fullmatch(r"[-+]?\d+(\.0+)?", text):
        return None
    result = int(float(text))
    return -result if negative else result


def parse_float(value: Optional[str]) -> Optional[float]:
    """Parse ``"53.00"`` into ``53.0``; ``None`` when not a number."""
    text = clean_optional(value)
    if text is None:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = _NUMBER_CLEAN_RE.sub("", text.strip("()"))
    if not re.fullmatch(r"[-+]?\d*\.?\d+", text):
        return None
    result = float(text)
    return -result if negative else result


def round_money(value: Optional[float], digits: int = 2) -> Optional[float]:
    """Round monetary values to the 2-decimal house standard."""
    if value is None:
        return None
    return round(value + 0.0, digits)


_SYMBOL_IN_NAME_RE = re.compile(r"\(([A-Z0-9][A-Z0-9&.\-]{0,14})\)\s*$")


def extract_symbol_from_company_label(label: Optional[str]) -> Optional[str]:
    """Pull ``GULF`` out of ``"... จำกัด (มหาชน) บมจ.(GULF)"``.

    The Form 59 listing appends ``บมจ.(SYMBOL)`` to the company name. The regex
    is anchored at the end so the ``(มหาชน)`` suffix is never mistaken for a
    ticker, and it requires ASCII upper-case to avoid matching Thai text.
    """
    text = clean_text(label)
    if not text:
        return None
    match = _SYMBOL_IN_NAME_RE.search(text)
    if match:
        return match.group(1)
    return None


def strip_symbol_suffix(label: Optional[str]) -> Optional[str]:
    """Remove the trailing ``บมจ.(SYMBOL)`` / ``(SYMBOL)`` marker from a name."""
    text = clean_text(label)
    if not text:
        return None
    text = re.sub(r"\s*บมจ\.\s*\([A-Z0-9&.\-]+\)\s*$", "", text)
    text = re.sub(r"\s*\([A-Z0-9&.\-]{1,15}\)\s*$", "", text)
    return clean_text(text) or None


# Thai personal-name titles, longest first so "นางสาว" wins over "นาง".
_TITLES = [
    "นางสาว", "น.ส.", "นาง", "นาย", "ดร.", "ดอกเตอร์",
    "ศ.ดร.", "รศ.ดร.", "ผศ.ดร.", "ศ.", "รศ.", "ผศ.",
    "พล.อ.", "พล.ต.", "พล.ท.", "พล.ร.อ.", "พล.อ.อ.", "พล.ต.อ.", "พล.ต.ท.", "พล.ต.ต.",
    "พ.อ.", "พ.ท.", "พ.ต.", "ร.อ.", "ร.ท.", "ร.ต.",
    "ม.ร.ว.", "ม.ล.", "หม่อมราชวงศ์", "หม่อมหลวง",
    "คุณหญิง", "ท่านผู้หญิง", "Mr.", "Mrs.", "Ms.", "Miss", "Dr.",
]


def split_person_title(full_name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split ``"นางสาว โชติกา ..."`` into ``("นางสาว", "โชติกา ...")``.

    Returns ``(None, name)`` when no known title prefix is present.
    """
    text = clean_text(full_name)
    if not text:
        return None, None
    for title in sorted(_TITLES, key=len, reverse=True):
        if text.startswith(title):
            remainder = clean_text(text[len(title):])
            if remainder:
                return title, remainder
    return None, text
