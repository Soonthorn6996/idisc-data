"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os

BASE_URL = os.environ.get("SEC_BASE_URL", "https://market.sec.or.th").rstrip("/")

# Path templates on the SEC "idisc" portal.
PATH_FORM59 = "/public/idisc/th/Viewmore/r59-2"
PATH_COMPANY_PROFILE = "/public/idisc/th/CompanyProfile/Listed/{symbol}"
PATH_LISTED_INDEX = "/public/idisc/th/company/listed/{letter}"

# The portal rejects requests that do not look like a browser (the TLS/header
# combination used by urllib gets the connection closed), so a full header set
# is mandatory rather than cosmetic.
USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

HTTP_TIMEOUT = float(os.environ.get("SEC_HTTP_TIMEOUT", "60"))
HTTP_MAX_RETRIES = int(os.environ.get("SEC_HTTP_MAX_RETRIES", "3"))
HTTP_BACKOFF_BASE = float(os.environ.get("SEC_HTTP_BACKOFF_BASE", "0.8"))
HTTP_MAX_CONNECTIONS = int(os.environ.get("SEC_HTTP_MAX_CONNECTIONS", "4"))

# The portal is fronted by an F5/Shape bot defence that starts answering with a
# JavaScript interstitial instead of content when a client requests too fast.
# Measured the hard way: fanning ~28 index pages out concurrently trips it and
# the block then persists for minutes. These two knobs are the primary guard -
# raise them at your own risk.
MIN_REQUEST_INTERVAL = float(os.environ.get("SEC_MIN_REQUEST_INTERVAL", "1.0"))
MAX_CONCURRENT_REQUESTS = int(os.environ.get("SEC_MAX_CONCURRENT_REQUESTS", "2"))
# Backing off from a challenge needs to be far slower than from a network blip:
# the defence relaxes with elapsed time, not with retry count.
CHALLENGE_BACKOFF_BASE = float(os.environ.get("SEC_CHALLENGE_BACKOFF_BASE", "5.0"))

# Cache time-to-live, in seconds.
CACHE_TTL_SYMBOLS = int(os.environ.get("SEC_CACHE_TTL_SYMBOLS", str(24 * 3600)))
CACHE_TTL_PROFILE = int(os.environ.get("SEC_CACHE_TTL_PROFILE", str(6 * 3600)))
CACHE_TTL_FORM59 = int(os.environ.get("SEC_CACHE_TTL_FORM59", str(30 * 60)))

# Maximum symbols accepted by a single bulk request, and how many of those are
# fetched from the SEC portal at the same time. Effective parallelism is also
# capped by MAX_CONCURRENT_REQUESTS above, which the rate limiter enforces.
BULK_MAX_SYMBOLS = int(os.environ.get("SEC_BULK_MAX_SYMBOLS", "50"))
BULK_CONCURRENCY = int(os.environ.get("SEC_BULK_CONCURRENCY", "2"))

# Letters used by the A-Z listed-company index. Discovered from the live page
# (it exposes 2, 8 and A-Z); kept here as the fallback if discovery fails.
FALLBACK_INDEX_LETTERS = ["2", "8"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]

DISCLAIMER = (
    "การวิเคราะห์นี้เป็นเพียงการรวบรวมข้อมูลเพื่อการศึกษาเท่านั้น "
    "ไม่ใช่คำชี้ชวนในการลงทุน ผู้ลงทุนควรศึกษาข้อมูลเพิ่มเติมก่อนตัดสินใจ"
)
SOURCE_ATTRIBUTION = "สำนักงานคณะกรรมการกำกับหลักทรัพย์และตลาดหลักทรัพย์ (ก.ล.ต.) - market.sec.or.th"
