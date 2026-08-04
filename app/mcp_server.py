"""MCP (Model Context Protocol) server exposing the SEC datasets as tools.

Transport is Streamable HTTP, mounted into the same FastAPI process as the REST
API. That co-location is deliberate: the tools call the **service layer directly**
rather than looping back through HTTP, so MCP traffic shares the one rate limiter
and the one TTL cache. A separate MCP process would double the request rate
against ``market.sec.or.th`` and trip its bot defence.

Every tool caps how much it returns. Tool results land in an LLM context window,
and a single company can hold 233 Form 59 rows (~300 KB as JSON), so records are
off by default and always truncated to ``max_records`` with an explicit note when
rows are dropped - silent truncation would read as "that is all the data".
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from . import config
from .deps import DEFAULT_LOOKBACK_DAYS, today_bangkok
from .http_client import BotChallengeError, NotFoundError, UpstreamError
from .services import form59 as form59_service
from .services import sustainability as sustainability_service
from .services import symbol_registry
from .utils.thai_date import parse_flexible_date, to_iso

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
เครื่องมือดึงข้อมูลเปิดเผยของบริษัทจดทะเบียนไทยจากเว็บ ก.ล.ต. (SEC Thailand,
market.sec.or.th) รองรับหุ้นทุกตัวในตลาด SET และ mai

ชุดข้อมูลที่ให้บริการ:
1. แบบ 59 - รายงานการเปลี่ยนแปลงการถือหลักทรัพย์ของผู้บริหาร
   (executive securities-holding changes / insider filings)
2. Sustainability Development - CG Score, AGM Level, Thai-CAC, SET ESG Ratings

ข้อควรทราบเมื่อนำข้อมูลไปวิเคราะห์:
- ตัวเลขทุกค่าเป็นชนิดตัวเลข วันที่เป็น ISO 8601 (ค.ศ.) พร้อมค่า พ.ศ. ต้นฉบับ
- ใช้ `market_activity` สำหรับการซื้อขายผ่านตลาด และ `non_market_activity`
  สำหรับการโอน/รับโอน/มรดก อย่ารวมสองค่านี้เข้าด้วยกัน
- `by_holder` คือผู้ถือหลักทรัพย์จริง ส่วน `by_executive` คือผู้บริหารที่มีหน้าที่รายงาน
  ทั้งสองค่ามักไม่ใช่คนเดียวกัน
- ก.ล.ต. เตือนว่าผู้บริหารที่เป็นคู่สมรสกันจะรายงานรายการเดียวกันทั้งสองคน
  ระบบทำเครื่องหมาย `is_potential_duplicate` และไม่นับรวมในค่าสรุปให้แล้ว
- ข้อมูลนี้ใช้เพื่อการศึกษาและวิเคราะห์เชิงสถิติ ไม่ใช่คำแนะนำการลงทุน
"""

mcp: FastMCP = FastMCP(
    name="efin-sec-idisc",
    instructions=INSTRUCTIONS,
    # Stateless: every request is self-contained, so there is no session to lose
    # on redeploy and no need for sticky routing.
    stateless_http=True,
    # Plain JSON responses instead of SSE framing - friendlier to proxies and to
    # clients that do not implement the streaming path.
    json_response=True,
    streamable_http_path="/",
)

# Records are heavy; these bounds keep a tool result context-sized.
DEFAULT_MAX_RECORDS = 50
HARD_MAX_RECORDS = 300


def _error(message: str, *, code: str, hint: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error_code": code, "error": message}
    if hint:
        payload["hint"] = hint
    return payload


def _handle(exc: Exception) -> dict[str, Any]:
    """Turn a service-layer exception into a result the model can act on."""
    if isinstance(exc, NotFoundError):
        return _error(str(exc), code="not_found",
                      hint="ตรวจสอบชื่อย่อหลักทรัพย์ หรือใช้ search_thai_stocks เพื่อค้นหา")
    if isinstance(exc, BotChallengeError):
        return _error(
            str(exc), code="upstream_rate_limited",
            hint="เว็บ ก.ล.ต. ปิดกั้นชั่วคราว ให้รออีก 2-5 นาทีแล้วลองใหม่ "
                 "อย่าเรียกซ้ำติดต่อกันทันที",
        )
    if isinstance(exc, UpstreamError):
        return _error(str(exc), code="upstream_error",
                      hint="เว็บ ก.ล.ต. อาจไม่พร้อมให้บริการชั่วคราว ลองใหม่อีกครั้ง")
    if isinstance(exc, ValueError):
        return _error(str(exc), code="invalid_argument")
    logger.exception("Unexpected MCP tool failure")
    return _error(f"เกิดข้อผิดพลาดภายในระบบ: {exc}", code="internal_error")


def _resolve_dates(date_from: Optional[str], date_to: Optional[str]) -> tuple[date, date]:
    """Validate the window, defaulting to the trailing 365 days."""
    parsed_to = parse_flexible_date(date_to, "date_to") or today_bangkok()
    parsed_from = parse_flexible_date(date_from, "date_from") or (
        parsed_to - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    )
    if parsed_from > parsed_to:
        raise ValueError(
            f"ช่วงวันที่ไม่ถูกต้อง: date_from ({parsed_from.isoformat()}) "
            f"ต้องไม่เกิน date_to ({parsed_to.isoformat()})"
        )
    return parsed_from, parsed_to


def _company_block(record: Optional[dict], fallback_symbol: Optional[str]) -> dict[str, Any]:
    if not record:
        return {"symbol": fallback_symbol}
    return {
        "symbol": record.get("symbol") or fallback_symbol,
        "company_name_th": record.get("company_name_th"),
        "unique_id_reference": record.get("unique_id_reference"),
        "market": record.get("market"),
        "sector_code": record.get("sector_code"),
        "sector_name_th": record.get("sector_name_th"),
    }


@mcp.tool()
async def lookup_thai_stock(symbol: str) -> dict[str, Any]:
    """ค้นหาข้อมูลพื้นฐานของหุ้นไทยหนึ่งตัวจากชื่อย่อ (ticker).

    Resolve a Thai stock ticker to its SEC company record: full Thai company
    name, the SEC internal reference id, market (SET or mai), and industry
    sector. Use this to confirm a ticker exists before pulling other datasets,
    or to get the official company name.

    Args:
        symbol: ชื่อย่อหลักทรัพย์ เช่น "GULF", "PTT", "SCB", "2S"

    Returns:
        Company identity, or an error object when the ticker is unknown.
        ``unique_id_reference`` may be null for a few companies whose filings the
        SEC does not link; Form 59 is unavailable for those but sustainability
        data still works.
    """
    try:
        record = await symbol_registry.resolve_symbol(symbol)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as data
        return _handle(exc)

    return {
        "ok": True,
        **_company_block(record, symbol),
        "resolved_via": record.get("resolved_via"),
        "form59_available": bool(record.get("unique_id_reference")),
    }


@mcp.tool()
async def search_thai_stocks(
    query: Optional[str] = None,
    market: Optional[str] = None,
    sector: Optional[str] = None,
    limit: int = 30,
) -> dict[str, Any]:
    """ค้นหารายชื่อหุ้นไทยจากคำค้น ตลาด หรือหมวดอุตสาหกรรม.

    Search the full directory of listed Thai companies (866 across SET and mai).
    Useful for answering "which banks are listed?", "find companies with 'energy'
    in the name", or resolving a partially remembered ticker.

    Note: the first call after a restart crawls the SEC A-Z index and takes about
    30 seconds; results are then cached for 24 hours.

    Args:
        query: คำค้นในชื่อย่อหรือชื่อบริษัท (บางส่วนได้) เช่น "ธนาคาร", "energy"
        market: กรองตามตลาด - "SET" หรือ "mai"
        sector: รหัสหมวดอุตสาหกรรม เช่น "BANK", "ENERG", "ICT", "PROP", "FOOD"
        limit: จำนวนผลลัพธ์สูงสุด (1-200, ค่าเริ่มต้น 30)

    Returns:
        Matching companies with ticker, Thai name, SEC id, market and sector.
    """
    capped_limit = max(1, min(int(limit or 30), 200))
    try:
        directory = await symbol_registry.get_directory()
    except Exception as exc:  # noqa: BLE001
        return _handle(exc)

    records = list(directory.values())

    if query:
        needle = query.strip().lower()
        records = [
            r for r in records
            if needle in r["symbol"].lower()
            or needle in (r.get("company_name_th") or "").lower()
        ]
    if market:
        wanted = market.strip().lower()
        records = [r for r in records if (r.get("market") or "").lower() == wanted]
    if sector:
        wanted = sector.strip().lower()
        records = [r for r in records if (r.get("sector_code") or "").lower() == wanted]

    records.sort(key=lambda r: r["symbol"])
    total = len(records)
    trimmed = records[:capped_limit]

    return {
        "ok": True,
        "total_matches": total,
        "returned": len(trimmed),
        "truncated": total > len(trimmed),
        "results": [_company_block(r, None) for r in trimmed],
    }


@mcp.tool()
async def get_form59_executive_trades(
    symbol: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_records: bool = False,
    max_records: int = DEFAULT_MAX_RECORDS,
    market_trades_only: bool = False,
    person: Optional[str] = None,
) -> dict[str, Any]:
    """ดึงรายงานแบบ 59 - การเปลี่ยนแปลงการถือหลักทรัพย์ของผู้บริหาร พร้อมค่าสรุปเชิงวิเคราะห์.

    Fetch Form 59 filings (แบบ 59) for a Thai listed company: every reported
    change in securities holdings by executives, their spouses and children, and
    juristic persons their group controls. Returns pre-computed analytics so you
    do not have to add up rows yourself.

    Reading the result correctly:

    * ``market_activity`` covers open-market buys and sells. ``non_market_activity``
      covers transfers, gifts and inheritances. **Do not add them together** - an
      intra-family transfer is not insider buying.
    * ``by_holder`` is who actually accumulated or sold. ``by_executive`` is the
      executive carrying the reporting duty. These are frequently different
      people, and a holder may be a company rather than an individual.
    * ``duplicate_records_excluded`` counts rows the SEC itself double-publishes
      (married executives each file the same trade). Totals already exclude them.
    * A caveat this tool cannot detect: some related-party restructurings are
      filed as ซื้อ/ขาย with a price, so they inflate ``market_activity`` despite
      never touching the open market. If you see a very large buy and sell of the
      identical volume and price on nearby dates by related holders, treat it as
      a group restructuring, not market activity.

    Args:
        symbol: ชื่อย่อหลักทรัพย์ เช่น "GULF"
        date_from: วันเริ่มต้น - "YYYYMMDD", "YYYY-MM-DD" หรือ "DD/MM/BBBB" (พ.ศ.)
            ค่าเริ่มต้นคือย้อนหลัง 365 วัน
        date_to: วันสิ้นสุด (ค่าเริ่มต้นคือวันนี้)
        include_records: True เพื่อขอรายการรายตัว (ค่าเริ่มต้น False ส่งเฉพาะค่าสรุป
            เพื่อประหยัด context)
        max_records: จำนวนรายการสูงสุดเมื่อ include_records=True (สูงสุด 300)
        market_trades_only: True เพื่อนับเฉพาะการซื้อขายผ่านตลาด ตัดการโอนออก
        person: กรองตามชื่อบุคคล ค้นทั้งผู้บริหารและผู้ถือหลักทรัพย์

    Returns:
        Company identity, the query window, analytics, and optionally records.
    """
    try:
        window_from, window_to = _resolve_dates(date_from, date_to)
        unique_id, company_record = await symbol_registry.resolve_unique_id(symbol, None)
        parsed = await form59_service.fetch_form59(unique_id, window_from, window_to, 1)
    except Exception as exc:  # noqa: BLE001
        return _handle(exc)

    records = parsed["records"]
    if market_trades_only:
        records = [r for r in records if r.get("is_market_trade")]
    if person:
        needle = person.strip().lower()
        records = [
            r for r in records
            if needle in (r.get("executive_name") or "").lower()
            or needle in (r.get("holder_name") or "").lower()
        ]

    analytics = form59_service.build_analytics(records)

    result: dict[str, Any] = {
        "ok": True,
        "company": _company_block(company_record, symbol),
        "query": {
            "date_from": to_iso(window_from),
            "date_to": to_iso(window_to),
            "market_trades_only": market_trades_only,
            "person": person,
        },
        "data_quality": {
            "records_reported_by_source": parsed.get("reported_count"),
            "records_parsed": parsed.get("parsed_count"),
            "parse_complete": parsed.get("parse_complete"),
        },
        "analytics": analytics,
        "source_url": parsed.get("source_url"),
        "disclaimer": config.DISCLAIMER,
    }

    if include_records:
        cap = max(1, min(int(max_records or DEFAULT_MAX_RECORDS), HARD_MAX_RECORDS))
        clean = form59_service.strip_internal_fields(records)
        result["records"] = clean[:cap]
        result["records_returned"] = len(clean[:cap])
        result["records_truncated"] = len(clean) > cap
        if len(clean) > cap:
            # Say so explicitly - a silently short list reads as complete data.
            result["records_truncation_note"] = (
                f"แสดง {cap} จาก {len(clean)} รายการ "
                "ค่าสรุปใน analytics คำนวณจากรายการทั้งหมด "
                "หากต้องการรายการเพิ่ม ให้เพิ่ม max_records หรือแบ่งช่วงวันที่ให้แคบลง"
            )

    return result


@mcp.tool()
async def get_sustainability_ratings(symbol: str) -> dict[str, Any]:
    """ดึงข้อมูล Sustainability Development - CG Score, AGM Level, Thai-CAC, SET ESG Ratings.

    Fetch governance and sustainability indicators for a Thai listed company:

    * **CG Score** (1-5) - Corporate Governance Report rating from Thai IOD
    * **AGM Level** (1-5) - shareholder-meeting quality from the Thai Investors
      Association
    * **Thai-CAC** - anti-corruption certification status. ``certified`` and
      ``declared_intent`` are different things: the latter means the company has
      signed the declaration but is not yet certified.
    * **SET ESG Ratings** - AAA / AA / A / BBB, with ``rank`` where 1 is best

    Important: an absent rating is **not** a poor rating. Check ``is_rated`` -
    when false the company simply has no published rating, which is common for
    smaller companies. ``disclosed_indicators`` counts how many of the four the
    company actually has on file.

    Args:
        symbol: ชื่อย่อหลักทรัพย์ เช่น "GULF", "SCB"

    Returns:
        The four indicators, a condensed scorecard, company contact details, and
        footnotes stating each rating's vintage year.
    """
    try:
        data = await sustainability_service.fetch_sustainability(symbol)
    except Exception as exc:  # noqa: BLE001
        return _handle(exc)

    return {
        "ok": True,
        "company": _company_block(data, symbol),
        "sustainability": data["sustainability"],
        "scorecard": data["sustainability_scorecard"],
        "basic_info": data.get("basic_info"),
        "contacts": data.get("contacts"),
        "footnotes": data.get("footnotes"),
        "last_updated_th": data.get("last_updated_th"),
        "source_url": data.get("source_url"),
        "disclaimer": config.DISCLAIMER,
    }


@mcp.tool()
async def compare_sustainability_ratings(symbols: list[str]) -> dict[str, Any]:
    """เปรียบเทียบคะแนนธรรมาภิบาลและความยั่งยืนของหุ้นไทยหลายตัว.

    Compare CG Score, AGM Level, Thai-CAC status and SET ESG Ratings across
    several companies in one call - useful for ranking peers within a sector.

    Fetches are paced to respect the SEC portal's rate limits, so expect roughly
    one second per symbol. A failure on one symbol does not fail the others; each
    result carries its own ``ok`` flag.

    Args:
        symbols: รายชื่อย่อหลักทรัพย์ เช่น ["GULF", "EA", "SCB"] (สูงสุด 20 ตัว)

    Returns:
        One row per symbol with the four indicators side by side.
    """
    cleaned = [s.strip().upper() for s in (symbols or []) if s and s.strip()]
    if not cleaned:
        return _error("ต้องระบุ symbols อย่างน้อยหนึ่งรายการ", code="invalid_argument")

    limit = min(20, config.BULK_MAX_SYMBOLS)
    if len(cleaned) > limit:
        return _error(
            f"ระบุได้ไม่เกิน {limit} หลักทรัพย์ต่อครั้ง (ได้รับ {len(cleaned)})",
            code="invalid_argument",
            hint="แบ่งเรียกหลายครั้ง",
        )

    rows: list[dict[str, Any]] = []
    for symbol in cleaned:
        # Sequential on purpose: the shared rate limiter would serialise these
        # anyway, and this keeps the portal load predictable.
        try:
            data = await sustainability_service.fetch_sustainability(symbol)
        except Exception as exc:  # noqa: BLE001 - keep the batch alive
            rows.append({"symbol": symbol, "ok": False, "error": str(exc)})
            continue

        block = data["sustainability"]
        card = data["sustainability_scorecard"]
        rows.append({
            "symbol": symbol,
            "ok": True,
            "company_name_th": data.get("company_name_th"),
            "market": data.get("market"),
            "sector_code": data.get("sector_code"),
            "sector_name_th": data.get("sector_name_th"),
            "cg_score": block["cg_score"]["score"],
            "cg_label_th": block["cg_score"]["label_th"],
            "agm_level": block["agm_level"]["score"],
            "agm_label_th": block["agm_level"]["label_th"],
            "thai_cac_status": block["thai_cac"]["status"],
            "thai_cac_certified": block["thai_cac"]["is_certified"],
            "set_esg_rating": block["set_esg_rating"]["rating"],
            "set_esg_rank": block["set_esg_rating"]["rank"],
            "disclosed_indicators": card["disclosed_indicators"],
        })

    succeeded = sum(1 for r in rows if r.get("ok"))
    return {
        "ok": True,
        "requested": len(cleaned),
        "succeeded": succeeded,
        "failed": len(cleaned) - succeeded,
        "comparison": rows,
        "note_th": "is_rated=false หรือค่า null หมายถึงไม่มีข้อมูลเปิดเผย ไม่ใช่คะแนนต่ำ",
        "disclaimer": config.DISCLAIMER,
    }


def build_mcp_asgi_app():
    """Return the Streamable HTTP ASGI app for mounting."""
    return mcp.streamable_http_app()
