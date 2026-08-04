"""Endpoints for แบบ 59 - executive securities-holding change reports."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Path, Query

from .. import config
from ..deps import build_meta, resolve_date_range, to_http_exception
from ..models import BulkForm59Request, BulkItemResult, BulkResponse, CompanyRef, Form59Response
from ..services import form59 as form59_service
from ..services import symbol_registry
from ..utils.thai_date import to_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/form59", tags=["แบบ 59 - รายงานการถือหลักทรัพย์ผู้บริหาร"])


@router.get(
    "/{symbol}",
    response_model=Form59Response,
    summary="ดึงรายงานแบบ 59 ตามชื่อย่อหลักทรัพย์",
    response_description="รายการแบบ 59 พร้อมข้อมูลสรุปเชิงวิเคราะห์",
)
async def get_form59_by_symbol(
    symbol: str = Path(description="ชื่อย่อหลักทรัพย์ เช่น GULF, PTT, 2S", examples=["GULF"]),
    date_from: Optional[str] = Query(
        default=None,
        description="วันเริ่มต้น รองรับ YYYYMMDD, YYYY-MM-DD หรือ DD/MM/BBBB (ค่าเริ่มต้น: ย้อนหลัง 365 วัน)",
        examples=["20220101"],
    ),
    date_to: Optional[str] = Query(
        default=None, description="วันสิ้นสุด (ค่าเริ่มต้น: วันนี้)", examples=["20260804"]
    ),
    date_type: int = Query(default=1, ge=1, le=3, description="ประเภทวันที่ตามพารามิเตอร์ของ ก.ล.ต."),
    method: Optional[str] = Query(
        default=None,
        description="กรองตามวิธีการ เช่น buy, sell, transfer_in, transfer_out (คั่นด้วย , ได้)",
    ),
    person: Optional[str] = Query(
        default=None,
        description="กรองตามชื่อบุคคล ค้นทั้งชื่อผู้บริหารและชื่อผู้ถือหลักทรัพย์ (ระบุบางส่วนได้)",
    ),
    market_trades_only: bool = Query(
        default=False, description="True = แสดงเฉพาะรายการซื้อขายผ่านตลาด"
    ),
    exclude_duplicates: bool = Query(
        default=False,
        description=(
            "True = ตัดรายการที่ถูกทำเครื่องหมาย is_potential_duplicate ออกจากผลลัพธ์ด้วย "
            "(ค่าสรุปใน analytics ตัดออกให้อยู่แล้วเสมอ)"
        ),
    ),
    include_records: bool = Query(default=True, description="False = ส่งเฉพาะสรุปเชิงวิเคราะห์"),
    refresh: bool = Query(default=False, description="True = ข้าม cache และดึงข้อมูลใหม่"),
) -> Form59Response:
    parsed_from, parsed_to = resolve_date_range(date_from, date_to)

    try:
        unique_id, company_record = await symbol_registry.resolve_unique_id(symbol, None)
    except Exception as exc:  # noqa: BLE001 - mapped to a proper HTTP status
        raise to_http_exception(exc) from exc

    return await _build_response(
        unique_id=unique_id,
        company_record=company_record,
        date_from=parsed_from,
        date_to=parsed_to,
        date_type=date_type,
        method=method,
        person=person,
        market_trades_only=market_trades_only,
        exclude_duplicates=exclude_duplicates,
        include_records=include_records,
        refresh=refresh,
        requested_symbol=symbol,
    )


@router.get(
    "",
    response_model=Form59Response,
    summary="ดึงรายงานแบบ 59 ด้วย uniqueIDReference โดยตรง",
    description=(
        "ใช้เมื่อทราบรหัสอ้างอิง 10 หลักของ ก.ล.ต. อยู่แล้ว "
        "เหมาะกับบริษัทที่ถูกเพิกถอนหรือไม่ปรากฏในดัชนีรายชื่อ"
    ),
)
async def get_form59_by_unique_id(
    uniqueIDReference: str = Query(  # noqa: N803 - mirrors the SEC parameter name
        description="รหัสอ้างอิงบริษัท 10 หลัก", examples=["0000008616"]
    ),
    date_from: Optional[str] = Query(default=None, examples=["20220101"]),
    date_to: Optional[str] = Query(default=None, examples=["20260804"]),
    date_type: int = Query(default=1, ge=1, le=3),
    method: Optional[str] = Query(default=None),
    person: Optional[str] = Query(default=None),
    market_trades_only: bool = Query(default=False),
    exclude_duplicates: bool = Query(default=False),
    include_records: bool = Query(default=True),
    refresh: bool = Query(default=False),
) -> Form59Response:
    parsed_from, parsed_to = resolve_date_range(date_from, date_to)

    try:
        unique_id, company_record = await symbol_registry.resolve_unique_id(None, uniqueIDReference)
    except Exception as exc:  # noqa: BLE001
        raise to_http_exception(exc) from exc

    return await _build_response(
        unique_id=unique_id,
        company_record=company_record,
        date_from=parsed_from,
        date_to=parsed_to,
        date_type=date_type,
        method=method,
        person=person,
        market_trades_only=market_trades_only,
        exclude_duplicates=exclude_duplicates,
        include_records=include_records,
        refresh=refresh,
        requested_symbol=None,
    )


async def _build_response(
    *,
    unique_id: str,
    company_record: Optional[dict],
    date_from,
    date_to,
    date_type: int,
    method: Optional[str],
    person: Optional[str],
    market_trades_only: bool,
    exclude_duplicates: bool,
    include_records: bool,
    refresh: bool,
    requested_symbol: Optional[str],
) -> Form59Response:
    try:
        parsed = await form59_service.fetch_form59(
            unique_id, date_from, date_to, date_type, use_cache=not refresh
        )
    except Exception as exc:  # noqa: BLE001
        raise to_http_exception(exc) from exc

    records = _apply_filters(
        parsed["records"], method, person, market_trades_only, exclude_duplicates
    )
    analytics = form59_service.build_analytics(records)

    # Prefer the company identity from the resolver; fall back to the report rows,
    # which repeat the company name and ticker on every line.
    company = _build_company_ref(company_record, records, unique_id, requested_symbol)

    return Form59Response(
        query={
            "symbol": company.symbol or requested_symbol,
            "unique_id_reference": unique_id,
            "date_from": to_iso(date_from),
            "date_to": to_iso(date_to),
            "date_type": date_type,
            "filters": {
                "method": method,
                "person": person,
                "market_trades_only": market_trades_only,
                "exclude_duplicates": exclude_duplicates,
            },
        },
        company=company,
        records=form59_service.strip_internal_fields(records) if include_records else [],
        analytics=analytics,
        meta=build_meta(
            source_url=parsed.get("source_url"),
            reported=parsed.get("reported_count"),
            parsed=parsed.get("parsed_count"),
            parse_complete=parsed.get("parse_complete"),
        ),
    )


def _build_company_ref(
    company_record: Optional[dict],
    records: list[dict[str, Any]],
    unique_id: str,
    requested_symbol: Optional[str],
) -> CompanyRef:
    if company_record:
        return CompanyRef(
            symbol=company_record.get("symbol"),
            company_name_th=company_record.get("company_name_th"),
            unique_id_reference=company_record.get("unique_id_reference") or unique_id,
            market=company_record.get("market"),
            sector_code=company_record.get("sector_code"),
            sector_name_th=company_record.get("sector_name_th"),
            resolved_via=company_record.get("resolved_via"),
        )

    symbol = requested_symbol
    name = None
    for record in records:
        symbol = symbol or record.get("symbol")
        name = name or record.get("company_name_th")
        if symbol and name:
            break

    return CompanyRef(
        symbol=symbol,
        company_name_th=name,
        unique_id_reference=unique_id,
        resolved_via="form59_rows" if name else None,
    )


def _apply_filters(
    records: list[dict[str, Any]],
    method: Optional[str],
    person: Optional[str],
    market_trades_only: bool,
    exclude_duplicates: bool,
) -> list[dict[str, Any]]:
    result = records

    if exclude_duplicates:
        result = [r for r in result if not r.get("is_potential_duplicate")]

    if market_trades_only:
        result = [r for r in result if r.get("is_market_trade")]

    if method:
        wanted = {m.strip().lower() for m in method.split(",") if m.strip()}
        if wanted:
            result = [r for r in result if (r.get("method_code") or "").lower() in wanted]

    if person:
        needle = person.strip().lower()
        if needle:
            # Matches either role, so searching a name finds filings where the
            # person is the reporting executive and where they are the holder.
            result = [
                r for r in result
                if needle in (r.get("executive_name") or "").lower()
                or needle in (r.get("executive_name_raw") or "").lower()
                or needle in (r.get("holder_name") or "").lower()
            ]

    return result


@router.post(
    "/bulk",
    response_model=BulkResponse,
    summary="ดึงรายงานแบบ 59 หลายหลักทรัพย์ในคำขอเดียว",
    description=(
        f"รองรับสูงสุด {config.BULK_MAX_SYMBOLS} หลักทรัพย์ต่อคำขอ "
        f"และดึงข้อมูลพร้อมกันไม่เกิน {config.BULK_CONCURRENCY} รายการ "
        "เพื่อไม่ให้สร้างภาระกับเว็บ ก.ล.ต."
    ),
)
async def get_form59_bulk(payload: BulkForm59Request) -> BulkResponse:
    symbols = [s.strip().upper() for s in payload.symbols if s and s.strip()]
    if not symbols:
        raise HTTPException(status_code=422, detail="ต้องระบุ symbols อย่างน้อยหนึ่งรายการ")
    if len(symbols) > config.BULK_MAX_SYMBOLS:
        raise HTTPException(
            status_code=422,
            detail=f"ระบุได้ไม่เกิน {config.BULK_MAX_SYMBOLS} หลักทรัพย์ต่อคำขอ (ได้รับ {len(symbols)})",
        )

    parsed_from, parsed_to = resolve_date_range(payload.date_from, payload.date_to)
    semaphore = asyncio.Semaphore(config.BULK_CONCURRENCY)

    async def one(symbol: str) -> BulkItemResult:
        async with semaphore:
            try:
                unique_id, company_record = await symbol_registry.resolve_unique_id(symbol, None)
                parsed = await form59_service.fetch_form59(
                    unique_id, parsed_from, parsed_to, payload.date_type
                )
                analytics = form59_service.build_analytics(parsed["records"])
                data: dict[str, Any] = {
                    "company": _build_company_ref(
                        company_record, parsed["records"], unique_id, symbol
                    ).model_dump(),
                    "analytics": analytics,
                    "records_reported_by_source": parsed.get("reported_count"),
                    "parse_complete": parsed.get("parse_complete"),
                    "source_url": parsed.get("source_url"),
                }
                if payload.include_records:
                    data["records"] = form59_service.strip_internal_fields(parsed["records"])
                return BulkItemResult(symbol=symbol, ok=True, data=data)
            except Exception as exc:  # noqa: BLE001 - one failure must not sink the batch
                logger.warning("Bulk Form 59 failed for %s: %s", symbol, exc)
                return BulkItemResult(symbol=symbol, ok=False, error=str(exc))

    results = await asyncio.gather(*(one(s) for s in symbols))
    succeeded = sum(1 for r in results if r.ok)

    return BulkResponse(
        requested=len(symbols),
        succeeded=succeeded,
        failed=len(symbols) - succeeded,
        results=list(results),
        meta=build_meta(),
    )
