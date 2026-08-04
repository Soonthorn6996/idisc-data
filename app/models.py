"""Pydantic response schemas.

Field names are deliberately explicit (``shares_signed``, ``is_market_trade``,
``net_direction``) so a model consuming the JSON can act on it without a data
dictionary. Thai source wording is retained next to every normalised token.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .config import DISCLAIMER, SOURCE_ATTRIBUTION


class ResponseMeta(BaseModel):
    source: str = Field(default=SOURCE_ATTRIBUTION, description="แหล่งที่มาของข้อมูล")
    source_url: Optional[str] = Field(default=None, description="URL หน้าเว็บต้นทางที่ดึงข้อมูล")
    retrieved_at: str = Field(description="เวลาที่ดึงข้อมูล (ISO 8601)")
    cached: bool = Field(default=False, description="ผลลัพธ์นี้มาจาก cache หรือไม่")
    records_reported_by_source: Optional[int] = Field(
        default=None,
        description="จำนวนรายการที่หน้าเว็บ ก.ล.ต. ระบุว่าพบ (ใช้ตรวจความครบถ้วนของการ parse)",
    )
    records_parsed: Optional[int] = Field(default=None, description="จำนวนรายการที่ระบบ parse ได้")
    parse_complete: Optional[bool] = Field(
        default=None,
        description="True เมื่อจำนวนรายการที่ parse ได้ตรงกับที่หน้าเว็บระบุ",
    )


class CompanyRef(BaseModel):
    symbol: Optional[str] = Field(default=None, description="ชื่อย่อหลักทรัพย์ เช่น GULF")
    company_name_th: Optional[str] = Field(default=None, description="ชื่อบริษัทภาษาไทย")
    unique_id_reference: Optional[str] = Field(
        default=None, description="รหัสอ้างอิงภายในของ ก.ล.ต. (10 หลัก)"
    )
    market: Optional[str] = Field(default=None, description="ตลาด: SET หรือ mai")
    sector_code: Optional[str] = Field(default=None, description="รหัสหมวดอุตสาหกรรม เช่น ENERG")
    sector_name_th: Optional[str] = Field(default=None, description="ชื่อหมวดอุตสาหกรรมภาษาไทย")
    resolved_via: Optional[str] = Field(
        default=None, description="วิธีที่ใช้ค้นหารหัสบริษัท: company_profile หรือ listed_index"
    )


# --------------------------------------------------------------------------
# Form 59
# --------------------------------------------------------------------------
class Form59Record(BaseModel):
    """หนึ่งรายการรายงานการเปลี่ยนแปลงการถือหลักทรัพย์ (แบบ 59)."""

    symbol: Optional[str] = None
    company_name_th: Optional[str] = None
    company_label_raw: Optional[str] = Field(
        default=None, description="ข้อความชื่อบริษัทดิบจากหน้าเว็บ"
    )

    executive_name: Optional[str] = Field(
        default=None,
        description="ชื่อผู้บริหารที่มีหน้าที่รายงาน (คอลัมน์ 'ชื่อผู้บริหาร' ไม่รวมคำนำหน้า)",
    )
    executive_title: Optional[str] = Field(default=None, description="คำนำหน้าชื่อ เช่น นาย นาง นางสาว")
    executive_name_raw: Optional[str] = None

    relationship_code: str = Field(
        description=(
            "ความสัมพันธ์ของผู้ถือหลักทรัพย์ต่อผู้บริหาร: "
            "self, spouse, child, minor_child, juristic_person, other"
        )
    )
    relationship_th: Optional[str] = Field(default=None, description="ข้อความความสัมพันธ์ต้นฉบับ")
    relationship_en: Optional[str] = None
    is_self_filing: Optional[bool] = Field(
        default=None, description="True เมื่อหลักทรัพย์ที่เปลี่ยนแปลงเป็นของผู้บริหารเอง"
    )

    holder_name: Optional[str] = Field(
        default=None,
        description=(
            "ชื่อผู้ถือหลักทรัพย์ที่การถือครองเปลี่ยนแปลงจริง "
            "(เท่ากับผู้บริหารเมื่อ relationship_code = self, "
            "หรือเป็นคู่สมรส/บุตร/นิติบุคคลที่เกี่ยวข้อง)"
        ),
    )
    holder_type: str = Field(description="individual, juristic_person หรือ unknown")
    holder_is_executive: bool = Field(
        description="True เมื่อผู้ถือหลักทรัพย์คือผู้บริหารเอง"
    )

    security_type_code: str = Field(
        description="ประเภทหลักทรัพย์: common_share, warrant, convertible_debenture ฯลฯ"
    )
    security_type_th: Optional[str] = None
    security_type_en: Optional[str] = None
    asset_class: str = Field(description="equity, equity_linked, debt, derivative, fund, unknown")

    transaction_date: Optional[str] = Field(
        default=None, description="วันที่ทำรายการ (ค.ศ. รูปแบบ ISO 8601 YYYY-MM-DD)"
    )
    transaction_date_be: Optional[str] = Field(
        default=None, description="วันที่ทำรายการตามต้นฉบับ (พ.ศ. DD/MM/BBBB)"
    )
    transaction_date_thai: Optional[str] = Field(
        default=None, description="วันที่แบบข้อความไทย เช่น 27 กุมภาพันธ์ 2566"
    )

    shares: Optional[int] = Field(default=None, description="จำนวนหน่วย")
    shares_signed: Optional[int] = Field(
        default=None,
        description="จำนวนหน่วยแบบมีเครื่องหมาย (+ ได้มา / - จำหน่าย) ใช้รวมยอดสุทธิได้ทันที",
    )
    price_per_share: Optional[float] = Field(
        default=None, description="ราคาต่อหน่วย (บาท) เป็น null เมื่อไม่ระบุราคา"
    )
    transaction_value: Optional[float] = Field(
        default=None, description="มูลค่ารายการ = จำนวน x ราคา (บาท)"
    )
    currency: str = "THB"

    method_code: str = Field(
        description="วิธีการ: buy, sell, transfer_in, transfer_out, exercise, conversion ฯลฯ"
    )
    method_th: Optional[str] = None
    method_en: Optional[str] = None
    direction: Literal["acquire", "dispose", "neutral", "unknown"] = Field(
        description="ทิศทางการเปลี่ยนแปลงการถือครอง"
    )
    is_market_trade: bool = Field(
        description="True เมื่อเป็นการซื้อขายผ่านตลาด (ไม่ใช่การโอน/ให้/มรดก)"
    )

    is_potential_duplicate: bool = Field(
        default=False,
        description=(
            "True เมื่อรายการนี้เป็นการรายงานซ้ำของรายการเดียวกัน "
            "(เกิดเมื่อผู้บริหารที่เป็นคู่สมรสกันต่างรายงานรายการเดียวกัน) "
            "ค่าสรุปใน analytics ไม่นับรายการที่ถูกทำเครื่องหมายนี้"
        ),
    )
    duplicate_of_index: Optional[int] = Field(
        default=None, description="ลำดับของรายการต้นฉบับที่รายการนี้ซ้ำกับ"
    )

    report_url: Optional[str] = Field(default=None, description="ลิงก์แบบรายงานฉบับเต็มบนเว็บ ก.ล.ต.")
    narrative_th: Optional[str] = Field(
        default=None, description="ประโยคสรุปรายการภาษาไทย พร้อมให้ AI นำไปใช้"
    )


class FlowSummary(BaseModel):
    """สรุปกระแสการเปลี่ยนแปลงการถือครองของกลุ่มรายการ.

    ใช้คำว่า acquire/dispose แทน buy/sell เพราะโครงสร้างนี้ใช้สรุปทั้งกลุ่ม
    ที่ซื้อขายผ่านตลาด และกลุ่มที่ไม่ใช่การซื้อขาย (เช่น การรับโอน/มรดก)
    """

    records: int
    acquire_records: int = Field(description="จำนวนรายการที่ทำให้การถือครองเพิ่มขึ้น")
    dispose_records: int = Field(description="จำนวนรายการที่ทำให้การถือครองลดลง")
    acquire_shares: int
    dispose_shares: int
    acquire_value: Optional[float] = Field(description="มูลค่ารวมฝั่งได้มา (บาท)")
    dispose_value: Optional[float] = Field(description="มูลค่ารวมฝั่งจำหน่าย (บาท)")
    net_shares: int = Field(description="acquire_shares - dispose_shares")
    net_value: Optional[float]
    net_direction: Literal["net_acquisition", "net_disposal", "balanced", "no_activity"]


class Form59Analytics(BaseModel):
    total_records: int = Field(description="จำนวนรายการทั้งหมดที่ดึงมาได้ (รวมรายการซ้ำ)")
    records_used_in_totals: int = Field(
        description="จำนวนรายการที่ใช้คำนวณค่าสรุป (ตัดรายการซ้ำออกแล้ว)"
    )
    duplicate_records_excluded: int = Field(
        description="จำนวนรายการซ้ำซ้อนที่ถูกตัดออกจากการคำนวณ"
    )
    duplicate_caution_th: Optional[str] = Field(
        default=None, description="คำเตือนของ ก.ล.ต. เรื่องรายการซ้ำ (มีค่าเมื่อพบรายการซ้ำ)"
    )
    date_range: dict[str, Optional[str]]
    market_activity: FlowSummary = Field(
        description="สรุปเฉพาะรายการซื้อขายผ่านตลาด (ซื้อ/ขาย)"
    )
    non_market_activity: FlowSummary = Field(
        description="สรุปรายการที่ไม่ใช่การซื้อขายผ่านตลาด เช่น โอน/รับโอน/มรดก"
    )
    net_position_change_shares: int = Field(
        description="ผลรวม shares_signed ของรายการที่ไม่ซ้ำกัน"
    )
    by_method: list[dict[str, Any]]
    by_security_type: list[dict[str, Any]]
    by_holder: list[dict[str, Any]] = Field(
        description="สรุปตามผู้ถือหลักทรัพย์จริง - ใช้ตอบว่า 'ใครสะสมหรือลดการถือครอง'"
    )
    by_executive: list[dict[str, Any]] = Field(
        description="สรุปตามผู้บริหารที่มีหน้าที่รายงาน - ตรงกับมุมมองของหน้าเว็บ ก.ล.ต."
    )
    by_month: list[dict[str, Any]]
    largest_transactions: list[dict[str, Any]]
    activity_summary_th: str


class Form59Response(BaseModel):
    """ผลลัพธ์แบบ 59 พร้อมข้อมูลสรุปเชิงวิเคราะห์."""

    query: dict[str, Any] = Field(description="เงื่อนไขที่ใช้ค้นหา")
    company: CompanyRef
    records: list[Form59Record]
    analytics: Form59Analytics
    meta: ResponseMeta
    disclaimer: str = DISCLAIMER


# --------------------------------------------------------------------------
# Sustainability
# --------------------------------------------------------------------------
class ScoreIndicator(BaseModel):
    score: Optional[int] = Field(default=None, description="คะแนน 1-5 (null เมื่อไม่มีข้อมูล)")
    max_score: int = 5
    label_th: Optional[str] = None
    label_en: Optional[str] = None
    is_rated: bool = Field(description="True เมื่อบริษัทมีคะแนนเปิดเผยไว้")
    scale: Optional[str] = None
    source: Optional[str] = None
    raw_image: Optional[str] = Field(
        default=None,
        description="URL รูปภาพต้นฉบับ - ก.ล.ต. เปิดเผยคะแนนนี้เป็นรูปภาพเท่านั้น",
    )
    note_th: Optional[str] = None


class ThaiCacIndicator(BaseModel):
    status: Literal["certified", "declared_intent", "not_certified_or_no_data", "other"]
    is_certified: bool
    label_th: Optional[str] = None
    label_en: Optional[str] = None
    source: Optional[str] = None


class SetEsgIndicator(BaseModel):
    rating: Optional[str] = Field(default=None, description="AAA, AA, A หรือ BBB")
    rank: Optional[int] = Field(default=None, description="อันดับชั้น (1 = ดีที่สุด)")
    total_tiers: int = 4
    label_en: Optional[str] = None
    is_rated: bool
    raw: Optional[str] = None
    source: Optional[str] = None
    note_th: Optional[str] = None


class SustainabilityBlock(BaseModel):
    cg_score: ScoreIndicator
    agm_level: ScoreIndicator
    thai_cac: ThaiCacIndicator
    set_esg_rating: SetEsgIndicator


class SustainabilityScorecard(BaseModel):
    disclosed_indicators: int
    total_indicators: int
    cg_score: Optional[int]
    agm_level: Optional[int]
    thai_cac_certified: Optional[bool]
    set_esg_rating: Optional[str]
    highlights_th: list[str]
    summary_th: str


class SustainabilityResponse(BaseModel):
    """ข้อมูล Sustainability Development ของบริษัทจดทะเบียน."""

    company: CompanyRef
    sustainability: SustainabilityBlock
    sustainability_scorecard: SustainabilityScorecard
    business_description_th: Optional[str] = None
    basic_info: dict[str, Optional[str]] = Field(description="ที่อยู่ โทรศัพท์ โทรสาร เว็บไซต์")
    contacts: dict[str, Optional[str]] = Field(description="ผู้ลงทุนสัมพันธ์ และเลขานุการบริษัท")
    footnotes: list[str] = Field(default_factory=list, description="หมายเหตุ/ปีข้อมูลของแต่ละดัชนี")
    last_updated_th: Optional[str] = Field(default=None, description="วันที่ปรับปรุงล่าสุดตามหน้าเว็บ")
    meta: ResponseMeta
    disclaimer: str = DISCLAIMER


# --------------------------------------------------------------------------
# Symbols directory
# --------------------------------------------------------------------------
class SymbolRecord(BaseModel):
    symbol: str
    company_name_th: Optional[str] = None
    unique_id_reference: Optional[str] = None
    market: Optional[str] = None
    sector_code: Optional[str] = None
    sector_name_th: Optional[str] = None


class SymbolListResponse(BaseModel):
    total: int
    returned: int
    symbols: list[SymbolRecord]
    meta: ResponseMeta


# --------------------------------------------------------------------------
# Combined + bulk
# --------------------------------------------------------------------------
class CompanyFullResponse(BaseModel):
    """รวมข้อมูลแบบ 59 และ Sustainability ในคำตอบเดียว."""

    company: CompanyRef
    sustainability: Optional[SustainabilityBlock] = None
    sustainability_scorecard: Optional[SustainabilityScorecard] = None
    form59: Optional[dict[str, Any]] = None
    errors: list[dict[str, str]] = Field(
        default_factory=list,
        description="ส่วนที่ดึงไม่สำเร็จ จะรายงานไว้ที่นี่โดยไม่ทำให้ทั้ง request ล้มเหลว",
    )
    meta: ResponseMeta
    disclaimer: str = DISCLAIMER


class BulkForm59Request(BaseModel):
    symbols: list[str] = Field(description="รายชื่อย่อหลักทรัพย์", examples=[["GULF", "PTT", "ADVANC"]])
    date_from: Optional[str] = Field(
        default=None, description="วันเริ่มต้น (YYYYMMDD, YYYY-MM-DD หรือ DD/MM/BBBB)"
    )
    date_to: Optional[str] = Field(default=None, description="วันสิ้นสุด")
    date_type: int = Field(default=1, ge=1, le=3)
    include_records: bool = Field(
        default=False, description="True = ส่งรายการรายตัวมาด้วย, False = ส่งเฉพาะสรุป"
    )


class BulkSustainabilityRequest(BaseModel):
    symbols: list[str] = Field(examples=[["GULF", "PTT", "KBANK"]])


class BulkItemResult(BaseModel):
    symbol: str
    ok: bool
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class BulkResponse(BaseModel):
    requested: int
    succeeded: int
    failed: int
    results: list[BulkItemResult]
    meta: ResponseMeta
    disclaimer: str = DISCLAIMER


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
    hint: Optional[str] = None
