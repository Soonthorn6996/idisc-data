"""Endpoints for the listed-company directory (ticker -> SEC company id)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Path, Query

from ..deps import build_meta, to_http_exception
from ..models import SymbolListResponse, SymbolRecord
from ..services import symbol_registry

router = APIRouter(prefix="/api/v1/symbols", tags=["รายชื่อบริษัทจดทะเบียน"])


@router.get(
    "",
    response_model=SymbolListResponse,
    summary="รายชื่อหลักทรัพย์ทั้งหมดพร้อมรหัสอ้างอิงของ ก.ล.ต.",
    description=(
        "รวบรวมจากดัชนีรายชื่อบริษัทจดทะเบียน A-Z (รวมตัวเลข 2 และ 8) "
        "ผลลัพธ์ถูก cache ไว้เพื่อลดภาระเว็บ ก.ล.ต."
    ),
)
async def list_symbols(
    q: Optional[str] = Query(
        default=None, description="ค้นหาจากชื่อย่อหรือชื่อบริษัท (บางส่วนได้)"
    ),
    market: Optional[str] = Query(default=None, description="กรองตามตลาด: SET หรือ mai"),
    sector: Optional[str] = Query(default=None, description="กรองตามรหัสหมวด เช่น ENERG, BANK"),
    limit: int = Query(default=2000, ge=1, le=5000, description="จำนวนรายการสูงสุดที่ส่งกลับ"),
    refresh: bool = Query(default=False, description="True = ดึงดัชนีใหม่ทั้งหมด"),
) -> SymbolListResponse:
    try:
        directory = await symbol_registry.get_directory(force_refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        raise to_http_exception(exc) from exc

    records = list(directory.values())

    if q:
        needle = q.strip().lower()
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
    trimmed = records[:limit]

    return SymbolListResponse(
        total=total,
        returned=len(trimmed),
        symbols=[SymbolRecord(**r) for r in trimmed],
        meta=build_meta(),
    )


@router.get(
    "/{symbol}",
    response_model=SymbolRecord,
    summary="ค้นหารหัสอ้างอิง (uniqueIDReference) ของหลักทรัพย์หนึ่งตัว",
    description=(
        "ลองอ่านจากหน้า Company Profile ก่อน (1 request) "
        "หากไม่พบจึงย้อนไปค้นจากดัชนีรายชื่อที่ cache ไว้"
    ),
)
async def get_symbol(
    symbol: str = Path(description="ชื่อย่อหลักทรัพย์", examples=["GULF"]),
) -> SymbolRecord:
    try:
        record = await symbol_registry.resolve_symbol(symbol)
    except Exception as exc:  # noqa: BLE001
        raise to_http_exception(exc) from exc

    return SymbolRecord(
        symbol=record["symbol"],
        company_name_th=record.get("company_name_th"),
        unique_id_reference=record.get("unique_id_reference"),
        market=record.get("market"),
        sector_code=record.get("sector_code"),
        sector_name_th=record.get("sector_name_th"),
    )
