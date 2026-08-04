"""Thai Buddhist-Era (BE) date handling.

The SEC portal renders dates as ``DD/MM/BBBB`` in the Buddhist calendar
(BE = AD + 543) but accepts query dates as ``YYYYMMDD`` in the *Gregorian*
calendar. Both directions are needed.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

BE_OFFSET = 543

_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

_BE_DATE_RE = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})\s*$")
_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")
_COMPACT_RE = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})\s*$")

THAI_MONTHS_FULL = [
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
]
THAI_MONTHS_ABBR = [
    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
]


def normalize_thai_digits(value: str) -> str:
    """Convert Thai numerals to ASCII digits."""
    return (value or "").translate(_THAI_DIGITS)


def parse_be_date(value: Optional[str]) -> Optional[date]:
    """Parse ``DD/MM/BBBB`` (Buddhist Era) into a Gregorian :class:`date`.

    Returns ``None`` when the value is missing, a placeholder such as ``-``, or
    not a well-formed date. Years that are already Gregorian (< 2200) are
    accepted as-is so the parser survives a future change on the SEC side.
    """
    if not value:
        return None
    text = normalize_thai_digits(value).strip()
    match = _BE_DATE_RE.match(text)
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    # A BE year is ~2500+. Treat anything below 2200 as already Gregorian.
    if year >= 2200:
        year -= BE_OFFSET
    try:
        return date(year, month, day)
    except ValueError:
        return None


def to_be_year(gregorian_year: int) -> int:
    return gregorian_year + BE_OFFSET


def format_be_date(value: Optional[date]) -> Optional[str]:
    """Render a Gregorian date back as ``DD/MM/BBBB``."""
    if value is None:
        return None
    return f"{value.day:02d}/{value.month:02d}/{to_be_year(value.year)}"


def format_thai_long(value: Optional[date]) -> Optional[str]:
    """Render as e.g. ``27 กุมภาพันธ์ 2566`` - useful for AI narratives."""
    if value is None:
        return None
    return f"{value.day} {THAI_MONTHS_FULL[value.month - 1]} {to_be_year(value.year)}"


def parse_flexible_date(value: Optional[str], field_name: str = "date") -> Optional[date]:
    """Accept ``YYYYMMDD``, ``YYYY-MM-DD`` or ``DD/MM/BBBB`` from API callers.

    Raises :class:`ValueError` with a caller-friendly message on bad input so the
    router can turn it into an HTTP 422.
    """
    if value is None:
        return None
    text = normalize_thai_digits(str(value)).strip()
    if not text:
        return None

    for pattern in (_ISO_RE, _COMPACT_RE):
        match = pattern.match(text)
        if match:
            year, month, day = (int(g) for g in match.groups())
            if year >= 2200:  # caller supplied a BE year
                year -= BE_OFFSET
            try:
                return date(year, month, day)
            except ValueError as exc:
                raise ValueError(f"{field_name}: ไม่ใช่วันที่ที่ถูกต้อง ({value})") from exc

    be = parse_be_date(text)
    if be is not None:
        return be

    raise ValueError(
        f"{field_name}: รูปแบบวันที่ไม่ถูกต้อง ({value}) - "
        "รองรับ YYYYMMDD, YYYY-MM-DD หรือ DD/MM/BBBB"
    )


def to_compact(value: date) -> str:
    """Format a date the way the SEC query string expects (``YYYYMMDD``)."""
    return f"{value.year:04d}{value.month:02d}{value.day:02d}"


def to_iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None
