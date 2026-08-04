"""FastAPI application: SEC Thailand (ก.ล.ต.) disclosure data service.

Exposes two datasets scraped from ``market.sec.or.th``:

1. **แบบ 59** - executive securities-holding change reports, normalised into
   signed share deltas with per-filer and per-period aggregates.
2. **Sustainability Development** - CG Score, AGM Level, Thai-CAC and
   SET ESG Ratings, normalised into numeric scores with Thai/English labels.

Every response keeps the original Thai wording alongside machine tokens, uses ISO
dates, and carries the source URL so figures can be traced back to the portal.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import auth, config
from .deps import build_meta
from .http_client import BotChallengeError, NotFoundError, UpstreamError, close_client
from .routers import company, form59, sustainability, symbols
from .services import form59 as form59_service
from .services import sustainability as sustainability_service
from .services import symbol_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# The MCP server is only imported when a token is configured, so a deployment
# without MCP_AUTH_TOKEN never even constructs the endpoint.
_mcp_asgi_app = None
_mcp_module = None
if auth.MCP_ENABLED:
    from . import mcp_server as _mcp_module

    _mcp_asgi_app = _mcp_module.build_mcp_asgi_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting SEC iDisc API against %s", config.BASE_URL)
    auth.log_auth_status()

    if _mcp_module is not None:
        # FastMCP's session manager must be running for the mounted Streamable
        # HTTP app to serve requests; entering it here ties its lifetime to the
        # parent app's.
        async with _mcp_module.mcp.session_manager.run():
            logger.info("MCP Streamable HTTP endpoint mounted at /mcp")
            yield
    else:
        yield

    await close_client()
    logger.info("Shut down; HTTP client closed")


DESCRIPTION = """
บริการ API สำหรับดึงและจัดรูปแบบข้อมูลเปิดเผยจากเว็บไซต์
**สำนักงานคณะกรรมการกำกับหลักทรัพย์และตลาดหลักทรัพย์ (ก.ล.ต.)**
รองรับหลักทรัพย์ทุกตัวในตลาดหลักทรัพย์ไทย (SET และ mai)

### ชุดข้อมูลที่รองรับ

| ชุดข้อมูล | Endpoint | รายละเอียด |
|---|---|---|
| แบบ 59 | `/api/v1/form59/{symbol}` | รายงานการเปลี่ยนแปลงการถือหลักทรัพย์และสัญญาซื้อขายล่วงหน้าของผู้บริหาร |
| Sustainability | `/api/v1/sustainability/{symbol}` | CG Score, AGM Level, Thai-CAC, SET ESG Ratings |
| รายชื่อบริษัท | `/api/v1/symbols` | ชื่อย่อหลักทรัพย์ พร้อมรหัสอ้างอิง 10 หลักของ ก.ล.ต. |
| รวมทุกชุด | `/api/v1/company/{symbol}/full` | ส่งข้อมูลทั้งสองชุดในคำขอเดียว |

### จุดที่ออกแบบไว้เพื่อให้ AI นำไปวิเคราะห์ได้ทันที

* **วันที่**: แปลง พ.ศ. เป็น ค.ศ. รูปแบบ ISO 8601 ควบคู่กับค่าต้นฉบับ
* **ตัวเลข**: เป็นชนิด number ไม่มีคอมมา คำนวณต่อได้ทันที และมี `shares_signed`
  (+ ได้มา / − จำหน่าย) ให้รวมยอดสุทธิได้โดยไม่ต้องตีความ
* **การจำแนก**: แยก `is_market_trade` ระหว่างการซื้อขายผ่านตลาดกับการโอน/รับโอน
  เพื่อไม่ให้การโอนภายในครอบครัวถูกนับรวมเป็นการซื้อขายจริง
* **คำอธิบาย**: มี `narrative_th` ระดับรายการ และ `activity_summary_th` ระดับภาพรวม
* **ความครบถ้วน**: `meta.parse_complete` เทียบจำนวนรายการที่ parse ได้กับที่หน้าเว็บระบุ

> การวิเคราะห์ทั้งหมดเป็นการรวบรวมข้อมูลเชิงสถิติเพื่อการศึกษา ไม่ใช่คำแนะนำการลงทุน
"""

app = FastAPI(
    title="SEC Thailand iDisc Data API",
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    contact={"name": "efinanceThai", "url": "https://www.efinancethai.com"},
    openapi_tags=[
        {"name": "แบบ 59 - รายงานการถือหลักทรัพย์ผู้บริหาร",
         "description": "รายงานการเปลี่ยนแปลงการถือหลักทรัพย์ของผู้บริหาร (แบบ 59)"},
        {"name": "Sustainability Development",
         "description": "ข้อมูลธรรมาภิบาลและความยั่งยืนของบริษัทจดทะเบียน"},
        {"name": "รายชื่อบริษัทจดทะเบียน",
         "description": "ค้นหาชื่อย่อหลักทรัพย์และรหัสอ้างอิงของ ก.ล.ต."},
        {"name": "ข้อมูลบริษัทแบบรวม", "description": "รวมทุกชุดข้อมูลในคำขอเดียว"},
        {"name": "ระบบ", "description": "สถานะระบบและการจัดการ cache"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(form59.router)
app.include_router(sustainability.router)
app.include_router(symbols.router)
app.include_router(company.router)

if _mcp_asgi_app is not None:
    # The token guard wraps the MCP app rather than being global middleware, so
    # /health and the REST API keep working for existing callers while /mcp is
    # closed to anyone without the shared secret.
    app.mount("/mcp", auth.BearerTokenMiddleware(_mcp_asgi_app))


@app.middleware("http")
async def optional_rest_auth(request: Request, call_next):
    """Enforce the token on REST too, when REST_AUTH_REQUIRED is enabled.

    Off by default so turning on MCP does not silently break existing REST
    consumers. ``/health`` stays open either way so platform healthchecks and
    uptime probes keep working, and it exposes no company data.
    """
    if auth.REST_AUTH_REQUIRED and request.url.path not in {"/health", "/"}:
        # /mcp is already guarded by its own mount-level middleware.
        if not request.url.path.startswith("/mcp"):
            headers = {k.encode("latin-1"): v.encode("latin-1")
                       for k, v in request.headers.items()}
            if not auth.is_authorized(headers):
                return JSONResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="idisc-rest"'},
                    content={
                        "error": "unauthorized",
                        "detail": "ต้องระบุ Authorization: Bearer <token>",
                    },
                )
    return await call_next(request)


@app.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.exception_handler(BotChallengeError)
async def bot_challenge_handler(request: Request, exc: BotChallengeError) -> JSONResponse:
    # Registered before the UpstreamError handler because it is a subclass.
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "180"},
        content={
            "error": "bot_challenge",
            "detail": str(exc),
            "hint": (
                "เว็บ ก.ล.ต. ตอบด้วยหน้าตรวจสอบ bot กรุณาลองใหม่ในอีก 2-5 นาที "
                "และพิจารณาเพิ่มค่า SEC_MIN_REQUEST_INTERVAL เพื่อลดความถี่การเรียก"
            ),
        },
    )


@app.exception_handler(UpstreamError)
async def upstream_handler(request: Request, exc: UpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "error": "upstream_error",
            "detail": str(exc),
            "hint": "เว็บ ก.ล.ต. อาจไม่พร้อมให้บริการชั่วคราว กรุณาลองใหม่อีกครั้ง",
        },
    )


@app.get("/", tags=["ระบบ"], summary="ข้อมูลบริการและรายการ endpoint")
async def root() -> dict:
    return {
        "service": "SEC Thailand iDisc Data API",
        "version": app.version,
        "source": config.SOURCE_ATTRIBUTION,
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": {
            "form59_by_symbol": "/api/v1/form59/{symbol}",
            "form59_by_unique_id": "/api/v1/form59?uniqueIDReference=0000008616",
            "form59_bulk": "POST /api/v1/form59/bulk",
            "sustainability": "/api/v1/sustainability/{symbol}",
            "sustainability_bulk": "POST /api/v1/sustainability/bulk",
            "symbols": "/api/v1/symbols",
            "symbol_lookup": "/api/v1/symbols/{symbol}",
            "company_full": "/api/v1/company/{symbol}/full",
            "health": "/health",
            "mcp": "/mcp" if auth.MCP_ENABLED else None,
        },
        "mcp": {
            "enabled": auth.MCP_ENABLED,
            "transport": "streamable-http" if auth.MCP_ENABLED else None,
            "endpoint": "/mcp" if auth.MCP_ENABLED else None,
            "auth": "Authorization: Bearer <token>" if auth.MCP_ENABLED else None,
            "tools": sorted(_MCP_TOOL_NAMES) if auth.MCP_ENABLED else [],
        },
        "disclaimer": config.DISCLAIMER,
    }


# Listed statically so the root endpoint can advertise them without awaiting the
# MCP registry on every request.
_MCP_TOOL_NAMES = [
    "lookup_thai_stock",
    "search_thai_stocks",
    "get_form59_executive_trades",
    "get_sustainability_ratings",
    "compare_sustainability_ratings",
]


@app.get("/health", tags=["ระบบ"], summary="ตรวจสอบสถานะบริการและ cache")
async def health() -> dict:
    return {
        "status": "ok",
        "upstream": config.BASE_URL,
        "mcp_enabled": auth.MCP_ENABLED,
        # A fingerprint, never the token itself - lets an operator confirm which
        # secret is loaded without exposing it.
        "mcp_token_fingerprint": auth.token_fingerprint(),
        "rest_auth_required": auth.REST_AUTH_REQUIRED,
        "caches": [
            *symbol_registry.cache_stats(),
            form59_service.cache_stats(),
            sustainability_service.cache_stats(),
        ],
        "meta": build_meta().model_dump(),
    }


@app.post("/admin/cache/clear", tags=["ระบบ"], summary="ล้าง cache ทั้งหมด")
async def clear_cache() -> dict:
    symbol_registry.invalidate_caches()
    form59_service.invalidate_cache()
    sustainability_service.invalidate_cache()
    return {"status": "cleared"}
