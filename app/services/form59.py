"""Scrape and normalise แบบ 59 - executive securities-holding change reports.

Source page (verified live):
``/public/idisc/th/Viewmore/r59-2?DateFrom=&DateTo=&DateType=&uniqueIDReference=``

Observed behaviour that shapes this module:

* Results live in a single ``<table id="gPP09T01">`` with nine columns and **no
  pagination** - a 49-record response renders every row inline.
* The card heading carries an authoritative count
  (``จำนวนรายการที่พบ N รายการ``), used to verify the parse.
* ``DateType`` **must** be present or the date range is silently ignored and the
  portal returns every record on file. Values 1/2/3 behaved identically in
  testing; 1 is what the SEC UI sends.
* Dates render in the Buddhist calendar (``27/02/2566``) while the query string
  expects Gregorian ``YYYYMMDD``.

Two role columns, easy to conflate:

``ชื่อผู้บริหาร``
    The **executive who carries the reporting duty**. Constant across all of that
    executive's rows.
``ความสัมพันธ์``
    **Whose holdings actually changed**, relative to that executive - either
    ``ผู้รายงาน`` (the executive themself) or a related party named in the
    trailing parenthetical: a spouse, a minor child, or a juristic person the
    executive's group controls.

The page also carries an explicit SEC caution: when two executives are married to
each other, a single trade is filed by **both**, so the listing shows it twice.
:func:`mark_duplicates` detects that pattern so aggregates do not double-count.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date
from typing import Any, Optional

from bs4 import BeautifulSoup

from .. import config
from ..http_client import TTLCache, fetch_html
from ..mappings import map_acquisition_method, map_relationship, map_security_type
from ..utils.text import (
    clean_optional,
    clean_text,
    extract_symbol_from_company_label,
    parse_float,
    parse_int,
    round_money,
    split_person_title,
    strip_symbol_suffix,
)
from ..utils.thai_date import format_thai_long, parse_be_date, to_compact, to_iso

logger = logging.getLogger(__name__)

_cache = TTLCache(config.CACHE_TTL_FORM59, "form59")

_COUNT_RE = re.compile(r"จำนวนรายการที่พบ\s*([\d,]+)\s*รายการ")

# The SEC's own warning about cross-reported (duplicated) rows, shown under the
# table and surfaced in the API response so consumers see it too.
REVOKED_CAUTION_TH = (
    "พบรายการที่ผู้รายงานยกเลิกภายหลัง (Revoked by Reporter) ซึ่งเว็บ ก.ล.ต. ยังคงแสดงไว้ "
    "โดยขีดฆ่าตัวเลขจำนวนหน่วย ระบบทำเครื่องหมาย is_revoked=true และไม่นับรวมในค่าสรุป "
    "เนื่องจากรายการเหล่านี้ไม่มีผลต่อการถือครองจริง"
)

DUPLICATE_CAUTION_TH = (
    "กรณีที่บริษัทมีผู้บริหารเป็นคู่สมรสกัน การซื้อขายหนึ่งรายการจะถูกรายงานโดยคู่สมรสทั้งสองคน "
    "ทำให้ปรากฏเป็นรายการซ้ำซ้อนในหน้าเว็บ ก.ล.ต. ระบบได้ตรวจหาและทำเครื่องหมายรายการซ้ำไว้ "
    "ในฟิลด์ is_potential_duplicate และคำนวณค่าสรุปจากรายการที่ไม่ซ้ำเท่านั้น"
)

# Column order on the live page. Header text is matched first; this is the
# positional fallback so a reordered table still parses.
_COLUMN_ORDER = [
    "company", "executive", "relationship", "security_type",
    "transaction_date", "volume", "price", "method", "remark",
]

_HEADER_HINTS: list[tuple[str, str]] = [
    ("ชื่อบริษัท", "company"),
    ("ชื่อผู้บริหาร", "executive"),
    ("ความสัมพันธ์", "relationship"),
    ("ประเภทหลักทรัพย์", "security_type"),
    ("วันที่ได้มา", "transaction_date"),
    ("จำนวน", "volume"),
    ("ราคา", "price"),
    ("วิธีการได้มา", "method"),
    ("หมายเหตุ", "remark"),
]


def _map_columns(table) -> dict[str, int]:
    """Build a column-name -> index map from the header row."""
    header_row = table.find("tr")
    headers = header_row.find_all("th") if header_row is not None else []
    if not headers:
        return {name: index for index, name in enumerate(_COLUMN_ORDER)}

    mapping: dict[str, int] = {}
    for index, cell in enumerate(headers):
        text = clean_text(cell.get_text())
        for hint, key in _HEADER_HINTS:
            if hint in text and key not in mapping:
                mapping[key] = index
                break

    for index, name in enumerate(_COLUMN_ORDER):
        mapping.setdefault(name, index)
    return mapping


def _cell(cells: list, columns: dict[str, int], key: str):
    index = columns.get(key)
    if index is None or index >= len(cells):
        return None
    return cells[index]


def _cell_text(cells: list, columns: dict[str, int], key: str) -> Optional[str]:
    cell = _cell(cells, columns, key)
    return clean_optional(cell.get_text()) if cell is not None else None


# A withdrawn filing is rendered with the figure struck through and annotated,
# e.g. ``<span style="text-decoration: line-through">429,000</span><br/>Revoked
# by Reporter``. Plain get_text() yields "429,000Revoked by Reporter", which no
# number parser accepts - so the struck span is read on its own.
_LINE_THROUGH_RE = re.compile(r"line-through", re.IGNORECASE)


def _struck_span(cell):
    return cell.find("span", style=_LINE_THROUGH_RE) if cell is not None else None


def _cell_value_text(cells: list, columns: dict[str, int], key: str) -> Optional[str]:
    """Numeric cell text, reading the struck-through value on revoked rows."""
    cell = _cell(cells, columns, key)
    if cell is None:
        return None
    span = _struck_span(cell)
    if span is not None:
        return clean_optional(span.get_text())
    return clean_optional(cell.get_text())


def _detect_revocation(cells: list) -> tuple[bool, Optional[str]]:
    """Return ``(is_revoked, note)`` for a row.

    The SEC keeps withdrawn filings visible in the listing rather than deleting
    them, so they must be recognised or a cancelled trade reads as a real one.
    """
    for cell in cells:
        span = _struck_span(cell)
        if span is None:
            continue
        # The annotation is whatever the cell says besides the struck figure.
        remainder = cell.get_text().replace(span.get_text(), "", 1)
        return True, clean_optional(remainder) or "Revoked"
    return False, None


def parse_form59_html(html: str) -> dict[str, Any]:
    """Parse the Form 59 listing page into normalised records.

    Returns the records plus parse diagnostics (the source-reported count and
    whether it matches) so callers can detect silent upstream changes rather than
    trusting a short list.
    """
    soup = BeautifulSoup(html, "lxml")

    reported_count: Optional[int] = None
    heading = soup.select_one(".card-heading")
    if heading:
        match = _COUNT_RE.search(heading.get_text())
        if match:
            reported_count = parse_int(match.group(1))

    table = soup.find("table", id="gPP09T01")
    if table is None:
        candidates = soup.find_all("table")
        table = max(candidates, key=lambda t: len(t.find_all("tr")), default=None)

    records: list[dict[str, Any]] = []
    if table is not None:
        columns = _map_columns(table)
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 5:
                continue  # header row, or the "no data" placeholder
            record = _parse_row(cells, columns)
            if record is not None:
                records.append(record)

    mark_duplicates(records)

    return {
        "records": records,
        "reported_count": reported_count,
        "parsed_count": len(records),
        "parse_complete": (reported_count is None or reported_count == len(records)),
    }


def _normalise_person_key(name: Optional[str]) -> Optional[str]:
    """Key for identity comparison across columns.

    Necessary because the two columns format titles differently - the executive
    column writes ``"นาย สารัชถ์ รัตนาวะดี"`` while the parenthetical writes
    ``"นางนลินี รัตนาวะดี"`` with no space - so the title is stripped and all
    whitespace removed before comparing.
    """
    if not name:
        return None
    _, bare = split_person_title(name)
    if not bare:
        return None
    return re.sub(r"\s+", "", bare)


def _parse_row(cells: list, columns: dict[str, int]) -> Optional[dict[str, Any]]:
    company_label = _cell_text(cells, columns, "company")
    executive_raw = _cell_text(cells, columns, "executive")
    transaction_date_raw = _cell_text(cells, columns, "transaction_date")

    # A row with neither a person nor a date is a spacer, not a filing.
    if not executive_raw and not transaction_date_raw:
        return None

    transaction_date = parse_be_date(transaction_date_raw)
    is_revoked, revocation_note = _detect_revocation(cells)
    shares = parse_int(_cell_value_text(cells, columns, "volume"))
    price = parse_float(_cell_value_text(cells, columns, "price"))

    method = map_acquisition_method(_cell_text(cells, columns, "method"))
    security = map_security_type(_cell_text(cells, columns, "security_type"))
    relationship = map_relationship(_cell_text(cells, columns, "relationship"))

    executive_title, executive_name = split_person_title(executive_raw)

    # The holder is the related party when one is named, otherwise the executive.
    related = relationship.get("related_person")
    if related:
        holder_raw = related
        holder_is_executive = False
    else:
        holder_raw = executive_raw
        holder_is_executive = True

    holder_type = relationship.get("holder_type") or "unknown"
    if holder_type == "juristic_person":
        # Company names carry no personal title; keep them verbatim.
        holder_title, holder_name = None, clean_text(holder_raw) or None
    else:
        holder_title, holder_name = split_person_title(holder_raw)

    # A revoked filing has no signed effect on holdings: the reported figure is
    # kept for reference, but left out of signed arithmetic entirely.
    shares_signed: Optional[int] = None
    if shares is not None and not is_revoked:
        if method["direction"] == "acquire":
            shares_signed = shares
        elif method["direction"] == "dispose":
            shares_signed = -shares
        elif method["direction"] == "neutral":
            shares_signed = 0

    transaction_value = (
        round_money(shares * price) if (shares is not None and price is not None) else None
    )

    remark_cell = _cell(cells, columns, "remark")
    report_url = None
    if remark_cell is not None:
        anchor = remark_cell.find("a", href=True)
        if anchor:
            report_url = anchor["href"]

    return {
        "symbol": extract_symbol_from_company_label(company_label),
        "company_name_th": strip_symbol_suffix(company_label),
        "company_label_raw": company_label,

        "executive_name": executive_name,
        "executive_title": executive_title,
        "executive_name_raw": executive_raw,

        "relationship_code": relationship["code"],
        "relationship_th": relationship["label_th"],
        "relationship_en": relationship["label_en"],
        "is_self_filing": relationship["is_self"],

        "holder_name": holder_name,
        "holder_title": holder_title,
        "holder_type": holder_type,
        "holder_is_executive": holder_is_executive,

        "security_type_code": security["code"],
        "security_type_th": security["label_th"],
        "security_type_en": security["label_en"],
        "asset_class": security["asset_class"],

        "transaction_date": to_iso(transaction_date),
        "transaction_date_be": transaction_date_raw,
        "transaction_date_thai": format_thai_long(transaction_date),

        "shares": shares,
        "shares_signed": shares_signed,
        "price_per_share": price,
        "transaction_value": transaction_value,
        "currency": "THB",

        "method_code": method["code"],
        "method_th": method["label_th"],
        "method_en": method["label_en"],
        "direction": method["direction"],
        "is_market_trade": method["is_market_trade"],

        "is_potential_duplicate": False,
        "duplicate_of_index": None,
        "is_revoked": is_revoked,
        "revocation_note": revocation_note,

        "report_url": report_url,
        "narrative_th": _build_narrative(
            executive_name, holder_name, holder_is_executive, relationship,
            method, security, shares, price, format_thai_long(transaction_date),
            is_revoked,
        ),
        # Internal, stripped before the response is returned.
        "_holder_key": _normalise_person_key(holder_raw)
        if holder_type != "juristic_person"
        else re.sub(r"\s+", "", clean_text(holder_raw or "")),
        "_executive_key": _normalise_person_key(executive_raw),
    }


def mark_duplicates(records: list[dict[str, Any]]) -> int:
    """Flag rows that are the same trade filed by two married executives.

    The SEC warns that when both spouses are executives of the same company, one
    trade produces two rows: ``(executive=A, relationship=self)`` and
    ``(executive=B, relationship=spouse of A)``. Both describe the *same* holder,
    date, volume, price and method.

    Dedup is deliberately conservative - a group only counts as duplicated when
    its rows come from **different executives**. Two identical rows filed by the
    same executive are left alone, since those may be genuinely separate trades
    that happen to match on every visible field.

    Returns the number of rows flagged. Rows keep their data; only the
    ``is_potential_duplicate`` flag is set, so no information is lost.
    """
    groups: dict[tuple, list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        if not record.get("_holder_key") or not record.get("transaction_date"):
            continue
        if record.get("is_revoked"):
            # A withdrawn row must not shadow the real filing it resembles.
            continue
        key = (
            record["_holder_key"],
            record["transaction_date"],
            record.get("shares"),
            record.get("price_per_share"),
            record.get("method_code"),
            record.get("security_type_code"),
        )
        groups[key].append(index)

    flagged = 0
    for indices in groups.values():
        if len(indices) < 2:
            continue
        distinct_executives = {records[i].get("_executive_key") for i in indices}
        if len(distinct_executives) < 2:
            continue  # same filer twice - not the cross-reporting pattern
        canonical = indices[0]
        for index in indices[1:]:
            records[index]["is_potential_duplicate"] = True
            records[index]["duplicate_of_index"] = canonical
            flagged += 1
    return flagged


def strip_internal_fields(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the ``_``-prefixed helper keys before serialising."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]


def _build_narrative(
    executive_name: Optional[str],
    holder_name: Optional[str],
    holder_is_executive: bool,
    relationship: dict,
    method: dict,
    security: dict,
    shares: Optional[int],
    price: Optional[float],
    date_thai: Optional[str],
    is_revoked: bool = False,
) -> str:
    """Compose one plain-Thai sentence per filing for direct AI consumption."""
    action = method.get("label_th") or "ทำรายการ"
    what = security.get("label_th") or "หลักทรัพย์"
    volume = f"{shares:,} หน่วย" if shares is not None else "จำนวนไม่ระบุ"

    if holder_is_executive:
        who = executive_name or "ผู้บริหาร"
    else:
        # Name the holder, then the executive the filing hangs off, using the
        # short relationship label so brackets are not nested.
        short_label = (relationship.get("label_th") or "").split("(")[0].strip()
        short_label = re.sub(r"\s+", " ", short_label)
        if len(short_label) > 40:  # the juristic-person label is a paragraph
            short_label = "นิติบุคคลที่เกี่ยวข้อง"
        who = holder_name or "ผู้ที่เกี่ยวข้อง"
        if executive_name:
            who = f"{who} ({short_label}ของผู้บริหาร {executive_name})"

    parts = [f"{who} {action}{what} {volume}"]
    if price is not None:
        parts.append(f"ที่ราคา {price:,.2f} บาท/หน่วย")
        if shares is not None:
            parts.append(f"คิดเป็นมูลค่า {shares * price:,.2f} บาท")
    if date_thai:
        parts.append(f"เมื่อวันที่ {date_thai}")
    if is_revoked:
        # Stated first thing a reader sees, so the sentence cannot be quoted as
        # evidence of a trade that was withdrawn.
        return "[ยกเลิกรายการแล้ว] " + " ".join(parts) + " (ผู้รายงานยกเลิกรายการนี้ ไม่นับรวมในค่าสรุป)"
    return " ".join(parts)


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def build_analytics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive aggregates an AI can reason over without re-reading every row.

    Two separations are applied deliberately:

    * **Duplicates excluded** - rows flagged by :func:`mark_duplicates` are left
      out of every total, per the SEC's own caution.
    * **Market trades vs. transfers** - an intra-family transfer of 550,000
      shares is not insider buying, so ``market_activity`` and
      ``non_market_activity`` are reported separately instead of being merged.
    """
    duplicates = [r for r in records if r.get("is_potential_duplicate")]
    revoked = [r for r in records if r.get("is_revoked")]
    unique = [
        r for r in records
        if not r.get("is_potential_duplicate") and not r.get("is_revoked")
    ]

    if not unique:
        return {
            "total_records": len(records),
            "records_used_in_totals": 0,
            "duplicate_records_excluded": len(duplicates),
            "revoked_records_excluded": len(revoked),
            "revoked_caution_th": REVOKED_CAUTION_TH if revoked else None,
            "duplicate_caution_th": DUPLICATE_CAUTION_TH if duplicates else None,
            "date_range": {"first": None, "last": None},
            "market_activity": _empty_flow(),
            "non_market_activity": _empty_flow(),
            "net_position_change_shares": 0,
            "by_method": [],
            "by_security_type": [],
            "by_holder": [],
            "by_executive": [],
            "by_month": [],
            "largest_transactions": [],
            "activity_summary_th": "ไม่พบรายการรายงานการเปลี่ยนแปลงการถือหลักทรัพย์ในช่วงเวลาที่ระบุ",
        }

    dates = sorted(r["transaction_date"] for r in unique if r.get("transaction_date"))

    market = [r for r in unique if r.get("is_market_trade")]
    non_market = [r for r in unique if not r.get("is_market_trade")]

    by_method: dict[str, dict] = defaultdict(lambda: {"records": 0, "shares": 0, "value": 0.0})
    for record in unique:
        bucket = by_method[record["method_code"]]
        bucket["records"] += 1
        bucket["shares"] += record.get("shares") or 0
        bucket["value"] += record.get("transaction_value") or 0.0
        bucket["method_th"] = record.get("method_th")
        bucket["method_en"] = record.get("method_en")
        bucket["direction"] = record.get("direction")

    by_security: dict[str, dict] = defaultdict(lambda: {"records": 0, "shares": 0})
    for record in unique:
        bucket = by_security[record["security_type_code"]]
        bucket["records"] += 1
        bucket["shares"] += record.get("shares") or 0
        bucket["security_type_th"] = record.get("security_type_th")
        bucket["security_type_en"] = record.get("security_type_en")

    by_holder = _group_by_actor(
        unique, key_field="holder_name", title_field="holder_title", include_holder_type=True
    )
    by_executive = _group_by_actor(
        unique, key_field="executive_name", title_field="executive_title",
        include_holder_type=False,
    )

    by_month: dict[str, dict] = defaultdict(
        lambda: {"records": 0, "buy_shares": 0, "sell_shares": 0, "net_shares": 0}
    )
    for record in unique:
        iso = record.get("transaction_date")
        if not iso:
            continue
        bucket = by_month[iso[:7]]
        bucket["records"] += 1
        if record.get("is_market_trade"):
            if record.get("direction") == "acquire":
                bucket["buy_shares"] += record.get("shares") or 0
            elif record.get("direction") == "dispose":
                bucket["sell_shares"] += record.get("shares") or 0
        if record.get("shares_signed") is not None:
            bucket["net_shares"] += record["shares_signed"]

    largest = sorted(
        (r for r in unique if r.get("transaction_value") is not None),
        key=lambda r: r["transaction_value"],
        reverse=True,
    )[:5]

    market_flow = _summarise_flow(market)
    non_market_flow = _summarise_flow(non_market)
    net_change = sum(r["shares_signed"] for r in unique if r.get("shares_signed") is not None)

    return {
        "total_records": len(records),
        "records_used_in_totals": len(unique),
        "duplicate_records_excluded": len(duplicates),
        "revoked_records_excluded": len(revoked),
        "revoked_caution_th": REVOKED_CAUTION_TH if revoked else None,
        "duplicate_caution_th": DUPLICATE_CAUTION_TH if duplicates else None,
        "date_range": {"first": dates[0] if dates else None, "last": dates[-1] if dates else None},
        "market_activity": market_flow,
        "non_market_activity": non_market_flow,
        "net_position_change_shares": net_change,
        "by_method": [
            {"method_code": code,
             **{k: (round_money(v) if k == "value" else v) for k, v in data.items()}}
            for code, data in sorted(by_method.items(), key=lambda kv: -kv[1]["records"])
        ],
        "by_security_type": [
            {"security_type_code": code, **data}
            for code, data in sorted(by_security.items(), key=lambda kv: -kv[1]["records"])
        ],
        "by_holder": by_holder,
        "by_executive": by_executive,
        "by_month": [{"month": month, **data} for month, data in sorted(by_month.items())],
        "largest_transactions": [
            {
                "holder_name": r.get("holder_name"),
                "executive_name": r.get("executive_name"),
                "transaction_date": r.get("transaction_date"),
                "method_code": r.get("method_code"),
                "shares": r.get("shares"),
                "price_per_share": r.get("price_per_share"),
                "transaction_value": r.get("transaction_value"),
            }
            for r in largest
        ],
        "activity_summary_th": _summary_sentence(
            len(unique), market_flow, non_market_flow, dates,
            len(duplicates), len(revoked),
        ),
    }


def _group_by_actor(
    records: list[dict[str, Any]],
    key_field: str,
    title_field: str,
    include_holder_type: bool,
) -> list[dict[str, Any]]:
    """Aggregate per holder or per executive.

    ``by_holder`` answers "who actually accumulated or sold"; ``by_executive``
    mirrors the portal's own grouping. Both are useful, so both are returned.
    """
    buckets: dict[str, dict] = defaultdict(
        lambda: {
            "records": 0, "buy_shares": 0, "sell_shares": 0,
            "buy_value": 0.0, "sell_value": 0.0,
            "transfer_in_shares": 0, "transfer_out_shares": 0,
            "net_shares": 0, "dates": [],
            "relationship_codes": set(), "holder_types": set(),
            "title": None,
        }
    )

    for record in records:
        name = record.get(key_field) or "ไม่ระบุ"
        bucket = buckets[name]
        bucket["records"] += 1
        # Title of whichever actor this grouping is keyed on - using the
        # executive's title for a holder row would mislabel spouses and companies.
        bucket["title"] = bucket["title"] or record.get(title_field)
        if record.get("relationship_code"):
            bucket["relationship_codes"].add(record["relationship_code"])
        if record.get("holder_type"):
            bucket["holder_types"].add(record["holder_type"])

        shares = record.get("shares") or 0
        value = record.get("transaction_value") or 0.0

        if record.get("is_market_trade"):
            if record.get("direction") == "acquire":
                bucket["buy_shares"] += shares
                bucket["buy_value"] += value
            elif record.get("direction") == "dispose":
                bucket["sell_shares"] += shares
                bucket["sell_value"] += value
        else:
            if record.get("direction") == "acquire":
                bucket["transfer_in_shares"] += shares
            elif record.get("direction") == "dispose":
                bucket["transfer_out_shares"] += shares

        if record.get("shares_signed") is not None:
            bucket["net_shares"] += record["shares_signed"]
        if record.get("transaction_date"):
            bucket["dates"].append(record["transaction_date"])

    result = []
    for name, data in sorted(buckets.items(), key=lambda kv: -kv[1]["records"]):
        entry = {
            "name": name,
            "title": data["title"],
            "relationship_codes": sorted(data["relationship_codes"]),
            "records": data["records"],
            "buy_shares": data["buy_shares"],
            "sell_shares": data["sell_shares"],
            "buy_value": round_money(data["buy_value"]),
            "sell_value": round_money(data["sell_value"]),
            "transfer_in_shares": data["transfer_in_shares"],
            "transfer_out_shares": data["transfer_out_shares"],
            "net_shares": data["net_shares"],
            "first_transaction": min(data["dates"]) if data["dates"] else None,
            "last_transaction": max(data["dates"]) if data["dates"] else None,
        }
        if include_holder_type:
            types = sorted(data["holder_types"])
            entry["holder_type"] = types[0] if len(types) == 1 else "mixed"
        result.append(entry)
    return result


def _empty_flow() -> dict[str, Any]:
    return {
        "records": 0, "acquire_records": 0, "dispose_records": 0,
        "acquire_shares": 0, "dispose_shares": 0,
        "acquire_value": 0.0, "dispose_value": 0.0,
        "net_shares": 0, "net_value": 0.0,
        "net_direction": "no_activity",
    }


def _summarise_flow(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum a set of filings into an acquire/dispose flow.

    Named by direction rather than buy/sell because this same helper summarises
    the non-market bucket, where an "acquire" is a transfer in or an inheritance
    rather than a purchase.
    """
    acquire_shares = dispose_shares = 0
    acquire_value = dispose_value = 0.0
    acquire_records = dispose_records = 0

    for record in records:
        shares = record.get("shares") or 0
        value = record.get("transaction_value") or 0.0
        if record.get("direction") == "acquire":
            acquire_records += 1
            acquire_shares += shares
            acquire_value += value
        elif record.get("direction") == "dispose":
            dispose_records += 1
            dispose_shares += shares
            dispose_value += value

    net_shares = acquire_shares - dispose_shares
    if not records:
        direction = "no_activity"
    elif net_shares > 0:
        direction = "net_acquisition"
    elif net_shares < 0:
        direction = "net_disposal"
    else:
        direction = "balanced"

    return {
        "records": len(records),
        "acquire_records": acquire_records,
        "dispose_records": dispose_records,
        "acquire_shares": acquire_shares,
        "dispose_shares": dispose_shares,
        "acquire_value": round_money(acquire_value),
        "dispose_value": round_money(dispose_value),
        "net_shares": net_shares,
        "net_value": round_money(acquire_value - dispose_value),
        "net_direction": direction,
    }


_DIRECTION_TH = {
    "net_acquisition": "ซื้อสุทธิ",
    "net_disposal": "ขายสุทธิ",
    "balanced": "ซื้อขายสมดุล",
    "no_activity": "ไม่มีรายการซื้อขายในตลาด",
}


def _summary_sentence(
    total: int, market: dict, non_market: dict, dates: list[str],
    duplicates: int, revoked: int = 0,
) -> str:
    """A factual, non-advisory digest of the period."""
    period = f"ระหว่างวันที่ {dates[0]} ถึง {dates[-1]} " if dates else ""
    parts = [f"พบรายการแบบ 59 ที่ไม่ซ้ำกัน {total:,} รายการ {period}".strip()]

    if market["records"]:
        parts.append(
            f"แบ่งเป็นรายการซื้อขายผ่านตลาด {market['records']:,} รายการ "
            f"(ซื้อ {market['acquire_shares']:,} หน่วย มูลค่า {market['acquire_value']:,.2f} บาท / "
            f"ขาย {market['dispose_shares']:,} หน่วย มูลค่า {market['dispose_value']:,.2f} บาท) "
            f"สรุปเป็นการ{_DIRECTION_TH.get(market['net_direction'], '')} "
            f"{abs(market['net_shares']):,} หน่วย"
        )
    else:
        parts.append("ไม่มีรายการซื้อขายผ่านตลาด")

    if non_market["records"]:
        parts.append(
            "และมีรายการที่ไม่ใช่การซื้อขายผ่านตลาด (เช่น การโอน/รับโอน) "
            f"อีก {non_market['records']:,} รายการ"
        )
    excluded = []
    if duplicates:
        excluded.append(f"รายการซ้ำซ้อน {duplicates:,} รายการ")
    if revoked:
        excluded.append(f"รายการที่ถูกยกเลิก {revoked:,} รายการ")
    if excluded:
        parts.append("(ไม่นับรวม " + " และ ".join(excluded) + ")")
    return " ".join(parts)


# --------------------------------------------------------------------------
# Fetch orchestration
# --------------------------------------------------------------------------
async def fetch_form59(
    unique_id: str,
    date_from: date,
    date_to: date,
    date_type: int = 1,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Fetch and parse Form 59 for one company over a date range."""
    params = {
        "DateFrom": to_compact(date_from),
        "DateTo": to_compact(date_to),
        # Must always be sent: omitting DateType makes the portal ignore the
        # range and return every record on file.
        "DateType": str(date_type),
        "uniqueIDReference": unique_id,
    }
    cache_key = "|".join(f"{k}={v}" for k, v in sorted(params.items()))

    async def factory() -> dict[str, Any]:
        html = await fetch_html(config.PATH_FORM59, params=params)
        parsed = parse_form59_html(html)
        if not parsed["parse_complete"]:
            logger.warning(
                "Form 59 parse mismatch for %s: page reported %s rows, parsed %s",
                unique_id, parsed["reported_count"], parsed["parsed_count"],
            )
        parsed["source_url"] = f"{config.BASE_URL}{config.PATH_FORM59}?" + "&".join(
            f"{k}={v}" for k, v in params.items()
        )
        return parsed

    if not use_cache:
        return await factory()
    return await _cache.get_or_set(cache_key, factory)


def cache_stats() -> dict:
    return _cache.stats()


def invalidate_cache() -> None:
    _cache.invalidate()
