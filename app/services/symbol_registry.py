"""Resolve a Thai ticker (e.g. ``GULF``) to the SEC ``uniqueIDReference``.

The Form 59 report is keyed by a 10-digit zero-padded internal company id, not
by ticker. Two independent resolution paths are implemented:

1. **Direct profile lookup** (1 request) - the company profile page embeds the id
   in its own links, e.g. ``/FinancialReport/ALLMIXED-0000008616?symbol=GULF``.
2. **A-Z index crawl** (~28 requests, cached) - each index page lists every
   listed company with a ``/FinancialReport/FS-<id>`` link. This is the bulk
   source, used for the directory endpoint and as a fallback when a profile page
   does not expose an id.

Both were verified against the live portal: GULF resolves to ``0000008616``,
matching the id in the reference URL.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from .. import config
from ..http_client import (
    BotChallengeError,
    NotFoundError,
    TTLCache,
    UpstreamError,
    fetch_html,
)
from ..mappings import map_market_sector
from ..utils.text import clean_optional, clean_text

logger = logging.getLogger(__name__)

# Matches the id in any of the report links the portal emits:
#   ALLMIXED-0000008616 / FS-0000008616 / R561-0000008616 / ALL-0000008616
_ID_IN_LINK_RE = re.compile(r"(?:ALLMIXED|FS|R561|R562|ALL)-(\d{10})")
_ID_ONLY_RE = re.compile(r"^\d{10}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&.\-]{0,14}$")

_directory_cache = TTLCache(config.CACHE_TTL_SYMBOLS, "symbol_directory")
_single_symbol_cache = TTLCache(config.CACHE_TTL_SYMBOLS, "symbol_lookup")


def normalize_symbol(symbol: str) -> str:
    """Upper-case and strip a ticker, per the house formatting standard."""
    text = clean_text(symbol).upper()
    # Tickers of non-common share classes carry a suffix, e.g. "PTT-F", "TRUE-R".
    return text


def is_valid_symbol(symbol: str) -> bool:
    return bool(_SYMBOL_RE.match(normalize_symbol(symbol)))


def looks_like_unique_id(value: str) -> bool:
    return bool(_ID_ONLY_RE.match(clean_text(value)))


async def _discover_index_letters() -> list[str]:
    """Read the letter pagination off an index page.

    The live page exposes ``2, 8, A-Z``; if the markup changes we fall back to
    the configured list rather than silently crawling nothing.
    """
    try:
        html = await fetch_html(config.PATH_LISTED_INDEX.format(letter="A"))
    except BotChallengeError:
        raise  # no point crawling 28 more pages into an active block
    except Exception as exc:  # noqa: BLE001 - discovery must never be fatal
        logger.warning("Letter discovery failed, using fallback list: %s", exc)
        return list(config.FALLBACK_INDEX_LETTERS)

    letters = re.findall(r"/public/idisc/th/company/listed/([A-Z0-9])\"", html)
    unique = sorted({letter.upper() for letter in letters})
    if not unique:
        logger.warning("Letter discovery found nothing, using fallback list")
        return list(config.FALLBACK_INDEX_LETTERS)
    return unique


def _parse_index_page(html: str) -> list[dict]:
    """Extract one record per company from an A-Z index page."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id=re.compile(r"^gcompany_", re.IGNORECASE))
    if table is None:
        # Fall back to any table that contains company-profile links.
        for candidate in soup.find_all("table"):
            if candidate.find("a", href=re.compile(r"companyprofile/listed/", re.I)):
                table = candidate
                break
    if table is None:
        return []

    records: list[dict] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue  # header or spacer row

        symbol = normalize_symbol(cells[0].get_text())
        if not symbol or not is_valid_symbol(symbol):
            continue

        name_link = cells[1].find("a")
        company_name = clean_optional(
            name_link.get_text() if name_link else cells[1].get_text()
        )

        # The id appears in the Filing / financial-report links of this row.
        unique_id = None
        for anchor in row.find_all("a", href=True):
            match = _ID_IN_LINK_RE.search(anchor["href"])
            if match:
                unique_id = match.group(1)
                break

        ranking_code = None
        for anchor in row.find_all("a", href=True):
            match = re.search(r"Ranking/Listed/Sector/([\w\-]+)", anchor["href"])
            if match:
                ranking_code = match.group(1)
                break

        if unique_id is None:
            # Without an id the row is not actionable for Form 59.
            logger.debug("Index row for %s has no uniqueIDReference", symbol)

        records.append({
            "symbol": symbol,
            "company_name_th": company_name,
            "unique_id_reference": unique_id,
            **map_market_sector(ranking_code),
        })
    return records


async def _load_directory() -> dict[str, dict]:
    """Crawl every index letter and build the ticker -> record map.

    Deliberately **sequential**. Fanning the ~28 letters out concurrently is
    exactly what trips the portal's bot defence, and a tripped defence blocks
    every request for minutes. The rate limiter paces each call, so a full crawl
    takes roughly half a minute - acceptable for a result cached for 24 hours.

    A challenge aborts the crawl immediately rather than grinding through the
    remaining letters, and propagates so the caller can report it honestly
    instead of returning a half-empty directory that looks like real data.
    """
    letters = await _discover_index_letters()

    directory: dict[str, dict] = {}
    failed: list[str] = []

    for letter in letters:
        try:
            html = await fetch_html(config.PATH_LISTED_INDEX.format(letter=letter))
        except BotChallengeError:
            logger.error(
                "Bot challenge during directory crawl at letter %s; aborting with "
                "%d companies collected so far", letter, len(directory),
            )
            if not directory:
                raise
            # Partial data is better than none, but the caller must know.
            failed.append(letter)
            break
        except Exception as exc:  # noqa: BLE001 - one bad letter must not sink
            # the whole directory; log it and continue with the rest.
            logger.warning("Failed to load index letter %s: %s", letter, exc)
            failed.append(letter)
            continue

        for record in _parse_index_page(html):
            # First occurrence wins; letters do not overlap in practice.
            directory.setdefault(record["symbol"], record)

    if not directory:
        raise UpstreamError(
            "ไม่สามารถอ่านรายชื่อบริษัทจดทะเบียนจากเว็บ ก.ล.ต. ได้ "
            "(อาจถูกระบบป้องกัน bot ปิดกั้นชั่วคราว)",
            url=config.PATH_LISTED_INDEX.format(letter="A"),
        )

    logger.info(
        "Loaded SEC symbol directory: %d companies from %d/%d letters%s",
        len(directory), len(letters) - len(failed), len(letters),
        f" (failed: {','.join(failed)})" if failed else "",
    )
    return directory


async def get_directory(force_refresh: bool = False) -> dict[str, dict]:
    if force_refresh:
        _directory_cache.invalidate("all")
    return await _directory_cache.get_or_set("all", _load_directory)


async def _resolve_via_profile(symbol: str) -> Optional[dict]:
    """Resolve one ticker using only its company profile page.

    A bot challenge is re-raised rather than swallowed: treating it as "not found"
    would send the caller into the 28-request directory crawl, hammering a portal
    that is already pushing back.
    """
    try:
        html = await fetch_html(config.PATH_COMPANY_PROFILE.format(symbol=symbol))
    except BotChallengeError:
        raise
    except NotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Profile lookup failed for %s: %s", symbol, exc)
        return None

    soup = BeautifulSoup(html, "lxml")
    title_block = soup.select_one(".compTitle h3") or soup.select_one(".compTitle")
    name = clean_optional(title_block.get_text()) if title_block else None
    if name is None:
        # No company heading at all - this ticker has no profile page.
        return None

    # A handful of companies (e.g. POWER, TBSP) have a live profile page but no
    # filing links, so no id can be recovered. The record is still returned with
    # a null id: the company exists, it just is not addressable for Form 59.
    match = _ID_IN_LINK_RE.search(html)
    ranking = re.search(r"Ranking/Listed/Sector/([\w\-]+)", html)

    return {
        "symbol": symbol,
        "company_name_th": name,
        "unique_id_reference": match.group(1) if match else None,
        **map_market_sector(ranking.group(1) if ranking else None),
    }


async def resolve_symbol(symbol: str) -> dict:
    """Resolve a ticker to its SEC record.

    Tries the cheap single-page lookup first and falls back to the cached A-Z
    directory. Raises :class:`NotFoundError` when the ticker is unknown.
    """
    normalized = normalize_symbol(symbol)
    if not is_valid_symbol(normalized):
        raise ValueError(
            f"รูปแบบชื่อย่อหลักทรัพย์ไม่ถูกต้อง: {symbol!r} "
            "(ต้องเป็นตัวอักษร/ตัวเลขภาษาอังกฤษ เช่น GULF, PTT, 2S)"
        )

    async def factory() -> dict:
        record = await _resolve_via_profile(normalized)
        if record and record.get("unique_id_reference"):
            record["resolved_via"] = "company_profile"
            return record

        directory = await get_directory()
        from_directory = directory.get(normalized)
        if from_directory and from_directory.get("unique_id_reference"):
            return {**from_directory, "resolved_via": "listed_index"}

        # Neither source produced an id. If either at least knows the ticker, say
        # so with a null id instead of claiming the company does not exist.
        known = record or from_directory
        if known:
            return {**known, "unique_id_reference": None,
                    "resolved_via": "company_profile" if record else "listed_index"}

        raise NotFoundError(
            f"ไม่พบชื่อย่อหลักทรัพย์ {normalized} ในฐานข้อมูลบริษัทจดทะเบียนของ ก.ล.ต."
        )

    return await _single_symbol_cache.get_or_set(normalized, factory)


async def resolve_unique_id(symbol: Optional[str], unique_id: Optional[str]) -> tuple[str, Optional[dict]]:
    """Resolve the ``uniqueIDReference`` from either input.

    Passing ``unique_id`` directly skips resolution entirely, which lets callers
    query companies that are delisted or otherwise missing from the index.
    """
    if unique_id:
        cleaned = clean_text(unique_id)
        if not looks_like_unique_id(cleaned):
            # Accept a shorter id by zero-padding to the portal's 10 digits.
            if cleaned.isdigit() and len(cleaned) < 10:
                cleaned = cleaned.zfill(10)
            else:
                raise ValueError(
                    f"uniqueIDReference ต้องเป็นตัวเลข 10 หลัก เช่น 0000008616 (ได้รับ: {unique_id!r})"
                )
        return cleaned, None

    if not symbol:
        raise ValueError("ต้องระบุ symbol หรือ uniqueIDReference อย่างน้อยหนึ่งค่า")

    record = await resolve_symbol(symbol)
    resolved = record.get("unique_id_reference")
    if not resolved:
        # The company exists but the portal exposes no filing id for it, so the
        # Form 59 report simply cannot be addressed. Say that precisely.
        raise NotFoundError(
            f"พบบริษัท {record.get('company_name_th') or symbol} "
            f"({record['symbol']}) แต่เว็บ ก.ล.ต. ไม่ได้เปิดเผยรหัสอ้างอิง "
            "(uniqueIDReference) สำหรับหลักทรัพย์นี้ จึงไม่สามารถเรียกรายงานแบบ 59 ได้ "
            "โดยข้อมูล Sustainability ยังเรียกดูได้ตามปกติ"
        )
    return resolved, record


def cache_stats() -> list[dict]:
    return [_directory_cache.stats(), _single_symbol_cache.stats()]


def invalidate_caches() -> None:
    _directory_cache.invalidate()
    _single_symbol_cache.invalidate()
