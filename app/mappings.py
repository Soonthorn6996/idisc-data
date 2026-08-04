"""Thai -> normalised-English vocabulary for SEC disclosure data.

Every mapping keeps the original Thai string alongside a stable machine token so
downstream AI consumers can filter/aggregate on the token while still being able
to quote the source wording.
"""
from __future__ import annotations

import re
from typing import Optional

from .utils.text import clean_text

# --------------------------------------------------------------------------
# Form 59: วิธีการได้มา/จำหน่าย  (acquisition / disposal method)
# --------------------------------------------------------------------------
# ``direction`` drives the signed share delta:
#   acquire -> holdings go up, dispose -> holdings go down.
# ``is_market_trade`` separates open-market buy/sell (which carry a price and are
# meaningful for flow analysis) from transfers, gifts and corporate actions
# (which usually have no price and merely move shares between related parties).
ACQUISITION_METHODS: dict[str, dict] = {
    "ซื้อ": {"code": "buy", "label_en": "Buy", "direction": "acquire", "is_market_trade": True},
    "ขาย": {"code": "sell", "label_en": "Sell", "direction": "dispose", "is_market_trade": True},
    "รับโอน": {"code": "transfer_in", "label_en": "Transfer in", "direction": "acquire", "is_market_trade": False},
    "โอน": {"code": "transfer_out", "label_en": "Transfer out", "direction": "dispose", "is_market_trade": False},
    "รับให้": {"code": "gift_in", "label_en": "Received as gift", "direction": "acquire", "is_market_trade": False},
    "ให้": {"code": "gift_out", "label_en": "Given as gift", "direction": "dispose", "is_market_trade": False},
    "แลกเปลี่ยน": {"code": "exchange", "label_en": "Exchange", "direction": "neutral", "is_market_trade": False},
    "รับมรดก": {"code": "inheritance_in", "label_en": "Inheritance received", "direction": "acquire", "is_market_trade": False},
    "ใช้สิทธิ": {"code": "exercise", "label_en": "Exercise of rights", "direction": "acquire", "is_market_trade": False},
    "แปลงสภาพ": {"code": "conversion", "label_en": "Conversion", "direction": "acquire", "is_market_trade": False},
    "ขายชอร์ต": {"code": "short_sell", "label_en": "Short sell", "direction": "dispose", "is_market_trade": True},
    "ซื้อคืน": {"code": "buy_back", "label_en": "Buy back", "direction": "acquire", "is_market_trade": True},
}


def map_acquisition_method(raw: Optional[str]) -> dict:
    """Resolve the Thai method label to a normalised descriptor.

    Unknown wording degrades gracefully: the raw text is preserved, the code
    becomes ``other`` and the direction ``unknown`` so it is excluded from
    signed-flow maths instead of being silently counted as a buy.
    """
    text = clean_text(raw)
    if not text:
        return {"code": "unknown", "label_en": None, "label_th": None,
                "direction": "unknown", "is_market_trade": False}

    if text in ACQUISITION_METHODS:
        entry = ACQUISITION_METHODS[text]
        return {**entry, "label_th": text}

    # Longest-prefix fallback for decorated variants, e.g. "ซื้อ (ผ่านตลาด)".
    # Sorted by descending length so "รับโอน" is tested before "โอน".
    for key in sorted(ACQUISITION_METHODS, key=len, reverse=True):
        if key in text:
            entry = ACQUISITION_METHODS[key]
            return {**entry, "label_th": text}

    return {"code": "other", "label_en": None, "label_th": text,
            "direction": "unknown", "is_market_trade": False}


# --------------------------------------------------------------------------
# Form 59: ประเภทหลักทรัพย์  (security type)
# --------------------------------------------------------------------------
SECURITY_TYPES: dict[str, dict] = {
    "หุ้นสามัญ": {"code": "common_share", "label_en": "Common share", "asset_class": "equity"},
    "หุ้นบุริมสิทธิ": {"code": "preferred_share", "label_en": "Preferred share", "asset_class": "equity"},
    "ใบสำคัญแสดงสิทธิที่จะซื้อหุ้น": {"code": "warrant", "label_en": "Warrant", "asset_class": "equity_linked"},
    "ใบสำคัญแสดงสิทธิ": {"code": "warrant", "label_en": "Warrant", "asset_class": "equity_linked"},
    "ใบสำคัญแสดงสิทธิอนุพันธ์": {"code": "derivative_warrant", "label_en": "Derivative warrant", "asset_class": "derivative"},
    "หุ้นกู้แปลงสภาพ": {"code": "convertible_debenture", "label_en": "Convertible debenture", "asset_class": "equity_linked"},
    "หุ้นกู้": {"code": "debenture", "label_en": "Debenture", "asset_class": "debt"},
    "สัญญาซื้อขายล่วงหน้า": {"code": "derivatives_contract", "label_en": "Derivatives contract", "asset_class": "derivative"},
    "ใบแสดงสิทธิในผลประโยชน์": {"code": "nvdr", "label_en": "NVDR / depositary receipt", "asset_class": "equity_linked"},
    "หน่วยลงทุน": {"code": "unit_trust", "label_en": "Investment unit", "asset_class": "fund"},
    "ใบสำคัญแสดงสิทธิที่จะซื้อหุ้นเพิ่มทุนที่โอนสิทธิได้": {"code": "tsr", "label_en": "Transferable subscription right", "asset_class": "equity_linked"},
}


def map_security_type(raw: Optional[str]) -> dict:
    text = clean_text(raw)
    if not text:
        return {"code": "unknown", "label_en": None, "label_th": None, "asset_class": "unknown"}
    if text in SECURITY_TYPES:
        return {**SECURITY_TYPES[text], "label_th": text}
    for key in sorted(SECURITY_TYPES, key=len, reverse=True):
        if key in text:
            return {**SECURITY_TYPES[key], "label_th": text}
    return {"code": "other", "label_en": None, "label_th": text, "asset_class": "unknown"}


# --------------------------------------------------------------------------
# Form 59: ความสัมพันธ์  (relationship of the filer to the executive)
# --------------------------------------------------------------------------
# ORDER IS SIGNIFICANT - matched top-down, first hit wins.
#
# ``นิติบุคคล`` must be tested FIRST because the juristic-person wording contains
# the wording of almost every other category:
#   "นิติบุคคลซึ่งผู้จัดทำรายงาน คู่สมรสหรือผู้ที่อยู่กินด้วยกันฉันสามีภริยา
#    และบุตรที่ยังไม่บรรลุนิติภาวะ ถือหุ้นรวมกันเกินร้อยละ 30 ... (บริษัท ...)"
# It embeds ``ผู้จัดทำรายงาน``, ``คู่สมรส`` and ``บุตรที่ยังไม่บรรลุนิติภาวะ``,
# so any of those tested earlier would misclassify a controlled company's trade
# as the executive's own or their spouse's. Likewise
# ``บุตรที่ยังไม่บรรลุนิติภาวะ`` precedes the bare ``บุตร``.
RELATIONSHIP_TYPES: list[tuple[str, dict]] = [
    ("นิติบุคคล", {"code": "juristic_person",
                   "label_en": "Juristic person controlled by the executive's group",
                   "is_self": False, "holder_type": "juristic_person"}),
    ("ผู้รายงาน", {"code": "self", "label_en": "The executive (self)",
                   "is_self": True, "holder_type": "individual"}),
    ("ผู้จัดทำรายงาน", {"code": "self", "label_en": "The executive (self)",
                        "is_self": True, "holder_type": "individual"}),
    ("บุตรที่ยังไม่บรรลุนิติภาวะ", {"code": "minor_child", "label_en": "Minor child",
                                    "is_self": False, "holder_type": "individual"}),
    ("คู่สมรส", {"code": "spouse", "label_en": "Spouse or cohabiting partner",
                 "is_self": False, "holder_type": "individual"}),
    ("ผู้สมรส", {"code": "spouse", "label_en": "Spouse or cohabiting partner",
                 "is_self": False, "holder_type": "individual"}),
    ("ผู้ที่อยู่กินด้วยกันฉันสามีภริยา", {"code": "spouse", "label_en": "Spouse or cohabiting partner",
                                          "is_self": False, "holder_type": "individual"}),
    ("บุตร", {"code": "child", "label_en": "Child",
              "is_self": False, "holder_type": "individual"}),
]


def extract_trailing_parenthetical(text: str) -> Optional[str]:
    """Return the content of the final balanced ``(...)`` group in ``text``.

    A plain ``\\(([^()]+)\\)$`` regex cannot handle the nested parentheses the
    portal emits, e.g. ``(บริษัท กัลฟ์ โฮลดิ้งส์ (ประเทศไทย) จำกัด)`` - so the
    closing bracket is walked backwards with a depth counter instead.
    """
    stripped = text.rstrip()
    if not stripped.endswith(")"):
        return None

    depth = 0
    for index in range(len(stripped) - 1, -1, -1):
        char = stripped[index]
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth == 0:
                return clean_text(stripped[index + 1:-1]) or None
    return None


def map_relationship(raw: Optional[str]) -> dict:
    """Classify the ``ความสัมพันธ์`` cell.

    Semantics confirmed against the live portal: the ``ชื่อผู้บริหาร`` column
    names the **executive who carries the reporting duty**, while this cell names
    **whose holdings actually changed** relative to that executive - either
    ``ผู้รายงาน`` (the executive themself) or a related party whose name appears
    in the trailing parenthetical, e.g.::

        คู่สมรส/ผู้ที่อยู่กินด้วยกันฉันสามีภริยา (นางนลินี รัตนาวะดี)
        นิติบุคคลซึ่งผู้จัดทำรายงาน ... (บริษัท กัลฟ์ โฮลดิ้งส์ (ประเทศไทย) จำกัด)
    """
    text = clean_text(raw)
    if not text:
        return {"code": "unknown", "label_en": None, "label_th": None,
                "is_self": None, "related_person": None, "holder_type": "unknown"}

    related_person = None
    candidate = extract_trailing_parenthetical(text)
    # Ignore purely numeric annotations; keep anything that reads like a name.
    if candidate and not candidate.isdigit():
        related_person = candidate

    for needle, entry in RELATIONSHIP_TYPES:
        if needle in text:
            return {**entry, "label_th": text, "related_person": related_person}

    return {"code": "other", "label_en": None, "label_th": text, "is_self": False,
            "related_person": related_person, "holder_type": "unknown"}


# --------------------------------------------------------------------------
# Sustainability: CG Score (Thai IOD "CGR" - number of logos, 1-5)
# --------------------------------------------------------------------------
CG_SCORE_LABELS: dict[int, dict] = {
    5: {"label_th": "ดีเลิศ", "label_en": "Excellent"},
    4: {"label_th": "ดีมาก", "label_en": "Very Good"},
    3: {"label_th": "ดี", "label_en": "Good"},
    2: {"label_th": "ดีพอใช้", "label_en": "Satisfactory"},
    1: {"label_th": "ผ่าน", "label_en": "Pass"},
}

# Sustainability: AGM Level (Thai Investors Association AGM Checklist, 1-5)
AGM_LEVEL_LABELS: dict[int, dict] = {
    5: {"label_th": "ดีเยี่ยม", "label_en": "Excellent"},
    4: {"label_th": "ดีมาก", "label_en": "Very Good"},
    3: {"label_th": "ดี", "label_en": "Good"},
    2: {"label_th": "พอใช้", "label_en": "Fair"},
    1: {"label_th": "ต้องปรับปรุง", "label_en": "Needs improvement"},
}

# Sustainability: SET ESG Ratings. ``rank`` is 1 = best so results sort naturally.
SET_ESG_RATINGS: dict[str, dict] = {
    "AAA": {"rank": 1, "label_en": "AAA - highest ESG rating"},
    "AA": {"rank": 2, "label_en": "AA"},
    "A": {"rank": 3, "label_en": "A"},
    "BBB": {"rank": 4, "label_en": "BBB"},
}
SET_ESG_TOTAL_TIERS = len(SET_ESG_RATINGS)


def map_set_esg_rating(raw: Optional[str]) -> Optional[dict]:
    """Normalise the SET ESG Ratings cell (``AAA``/``AA``/``A``/``BBB``)."""
    text = clean_text(raw).upper()
    if not text or text in {"N/A", "NA", "-"}:
        return None
    entry = SET_ESG_RATINGS.get(text)
    if entry is None:
        # Preserve an unrecognised future tier rather than dropping it.
        return {"rating": text, "rank": None, "total_tiers": SET_ESG_TOTAL_TIERS,
                "label_en": text, "is_rated": True}
    return {"rating": text, "rank": entry["rank"], "total_tiers": SET_ESG_TOTAL_TIERS,
            "label_en": entry["label_en"], "is_rated": True}


# Sustainability: Thai CAC (Collective Action Against Corruption)
def map_thai_cac(raw: Optional[str]) -> dict:
    """Normalise the Thai-CAC certification cell."""
    text = clean_text(raw)
    if not text or text.lower() in {"n/a", "na", "-"}:
        return {"status": "not_certified_or_no_data", "is_certified": False,
                "label_th": text or None,
                "label_en": "No Thai-CAC certification data disclosed"}
    if "ได้รับการรับรอง" in text:
        return {"status": "certified", "is_certified": True, "label_th": text,
                "label_en": "Thai-CAC certified"}
    if "ประกาศเจตนารมณ์" in text:
        return {"status": "declared_intent", "is_certified": False, "label_th": text,
                "label_en": "Declared intent (not yet certified)"}
    return {"status": "other", "is_certified": False, "label_th": text, "label_en": None}


# --------------------------------------------------------------------------
# Market / sector, parsed from the "Ranking" link (e.g. SET-ENERG, mai-TECH)
# --------------------------------------------------------------------------
SECTOR_NAMES_TH: dict[str, str] = {
    "AGRI": "ธุรกิจการเกษตร", "FOOD": "อาหารและเครื่องดื่ม", "FASHION": "แฟชั่น",
    "HOME": "ของใช้ในครัวเรือนและสำนักงาน", "PERSON": "ของใช้ส่วนตัวและเวชภัณฑ์",
    "BANK": "ธนาคาร", "FIN": "เงินทุนและหลักทรัพย์", "INSUR": "ประกันภัยและประกันชีวิต",
    "AUTO": "ยานยนต์", "IMM": "วัสดุอุตสาหกรรมและเครื่องจักร", "PAPER": "กระดาษและวัสดุการพิมพ์",
    "PETRO": "ปิโตรเคมีและเคมีภัณฑ์", "PKG": "บรรจุภัณฑ์", "STEEL": "เหล็กและผลิตภัณฑ์โลหะ",
    "CONMAT": "วัสดุก่อสร้าง", "CONS": "บริการรับเหมาก่อสร้าง", "PROP": "พัฒนาอสังหาริมทรัพย์",
    "ENERG": "พลังงานและสาธารณูปโภค", "MINE": "เหมืองแร่", "COMM": "พาณิชย์",
    "HELTH": "การแพทย์", "MEDIA": "สื่อและสิ่งพิมพ์", "PROF": "บริการเฉพาะกิจ",
    "TOURISM": "การท่องเที่ยวและสันทนาการ", "TRANS": "ขนส่งและโลจิสติกส์",
    "ETRON": "ชิ้นส่วนอิเล็กทรอนิกส์", "ICT": "เทคโนโลยีสารสนเทศและการสื่อสาร",
    "CONSUMP": "สินค้าอุปโภคบริโภค", "SERVICE": "บริการ", "TECH": "เทคโนโลยี",
    "INDUS": "สินค้าอุตสาหกรรม", "RESOURC": "ทรัพยากร", "FINCIAL": "ธุรกิจการเงิน",
    "PROPCON": "อสังหาริมทรัพย์และก่อสร้าง", "AGRO": "เกษตรและอุตสาหกรรมอาหาร",
}


def map_market_sector(ranking_path: Optional[str]) -> dict:
    """Split ``"SET-ENERG"`` into market ``SET`` and sector ``ENERG``."""
    text = clean_text(ranking_path)
    result: dict = {"market": None, "sector_code": None, "sector_name_th": None, "raw": text or None}
    if not text:
        return result
    if "-" in text:
        market, _, sector = text.partition("-")
        market = market.strip()
        sector = sector.strip()
        result["market"] = market.upper() if market.upper() == "SET" else market
        result["sector_code"] = sector or None
        result["sector_name_th"] = SECTOR_NAMES_TH.get(sector.upper())
    else:
        result["sector_code"] = text
        result["sector_name_th"] = SECTOR_NAMES_TH.get(text.upper())
    return result


_SCORE_IMAGE_RE = re.compile(r"(?:^|/)(cg|agm)(\d)\.gif", re.IGNORECASE)


def parse_score_image(src: Optional[str]) -> Optional[int]:
    """Extract the numeric score out of ``.../images/cg5.gif`` -> ``5``.

    The SEC portal renders CG Score and AGM Level as images only; the digit in
    the filename is the sole machine-readable carrier of the value.
    """
    if not src:
        return None
    match = _SCORE_IMAGE_RE.search(src)
    if not match:
        return None
    value = int(match.group(2))
    return value if 1 <= value <= 5 else None
