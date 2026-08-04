"""Shared async HTTP client, retry policy and a small TTL cache."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

from . import config

logger = logging.getLogger(__name__)


class UpstreamError(RuntimeError):
    """The SEC portal could not be reached or returned an unusable response."""

    def __init__(self, message: str, *, url: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class BotChallengeError(UpstreamError):
    """The portal served a JavaScript bot-protection interstitial.

    ``market.sec.or.th`` sits behind an F5/Shape defence that answers with a
    small script-only page (identifiable by the ``bobcmn`` payload) instead of
    the requested content once a client looks automated. The challenge cannot be
    solved without a JS engine, so the only remedies are to slow down and retry
    later - which is why this is a distinct error type: it must never be mistaken
    for "this symbol does not exist".
    """


class NotFoundError(LookupError):
    """The requested symbol or company does not exist on the SEC portal."""


def detect_bot_challenge(html: str) -> bool:
    """Return ``True`` when ``html`` is a bot-protection interstitial.

    ``bobcmn`` is the F5/Shape marker and the reliable signal. The size-based
    fallback catches a re-skinned interstitial: every genuine portal page carries
    the ASP.NET form and body wrapper, so a small script-only page without them
    is not content.
    """
    if "bobcmn" in html:
        return True
    if len(html) < 12000 and "aspnetForm" not in html and "divBody" not in html:
        return "<script" in html.lower()
    return False


class _RateLimiter:
    """Caps concurrency and enforces a minimum gap between portal requests.

    Fanning 28 index pages out at once is what trips the bot defence, so requests
    are both limited in parallelism and spaced apart. The delay is applied while
    holding the lock so the spacing applies globally, not per-worker.
    """

    def __init__(self, min_interval: float, max_concurrent: int):
        self.min_interval = min_interval
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    @contextlib.asynccontextmanager
    async def slot(self):
        async with self._semaphore:
            async with self._lock:
                gap = self._last_request + self.min_interval - time.monotonic()
                if gap > 0:
                    await asyncio.sleep(gap)
                self._last_request = time.monotonic()
            yield


_limiter = _RateLimiter(config.MIN_REQUEST_INTERVAL, config.MAX_CONCURRENT_REQUESTS)


_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def get_client() -> httpx.AsyncClient:
    """Return the process-wide client, creating it on first use."""
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    base_url=config.BASE_URL,
                    headers=config.DEFAULT_HEADERS,
                    timeout=httpx.Timeout(config.HTTP_TIMEOUT),
                    limits=httpx.Limits(
                        max_connections=config.HTTP_MAX_CONNECTIONS,
                        max_keepalive_connections=config.HTTP_MAX_CONNECTIONS,
                    ),
                    follow_redirects=True,
                )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# Transient conditions worth retrying. The portal sporadically resets
# connections under concurrency, which surfaces as ReadError/RemoteProtocolError.
_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}


async def fetch_html(path: str, params: Optional[dict[str, Any]] = None) -> str:
    """GET a portal page and return its decoded HTML.

    Retries transient failures with exponential backoff plus jitter. The portal
    serves UTF-8 without declaring a charset in a ``<meta>`` tag, so the encoding
    is pinned explicitly - letting httpx guess yields mojibake for Thai text.
    """
    client = await get_client()
    last_error: Optional[BaseException] = None
    url_for_error = path

    for attempt in range(1, config.HTTP_MAX_RETRIES + 1):
        try:
            async with _limiter.slot():
                response = await client.get(path, params=params)
            url_for_error = str(response.request.url)

            if response.status_code in _RETRYABLE_STATUS:
                last_error = UpstreamError(
                    f"SEC portal returned HTTP {response.status_code}",
                    url=url_for_error,
                    status_code=response.status_code,
                )
                if attempt < config.HTTP_MAX_RETRIES:
                    await _sleep_backoff(attempt)
                    continue
                raise last_error

            if response.status_code == 404:
                raise NotFoundError(f"ไม่พบหน้าที่ร้องขอบนเว็บ ก.ล.ต. ({url_for_error})")

            response.raise_for_status()

            # Pin UTF-8: the pages are UTF-8 but omit a charset declaration.
            html = response.content.decode("utf-8", errors="replace")

            if detect_bot_challenge(html):
                # Drop the challenge cookies: once the jar holds them every
                # subsequent request keeps being answered with the interstitial.
                client.cookies.clear()
                last_error = BotChallengeError(
                    "เว็บ ก.ล.ต. ตอบกลับด้วยหน้าตรวจสอบ bot (JavaScript challenge) "
                    "แทนข้อมูลที่ร้องขอ",
                    url=url_for_error,
                )
                logger.warning("Bot challenge served for %s (attempt %s/%s)",
                               path, attempt, config.HTTP_MAX_RETRIES)
                if attempt < config.HTTP_MAX_RETRIES:
                    # Back off harder than for a transient network error - the
                    # defence relaxes on time, not on repetition.
                    await _sleep_backoff(attempt, base=config.CHALLENGE_BACKOFF_BASE)
                    continue
                raise last_error

            return html

        except (NotFoundError, BotChallengeError):
            raise
        except _RETRYABLE_EXCEPTIONS as exc:
            last_error = exc
            logger.warning("Transient fetch failure (attempt %s/%s) for %s: %s",
                           attempt, config.HTTP_MAX_RETRIES, path, exc)
            if attempt < config.HTTP_MAX_RETRIES:
                await _sleep_backoff(attempt)
                continue
        except httpx.HTTPStatusError as exc:
            raise UpstreamError(
                f"SEC portal returned HTTP {exc.response.status_code}",
                url=url_for_error,
                status_code=exc.response.status_code,
            ) from exc

    raise UpstreamError(
        f"ไม่สามารถเชื่อมต่อเว็บ ก.ล.ต. ได้หลังลองใหม่ {config.HTTP_MAX_RETRIES} ครั้ง: {last_error}",
        url=url_for_error,
    )


async def _sleep_backoff(attempt: int, base: Optional[float] = None) -> None:
    delay = (base if base is not None else config.HTTP_BACKOFF_BASE) * (2 ** (attempt - 1))
    await asyncio.sleep(delay + random.uniform(0, 0.3))


class TTLCache:
    """Minimal async TTL cache with single-flight semantics.

    A per-key lock keeps a burst of concurrent requests for the same symbol from
    each firing its own scrape at the SEC portal.
    """

    def __init__(self, ttl_seconds: int, name: str = "cache"):
        self.ttl = ttl_seconds
        self.name = name
        self._store: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    def _fresh(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self.ttl:
            self._store.pop(key, None)
            return None
        return value

    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        cached = self._fresh(key)
        if cached is not None:
            self.hits += 1
            return cached

        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            # Re-check: another coroutine may have populated it while we waited.
            cached = self._fresh(key)
            if cached is not None:
                self.hits += 1
                return cached

            self.misses += 1
            value = await factory()
            self._store[key] = (time.monotonic(), value)
            return value

    def invalidate(self, key: Optional[str] = None) -> None:
        if key is None:
            self._store.clear()
        else:
            self._store.pop(key, None)

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entries": len(self._store),
            "ttl_seconds": self.ttl,
            "hits": self.hits,
            "misses": self.misses,
        }
