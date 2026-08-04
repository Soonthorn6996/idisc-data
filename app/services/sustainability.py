"""Scrape and normalise the ``ข้อมูล Sustainability Development`` block.

Source page (verified live):
``/public/idisc/th/CompanyProfile/Listed/{SYMBOL}``

Table ids on that page:

===========  =================================================================
``gPP11T01``  CG Score - rendered **only as an image** (``cg5.gif`` -> 5 of 5)
``gPP12T01``  AGM Level - likewise an image (``agm5.gif`` -> 5 of 5)
``gPP17T01``  Thai-CAC anti-corruption certification (text, ``n/a`` when none)
``gPP16T01``  SET ESG Ratings (text: ``AAA``/``AA``/``A``/``BBB`` or ``n/a``)
``gPP14T01``  ข้อมูลเบื้องต้น - address, phone, fax, website
``gPP15T01``  ข้อมูลผู้ติดต่อ - IR and company-secretary contacts
===========  =================================================================

The CG/AGM scores having no textual representation is the key constraint here:
the digit embedded in the image filename is the only machine-readable carrier,
so :func:`~app.mappings.parse_score_image` is load-bearing rather than a helper.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from bs4 import BeautifulSoup

from .. import config
from ..http_client import NotFoundError, TTLCache, fetch_html
from ..mappings import (
    AGM_LEVEL_LABELS,
    CG_SCORE_LABELS,
    map_market_sector,
    map_set_esg_rating,
    map_thai_cac,
    parse_score_image,
)
from ..utils.text import clean_optional, clean_text, is_empty_token

logger = logging.getLogger(__name__)

_cache = TTLCache(config.CACHE_TTL_PROFILE, "sustainability")

_LAST_UPDATE_RE = re.compile(r"ปรับปรุงล่าสุด\s*(.+)$")


def _first_data_cell(soup: BeautifulSoup, table_id: str):
    """Return the first ``<td>`` of a single-value table, or ``None``."""
    table = soup.find("table", id=table_id)
    if table is None:
        return None
    return table.find("td")


def _score_block(
    soup: BeautifulSoup,
    table_id: str,
    labels: dict[int, dict],
    scale_name: str,
    source_note: Optional[str],
) -> dict[str, Any]:
    """Normalise an image-encoded 1-5 score table (CG Score / AGM Level)."""
    cell = _first_data_cell(soup, table_id)
    if cell is None:
        return {
            "score": None, "max_score": 5, "label_th": None, "label_en": None,
            "is_rated": False, "scale": scale_name, "source": source_note,
            "raw_image": None,
            "note_th": "ไม่พบข้อมูลในหน้าเว็บ ก.ล.ต.",
        }

    image = cell.find("img")
    image_src = image.get("src") if image is not None else None
    score = parse_score_image(image_src)

    if score is None:
        # Some companies show text instead of (or alongside) the logo image.
        text = clean_optional(cell.get_text())
        if text and not is_empty_token(text):
            digit = re.search(r"([1-5])", text)
            if digit:
                score = int(digit.group(1))

    if score is None:
        return {
            "score": None, "max_score": 5, "label_th": None, "label_en": None,
            "is_rated": False, "scale": scale_name, "source": source_note,
            "raw_image": image_src,
            "note_th": "บริษัทนี้ไม่มีข้อมูลคะแนนเปิดเผยไว้",
        }

    label = labels.get(score, {})
    return {
        "score": score,
        "max_score": 5,
        "label_th": label.get("label_th"),
        "label_en": label.get("label_en"),
        "is_rated": True,
        "scale": scale_name,
        "source": source_note,
        "raw_image": image_src,
        "note_th": None,
    }


def _key_value_table(soup: BeautifulSoup, table_id: str) -> list[dict[str, Optional[str]]]:
    """Parse a two-column ``รายการ / รายละเอียด`` table into pairs."""
    table = soup.find("table", id=table_id)
    if table is None:
        return []

    pairs: list[dict[str, Optional[str]]] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        key = clean_optional(cells[0].get_text())
        value = clean_optional(cells[1].get_text())
        if key is None:
            continue
        pairs.append({"label_th": key, "value": value})
    return pairs


# ``ข้อมูลเบื้องต้น`` labels -> stable field names.
_BASIC_FIELD_MAP = {
    "ที่อยู่": "address",
    "เบอร์โทรศัพท์": "phone",
    "เบอร์โทรสาร": "fax",
    "URL": "website",
}

# ``ข้อมูลผู้ติดต่อ`` labels -> stable field names.
_CONTACT_FIELD_MAP = {
    "ฝ่ายผู้ลงทุนสัมพันธ์": "investor_relations",
    "เลขานุการบริษัท": "company_secretary",
}


def parse_profile_html(html: str, symbol: str) -> dict[str, Any]:
    """Parse a company profile page into the sustainability payload."""
    soup = BeautifulSoup(html, "lxml")

    title_block = soup.select_one(".compTitle h3") or soup.select_one(".compTitle")
    company_name = clean_optional(title_block.get_text()) if title_block else None
    if company_name is None:
        raise NotFoundError(
            f"ไม่พบข้อมูลบริษัทสำหรับชื่อย่อ {symbol} บนเว็บ ก.ล.ต."
        )

    # Footnotes carry the rating vintage, e.g. "CG Score ประจำปี 2568".
    footnotes: list[str] = []
    for remark in soup.select("span.ux-remark"):
        text = clean_optional(remark.get_text())
        if text:
            footnotes.append(text)

    cg_source = next((f for f in footnotes if "CG Score" in f), None)

    cg_score = _score_block(
        soup, "gPP11T01", CG_SCORE_LABELS,
        "CGR 1-5 (จำนวนตราสัญลักษณ์)",
        cg_source or "สมาคมส่งเสริมสถาบันกรรมการบริษัทไทย (Thai IOD)",
    )
    agm_level = _score_block(
        soup, "gPP12T01", AGM_LEVEL_LABELS,
        "AGM Checklist 1-5",
        "สมาคมส่งเสริมผู้ลงทุนไทย (Thai Investors Association)",
    )

    cac_cell = _first_data_cell(soup, "gPP17T01")
    thai_cac = map_thai_cac(cac_cell.get_text() if cac_cell is not None else None)
    thai_cac["source"] = "แนวร่วมต่อต้านคอร์รัปชันของภาคเอกชนไทย (Thai CAC)"

    esg_cell = _first_data_cell(soup, "gPP16T01")
    esg_raw = clean_text(esg_cell.get_text()) if esg_cell is not None else None
    esg = map_set_esg_rating(esg_raw)
    if esg is None:
        esg = {
            "rating": None, "rank": None, "total_tiers": 4,
            "label_en": None, "is_rated": False,
        }
        esg["note_th"] = "บริษัทนี้ไม่ได้รับการจัดอันดับ SET ESG Ratings"
    esg["raw"] = esg_raw
    esg["source"] = "ตลาดหลักทรัพย์แห่งประเทศไทย (SET ESG Ratings)"

    ranking_link = soup.find("a", href=re.compile(r"Ranking/Listed/Sector/", re.I))
    ranking_code = None
    if ranking_link is not None:
        match = re.search(r"Ranking/Listed/Sector/([\w\-]+)", ranking_link["href"])
        if match:
            ranking_code = match.group(1)

    # ``ลักษณะธุรกิจ`` is present as a heading but the SEC leaves its table empty
    # on every page sampled; parsed defensively so it populates if that changes.
    business_description = None
    business_heading = soup.find(id=re.compile(r"lblBusinessType$"))
    if business_heading is not None:
        container = business_heading.find_parent("div", class_="col-sb-12")
        sibling = container.find_next_sibling("div") if container else None
        if sibling is not None:
            business_description = clean_optional(sibling.get_text())

    basic_pairs = _key_value_table(soup, "gPP14T01")
    basic_info: dict[str, Optional[str]] = {v: None for v in _BASIC_FIELD_MAP.values()}
    for pair in basic_pairs:
        field = _BASIC_FIELD_MAP.get(pair["label_th"])
        if field:
            basic_info[field] = pair["value"]

    contact_pairs = _key_value_table(soup, "gPP15T01")
    contacts: dict[str, Optional[str]] = {v: None for v in _CONTACT_FIELD_MAP.values()}
    for pair in contact_pairs:
        field = _CONTACT_FIELD_MAP.get(pair["label_th"])
        if field:
            contacts[field] = pair["value"]

    last_update = None
    update_node = soup.find(id=re.compile(r"lbLastupdate$"))
    if update_node is not None:
        match = _LAST_UPDATE_RE.search(clean_text(update_node.get_text()))
        if match:
            last_update = clean_optional(match.group(1))

    unique_id_match = re.search(r"(?:ALLMIXED|FS|R561|R562|ALL)-(\d{10})", html)

    governance = {
        "cg_score": cg_score,
        "agm_level": agm_level,
        "thai_cac": thai_cac,
        "set_esg_rating": esg,
    }

    return {
        "symbol": symbol,
        "company_name_th": company_name,
        "unique_id_reference": unique_id_match.group(1) if unique_id_match else None,
        **map_market_sector(ranking_code),
        "sustainability": governance,
        "sustainability_scorecard": _build_scorecard(governance),
        "business_description_th": business_description,
        "basic_info": basic_info,
        "basic_info_pairs": basic_pairs,
        "contacts": contacts,
        "footnotes": footnotes,
        "last_updated_th": last_update,
    }


def _build_scorecard(governance: dict[str, Any]) -> dict[str, Any]:
    """Condense the four indicators into an AI-friendly digest.

    ``disclosed_indicators`` counts how many of the four the company actually has
    on file, which matters because an absent rating is not a poor rating.
    """
    cg = governance["cg_score"]
    agm = governance["agm_level"]
    cac = governance["thai_cac"]
    esg = governance["set_esg_rating"]

    disclosed = sum([
        bool(cg.get("is_rated")),
        bool(agm.get("is_rated")),
        bool(cac.get("is_certified")),
        bool(esg.get("is_rated")),
    ])

    highlights: list[str] = []
    if cg.get("is_rated"):
        highlights.append(f"CG Score {cg['score']}/5 ({cg.get('label_th')})")
    else:
        highlights.append("ไม่มีข้อมูล CG Score")
    if agm.get("is_rated"):
        highlights.append(f"AGM Level {agm['score']}/5 ({agm.get('label_th')})")
    else:
        highlights.append("ไม่มีข้อมูล AGM Level")
    highlights.append(
        "ได้รับการรับรอง Thai-CAC" if cac.get("is_certified")
        else "ไม่มีข้อมูลการรับรอง Thai-CAC"
    )
    if esg.get("is_rated"):
        highlights.append(f"SET ESG Ratings ระดับ {esg.get('rating')}")
    else:
        highlights.append("ไม่ได้รับการจัดอันดับ SET ESG Ratings")

    return {
        "disclosed_indicators": disclosed,
        "total_indicators": 4,
        "cg_score": cg.get("score"),
        "agm_level": agm.get("score"),
        "thai_cac_certified": cac.get("is_certified"),
        "set_esg_rating": esg.get("rating"),
        "highlights_th": highlights,
        "summary_th": (
            "สรุปข้อมูลด้านความยั่งยืนและธรรมาภิบาล: " + " | ".join(highlights)
        ),
    }


async def fetch_sustainability(symbol: str, use_cache: bool = True) -> dict[str, Any]:
    """Fetch and parse the sustainability block for one ticker."""
    normalized = clean_text(symbol).upper()

    async def factory() -> dict[str, Any]:
        html = await fetch_html(config.PATH_COMPANY_PROFILE.format(symbol=normalized))
        parsed = parse_profile_html(html, normalized)
        parsed["source_url"] = (
            f"{config.BASE_URL}{config.PATH_COMPANY_PROFILE.format(symbol=normalized)}"
        )
        return parsed

    if not use_cache:
        return await factory()
    return await _cache.get_or_set(normalized, factory)


def cache_stats() -> dict:
    return _cache.stats()


def invalidate_cache() -> None:
    _cache.invalidate()
