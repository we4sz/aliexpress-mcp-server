#!/usr/bin/env python3
"""
Offline unit tests for the pure helpers.

The golden harness next door is the safety net for *refactors*, but it needs
live cookies and a real account, so nobody but the author can run it. These
need neither: they pin the parsing and formatting logic that turns AliExpress's
inconsistent strings into the numbers a caller reasons about.

That logic is where the silent bugs have actually been. A sold-count regex that
never matched "100K+" hid the sold line on exactly the best-selling listings; a
price parser that guesses wrong about "1.234" invents a number rather than
admitting it doesn't know. Both classes are cheap to pin and expensive to miss,
because neither raises — they just quietly return the wrong thing.

    python3 -m unittest discover -s tests -v
    python3 tests/test_units.py
"""
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Pinned before import: several helpers read the configured country at module
# scope, and the duty logic is only meaningful relative to one.
os.environ["ALIEXPRESS_COUNTRY"] = "SE"
os.environ["ALIEXPRESS_CURRENCY"] = "SEK"

from aliexpress_mcp import catalog, core, scrape  # noqa: E402


class TestNormalizePrice(unittest.TestCase):
    """Locale-agnostic money parsing — both decimal conventions, any symbol."""

    def test_documented_conventions(self):
        for text, want in [
            ("US $1.79", 1.79),          # dot decimal
            ("808,96 грн.", 808.96),     # comma decimal, trailing dot to ignore
            ("C$1,192.72", 1192.72),     # comma thousands
            ("1.192,72 €", 1192.72),     # dot thousands, comma decimal
        ]:
            with self.subTest(text=text):
                self.assertEqual(core._normalize_price(text), want)

    def test_swedish_rendering(self):
        """The live SEK format: space-grouped thousands, comma decimal, 'kr' suffix."""
        self.assertEqual(core._normalize_price("1 789,11kr"), 1789.11)
        self.assertEqual(core._normalize_price("858,77kr"), 858.77)

    def test_bare_thousands_group_is_not_a_decimal(self):
        """"1,234" is one thousand two hundred — not 1.234."""
        self.assertEqual(core._normalize_price("1,234"), 1234.0)
        self.assertEqual(core._normalize_price("1,234,567"), 1234567.0)

    def test_no_number_returns_none(self):
        for text in (None, "", "   ", "free shipping", "kr", "..."):
            with self.subTest(text=text):
                self.assertIsNone(core._normalize_price(text))


class TestParsePrice(unittest.TestCase):
    def test_currency_prefixed(self):
        self.assertEqual(core.parse_price("US $12.34"), 12.34)
        self.assertEqual(core.parse_price("C$1,192.72"), 1192.72)

    def test_bare_decimal_fallback(self):
        self.assertEqual(core.parse_price("total 45.60 incl."), 45.60)

    def test_none_and_empty(self):
        self.assertIsNone(core.parse_price(""))
        self.assertIsNone(core.parse_price("no digits here"))


class TestCentsToFloat(unittest.TestCase):
    """Cart/wishlist prices arrive as nested {amount:{cent, currencyCode}} dicts."""

    def test_nested_cent_dict(self):
        self.assertEqual(
            core._cents_to_float({"amount": {"cent": 85877, "currencyCode": "SEK"}}),
            (858.77, "SEK"),
        )

    def test_flat_cent_dict(self):
        self.assertEqual(
            core._cents_to_float({"cent": 804, "currencyCode": "USD"}), (8.04, "USD")
        )

    def test_falls_back_to_formatted_string(self):
        amount, code = core._cents_to_float({"formattedAmount": "US $12.34"})
        self.assertEqual(amount, 12.34)
        self.assertIsNone(code)

    def test_rejects_non_dict(self):
        self.assertEqual(core._cents_to_float(None), (None, None))
        self.assertEqual(core._cents_to_float("8.04"), (None, None))


class TestSoldCount(unittest.TestCase):
    """The abbreviation bug: a digits-only pattern lost every top seller."""

    def test_abbreviations(self):
        for text, display, count in [
            ("100K+ sold", "100K+ sold", 100_000),
            ("5.7K+ sold", "5.7K+ sold", 5_700),
            ("1M+ sold", "1M+ sold", 1_000_000),
            ("2,345 sold", "2,345 sold", 2_345),
        ]:
            with self.subTest(text=text):
                self.assertEqual(core._parse_sold_count(text), (display, count))

    def test_prefers_the_first_figure_over_cross_platform(self):
        """"10,000+ sales ౹ 50,000+ cross-platform sales" must report the item's own."""
        display, count = core._parse_sold_count(
            "10,000+ sales ౹ 50,000+ cross-platform sales")
        self.assertEqual(display, "10,000+ sales")
        self.assertEqual(count, 10_000)

    def test_ambiguous_separator_reports_text_without_inventing_a_number(self):
        """Bare "1.234" is 1234 in one locale and 1.234 in another — refuse to guess."""
        display, count = core._parse_sold_count("1.234 sold")
        self.assertEqual(display, "1.234 sold")
        self.assertIsNone(count)

    def test_no_figure(self):
        self.assertEqual(core._parse_sold_count("free shipping"), (None, None))
        self.assertEqual(core._parse_sold_count(None), (None, None))


class TestResolveItemId(unittest.TestCase):
    """Offline forms only — the short-link branch deliberately hits the network."""

    def test_bare_numeric_id(self):
        self.assertEqual(core._resolve_item_id(item_id="1005004105448773"),
                         "1005004105448773")

    def test_full_url(self):
        self.assertEqual(
            core._resolve_item_id(url="https://www.aliexpress.com/item/1005006.html"),
            "1005006")

    def test_url_with_query_string(self):
        self.assertEqual(
            core._resolve_item_id(url="https://www.aliexpress.com/item/1005006.html?spm=a2g0o"),
            "1005006")

    def test_url_passed_as_item_id(self):
        """Callers mix the two arguments up constantly; accept it."""
        self.assertEqual(
            core._resolve_item_id(item_id="https://www.aliexpress.com/item/1005006.html"),
            "1005006")

    def test_unresolvable(self):
        self.assertIsNone(core._resolve_item_id())
        self.assertIsNone(core._resolve_item_id(item_id="not-an-id"))


class TestApplySort(unittest.TestCase):
    """Client-side ordering, because the server drops SortType when ship_from is set."""

    def setUp(self):
        self.rows = [{"price": 3.5}, {"price": 1.25}, {"price": 10.0}]

    def test_ascending_and_descending(self):
        self.assertEqual([p["price"] for p in catalog.apply_sort(self.rows, "price_asc")],
                         [1.25, 3.5, 10.0])
        self.assertEqual([p["price"] for p in catalog.apply_sort(self.rows, "price_desc")],
                         [10.0, 3.5, 1.25])

    def test_unpriced_rows_sink_rather_than_sorting_as_zero(self):
        rows = [{"price": None}, {"price": 3.5}, {"price": 1.25}]
        for sort_by in ("price_asc", "price_desc"):
            with self.subTest(sort_by=sort_by):
                out = catalog.apply_sort(rows, sort_by)
                self.assertIsNone(out[-1]["price"])
                self.assertEqual(len(out), 3)      # dropped nothing

    def test_unknown_sort_is_a_passthrough(self):
        out = catalog.apply_sort(self.rows, "best_match")
        self.assertEqual([p["price"] for p in out], [3.5, 1.25, 10.0])

    def test_does_not_mutate_the_input(self):
        catalog.apply_sort(self.rows, "price_asc")
        self.assertEqual([p["price"] for p in self.rows], [3.5, 1.25, 10.0])


class TestShortTitle(unittest.TestCase):
    def test_short_title_passes_through_unmarked(self):
        self.assertEqual(catalog._short_title("USB C Cable"), "USB C Cable")

    def test_collapses_whitespace(self):
        self.assertEqual(catalog._short_title("USB   C\n Cable"), "USB C Cable")

    def test_long_title_is_cut_and_marked(self):
        long = ("Fast Charging USB Type C Cable For Xiaomi Samsung Huawei Honor "
                "Realme OPPO Vivo OnePlus Nothing Phone Accessories")
        out = catalog._short_title(long)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), catalog.TITLE_MAX + 1)
        self.assertTrue(long.startswith(out[:-1].rstrip(" ,·-")))

    def test_cut_lands_on_a_word_boundary(self):
        out = catalog._short_title("word " * 40)
        self.assertNotIn("wor…", out)

    def test_handles_empty(self):
        self.assertEqual(catalog._short_title(""), "")
        self.assertEqual(catalog._short_title(None), "")


class TestDutyExpectations(unittest.TestCase):
    """Configured country is SE, so the EU customs union is the relevant bloc."""

    def test_same_country_warehouse(self):
        self.assertIs(catalog._duty_free_expected("SE"), True)

    def test_within_the_bloc(self):
        self.assertIs(catalog._duty_free_expected("ES"), True)
        self.assertIs(catalog._duty_free_expected("de"), True)   # case-insensitive

    def test_outside_the_bloc(self):
        self.assertIs(catalog._duty_free_expected("CN"), False)
        self.assertIs(catalog._duty_free_expected("US"), False)

    def test_unknown_origin_is_undecided_not_false(self):
        self.assertIsNone(catalog._duty_free_expected(None))
        self.assertIsNone(catalog._duty_free_expected(""))
        self.assertIsNone(catalog._duty_free_expected("   "))

    def test_country_outside_any_known_bloc(self):
        """A CA account has no modelled union, so only a CA warehouse is settled."""
        original = catalog.COUNTRY
        catalog.COUNTRY = "CA"
        try:
            self.assertIs(catalog._duty_free_expected("CA"), True)
            self.assertIs(catalog._duty_free_expected("ES"), False)
        finally:
            catalog.COUNTRY = original


class TestInformativeTaxNote(unittest.TestCase):
    """Keep the duty clause only when it does NOT follow from the warehouse."""

    def test_drops_redundant_duty_clause_but_keeps_vat(self):
        self.assertEqual(
            catalog._informative_tax_note("Price includes VAT | No extra duties", "ES"),
            "Price includes VAT")

    def test_keeps_surprising_duty_free_from_outside_the_bloc(self):
        """A CN warehouse with duties prepaid is exactly what's worth flagging."""
        note = catalog._informative_tax_note("Price includes VAT | No extra duties", "CN")
        self.assertIn("No extra duties", note)

    def test_drops_expected_charges_from_outside_the_bloc(self):
        self.assertIsNone(catalog._informative_tax_note("Import charges will apply", "CN"))

    def test_keeps_everything_when_origin_is_unknown(self):
        note = "Price includes VAT | No extra duties"
        self.assertEqual(catalog._informative_tax_note(note, None), note)

    def test_empty_note(self):
        self.assertIsNone(catalog._informative_tax_note(None, "ES"))
        self.assertIsNone(catalog._informative_tax_note("", "ES"))


class TestListingAge(unittest.TestCase):
    """Listing age is the relister tell no other field carries."""

    @staticmethod
    def _ago(days):
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def test_days_months_years(self):
        self.assertEqual(scrape._listing_age(self._ago(8)), "8d old")
        self.assertEqual(scrape._listing_age(self._ago(120)), "4mo old")
        self.assertEqual(scrape._listing_age(self._ago(1095)), "3.0y old")

    def test_date_only_format(self):
        day = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        self.assertEqual(scrape._listing_age(day), "10d old")

    def test_future_and_garbage_are_none(self):
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        self.assertIsNone(scrape._listing_age(future))
        for bad in (None, "", "   ", "not a date", 1786182538000):
            with self.subTest(bad=bad):
                self.assertIsNone(scrape._listing_age(bad))


class TestSmallFormatters(unittest.TestCase):
    def test_msrp_flag_only_past_the_threshold(self):
        self.assertEqual(core._msrp_flag(core.SUSPICIOUS_DISCOUNT), " ⚠ MSRP?")
        self.assertEqual(core._msrp_flag(95), " ⚠ MSRP?")
        self.assertEqual(core._msrp_flag(core.SUSPICIOUS_DISCOUNT - 1), "")
        self.assertEqual(core._msrp_flag(None), "")
        self.assertEqual(core._msrp_flag("60"), "")     # string, not a number

    def test_money_always_carries_its_currency(self):
        self.assertEqual(core._fmt_money(8.5, "USD"), "8.50 USD")
        self.assertEqual(core._fmt_money(8.5), f"8.50 {core.CURRENCY}")

    def test_strip_html(self):
        self.assertEqual(core._strip_html("<span>12.30 SEK off</span>"), "12.30 SEK off")
        self.assertIsNone(core._strip_html("<span></span>"))
        self.assertIsNone(core._strip_html(None))
        self.assertIsNone(core._strip_html(12345))

    def test_epoch_ms_to_date(self):
        ms = 1786182538000
        self.assertEqual(core._fmt_epoch_ms(ms),
                         time.strftime("%Y-%m-%d", time.localtime(ms / 1000.0)))

    def test_epoch_ms_rejects_junk(self):
        for bad in (0, -1, None, "1786182538000", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(core._fmt_epoch_ms(bad))


class TestRetProblem(unittest.TestCase):
    """The failure shapes shared by the cart, order and wishlist tools."""

    def test_success_is_not_a_problem(self):
        self.assertIsNone(core.ret_problem({"ret": ["SUCCESS::调用成功"]}))

    def test_session_expiry_asks_for_a_full_login(self):
        self.assertEqual(core.ret_problem({"ret": ["FAIL_SYS_SESSION_EXPIRED::…"]}),
                         core.FULL_AUTH_MSG)

    def test_token_expiry_asks_for_fresh_cookies(self):
        """AliExpress misspells this one — EXOIRED, not EXPIRED. Match on TOKEN."""
        self.assertEqual(core.ret_problem({"ret": ["FAIL_SYS_TOKEN_EXOIRED::…"]}),
                         core.AUTH_EXPIRED_MSG)

    def test_unknown_failure_is_surfaced_verbatim(self):
        msg = core.ret_problem({"ret": ["RGV587_ERROR::SM"]})
        self.assertIn("RGV587_ERROR", msg)

    def test_missing_ret(self):
        self.assertIn("unknown error", core.ret_problem({}))


class TestBlockHelpers(unittest.TestCase):
    """Component ids carry server-assigned numeric suffixes; match by prefix."""

    RESP = {"data": {"data": {
        "wln_page_product_I_100": {"tag": "wln_page_product"},
        "wln_paging_85633": {"fields": {"hasMore": True}},
        "not_a_dict": "scalar",
    }}}

    def test_blocks_digs_the_map(self):
        self.assertIn("wln_paging_85633", core.blocks(self.RESP))

    def test_blocks_tolerates_wrong_shapes(self):
        for bad in ({}, {"data": None}, {"data": {"data": []}}):
            with self.subTest(bad=bad):
                self.assertEqual(core.blocks(bad), {})

    def test_iter_blocks_skips_scalars(self):
        ids = [bid for bid, _ in core._iter_blocks(self.RESP)]
        self.assertNotIn("not_a_dict", ids)
        self.assertEqual(len(ids), 2)

    def test_block_by_prefix_survives_a_suffix_change(self):
        bid, block = core._block_by_prefix(self.RESP, "wln_paging")
        self.assertEqual(bid, "wln_paging_85633")
        self.assertTrue(block["fields"]["hasMore"])

    def test_block_by_prefix_miss(self):
        self.assertEqual(core._block_by_prefix(self.RESP, "nope"), (None, {}))


class TestGoldenSkeleton(unittest.TestCase):
    """
    The golden harness classifies each diff as volatile or structural. Getting
    that backwards is worse than having no harness — it trains you to wave real
    regressions through, which is exactly what happened once.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import golden
        self.golden = golden

    def test_moving_prices_are_volatile(self):
        old = ["- Cable — 3.23 SEK ★5.0 · 233 sold"]
        new = ["- Cable — 3.51 SEK ★4.8 · 291 sold"]
        self.assertTrue(self.golden.only_numbers_moved(old, new))

    def test_a_relabelled_line_is_structural_even_with_the_number_unchanged(self):
        """The regression this masking was written for: same number, new wording."""
        old = ["Subtotal (all items shown): 412.90 SEK"]
        new = ["Subtotal (23 of 24 items; 1 unpriced): 412.90 SEK"]
        self.assertFalse(self.golden.only_numbers_moved(old, new))

    def test_a_dropped_field_is_structural(self):
        old = ["- Cable — 3.23 SEK ★5.0 · 233 sold · ships from CN"]
        new = ["- Cable — 3.23 SEK ★5.0 · 233 sold"]
        self.assertFalse(self.golden.only_numbers_moved(old, new))

    def test_identical_input(self):
        same = ["Cart (24 item(s)):"]
        self.assertTrue(self.golden.only_numbers_moved(same, list(same)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
