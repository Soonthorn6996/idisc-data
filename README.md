# SEC Thailand iDisc Data API

FastAPI service that scrapes and normalises public disclosure data from the Thai
SEC portal (`market.sec.or.th`) into AI-ready JSON. Covers **every listed
security on SET and mai** (866 companies resolved from the live A–Z index).

Two datasets:

| # | Dataset | Endpoint |
|---|---------|----------|
| 1 | **แบบ 59** — รายงานการเปลี่ยนแปลงการถือหลักทรัพย์และสัญญาซื้อขายล่วงหน้าของผู้บริหาร | `/api/v1/form59/{symbol}` |
| 2 | **Sustainability Development** — CG Score, AGM Level, Thai-CAC, SET ESG Ratings | `/api/v1/sustainability/{symbol}` |

## Quick start

```bash
pip install -r requirements.txt
```

```bash
uvicorn app.main:app --reload --port 8000
```

Interactive docs at `http://127.0.0.1:8000/docs`.

```bash
curl "http://127.0.0.1:8000/api/v1/form59/GULF?date_from=20220101&date_to=20260804"
```

```bash
curl "http://127.0.0.1:8000/api/v1/sustainability/GULF"
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/form59/{symbol}` | Form 59 by ticker, with date range and filters |
| GET | `/api/v1/form59?uniqueIDReference=` | Form 59 by SEC id (works for delisted companies) |
| POST | `/api/v1/form59/bulk` | Up to 50 tickers per request |
| GET | `/api/v1/sustainability/{symbol}` | Governance / sustainability indicators |
| POST | `/api/v1/sustainability/bulk` | Compare indicators across tickers |
| GET | `/api/v1/symbols` | Full directory: ticker → SEC id, market, sector |
| GET | `/api/v1/symbols/{symbol}` | Resolve one ticker |
| GET | `/api/v1/company/{symbol}/full` | Both datasets in one call |
| GET | `/health` | Status and cache stats |
| POST | `/admin/cache/clear` | Flush all caches |

### Form 59 query parameters

| Parameter | Default | Notes |
|---|---|---|
| `date_from` / `date_to` | trailing 365 days | Accepts `YYYYMMDD`, `YYYY-MM-DD`, or `DD/MM/BBBB` (พ.ศ.) |
| `date_type` | `1` | Passed through to the SEC portal |
| `method` | — | Filter by `buy,sell,transfer_in,transfer_out,…` (comma-separated) |
| `person` | — | Substring match on **either** the executive or the holder |
| `market_trades_only` | `false` | Exclude transfers, gifts, inheritances |
| `exclude_duplicates` | `false` | Also drop flagged duplicate rows from `records` |
| `include_records` | `true` | `false` returns analytics only |
| `refresh` | `false` | Bypass cache |

## How the ticker → SEC id mapping works

Form 59 is keyed by a 10-digit internal id, not by ticker. Two paths, in order:

1. **Profile page** (1 request) — `/CompanyProfile/Listed/GULF` embeds its own id
   in a link: `/FinancialReport/ALLMIXED-0000008616?symbol=GULF`.
2. **A–Z index crawl** (28 requests, cached 24 h) — each index page lists every
   company with a `/FinancialReport/FS-<id>` link.

Verified: `GULF → 0000008616`, matching the id in the SEC's own reference URL.

Three companies (`POWER`, `TBSP`, `ASTR`) have live profile pages but expose **no**
id anywhere, so Form 59 is genuinely unavailable for them. These return HTTP 404
from `/form59` with an explicit reason, while `/sustainability` and `/symbols`
still work and report `unique_id_reference: null`.

## What makes the JSON AI-ready

**Dates** — Buddhist Era converted to ISO 8601, original preserved:

```json
{ "transaction_date": "2023-02-27",
  "transaction_date_be": "27/02/2566",
  "transaction_date_thai": "27 กุมภาพันธ์ 2566" }
```

**Numbers** — real numeric types, no thousands separators; missing prices are
`null`, never `0` (a transfer has no price, and `0` would corrupt averages).

**Signed volumes** — `shares_signed` is `+` for acquisitions and `−` for
disposals, so a net position change is a plain `sum()` with no interpretation.

**Bilingual vocabulary** — every classified field carries the Thai source wording
plus a stable machine token: `method_code`, `relationship_code`,
`security_type_code`, `asset_class`, `direction`.

**Narratives** — one Thai sentence per filing (`narrative_th`) and one per
response (`activity_summary_th`), ready to drop into a prompt.

**Parse integrity** — the portal states its own row count; the response reports
both and whether they agree:

```json
"meta": { "records_reported_by_source": 29, "records_parsed": 29, "parse_complete": true }
```

### Two domain distinctions the API makes for you

These were both discovered by reading the actual data, and getting either wrong
produces materially wrong numbers.

**1. Market trades vs. transfers.** `is_market_trade` separates open-market
buy/sell from transfers, gifts and inheritances. A 550,000-share intra-family
transfer is not insider buying, so `market_activity` and `non_market_activity`
are aggregated separately rather than summed together.

**2. Duplicate cross-reporting.** The SEC prints this caution under the table:

> กรณีที่บริษัทมีผู้บริหารเป็นคู่สมรสกัน ถ้ามีการซื้อขายหลักทรัพย์ คู่สมรสทั้ง 2 คน
> จะมีหน้าที่ต้องรายงาน ซึ่งจะทำให้เกิดการแสดงรายการซ้ำซ้อนกัน … จึงขอให้ใช้ข้อมูลด้วยความระมัดระวัง

When two executives are married, one trade appears **twice**. Rows are matched on
(holder, date, volume, price, method, security type) and flagged with
`is_potential_duplicate`; analytics exclude them and report
`duplicate_records_excluded`. Detection is conservative — a group only counts as
duplicated when the rows come from *different* executives, so two genuine
same-day trades by one person are never collapsed. No row is ever dropped.

### Executive vs. holder — two distinct roles

The `ชื่อผู้บริหาร` column is the executive **with the reporting duty**;
`ความสัมพันธ์` says **whose holdings actually changed**. They are frequently
different people, and conflating them misattributes trades:

```json
{ "executive_name": "สารัชถ์ รัตนาวะดี",
  "relationship_code": "juristic_person",
  "holder_name": "บริษัท กัลฟ์ โฮลดิ้งส์ (ประเทศไทย) จำกัด",
  "holder_type": "juristic_person",
  "holder_is_executive": false }
```

Analytics therefore ship **both** groupings: `by_holder` (who actually
accumulated or sold) and `by_executive` (matching the portal's own view). For
GULF this correctly separates Gulf Holdings' 18,324,900 shares from
สารัชถ์'s personal 31,100,100.

Relationship matching is order-sensitive, because the juristic-person label
*contains* the wording of the other categories (`ผู้จัดทำรายงาน`, `คู่สมรส`,
`บุตรที่ยังไม่บรรลุนิติภาวะ`). `นิติบุคคล` is tested first — see the comment in
`app/mappings.py`.

## Portal constraints worth knowing

**`DateType` is mandatory.** Omit it and the portal *silently ignores the date
range* and returns every record ever filed (49 rows instead of 1 for PTT in
testing). The service always sends it.

**Pages are UTF-8 without a charset declaration.** Encoding is pinned explicitly;
letting the HTTP client guess yields mojibake for Thai text.

**CG Score and AGM Level exist only as images.** There is no textual value on the
page — the digit in the filename is the only machine-readable carrier
(`cg5.gif` → 5). `raw_image` is preserved so the derivation stays auditable.

**Bot protection.** The portal sits behind an F5/Shape defence that answers with
a JavaScript interstitial once requests arrive too fast. Fanning 28 index pages
out concurrently trips it, and the block then persists for minutes. Mitigations:

- A global rate limiter: ≥ 1.0 s between requests, ≤ 2 concurrent.
- The A–Z crawl is **sequential** with early abort (≈ 28 s, cached 24 h).
- Challenge pages are detected (`bobcmn` marker) and raise `BotChallengeError`
  → **HTTP 503 + `Retry-After`**, never a misleading 404.
- Challenge cookies are cleared on detection, since a poisoned jar keeps failing.

If you see 503s, raise `SEC_MIN_REQUEST_INTERVAL` and wait a few minutes.

**No pagination.** The report table renders every row inline (49 rows verified),
so a single request is complete.

## Configuration

All optional, via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `SEC_MIN_REQUEST_INTERVAL` | `1.0` | Seconds between portal requests |
| `SEC_MAX_CONCURRENT_REQUESTS` | `2` | Concurrent portal requests |
| `SEC_CHALLENGE_BACKOFF_BASE` | `5.0` | Backoff base after a bot challenge |
| `SEC_HTTP_TIMEOUT` | `60` | Per-request timeout |
| `SEC_HTTP_MAX_RETRIES` | `3` | Retry attempts |
| `SEC_CACHE_TTL_SYMBOLS` | `86400` | Symbol directory TTL |
| `SEC_CACHE_TTL_PROFILE` | `21600` | Profile/sustainability TTL |
| `SEC_CACHE_TTL_FORM59` | `1800` | Form 59 TTL |
| `SEC_BULK_MAX_SYMBOLS` | `50` | Max tickers per bulk request |
| `SEC_BULK_CONCURRENCY` | `2` | Bulk fan-out width |

## MCP server (for other projects to consume)

The same datasets are exposed as MCP tools over **Streamable HTTP**, protected by
a fixed shared token.

```
Endpoint : https://idisc-data-production.up.railway.app/mcp/
Transport: streamable-http
Auth     : Authorization: Bearer <MCP_AUTH_TOKEN>
```

Use the **trailing slash**. `/mcp` answers with a 307 to `/mcp/`; that is correct
but only clients that follow redirects while preserving the POST body will work.

### Connecting

Claude Code:

```bash
claude mcp add --transport http efin-sec https://idisc-data-production.up.railway.app/mcp/ --header "Authorization: Bearer YOUR_TOKEN"
```

Any client that takes a JSON config:

```json
{
  "mcpServers": {
    "efin-sec": {
      "type": "http",
      "url": "https://idisc-data-production.up.railway.app/mcp/",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

### Tools

| Tool | Purpose |
|---|---|
| `lookup_thai_stock` | Resolve one ticker → name, SEC id, market, sector |
| `search_thai_stocks` | Search all 866 listed companies by name, market or sector |
| `get_form59_executive_trades` | Form 59 filings with pre-computed analytics |
| `get_sustainability_ratings` | CG Score, AGM Level, Thai-CAC, SET ESG |
| `compare_sustainability_ratings` | Same indicators across up to 20 tickers |

Tool results are **context-bounded**: `get_form59_executive_trades` returns
analytics only unless `include_records=true`, caps rows at `max_records`
(default 50, hard max 300), and states truncation explicitly in
`records_truncation_note` — one company can hold 233 rows (~300 KB of JSON), and
a silently shortened list would read as complete data.

### Security posture

| Property | Behaviour |
|---|---|
| **Fails closed** | Without `MCP_AUTH_TOKEN` the endpoint is never mounted. An unauthenticated MCP server cannot be exposed by accident. |
| **Weak tokens refused** | Anything under 24 characters is rejected at startup and MCP stays off. |
| **Constant-time compare** | `secrets.compare_digest` on **bytes** — it raises `TypeError` on non-ASCII `str`, which would turn a malformed header into a 500 instead of a 401. |
| **Token never logged** | Only an 8-char SHA-256 prefix, also served on `/health` so you can confirm which secret is loaded. |
| **Streaming-safe guard** | Raw ASGI middleware, not `BaseHTTPMiddleware`, which buffers and can deadlock long-lived Streamable HTTP responses. |
| **Lifespan passthrough** | Non-HTTP scopes skip the guard, or the mounted app would never start. |
| **DNS-rebinding protection** | Left **on**. The public host is allow-listed instead; an unrecognised `Host` still gets `421`. |
| **Accepted headers** | `Authorization: Bearer <token>`, or `X-API-Key: <token>` for clients that can only set a custom header. |

Rotate the token by updating the Railway variable and redeploying:

```bash
railway variables --set "MCP_AUTH_TOKEN=$(python -c 'import secrets;print(secrets.token_urlsafe(40))')" --service idisc-data
```

### Extra environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MCP_AUTH_TOKEN` | — | **Required to enable MCP.** Minimum 24 chars. |
| `REST_AUTH_REQUIRED` | `false` | Also require the token on the REST API. `/health` and `/` stay open for platform healthchecks. |
| `MCP_ALLOWED_HOSTS` | auto | Extra `Host` values, comma-separated, for custom domains. Railway's `RAILWAY_PUBLIC_DOMAIN` is picked up automatically. `*` disables the check (not recommended). |
| `MCP_ALLOWED_ORIGINS` | auto | Extra allowed `Origin` values. |

### Verified live

| Check | Result |
|---|---|
| No token / wrong token / malformed bytes | `401` (never `500`) |
| Unrecognised `Host` header | `421` — protection intact |
| Valid `Bearer` / `X-API-Key` | `200` |
| `initialize` | protocol `2025-06-18`, server `efin-sec-idisc` |
| `tools/list` | 5 tools with full schemas |
| `compare_sustainability_ratings` | GULF AA / EA n/a / SCB AAA — 3/3 |
| `get_form59_executive_trades` | SCB 3/3 rows, `parse_complete: true` |

## Deployment (Railway)

Live: **https://idisc-data-production.up.railway.app** — docs at `/docs`.

Config lives in `railway.json`; `railway up` deploys the working directory.

```bash
railway up
```

### Single worker and single replica is a correctness requirement

`railway.json` pins `--workers 1` and `numReplicas: 1`. This is **not** a cost
choice. The rate limiter and the TTL caches are both in-process, so every extra
worker or replica keeps its own limiter and its own cache. Two replicas means
double the request rate against `market.sec.or.th` and half the cache hit rate —
which trips the bot defence the limiter exists to prevent.

To scale beyond one instance you first need shared state: move the limiter and
caches to Redis, then raise the replica count. Raising it alone will cause 503s.

Consequences of in-process caching, worth knowing:

- Caches are cold after every redeploy; the first `/api/v1/symbols` call re-crawls
  the A–Z index (~29 s measured in production).
- `POST /admin/cache/clear` affects only the instance that serves the request.

### Verified in production

| Check | Result |
|---|---|
| `/health`, `/`, `/docs` | 200 |
| `/api/v1/sustainability/GULF` | 200 — CG 5/5, AGM 5/5, Thai-CAC certified, ESG AA |
| `/api/v1/form59/GULF` | 200 — 29/29 rows, `parse_complete: true` |
| `/api/v1/symbols` | 200 — 866 companies (SET 637 / mai 229) in 28.8 s |
| Unknown ticker / bad date | 404 / 422 with Thai messages |

The SEC portal serves Railway's datacenter IP without a bot challenge. That is
not guaranteed to hold — if 503s with `Retry-After` start appearing, raise
`SEC_MIN_REQUEST_INTERVAL` in the Railway service variables rather than retrying
harder.

## Tests

```bash
python -m pytest tests/ -q
```

63 tests, all against saved HTML fixtures — the suite never touches the portal,
so it cannot trip the bot defence. Coverage includes BE↔AD dates, Thai numerals,
title splitting, `รับโอน` vs `โอน` precedence, juristic-person precedence, nested
parentheses, image-encoded scores, duplicate detection, and unrated-company
handling (absent ≠ zero).

Refresh fixtures (rarely, never in CI):

```bash
python tests/refresh_fixtures.py
```

It fetches sequentially and refuses to overwrite a fixture with a challenge page.

## Layout

```
app/
  main.py          FastAPI app, exception handlers, OpenAPI metadata
  config.py        Env-driven settings
  deps.py          Meta building, date defaults, error → HTTP mapping
  http_client.py   Shared client, rate limiter, challenge detection, TTL cache
  mappings.py      Thai → normalised-English vocabulary (order-sensitive)
  models.py        Pydantic response schemas
  services/
    symbol_registry.py   ticker → uniqueIDReference (2 paths)
    form59.py            Form 59 scrape, parse, dedup, analytics
    sustainability.py    Profile scrape and indicator normalisation
  routers/         form59, sustainability, symbols, company
tests/             Fixture-based test suite
```

## Compliance

Responses carry a `disclaimer` field and source attribution. The service reports
statistics and source data only — no buy/sell/hold recommendations. Aggregate
fields are named factually (`net_direction: net_acquisition`) rather than as
sentiment or advice.

> การวิเคราะห์นี้เป็นเพียงการรวบรวมข้อมูลเพื่อการศึกษาเท่านั้น ไม่ใช่คำชี้ชวนในการลงทุน
> ผู้ลงทุนควรศึกษาข้อมูลเพิ่มเติมก่อนตัดสินใจ

Data source: สำนักงานคณะกรรมการกำกับหลักทรัพย์และตลาดหลักทรัพย์ (ก.ล.ต.) — `market.sec.or.th`
