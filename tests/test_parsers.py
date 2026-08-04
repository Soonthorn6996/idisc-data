"""Parser and normalisation tests.

These run entirely against saved HTML fixtures, so the suite never touches the
SEC portal - important because the portal rate-limits automated clients and a
test run must not trip that defence.

Regenerate the fixtures with ``python tests/refresh_fixtures.py``.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.http_client import detect_bot_challenge  # noqa: E402
from app.mappings import (  # noqa: E402
    extract_trailing_parenthetical,
    map_acquisition_method,
    map_market_sector,
    map_relationship,
    map_security_type,
    map_set_esg_rating,
    map_thai_cac,
    parse_score_image,
)
from app.services.form59 import (  # noqa: E402
    build_analytics,
    mark_duplicates,
    parse_form59_html,
)
from app.services.sustainability import parse_profile_html  # noqa: E402
from app.utils.text import (  # noqa: E402
    extract_symbol_from_company_label,
    parse_float,
    parse_int,
    split_person_title,
    strip_symbol_suffix,
)
from app.utils.thai_date import parse_be_date, parse_flexible_date, to_compact  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"fixture {name} missing - run tests/refresh_fixtures.py")
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Thai date handling
# --------------------------------------------------------------------------
class TestThaiDates:
    def test_buddhist_era_converts_to_gregorian(self):
        assert parse_be_date("27/02/2566") == date(2023, 2, 27)
        assert parse_be_date("14/01/2565") == date(2022, 1, 14)
        assert parse_be_date("21/02/2568") == date(2025, 2, 21)

    def test_placeholders_and_junk_return_none(self):
        for value in [None, "", "-", "n/a", "31/02/2566", "not a date"]:
            assert parse_be_date(value) is None

    def test_thai_numerals_are_accepted(self):
        assert parse_be_date("๒๗/๐๒/๒๕๖๖") == date(2023, 2, 27)

    def test_gregorian_year_passes_through(self):
        # Guards against double-subtracting if the portal ever switches to AD.
        assert parse_be_date("27/02/2023") == date(2023, 2, 27)

    @pytest.mark.parametrize("value", ["20220101", "2022-01-01", "01/01/2565"])
    def test_flexible_input_formats(self, value):
        assert parse_flexible_date(value) == date(2022, 1, 1)

    def test_invalid_input_raises(self):
        with pytest.raises(ValueError):
            parse_flexible_date("01-2022", "date_from")

    def test_compact_output_matches_sec_query_format(self):
        assert to_compact(date(2026, 8, 4)) == "20260804"


# --------------------------------------------------------------------------
# Number and text normalisation
# --------------------------------------------------------------------------
class TestTextNormalisation:
    def test_thousands_separators_are_stripped(self):
        assert parse_int("1,000,000") == 1000000
        assert parse_float("53.00") == 53.0

    def test_placeholder_values_become_none(self):
        assert parse_int("-") is None
        assert parse_float("-") is None
        assert parse_int("n/a") is None

    def test_symbol_extracted_from_company_label(self):
        label = "กัลฟ์ เอ็นเนอร์จี ดีเวลลอปเมนท์ จำกัด (มหาชน) บมจ.(GULF)"
        assert extract_symbol_from_company_label(label) == "GULF"
        assert strip_symbol_suffix(label).endswith("(มหาชน)")

    def test_thai_maha_chon_suffix_is_not_read_as_a_ticker(self):
        # "(มหาชน)" is Thai, so it must never be mistaken for a symbol.
        assert extract_symbol_from_company_label("บริษัท ก จำกัด (มหาชน)") is None

    @pytest.mark.parametrize("raw,title,name", [
        ("นาง โชติกุล สุขภิรมย์เกษม", "นาง", "โชติกุล สุขภิรมย์เกษม"),
        ("นางสาววชิราภรณ์ ชื่นสมจิตต์", "นางสาว", "วชิราภรณ์ ชื่นสมจิตต์"),
        ("นาย สารัชถ์ รัตนาวะดี", "นาย", "สารัชถ์ รัตนาวะดี"),
    ])
    def test_person_title_is_split(self, raw, title, name):
        assert split_person_title(raw) == (title, name)

    def test_nbsp_is_collapsed(self):
        assert split_person_title("นางสาวฉัตรตะวัน\xa0\xa0ไชยะกุล")[1] == "ฉัตรตะวัน ไชยะกุล"


# --------------------------------------------------------------------------
# Vocabulary mapping
# --------------------------------------------------------------------------
class TestMappings:
    def test_buy_and_sell_directions(self):
        assert map_acquisition_method("ซื้อ")["direction"] == "acquire"
        assert map_acquisition_method("ขาย")["direction"] == "dispose"

    def test_transfer_in_wins_over_transfer_out(self):
        # "โอน" is a substring of "รับโอน"; longest match must win or a receipt
        # would be booked as a disposal.
        received = map_acquisition_method("รับโอน")
        assert received["code"] == "transfer_in"
        assert received["direction"] == "acquire"
        assert map_acquisition_method("โอน")["code"] == "transfer_out"

    def test_transfers_are_not_market_trades(self):
        assert map_acquisition_method("ซื้อ")["is_market_trade"] is True
        assert map_acquisition_method("รับโอน")["is_market_trade"] is False

    def test_unknown_method_is_excluded_from_signed_maths(self):
        result = map_acquisition_method("วิธีการใหม่ที่ยังไม่รู้จัก")
        assert result["code"] == "other"
        assert result["direction"] == "unknown"

    def test_security_types(self):
        assert map_security_type("หุ้นสามัญ")["code"] == "common_share"
        assert map_security_type("ใบสำคัญแสดงสิทธิ")["code"] == "warrant"

    def test_self_filing_relationship(self):
        result = map_relationship("ผู้รายงาน")
        assert result["code"] == "self"
        assert result["is_self"] is True
        assert result["related_person"] is None

    def test_spouse_relationship_names_the_holder(self):
        result = map_relationship("คู่สมรส/ผู้ที่อยู่กินด้วยกันฉันสามีภริยา (นางนลินี รัตนาวะดี)")
        assert result["code"] == "spouse"
        assert result["related_person"] == "นางนลินี รัตนาวะดี"

    def test_juristic_person_beats_the_words_nested_inside_it(self):
        # This label embeds "ผู้จัดทำรายงาน", "คู่สมรส" and
        # "บุตรที่ยังไม่บรรลุนิติภาวะ". Matching any of those first would book a
        # controlled company's purchase as the executive's or the spouse's.
        label = (
            "นิติบุคคลซึ่งผู้จัดทำรายงาน คู่สมรสหรือผู้ที่อยู่กินด้วยกันฉันสามีภริยา "
            "และบุตรที่ยังไม่บรรลุนิติภาวะ ถือหุ้นรวมกันเกินร้อยละ 30 ของจำนวนสิทธิออกเสียงทั้งหมด "
            "และมีสัดส่วนการถือหุ้นมากที่สุด (บริษัท กัลฟ์ โฮลดิ้งส์ (ประเทศไทย) จำกัด)"
        )
        result = map_relationship(label)
        assert result["code"] == "juristic_person"
        assert result["holder_type"] == "juristic_person"
        assert result["related_person"] == "บริษัท กัลฟ์ โฮลดิ้งส์ (ประเทศไทย) จำกัด"

    def test_nested_parentheses_are_extracted(self):
        assert (
            extract_trailing_parenthetical("x (บริษัท ก (ประเทศไทย) จำกัด)")
            == "บริษัท ก (ประเทศไทย) จำกัด"
        )
        assert extract_trailing_parenthetical("no parens here") is None

    def test_score_images_carry_the_value(self):
        # CG Score and AGM Level exist only as images on the portal.
        assert parse_score_image("https://x/images/cg5.gif") == 5
        assert parse_score_image("https://x/images/agm4.gif") == 4
        assert parse_score_image("https://x/images/logo.png") is None
        assert parse_score_image(None) is None

    def test_esg_rating_ranks(self):
        assert map_set_esg_rating("AAA")["rank"] == 1
        assert map_set_esg_rating("AA")["rank"] == 2
        assert map_set_esg_rating("n/a") is None

    def test_unrecognised_esg_tier_is_preserved(self):
        result = map_set_esg_rating("AA+")
        assert result["rating"] == "AA+"
        assert result["rank"] is None

    def test_thai_cac_certification(self):
        assert map_thai_cac("ได้รับการรับรอง")["is_certified"] is True
        assert map_thai_cac("n/a")["is_certified"] is False

    def test_market_and_sector_split(self):
        assert map_market_sector("SET-ENERG")["market"] == "SET"
        assert map_market_sector("SET-ENERG")["sector_name_th"] == "พลังงานและสาธารณูปโภค"
        assert map_market_sector("mai-TECH")["market"] == "mai"


# --------------------------------------------------------------------------
# Bot-challenge detection
# --------------------------------------------------------------------------
class TestBotChallengeDetection:
    def test_shape_interstitial_is_detected(self):
        html = '<html><head><script>window["bobcmn"] = "1011";</script></head></html>'
        assert detect_bot_challenge(html) is True

    def test_real_page_is_not_flagged(self):
        html = '<html><body><div id="divBody"><form id="aspnetForm">x</form></div></body></html>'
        assert detect_bot_challenge(html) is False

    def test_small_script_only_page_is_flagged(self):
        assert detect_bot_challenge("<html><script>var a=1;</script></html>") is True


# --------------------------------------------------------------------------
# Form 59 parsing, against the live-captured fixture
# --------------------------------------------------------------------------
class TestForm59Parsing:
    @pytest.fixture(scope="class")
    def parsed(self):
        return parse_form59_html(_fixture("form59_gulf.html"))

    def test_every_reported_row_is_parsed(self, parsed):
        # The page states its own row count; a mismatch means silent data loss.
        assert parsed["reported_count"] == 29
        assert parsed["parsed_count"] == 29
        assert parsed["parse_complete"] is True

    def test_company_and_symbol(self, parsed):
        assert all(r["symbol"] == "GULF" for r in parsed["records"])

    def test_roles_are_not_conflated(self, parsed):
        """The executive column and the actual holder are distinct."""
        spouse_rows = [r for r in parsed["records"] if r["relationship_code"] == "spouse"]
        assert spouse_rows, "fixture should contain a spouse filing"
        row = spouse_rows[0]
        assert row["executive_name"] != row["holder_name"]
        assert row["holder_is_executive"] is False

    def test_juristic_person_rows_are_classified(self, parsed):
        juristic = [r for r in parsed["records"] if r["relationship_code"] == "juristic_person"]
        assert len(juristic) == 6
        assert all(r["holder_type"] == "juristic_person" for r in juristic)
        assert all("กัลฟ์ โฮลดิ้งส์" in r["holder_name"] for r in juristic)
        # A company has no personal title.
        assert all(r["holder_title"] is None for r in juristic)

    def test_signed_shares_follow_direction(self, parsed):
        for record in parsed["records"]:
            if record["direction"] == "acquire":
                assert record["shares_signed"] == record["shares"]
            elif record["direction"] == "dispose":
                assert record["shares_signed"] == -record["shares"]

    def test_transaction_value_is_computed(self, parsed):
        priced = [r for r in parsed["records"] if r["price_per_share"] is not None]
        assert priced
        for record in priced:
            assert record["transaction_value"] == pytest.approx(
                record["shares"] * record["price_per_share"]
            )

    def test_missing_price_yields_null_value_not_zero(self, parsed):
        unpriced = [r for r in parsed["records"] if r["price_per_share"] is None]
        assert unpriced
        assert all(r["transaction_value"] is None for r in unpriced)

    def test_dates_are_iso_and_original_is_kept(self, parsed):
        for record in parsed["records"]:
            assert record["transaction_date"].startswith("20")
            assert "/" in record["transaction_date_be"]

    def test_report_links_are_captured(self, parsed):
        assert all(r["report_url"].startswith("https://") for r in parsed["records"])


class TestForm59Analytics:
    @pytest.fixture(scope="class")
    def analytics(self):
        parsed = parse_form59_html(_fixture("form59_gulf.html"))
        return build_analytics(parsed["records"])

    def test_market_trades_are_separated_from_transfers(self, analytics):
        """An intra-family transfer must not read as insider buying."""
        assert analytics["market_activity"]["records"] == 22
        assert analytics["non_market_activity"]["records"] == 7
        # Transfers have no price, so they contribute no value.
        assert analytics["non_market_activity"]["acquire_value"] == 0.0

    def test_net_flow_direction(self, analytics):
        assert analytics["market_activity"]["net_direction"] == "net_acquisition"

    def test_holder_grouping_separates_company_from_person(self, analytics):
        by_name = {h["name"]: h for h in analytics["by_holder"]}
        company = next(n for n in by_name if "กัลฟ์ โฮลดิ้งส์" in n)
        assert by_name[company]["holder_type"] == "juristic_person"
        assert by_name["สารัชถ์ รัตนาวะดี"]["holder_type"] == "individual"
        # The two must not be merged into one bucket.
        assert by_name[company]["net_shares"] != by_name["สารัชถ์ รัตนาวะดี"]["net_shares"]

    def test_holder_titles_are_not_borrowed_from_the_executive(self, analytics):
        by_name = {h["name"]: h for h in analytics["by_holder"]}
        assert by_name["วชิราภรณ์ ชื่นสมจิตต์"]["title"] == "นางสาว"

    def test_totals_reconcile_with_row_level_data(self, analytics):
        market = analytics["market_activity"]
        assert market["net_shares"] == market["acquire_shares"] - market["dispose_shares"]

    def test_summary_is_populated(self, analytics):
        assert "แบบ 59" in analytics["activity_summary_th"]

    def test_empty_input_is_safe(self):
        empty = build_analytics([])
        assert empty["total_records"] == 0
        assert empty["market_activity"]["net_direction"] == "no_activity"
        assert empty["by_holder"] == []


class TestDuplicateDetection:
    """The SEC warns that married executives each file the same trade."""

    def _row(self, executive, relationship, holder_key, **overrides):
        row = {
            "executive_name": executive,
            "_executive_key": executive,
            "_holder_key": holder_key,
            "transaction_date": "2024-01-15",
            "shares": 100000,
            "price_per_share": 50.0,
            "method_code": "buy",
            "security_type_code": "common_share",
            "relationship_code": relationship,
            "is_potential_duplicate": False,
            "duplicate_of_index": None,
        }
        row.update(overrides)
        return row

    def test_cross_reported_trade_is_flagged_once(self):
        rows = [
            self._row("A", "self", "A"),
            self._row("B", "spouse", "A"),  # same trade, filed by the spouse
        ]
        assert mark_duplicates(rows) == 1
        assert rows[0]["is_potential_duplicate"] is False
        assert rows[1]["is_potential_duplicate"] is True
        assert rows[1]["duplicate_of_index"] == 0

    def test_same_executive_filing_twice_is_left_alone(self):
        # Two identical rows from one filer may be two genuine trades, so the
        # conservative choice is not to collapse them.
        rows = [self._row("A", "self", "A"), self._row("A", "self", "A")]
        assert mark_duplicates(rows) == 0

    def test_different_holders_are_not_duplicates(self):
        rows = [self._row("A", "self", "A"), self._row("B", "self", "B")]
        assert mark_duplicates(rows) == 0

    def test_transfer_pair_is_not_a_duplicate(self):
        # A transfer out and a transfer in on the same day are two real events.
        rows = [
            self._row("A", "self", "A", method_code="transfer_out"),
            self._row("A", "self", "A", method_code="transfer_in"),
        ]
        assert mark_duplicates(rows) == 0

    def test_duplicates_are_excluded_from_totals(self):
        rows = [
            self._row("A", "self", "A", shares=100000, is_market_trade=True,
                      direction="acquire", shares_signed=100000,
                      transaction_value=5000000.0, holder_name="A", holder_type="individual"),
            self._row("B", "spouse", "A", shares=100000, is_market_trade=True,
                      direction="acquire", shares_signed=100000,
                      transaction_value=5000000.0, holder_name="A", holder_type="individual"),
        ]
        mark_duplicates(rows)
        analytics = build_analytics(rows)
        assert analytics["total_records"] == 2
        assert analytics["records_used_in_totals"] == 1
        assert analytics["duplicate_records_excluded"] == 1
        # The key assertion: 100,000 shares, not 200,000.
        assert analytics["market_activity"]["acquire_shares"] == 100000
        assert analytics["duplicate_caution_th"] is not None


# --------------------------------------------------------------------------
# Sustainability parsing
# --------------------------------------------------------------------------
class TestSustainabilityParsing:
    @pytest.fixture(scope="class")
    def gulf(self):
        return parse_profile_html(_fixture("profile_gulf.html"), "GULF")

    def test_company_identity(self, gulf):
        assert gulf["company_name_th"] == "บริษัท กัลฟ์ ดีเวลลอปเมนท์ จำกัด (มหาชน)"
        assert gulf["unique_id_reference"] == "0000008616"
        assert gulf["market"] == "SET"
        assert gulf["sector_code"] == "ENERG"

    def test_image_encoded_scores_are_read(self, gulf):
        cg = gulf["sustainability"]["cg_score"]
        assert cg["score"] == 5
        assert cg["label_th"] == "ดีเลิศ"
        assert cg["is_rated"] is True
        assert gulf["sustainability"]["agm_level"]["score"] == 5

    def test_text_indicators(self, gulf):
        assert gulf["sustainability"]["thai_cac"]["is_certified"] is True
        assert gulf["sustainability"]["set_esg_rating"]["rating"] == "AA"

    def test_scorecard_counts_disclosed_indicators(self, gulf):
        card = gulf["sustainability_scorecard"]
        assert card["disclosed_indicators"] == 4
        assert card["total_indicators"] == 4
        assert card["summary_th"]

    def test_contact_and_basic_info(self, gulf):
        assert gulf["basic_info"]["website"] == "www.gulf.co.th"
        assert gulf["basic_info"]["phone"] == "0-2080-4499"
        assert gulf["contacts"]["company_secretary"] == "นางสาวฉัตรตะวัน ไชยะกุล"

    def test_placeholder_contact_becomes_null(self, gulf):
        # The page shows "-" for the IR contact; that must not leak through.
        assert gulf["contacts"]["investor_relations"] is None

    def test_footnotes_capture_the_rating_vintage(self, gulf):
        assert any("2568" in note for note in gulf["footnotes"])

    def test_unrated_company_reports_absence_not_zero(self):
        """An absent rating is not a bad rating."""
        gjs = parse_profile_html(_fixture("profile_gjs.html"), "GJS")
        assert gjs["sustainability"]["set_esg_rating"]["is_rated"] is False
        assert gjs["sustainability"]["set_esg_rating"]["rating"] is None
        assert gjs["sustainability"]["thai_cac"]["is_certified"] is False
        # CG Score is still present for this company.
        assert gjs["sustainability"]["cg_score"]["score"] == 1
        assert gjs["sustainability_scorecard"]["disclosed_indicators"] == 2
