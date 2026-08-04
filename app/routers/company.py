"""Combined endpoint: sustainability + Form 59 for one company in one call."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Path, Query

from ..deps import build_meta, resolve_date_range
from ..models import CompanyFullResponse, CompanyRef
from ..services import form59 as form59_service
from ..services import sustainability as sustainability_service
from ..services import symbol_registry
from ..utils.thai_date import to_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/company", tags=["ข้อมูลบริษัทแบบรวม"])


@router.get(
    "/{symbol}/full",
    response_model=CompanyFullResponse,
    summary="ดึงข้อมูล Sustainability และแบบ 59 พร้อมกันในคำขอเดียว",
    description=(
        "เหมาะกับการส่งบริบทให้ AI วิเคราะห์ในครั้งเดียว "
        "หากส่วนใดดึงไม่สำเร็จ จะรายงานไว้ในฟิลด์ `errors` โดยส่วนที่สำเร็จยังถูกส่งกลับตามปกติ"
    ),
)
async def get_company_full(
    symbol: str = Path(description="ชื่อย่อหลักทรัพย์", examples=["GULF"]),
    date_from: Optional[str] = Query(default=None, examples=["20220101"]),
    date_to: Optional[str] = Query(default=None, examples=["20260804"]),
    date_type: int = Query(default=1, ge=1, le=3),
    include_records: bool = Query(
        default=False, description="True = แนบรายการแบบ 59 รายตัวมาด้วย"
    ),
) -> CompanyFullResponse:
    parsed_from, parsed_to = resolve_date_range(date_from, date_to)
    normalized = symbol.strip().upper()
    errors: list[dict[str, str]] = []

    # Both halves are fetched concurrently; a failure in one is reported in
    # `errors` rather than failing the whole request.
    sustainability_task = asyncio.create_task(
        sustainability_service.fetch_sustainability(normalized)
    )
    form59_task = asyncio.create_task(
        _fetch_form59_for(normalized, parsed_from, parsed_to, date_type)
    )

    sustainability_data, form59_data = await asyncio.gather(
        sustainability_task, form59_task, return_exceptions=True
    )

    company = CompanyRef(symbol=normalized)
    sustainability_block = None
    scorecard = None

    if isinstance(sustainability_data, BaseException):
        errors.append({"section": "sustainability", "error": str(sustainability_data)})
    else:
        sustainability_block = sustainability_data["sustainability"]
        scorecard = sustainability_data["sustainability_scorecard"]
        company = CompanyRef(
            symbol=sustainability_data.get("symbol") or normalized,
            company_name_th=sustainability_data.get("company_name_th"),
            unique_id_reference=sustainability_data.get("unique_id_reference"),
            market=sustainability_data.get("market"),
            sector_code=sustainability_data.get("sector_code"),
            sector_name_th=sustainability_data.get("sector_name_th"),
        )

    form59_payload = None
    if isinstance(form59_data, BaseException):
        errors.append({"section": "form59", "error": str(form59_data)})
    else:
        form59_payload = {
            "query": {
                "date_from": to_iso(parsed_from),
                "date_to": to_iso(parsed_to),
                "date_type": date_type,
            },
            "analytics": form59_data["analytics"],
            "records_reported_by_source": form59_data.get("reported_count"),
            "parse_complete": form59_data.get("parse_complete"),
            "source_url": form59_data.get("source_url"),
        }
        if include_records:
            form59_payload["records"] = form59_data["records"]
        if company.unique_id_reference is None:
            company.unique_id_reference = form59_data.get("unique_id_reference")

    return CompanyFullResponse(
        company=company,
        sustainability=sustainability_block,
        sustainability_scorecard=scorecard,
        form59=form59_payload,
        errors=errors,
        meta=build_meta(),
    )


async def _fetch_form59_for(symbol: str, date_from, date_to, date_type: int) -> dict:
    unique_id, _ = await symbol_registry.resolve_unique_id(symbol, None)
    parsed = await form59_service.fetch_form59(unique_id, date_from, date_to, date_type)
    return {
        **parsed,
        "unique_id_reference": unique_id,
        "analytics": form59_service.build_analytics(parsed["records"]),
    }
