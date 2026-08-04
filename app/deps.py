"""Shared helpers for the routers: metadata, date defaults, error translation."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from .http_client import BotChallengeError, NotFoundError, UpstreamError
from .models import ResponseMeta
from .utils.thai_date import parse_flexible_date

# Bangkok time (UTC+7): the SEC publishes on this clock, so "today" must be
# evaluated there rather than in the server's local zone.
BANGKOK_TZ = timezone(timedelta(hours=7))

DEFAULT_LOOKBACK_DAYS = 365


def today_bangkok() -> date:
    return datetime.now(BANGKOK_TZ).date()


def build_meta(
    *,
    source_url: Optional[str] = None,
    cached: bool = False,
    reported: Optional[int] = None,
    parsed: Optional[int] = None,
    parse_complete: Optional[bool] = None,
) -> ResponseMeta:
    return ResponseMeta(
        source_url=source_url,
        retrieved_at=datetime.now(BANGKOK_TZ).isoformat(timespec="seconds"),
        cached=cached,
        records_reported_by_source=reported,
        records_parsed=parsed,
        parse_complete=parse_complete,
    )


def resolve_date_range(
    date_from: Optional[str], date_to: Optional[str]
) -> tuple[date, date]:
    """Validate and default the query window.

    Defaults to the trailing 365 days. Raises HTTP 422 on malformed input or an
    inverted range so callers get a clear message instead of a silently empty
    result set from the portal.
    """
    try:
        parsed_to = parse_flexible_date(date_to, "date_to") or today_bangkok()
        parsed_from = parse_flexible_date(date_from, "date_from") or (
            parsed_to - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if parsed_from > parsed_to:
        raise HTTPException(
            status_code=422,
            detail=(
                f"ช่วงวันที่ไม่ถูกต้อง: date_from ({parsed_from.isoformat()}) "
                f"ต้องไม่เกิน date_to ({parsed_to.isoformat()})"
            ),
        )
    return parsed_from, parsed_to


def to_http_exception(exc: Exception) -> HTTPException:
    """Translate service-layer errors into the right HTTP status."""
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, BotChallengeError):
        # 503 + Retry-After, not 404: the data exists, we were just turned away.
        return HTTPException(
            status_code=503,
            detail=(
                f"{exc} — เว็บ ก.ล.ต. กำลังปิดกั้นการเรียกข้อมูลอัตโนมัติชั่วคราว "
                "กรุณาลองใหม่ในอีก 2-5 นาที และลดความถี่ในการเรียก"
            ),
            headers={"Retry-After": "180"},
        )
    if isinstance(exc, UpstreamError):
        return HTTPException(
            status_code=502,
            detail=f"ไม่สามารถดึงข้อมูลจากเว็บ ก.ล.ต. ได้: {exc}",
        )
    return HTTPException(status_code=500, detail=f"เกิดข้อผิดพลาดภายในระบบ: {exc}")
