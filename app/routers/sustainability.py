"""Endpoints for ข้อมูล Sustainability Development."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Path, Query

from .. import config
from ..deps import build_meta, to_http_exception
from ..models import (
    BulkItemResult,
    BulkResponse,
    BulkSustainabilityRequest,
    CompanyRef,
    SustainabilityResponse,
)
from ..services import sustainability as sustainability_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/sustainability",
    tags=["Sustainability Development"],
)


@router.get(
    "/{symbol}",
    response_model=SustainabilityResponse,
    summary="ดึงข้อมูล Sustainability Development ตามชื่อย่อหลักทรัพย์",
    response_description="CG Score, AGM Level, Thai-CAC, SET ESG Ratings พร้อมข้อมูลบริษัท",
)
async def get_sustainability(
    symbol: str = Path(description="ชื่อย่อหลักทรัพย์ เช่น GULF, PTT", examples=["GULF"]),
    refresh: bool = Query(default=False, description="True = ข้าม cache และดึงข้อมูลใหม่"),
) -> SustainabilityResponse:
    try:
        data = await sustainability_service.fetch_sustainability(symbol, use_cache=not refresh)
    except Exception as exc:  # noqa: BLE001 - mapped to a proper HTTP status
        raise to_http_exception(exc) from exc

    return _to_response(data)


def _to_response(data: dict) -> SustainabilityResponse:
    return SustainabilityResponse(
        company=CompanyRef(
            symbol=data.get("symbol"),
            company_name_th=data.get("company_name_th"),
            unique_id_reference=data.get("unique_id_reference"),
            market=data.get("market"),
            sector_code=data.get("sector_code"),
            sector_name_th=data.get("sector_name_th"),
        ),
        sustainability=data["sustainability"],
        sustainability_scorecard=data["sustainability_scorecard"],
        business_description_th=data.get("business_description_th"),
        basic_info=data.get("basic_info", {}),
        contacts=data.get("contacts", {}),
        footnotes=data.get("footnotes", []),
        last_updated_th=data.get("last_updated_th"),
        meta=build_meta(source_url=data.get("source_url")),
    )


@router.post(
    "/bulk",
    response_model=BulkResponse,
    summary="ดึงข้อมูล Sustainability หลายหลักทรัพย์ในคำขอเดียว",
    description=(
        f"รองรับสูงสุด {config.BULK_MAX_SYMBOLS} หลักทรัพย์ต่อคำขอ "
        "เหมาะกับการเปรียบเทียบคะแนนธรรมาภิบาลระหว่างบริษัทในหมวดเดียวกัน"
    ),
)
async def get_sustainability_bulk(payload: BulkSustainabilityRequest) -> BulkResponse:
    symbols = [s.strip().upper() for s in payload.symbols if s and s.strip()]
    if not symbols:
        raise HTTPException(status_code=422, detail="ต้องระบุ symbols อย่างน้อยหนึ่งรายการ")
    if len(symbols) > config.BULK_MAX_SYMBOLS:
        raise HTTPException(
            status_code=422,
            detail=f"ระบุได้ไม่เกิน {config.BULK_MAX_SYMBOLS} หลักทรัพย์ต่อคำขอ (ได้รับ {len(symbols)})",
        )

    semaphore = asyncio.Semaphore(config.BULK_CONCURRENCY)

    async def one(symbol: str) -> BulkItemResult:
        async with semaphore:
            try:
                data = await sustainability_service.fetch_sustainability(symbol)
                return BulkItemResult(
                    symbol=symbol,
                    ok=True,
                    data={
                        "company_name_th": data.get("company_name_th"),
                        "market": data.get("market"),
                        "sector_code": data.get("sector_code"),
                        "sector_name_th": data.get("sector_name_th"),
                        "sustainability": data["sustainability"],
                        "sustainability_scorecard": data["sustainability_scorecard"],
                        "source_url": data.get("source_url"),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - keep the batch alive
                logger.warning("Bulk sustainability failed for %s: %s", symbol, exc)
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
