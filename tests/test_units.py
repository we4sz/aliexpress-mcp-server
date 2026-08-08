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
import base64
import gzip
import json
import os
import re
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Pinned before import: several helpers read the configured country at module
# scope, and the duty logic is only meaningful relative to one.
os.environ["ALIEXPRESS_COUNTRY"] = "SE"
os.environ["ALIEXPRESS_CURRENCY"] = "SEK"

from aliexpress_mcp import account, cart, catalog, core, scrape  # noqa: E402


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
        self.assertEqual(scrape._listing_age(self._ago(8)), "8d")
        self.assertEqual(scrape._listing_age(self._ago(120)), "4mo")
        self.assertEqual(scrape._listing_age(self._ago(1095)), "3.0y")

    def test_date_only_format(self):
        day = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        self.assertEqual(scrape._listing_age(day), "10d")

    def test_it_never_says_old_which_was_read_as_the_sellers_age(self):
        self.assertNotIn("old", scrape._listing_age(self._ago(1095)))

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
        msg = core.ret_problem({"ret": ["FAIL_SOME_NEW_CODE::SM"]})
        self.assertIn("FAIL_SOME_NEW_CODE", msg)

    def test_missing_ret(self):
        self.assertIn("unknown error", core.ret_problem({}))

    def test_user_validate_challenge_says_not_to_wait(self):
        # Report item #10: this code looked like an opaque platform string and
        # invited the natural-but-wrong response of retrying with backoff.
        # Testing established it is NOT time-based (45s and 90s retries both
        # still failed) and clears only once a human completes the challenge
        # in a logged-in browser tab — the message must say so plainly.
        msg = core.ret_problem({"ret": ["FAIL_SYS_USER_VALIDATE::访问被拒绝"]})
        self.assertIn("human-verification challenge", msg)
        self.assertIn("Waiting will NOT clear it", msg)
        self.assertIn("do not retry with backoff", msg)
        self.assertIn("logged into", msg)

    def test_rgv587_variant_is_also_detected(self):
        # AliExpress doesn't always lead with FAIL_SYS_USER_VALIDATE — the raw
        # RGV587_ERROR code shows up too. Both must map to the same actionable
        # message, not fall through to the generic "unknown error" text.
        msg = core.ret_problem({"ret": ["RGV587_ERROR::SM"]})
        self.assertEqual(msg, core.CHALLENGE_MSG)

    def test_challenge_url_is_included_when_present(self):
        resp = {
            "ret": ["FAIL_SYS_USER_VALIDATE::访问被拒绝"],
            "data": {"url": "https://verify.aliexpress.com/some-challenge"},
        }
        msg = core.ret_problem(resp)
        self.assertIn("https://verify.aliexpress.com/some-challenge", msg)

    def test_challenge_message_stands_alone_without_a_url(self):
        # No URL in the payload must never produce a fabricated one — the
        # message should still be complete and actionable on its own.
        msg = core.ret_problem({"ret": ["FAIL_SYS_USER_VALIDATE::访问被拒绝"]})
        self.assertEqual(msg, core.CHALLENGE_MSG)
        self.assertNotIn("Challenge URL", msg)


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


class TestOrderLineVariant(unittest.TestCase):
    """
    skuAttrs -> a "Name: text" display string. This is the only place the
    actually-purchased option lives — the order-line *title* is the listing's
    generic title (a "5/10PCS ..." title names every pack size the seller
    offers), not the one bought.
    """

    def test_single_attr(self):
        attrs = [{"id": 14, "name": "Color", "text": "10PCS", "vid": 350852}]
        self.assertEqual(account._order_line_variant(attrs), "Color: 10PCS")

    def test_multiple_attrs_join_in_order(self):
        attrs = [
            {"name": "Color", "text": "30cm"},
            {"name": "Specification", "text": "3Pse-1Set"},
            {"name": "Length", "text": "20pin"},
        ]
        self.assertEqual(
            account._order_line_variant(attrs),
            "Color: 30cm; Specification: 3Pse-1Set; Length: 20pin",
        )

    def test_missing_name_falls_back_to_bare_text(self):
        self.assertEqual(account._order_line_variant([{"text": "10PCS"}]), "10PCS")

    def test_absence_is_none_not_a_placeholder(self):
        """Single-variant products legitimately carry no skuAttrs — don't invent one."""
        for absent in (None, [], "not-a-list", [{"name": "Color"}], [{"text": ""}]):
            with self.subTest(absent=absent):
                self.assertIsNone(account._order_line_variant(absent))

    def test_skips_non_dict_entries(self):
        attrs = [{"name": "Color", "text": "Red"}, "junk", None]
        self.assertEqual(account._order_line_variant(attrs), "Color: Red")


class TestExtractOrders(unittest.TestCase):
    """
    Trimmed excerpt of a real `order.list` response (captured Aug 2026): one
    order line whose listing title is a multi-pack "5/10PCS ..." — the title
    alone can't say which pack size arrived, only skuAttrs can. A second,
    variant-less line checks that absence comes through as None, not invented.
    """

    RESP = {"data": {"data": {
        "pc_om_list_order_1": {"fields": {
            "orderId": "3067411194489960",
            "statusText": "Completed",
            "orderDateText": "Jan 8, 2026",
            "storeName": "Shop1103734083 Store",
            "totalPriceText": "US $6.19",
            "currencyCode": "USD",
            "orderLines": [
                {
                    "itemTitle": "5/10PCS Smart 3pin KY-015 DHT-11 DHT11 Digital Temperature Sensor",
                    "itemPriceText": "US $6.19",
                    "currencyCode": "USD",
                    "quantity": 1,
                    "productId": "1005006252875198",
                    "skuAttrs": [{"id": 14, "name": "Color", "text": "10PCS", "vid": 350852}],
                },
                {
                    "itemTitle": "5 Pair DC 12V Male Female Socket Panel Mount Barrels Jack Plug",
                    "itemPriceText": "US $1.99",
                    "currencyCode": "USD",
                    "quantity": 1,
                    "productId": "1005006979680743",
                    "skuAttrs": None,
                },
            ],
        }},
    }}}

    def test_variant_carries_the_purchased_pack_size(self):
        orders = account._extract_orders(self.RESP, 10)
        self.assertEqual(orders[0]["items"][0]["variant"], "Color: 10PCS")

    def test_line_without_skuattrs_reports_none(self):
        orders = account._extract_orders(self.RESP, 10)
        self.assertIsNone(orders[0]["items"][1]["variant"])

    def test_quantity_still_comes_through(self):
        orders = account._extract_orders(self.RESP, 10)
        self.assertEqual(orders[0]["items"][0]["quantity"], 1)


class TestOrderItemLine(unittest.TestCase):
    """
    Rendering: the variant sits right after the title, before ×qty / price —
    it answers "which one", which is what order history gets consulted for.
    """

    def test_variant_is_parenthesized_after_the_title(self):
        it = {"title": "5/10PCS Smart 3pin KY-015 DHT-11", "variant": "Color: 10PCS",
              "quantity": 1, "price": 6.19, "currency": "USD"}
        line = account._order_item_line(it)
        self.assertIn("(Color: 10PCS)", line)
        self.assertLess(line.index("(Color: 10PCS)"), line.index("6.19"))

    def test_variant_and_multi_quantity_together(self):
        it = {"title": "Cooling Fan", "variant": "Color: BK-1pcs",
              "quantity": 5, "price": 8.99, "currency": "USD"}
        line = account._order_item_line(it)
        self.assertIn("(Color: BK-1pcs)", line)
        self.assertIn("×5", line)
        self.assertLess(line.index("(Color: BK-1pcs)"), line.index("×5"))

    def test_no_variant_omits_the_parens_entirely(self):
        it = {"title": "Plain item", "variant": None, "quantity": 1, "price": 1.99, "currency": "USD"}
        self.assertNotIn("(", account._order_item_line(it))


def _ship_resp(biz=None, *, layouts=None, shipping=True, result=True):
    """A PDP response skeleton with just enough SHIPPING to drive the state machine."""
    if not result:
        return {"ret": ["SUCCESS::调用成功"], "data": {"result": {}}}
    res = {"PRODUCT_TITLE": {"text": "A Thing"}}
    if shipping:
        if layouts is None:
            layouts = [{"bizData": biz}] if biz is not None else []
        res["SHIPPING"] = {"originalLayoutResultList": layouts}
    return {"ret": ["SUCCESS::调用成功"], "data": {"result": res}}


class TestShippingStates(unittest.TestCase):
    """
    The reported worst-case: a degraded response rendering as a confident negative.

    "Does not ship to SE" may only ever come from AliExpress saying so. Everything
    else — no data, no block, no option, a quote for the wrong country — is unknown,
    and must SAY unknown.
    """

    def _fields(self, resp):
        return catalog._extract_pdp_fields(resp, "1005000000000")

    def test_real_quote_is_reported_as_a_quote(self):
        d = self._fields(_ship_resp({"shipToCode": "SE", "displayAmount": 18.68,
                                     "shipFromCode": "CN", "deliveryDayMin": 11}))
        self.assertEqual(d["ship_status"], catalog.SHIP_OK)
        self.assertEqual(d["shipping_cost"], 18.68)
        self.assertIsNone(d["ship_unreachable"])
        self.assertIn("18.68", catalog.shipping_line(d))

    def test_free_option_carries_no_amount(self):
        """8 of 22 live items: shippingFee="free" and no displayAmount."""
        d = self._fields(_ship_resp({"shipToCode": "SE", "shippingFee": "free"}))
        self.assertEqual(d["ship_status"], catalog.SHIP_OK)
        self.assertEqual(catalog.shipping_line(d), "Shipping: Free")

    def test_only_an_explicit_flag_produces_the_negative(self):
        d = self._fields(_ship_resp({"shipToCode": "SE", "unreachable": True}))
        self.assertEqual(d["ship_status"], catalog.SHIP_UNREACHABLE)
        self.assertIs(d["ship_unreachable"], True)
        self.assertIn("does not ship", catalog.shipping_line(d))

    def test_degraded_responses_are_unknown_never_negative(self):
        """Each of these once rendered as a confident statement about the listing."""
        cases = {
            "no_data": _ship_resp(result=False),
            "no_block": _ship_resp(shipping=False),
            "no_layouts": _ship_resp(layouts=[]),
            "no_option": _ship_resp(layouts=[{}]),
        }
        for reason, resp in cases.items():
            with self.subTest(reason=reason):
                d = self._fields(resp)
                self.assertEqual(d["ship_status"], catalog.SHIP_UNKNOWN)
                self.assertEqual(d["ship_status_reason"], reason)
                self.assertIsNone(d["ship_unreachable"])
                line = catalog.shipping_line(d)
                self.assertIn("UNKNOWN", line)
                self.assertNotIn("does not ship", line)

    def test_an_empty_result_dict_is_no_data_not_a_missing_block(self):
        """The anti-bot soft-block shape: the envelope arrives, the payload doesn't."""
        d = self._fields(_ship_resp(result=False))
        self.assertEqual(d["ship_status_reason"], "no_data")
        self.assertIn("no product data", catalog.shipping_line(d))

    def test_a_quote_for_another_country_is_discarded_not_reported(self):
        """The `_lang` bug's signature: real numbers, wrong destination."""
        d = self._fields(_ship_resp({
            "shipToCode": "US", "displayAmount": 18.68, "shippingFee": "charge",
            "displayEtaMinDate": "Aug. 15", "displayEtaMaxDate": "Aug. 20",
            "deliveryDayMin": 5, "deliveryDayMax": 9,
        }))
        self.assertEqual(d["ship_status_reason"], "wrong_destination")
        self.assertEqual(d["ship_to_code"], "US")
        for field in ("shipping_cost", "shipping_estimate", "ship_days_min", "ship_days_max"):
            self.assertIsNone(d[field], f"{field} survived a wrong-destination quote")
        self.assertEqual(d["shipping_alternatives"], [])
        self.assertIn("US", catalog.shipping_line(d))

    def test_the_configured_destination_passes_the_check(self):
        d = self._fields(_ship_resp({"shipToCode": "se", "displayAmount": 5.0}))
        self.assertEqual(d["ship_status"], catalog.SHIP_OK)

    def test_unknown_never_claims_the_item_cannot_ship(self):
        for resp in (_ship_resp(result=False), _ship_resp(shipping=False),
                     _ship_resp(layouts=[]), _ship_resp(layouts=[{}])):
            line = catalog.shipping_line(self._fields(resp))
            self.assertIn("not a report that the item cannot ship", line)


class TestShipFromFilter(unittest.TestCase):
    """The warehouse facet is inexact, so the rows are intersected here."""

    def test_accepts_every_input_shape(self):
        self.assertEqual(catalog.normalize_ship_from("pl"), ["PL"])
        self.assertEqual(catalog.normalize_ship_from("PL,CZ , es"), ["PL", "CZ", "ES"])
        self.assertEqual(catalog.normalize_ship_from(["PL", "cz"]), ["PL", "CZ"])
        self.assertEqual(catalog.normalize_ship_from(""), [])
        self.assertEqual(catalog.normalize_ship_from(None), [])

    def test_eu_expands_to_the_bloc_probing_the_biggest_warehouse_first(self):
        eu = catalog.normalize_ship_from("EU")
        self.assertEqual(len(eu), len(catalog.DUTY_FREE_BLOCS[0]))
        self.assertEqual(eu[0], "PL")      # 77 of 600 live cards; AT had zero
        self.assertIn("SE", eu)

    def test_duplicates_collapse_and_order_is_the_callers(self):
        self.assertEqual(catalog.normalize_ship_from(["ES", "PL", "es"]), ["ES", "PL"])

    def test_rows_from_other_warehouses_are_dropped(self):
        """A live PL request returned 27 PL cards plus ES 3, FR 2, DE 1, CZ 1."""
        rows = [{"title": "usb c cable", "ship_from": w}
                for w in ["PL", "PL", "ES", "FR", "DE", "CZ"]]
        kept, _total, notes = catalog._finish_search(rows, None, "usb c cable", ["PL"])
        self.assertEqual(len(kept), 2)
        self.assertTrue(any("4 of 6 were dropped" in n for n in notes))

    def test_a_bloc_request_keeps_every_member(self):
        rows = [{"title": "usb c cable", "ship_from": w} for w in ["PL", "ES", "DE", "CN"]]
        kept, _t, _n = catalog._finish_search(rows, None, "usb c cable",
                                              catalog.normalize_ship_from("EU"))
        self.assertEqual([p["ship_from"] for p in kept], ["PL", "ES", "DE"])

    def test_the_client_side_step_is_always_disclosed(self):
        rows = [{"title": "usb c cable", "ship_from": "PL"}]
        _k, _t, notes = catalog._finish_search(rows, None, "usb c cable", ["PL"])
        self.assertTrue(any("checked here against" in n for n in notes))

    def test_rows_with_no_warehouse_signal_are_kept_and_flagged(self):
        """Filtering on a field the fallback parser never sets would empty the list."""
        rows = [{"title": "usb c cable", "ship_from": None} for _ in range(3)]
        kept, _t, notes = catalog._finish_search(rows, None, "usb c cable", ["PL"])
        self.assertEqual(len(kept), 3)
        self.assertTrue(any("NOT applied" in n for n in notes))

    def test_no_filter_means_no_notes(self):
        rows = [{"title": "usb c cable", "ship_from": "CN"}]
        kept, _t, notes = catalog._finish_search(rows, None, "usb c cable", [])
        self.assertEqual((len(kept), notes), (1, []))


class TestQueryBroadening(unittest.TestCase):
    """
    AliExpress answers a thin keyword×warehouse intersection by swapping the
    keywords, not by returning few rows. Live: 5 healthy sets scored 0.95–1.00,
    the broadened set 0.00.
    """

    FAN = "4 pin PWM fan extension cable"

    def test_relevant_results_score_high(self):
        rows = [{"title": t} for t in [
            "New 4 Pin Pwm Fan Cable 1 To 4 Ways Splitter Black Sleeved Extension",
            "PWM Fan Splitter 4pin Adapter Cable 1 To 4 Computer CPU Fan Splitter",
        ]]
        self.assertEqual(catalog._relevant_fraction(rows, self.FAN), 1.0)

    def test_the_reported_broadening_scores_zero(self):
        rows = [{"title": t} for t in [
            "SucceBuy Cable Protector Ramp Wire Cable Cover Cord Guard 2 Channels",
            "SucceBuy Rubber Speed Bump 2 Channel Speed Bump Hump Garage",
            "3-Socket Extension Cord 3m White Power Strip",
        ]]
        self.assertEqual(catalog._relevant_fraction(rows, self.FAN), 0.0)

    def test_the_warning_fires_only_below_the_floor(self):
        good = [{"title": "4 pin PWM fan extension cable splitter", "ship_from": "PL"}]
        bad = [{"title": "Rubber Speed Bump Garage", "ship_from": "PL"}]
        _k, _t, good_notes = catalog._finish_search(good, None, self.FAN, ["PL"])
        _k, _t, bad_notes = catalog._finish_search(bad, None, self.FAN, ["PL"])
        self.assertFalse(any("broadened" in n for n in good_notes))
        self.assertTrue(any("broadened" in n for n in bad_notes))

    def test_no_tokens_or_no_rows_is_undecided(self):
        self.assertIsNone(catalog._relevant_fraction([], "usb c cable"))
        self.assertIsNone(catalog._relevant_fraction([{"title": "x"}], ""))


class TestSearchSlug(unittest.TestCase):
    """A percent-encoded title 404s the wholesale route, which read as "not found"."""

    def test_punctuation_is_blanked_not_escaped(self):
        slug = catalog._search_slug("2600pcs Metal Film Resistors 130 Values 1/4W 1%")
        self.assertNotIn("%", slug)
        self.assertEqual(slug, "2600pcs-Metal-Film-Resistors-130-Values-1-4W-1")

    def test_a_slash_becomes_a_token_break_not_a_join(self):
        """"1/4W" must read as "1 4W", never the meaningless "14W"."""
        self.assertIn("1-4W", catalog._search_slug("1/4W"))

    def test_plain_queries_are_unchanged(self):
        self.assertEqual(catalog._search_slug("usb c cable"), "usb-c-cable")

    def test_collapses_whitespace_and_survives_empties(self):
        self.assertEqual(catalog._search_slug("  usb   c  "), "usb-c")
        self.assertEqual(catalog._search_slug(""), "")


class TestTitleLadder(unittest.TestCase):
    """Long keyword-stuffed titles are the norm, so shortening is the common path."""

    TITLE = "2600pcs Metal Film Resistors Assorted Pack 130 Values 1/4W 1%"

    def test_rungs_get_shorter_and_keep_the_head(self):
        rungs = catalog._title_query_ladder(self.TITLE)
        self.assertEqual(rungs[0], self.TITLE)
        lengths = [len(r.split()) for r in rungs]
        self.assertEqual(lengths, sorted(lengths, reverse=True))
        for r in rungs:
            self.assertTrue(self.TITLE.startswith(r))

    def test_short_titles_yield_one_rung_without_duplicates(self):
        rungs = catalog._title_query_ladder("usb cable")
        self.assertEqual(rungs, ["usb cable"])

    def test_empty_title(self):
        self.assertEqual(catalog._title_query_ladder(""), [])
        self.assertEqual(catalog._title_query_ladder(None), [])


class TestSearchByTitle(unittest.TestCase):
    """
    The reported miss: the full title found nothing while a shorter form of the
    same phrase returned six near-identical listings.
    """

    def _run(self, answers):
        """Drive the ladder with a canned reply per rung; record which were tried."""
        tried = []

        def fake(query, sort_by="best_match", ship_from=""):
            tried.append(query)
            return (answers.get(len(tried) - 1, []), None, [])

        with mock.patch.object(catalog, "search_with_notes", side_effect=fake), \
                mock.patch.object(catalog, "_pace"):
            products, used, notes = catalog.search_by_title("A B C D E F G H I J K L")
        return products, used, notes, tried

    def test_a_hit_on_the_full_title_shortens_nothing(self):
        products, used, notes, tried = self._run({0: [{"item_id": "1"}]})
        self.assertEqual(len(tried), 1)
        self.assertEqual(used, "A B C D E F G H I J K L")
        self.assertEqual(notes, [])

    def test_it_shortens_until_aliexpress_answers(self):
        products, used, notes, tried = self._run({2: [{"item_id": "1"}]})
        self.assertEqual(len(products), 1)
        self.assertEqual(len(tried), 3)
        self.assertLess(len(used.split()), len(tried[0].split()))

    def test_the_query_actually_used_is_reported(self):
        """Silently answering a different question is the failure mode being fixed."""
        _p, used, notes, _t = self._run({1: [{"item_id": "1"}]})
        self.assertTrue(any(used in n for n in notes), notes)
        self.assertTrue(any("full title returned nothing" in n for n in notes))

    def test_giving_up_still_names_the_query_it_started_from(self):
        products, used, notes, tried = self._run({})
        self.assertEqual(products, [])
        self.assertEqual(used, "A B C D E F G H I J K L")
        self.assertEqual(tried, catalog._title_query_ladder("A B C D E F G H I J K L"))

    def test_the_ladder_never_broadens_to_a_category_query(self):
        """A 3-token rung always returns something — the wrong product, confidently."""
        for rung in catalog._title_query_ladder("A B C D E F G H I J K L"):
            self.assertGreaterEqual(len(rung.split()), 5)


class TestVariantCountSignal(unittest.TestCase):
    """
    The cross-tool "price disagreement": search quotes ONE SKU, the PDP quotes the
    range. `prices.minPrice` is not a minimum — one live card quoted the dearest
    config of its listing.
    """

    @staticmethod
    def _card(sku_images_blob, sku_id="12000039050573849"):
        import base64, gzip
        raw = base64.b64encode(gzip.compress(sku_images_blob.encode())).decode()
        return {"extraParams": {"sku_images": raw}, "prices": {"skuId": sku_id}}

    def test_counts_one_entry_per_sku(self):
        blob = "14:496:a.jpg;175:b.jpg;173:c.jpg"
        self.assertEqual(scrape._sku_count(self._card(blob)), 3)

    def test_repeated_images_still_count_as_separate_skus(self):
        """Several SKUs share one picture; the entry count is what tracks skuPaths."""
        blob = "14:200006155:a.jpg;200006155:a.jpg;201336447:b.jpg"
        self.assertEqual(scrape._sku_count(self._card(blob)), 3)

    def test_single_sku_listing(self):
        self.assertEqual(scrape._sku_count(self._card("14:29:only.jpg")), 1)

    def test_absent_or_unreadable_is_unknown_not_one(self):
        for card in ({}, {"extraParams": {}}, {"extraParams": {"sku_images": ""}},
                     {"extraParams": {"sku_images": "not-base64-gzip"}}):
            with self.subTest(card=card):
                self.assertIsNone(scrape._sku_count(card))

    def test_signals_expose_the_quoted_sku(self):
        sig = scrape._search_signals(self._card("14:1:a.jpg;2:b.jpg"))
        self.assertEqual(sig["variant_count"], 2)
        self.assertEqual(sig["price_sku_id"], "12000039050573849")

    def test_multi_variant_rows_are_marked_single_variant_rows_are_not(self):
        rows = [
            {"item_id": "1", "title": "Kit", "price": 50.26, "original_price": None,
             "discount_pct": None, "rating": None, "sold_count": None,
             "currency": "SEK", "variant_count": 21},
            {"item_id": "2", "title": "Cable", "price": 9.0, "original_price": None,
             "discount_pct": None, "rating": None, "sold_count": None,
             "currency": "SEK", "variant_count": 1},
        ]
        kit, cable = catalog._format_product_lines(rows, "H:").splitlines()[1:]
        self.assertIn("· 21 variants", kit)
        self.assertNotIn("variants", cable)     # a lone SKU has no range to warn about


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


class TestSubtotalByCurrency(unittest.TestCase):
    """
    A cart subtotal that silently adds USD to SEK is wrong in every currency
    while looking authoritative. One real session lost time chasing a ~310 kr
    discrepancy that did not exist, so this is pinned.
    """

    def setUp(self):
        import aliexpress_mcp.server as server
        self.fn = server._subtotal_by_currency

    def test_single_currency_reads_as_one_total(self):
        items = [{"price": 10.0, "currency": "SEK", "quantity": 2},
                 {"price": 5.5, "currency": "SEK"}]
        self.assertEqual(self.fn(items), {"SEK": 25.5})

    def test_mixed_currencies_stay_separate(self):
        items = [{"price": 10.0, "currency": "SEK"},
                 {"price": 4.0, "currency": "USD", "quantity": 3}]
        self.assertEqual(self.fn(items), {"SEK": 10.0, "USD": 12.0})

    def test_unpriced_lines_are_skipped_not_zeroed(self):
        items = [{"price": None, "currency": "SEK"}, {"price": 7.0, "currency": "SEK"}]
        self.assertEqual(self.fn(items), {"SEK": 7.0})

    def test_line_currency_wins_over_the_cart_default(self):
        items = [{"price": 3.0, "currency": "USD"}, {"price": 2.0}]
        self.assertEqual(self.fn(items, "SEK"), {"USD": 3.0, "SEK": 2.0})

    def test_unstated_currency_is_none_not_the_configured_one(self):
        """The whole point: never invent a currency the response did not state."""
        self.assertEqual(self.fn([{"price": 9.0}]), {None: 9.0})

    def test_bad_quantity_falls_back_to_one(self):
        self.assertEqual(self.fn([{"price": 2.0, "currency": "SEK", "quantity": "x"}]),
                         {"SEK": 2.0})


class TestCartLineSelected(unittest.TestCase):
    """
    Report #6: `view_cart` exposed no per-line selection state at all, so a
    line whose checkbox silently got cleared (still valid, still in the cart,
    just not part of the next checkout) was invisible. A real 24-line cart
    showed "Checkout (17)" against 18 rendered lines — one item would not
    have arrived and nothing said so.

    `_cart_line_selected` is the single place that guess lives (see the
    CART_SELECT_FIELD comment in cart.py above _extract_cart_droplet): try the
    droplet field name, fall back to the legacy one, else None rather than a
    wrong guess.
    """

    def test_reads_the_ticked_state(self):
        self.assertTrue(cart._cart_line_selected(
            {"checkbox": {"enable": True, "selected": True}}))

    def test_reads_the_unticked_state(self):
        self.assertFalse(cart._cart_line_selected(
            {"checkbox": {"enable": True, "selected": False}}))

    def test_both_render_shapes_use_the_same_field_name(self):
        """
        Confirmed live Aug 2026: the droplet shape carries a plain `checkbox`
        alongside its quantityView/priceViews/logisticsView siblings — it does
        NOT rename this one. An earlier "checkboxView" guess was wrong and was
        only masked because the lookup fell through to the legacy name.
        """
        self.assertEqual(cart.CART_SELECT_FIELD, "checkbox")

    def test_neither_field_present_is_none_not_false(self):
        """Unknown must never be reported as "unselected" — that's actionable and wrong."""
        self.assertIsNone(cart._cart_line_selected({}))
        self.assertIsNone(cart._cart_line_selected({"checkbox": "not-a-dict"}))


class TestExtractCartSelectionLegacy(unittest.TestCase):
    """
    Trimmed excerpt of the real legacy-shape `cart.render` fixture (captured
    Aug 2026): tag "product_item_component", fields promoted to the top level,
    `fields.checkbox.selected` is the real, confirmed field — cross-checked in
    the full fixture against the render's own `selectItemNum` count (10
    selected of 24 total matched exactly).
    """

    RESP = {"data": {"data": {
        "product_item_component_global_cart_30015571035972": {
            "tag": "product_item_component",
            "fields": {
                "itemId": 1005010146452067,
                "title": "30-Piece Set of Orange Combination Cable Connectors",
                "cartId": 30015571035972,
                "checkbox": {"enable": True, "selected": False},
                "quantity": {"current": 1},
            },
        },
        "product_item_component_global_cart_30015515153709": {
            "tag": "product_item_component",
            "fields": {
                "itemId": 1005005062960321,
                "title": "Silicone hookup wire",
                "cartId": 30015515153709,
                "checkbox": {"enable": True, "selected": True},
                "quantity": {"current": 1},
            },
        },
    }}}

    def test_selected_and_unselected_lines_both_come_through(self):
        result = cart._extract_cart(self.RESP)
        by_cart_id = {it["cart_id"]: it["selected"] for it in result["items"]}
        self.assertIs(by_cart_id[30015571035972], False)
        self.assertIs(by_cart_id[30015515153709], True)


class TestExtractCartSelectionDroplet(unittest.TestCase):
    """
    The droplet shape `_cart_operate` actually writes against. No real
    droplet-shaped cart render was available to confirm its checkbox field
    name (see the CART_SELECT_FIELD comment in cart.py) — this pins the
    best-effort fallback chain instead: try CART_SELECT_FIELD, then the
    legacy name, then give up honestly with None.
    """

    RESP = {"data": {"data": {
        "product_component_1": {
            "fields": {
                "itemView": {"itemId": 111, "title": "Guessed field name present",
                            "cartId": 999, "valid": True},
                "quantityView": {"current": 3},
                cart.CART_SELECT_FIELD: {"enable": True, "selected": True},
            },
        },
        "product_component_2": {
            "fields": {
                "itemView": {"itemId": 222, "title": "Only the legacy field name present",
                            "cartId": 888, "valid": True},
                "quantityView": {"current": 1},
                "checkbox": {"enable": True, "selected": False},
            },
        },
        "product_component_3": {
            "fields": {
                "itemView": {"itemId": 333, "title": "Neither field name present",
                            "cartId": 777, "valid": True},
                "quantityView": {"current": 1},
            },
        },
    }}}

    def test_guessed_field_name_is_tried_first(self):
        result = cart._extract_cart_droplet(self.RESP)
        by_cart_id = {it["cart_id"]: it["selected"] for it in result["items"]}
        self.assertIs(by_cart_id[999], True)

    def test_legacy_field_name_is_a_fallback(self):
        result = cart._extract_cart_droplet(self.RESP)
        by_cart_id = {it["cart_id"]: it["selected"] for it in result["items"]}
        self.assertIs(by_cart_id[888], False)

    def test_absence_is_none_not_a_guess(self):
        result = cart._extract_cart_droplet(self.RESP)
        by_cart_id = {it["cart_id"]: it["selected"] for it in result["items"]}
        self.assertIsNone(by_cart_id[777])


class TestCartSelectionConstants(unittest.TestCase):
    """
    Pin the two selection constants directly, by literal value, independent of
    any behavioral test that happens to exercise them. Both were wrong guesses
    once — "checkboxView" (by analogy with quantity -> quantityView) and
    "update_checkbox" (by analogy with "update_quantity") — and both wrong
    guesses were plausible enough that a future "tidy-up" could re-derive them
    the same way. See the CART_SELECT_FIELD / CART_OP_SELECT comments in
    cart.py for how each was actually confirmed (a live render vs. a browser
    capture of a real tick).
    """

    def test_select_field_is_the_bare_noun(self):
        self.assertEqual(cart.CART_SELECT_FIELD, "checkbox")

    def test_select_operation_is_the_bare_noun_not_update_checkbox(self):
        self.assertEqual(cart.CART_OP_SELECT, "checkbox")


class TestCartOperateChecknbox(unittest.TestCase):
    """
    _cart_operate's checkbox branch sends the COMPLETE checkbox object with
    `enable` preserved and only `selected` flipped — unlike quantityView, which
    IS replaced with a bare {"current": N}. That asymmetry is counter-intuitive
    (mirroring the quantity idiom for checkbox was the earlier wrong guess) so
    it is pinned here rather than left to be "simplified" back to a bare dict.

    _cart_operate ends in a real mtop_call, which normally makes it
    network-only. It is still testable offline: `mtop_call` and `_pace` are
    plain module-level names in `cart`'s namespace (imported from core), so
    swapping them for the duration of one test — the same technique already
    used for `catalog._pace` / `catalog.search_with_notes` in
    TestSearchByTitle — intercepts the outgoing payload without ever reaching
    the network. What's captured is decoded exactly as AliExpress would
    receive it (gzip + base64 inside `params`), so this checks our own
    payload-construction logic, not a mocked-away assertion.
    """

    COMPONENT_ID = "product_component_I_123"

    def _capture_payload(self, resp, selected):
        captured = {}

        def fake_mtop_call(api, version, payload, **kwargs):
            captured["payload"] = payload
            return {"ret": ["SUCCESS::调用成功"]}

        with mock.patch.object(cart, "mtop_call", side_effect=fake_mtop_call), \
                mock.patch.object(cart, "_pace"):
            ret = cart._cart_operate({}, resp, self.COMPONENT_ID, cart.CART_OP_SELECT,
                                     selected=selected)
        outer = json.loads(gzip.decompress(base64.b64decode(captured["payload"]["params"])))
        return ret, outer["data"][self.COMPONENT_ID]["fields"]

    def test_existing_checkbox_is_echoed_whole_with_only_selected_flipped(self):
        resp = {"data": {
            "data": {self.COMPONENT_ID: {
                "fields": {"checkbox": {"enable": True, "selected": True}},
            }},
            "page": {"root": None},
        }}
        ret, fields = self._capture_payload(resp, selected=False)
        self.assertEqual(ret, "SUCCESS::调用成功")
        self.assertEqual(fields["checkbox"], {"enable": True, "selected": False})
        self.assertEqual(fields["operationType"], "checkbox")

    def test_missing_checkbox_still_sends_a_complete_object(self):
        """No prior checkbox on the component: enable defaults True, not omitted."""
        resp = {"data": {
            "data": {self.COMPONENT_ID: {"fields": {}}},
            "page": {"root": None},
        }}
        _ret, fields = self._capture_payload(resp, selected=True)
        self.assertEqual(fields["checkbox"], {"enable": True, "selected": True})


class TestCartVariantLabel(unittest.TestCase):
    """
    Report #8: `add_to_cart` confirmed with an opaque id — "Added item
    1005007805734021 (variant 12000042262236139) x1" — which caught nothing
    when three wrong-variant adds happened in one session. `_cart_variant_label`
    is the data layer for the fix: resolve a sku_id to what a person can
    actually check, e.g. '"28 AWG 60m" - 106.58 SEK'.

    Trimmed PDP `result` shape: SKU.skuPaths (inline "id#Name" skuAttr
    encoding, so no skuProperties needed) joined with PRICE.skuPriceInfoMap,
    same source catalog._extract_variants / catalog._sku_spec_for_id already
    read for get_variants.
    """

    RESULT = {
        "SKU": {
            "skuPaths": [
                {"skuIdStr": "111", "skuAttr": "14:1#Red;200:2#Small"},
                {"skuIdStr": "222", "skuAttr": "14:2#Blue;200:2#Small"},
                # Same spec + price as 111: _extract_variants collapses this
                # into 111's row and files it under covered_skus instead of
                # giving it its own top-level entry.
                {"skuIdStr": "333", "skuAttr": "14:1#Red;200:2#Small"},
            ],
        },
        "PRICE": {
            "skuPriceInfoMap": {
                "111": {"salePriceString": "US $12.34"},
                "222": {"salePriceString": "US $9.99"},
                "333": {"salePriceString": "US $12.34"},
            },
        },
        "GLOBAL_DATA": {"globalData": {"currencyCode": "USD"}},
    }

    def test_spec_and_price_for_a_plain_sku(self):
        spec, price, currency = cart._cart_variant_label(self.RESULT, "222")
        self.assertEqual(spec, "Blue · Small")
        self.assertEqual(price, 9.99)
        self.assertEqual(currency, "USD")

    def test_price_still_resolves_for_a_sku_collapsed_into_another_row(self):
        """333 shares 111's (spec, price) and has no top-level row of its own
        in _extract_variants' output — covered_skus must still be checked."""
        spec, price, currency = cart._cart_variant_label(self.RESULT, "333")
        self.assertEqual(spec, "Red · Small")   # from the direct skuPaths lookup
        self.assertEqual(price, 12.34)          # from the row that absorbed it
        self.assertEqual(currency, "USD")

    def test_unknown_sku_id_returns_all_none(self):
        spec, price, currency = cart._cart_variant_label(self.RESULT, "does-not-exist")
        self.assertIsNone(spec)
        self.assertIsNone(price)
        self.assertIsNone(currency)


class TestAddManyToCart(unittest.TestCase):
    """
    The bulk add exists because ~25 rapid single adds is what trips AliExpress's
    anti-bot check — a block that does NOT lift by waiting. So the behaviour that
    actually matters is what it does once challenged: stop, and say precisely
    which items went in, so a retry neither repeats nor loses any.

    _add_one_to_cart is patched out; these test the loop, not the HTTP call.
    """

    def setUp(self):
        import aliexpress_mcp.server as server
        self.server = server
        self.fn = getattr(server.add_many_to_cart, "fn", server.add_many_to_cart)

    @staticmethod
    def _result(ok=True, challenged=False, descr="x", text=""):
        return {"ok": ok, "challenged": challenged, "descr": descr, "text": text,
                "cart_num": None, "cart_id": None}

    def test_a_challenge_stops_the_run_and_reports_what_was_untried(self):
        calls = []

        def fake(cookies, item_id, sku_id="", quantity=1):
            calls.append(item_id)
            if item_id == "2":
                return self._result(ok=False, challenged=True,
                                    text="AliExpress is holding a human-verification challenge")
            return self._result(descr='"spec"')

        with mock.patch.object(self.server, "load_cookies", return_value={"x": "y"}), \
             mock.patch.object(self.server, "_add_one_to_cart", side_effect=fake):
            out = self.fn([{"item_id": "1"}, {"item_id": "2"}, {"item_id": "3"}, {"item_id": "4"}])

        # Items after the challenge must not be attempted at all.
        self.assertEqual(calls, ["1", "2"])
        self.assertIn("Added 1 of 4", out)
        self.assertIn("Not attempted (2)", out)
        self.assertIn("human-verification challenge", out)

    def test_one_bad_item_does_not_abort_the_rest(self):
        """An ordinary failure is not a challenge — keep going."""
        def fake(cookies, item_id, sku_id="", quantity=1):
            if item_id == "2":
                return self._result(ok=False, text="sold out")
            return self._result(descr='"spec"')

        with mock.patch.object(self.server, "load_cookies", return_value={"x": "y"}), \
             mock.patch.object(self.server, "_add_one_to_cart", side_effect=fake):
            out = self.fn([{"item_id": "1"}, {"item_id": "2"}, {"item_id": "3"}])

        self.assertIn("Added 2 of 3", out)
        self.assertIn("sold out", out)
        self.assertNotIn("Not attempted", out)

    def test_accepts_bare_id_strings(self):
        with mock.patch.object(self.server, "load_cookies", return_value={"x": "y"}), \
             mock.patch.object(self.server, "_add_one_to_cart",
                               return_value=self._result(descr='"spec"')):
            out = self.fn(["1005006", "1005007"])
        self.assertIn("Added 2 of 2", out)

    def test_never_claims_an_order_was_placed(self):
        with mock.patch.object(self.server, "load_cookies", return_value={"x": "y"}), \
             mock.patch.object(self.server, "_add_one_to_cart",
                               return_value=self._result(descr='"spec"')):
            out = self.fn(["1005006"])
        self.assertIn("Nothing has been ordered or paid for.", out)


class TestDefaultVariantMarker(unittest.TestCase):
    """
    add_to_cart with no sku_id buys the preselected config, and get_variants gave
    no way to tell which row that is. It was the cheapest on 17 of 20 live
    listings — and the DEAREST on two.
    """

    @staticmethod
    def _result(default, paths, prices=None):
        prices = prices or {}
        return {
            "SKU": {"selectedSkuIdStr": default, "skuPaths": paths},
            "PRICE": {"skuPriceInfoMap": {
                k: {"salePriceString": v} for k, v in prices.items()}},
        }

    def test_the_default_row_is_marked_and_others_are_not(self):
        res = self._result("222", [
            {"skuIdStr": "111", "skuAttr": "14:1#Red", "salable": True},
            {"skuIdStr": "222", "skuAttr": "14:2#Blue", "salable": True},
        ], {"111": "10,00kr", "222": "20,00kr"})
        rows = catalog._extract_variants(res)
        marked = [v for v in rows if v["is_default"]]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["sku_id"], "222")
        self.assertEqual(marked[0]["default_sku_id"], "222")

    def test_a_default_hiding_inside_a_collapsed_row_is_still_found(self):
        """Collapsed rows share spec+price; the survivor may not be the default."""
        res = self._result("222", [
            {"skuIdStr": "111", "skuAttr": "14:1#Red", "salable": True},
            {"skuIdStr": "222", "skuAttr": "14:1#Red", "salable": True},
        ], {"111": "10,00kr", "222": "10,00kr"})
        rows = catalog._extract_variants(res)
        self.assertEqual(len(rows), 1)                    # collapsed
        self.assertNotEqual(rows[0]["sku_id"], "222")     # survivor is not the default
        self.assertTrue(rows[0]["is_default"])
        self.assertEqual(rows[0]["default_sku_id"], "222")

    def test_the_numeric_field_is_read_too(self):
        res = {"SKU": {"selectedSkuId": 222,
                       "skuPaths": [{"skuIdStr": "222", "skuAttr": "14:1#Red", "salable": True}]},
               "PRICE": {}}
        self.assertTrue(catalog._extract_variants(res)[0]["is_default"])

    def test_no_default_marks_nothing(self):
        res = self._result(None, [{"skuIdStr": "111", "skuAttr": "14:1#Red", "salable": True}])
        self.assertFalse(any(v["is_default"] for v in catalog._extract_variants(res)))

    def test_an_unknown_default_marks_nothing_rather_than_guessing(self):
        res = self._result("999", [{"skuIdStr": "111", "skuAttr": "14:1#Red", "salable": True}])
        self.assertFalse(any(v["is_default"] for v in catalog._extract_variants(res)))

    def test_every_row_carries_the_keys_even_when_unmarked(self):
        res = self._result("999", [{"skuIdStr": "111", "skuAttr": "14:1#Red", "salable": True}])
        for v in catalog._extract_variants(res):
            self.assertIn("is_default", v)
            self.assertIn("default_sku_id", v)


class TestSearchRowSchema(unittest.TestCase):
    """
    Three parsers used to emit three key sets, so a fallback parse produced rows
    that looked like listings with no warehouse and no age.
    """

    def test_every_parser_emits_the_same_keys(self):
        rows = [
            scrape._search_row(item_id="1", data_source="ssr"),
            scrape._search_row(item_id="2", data_source="legacy"),
            scrape._search_row(item_id="3", data_source="html"),
        ]
        for r in rows:
            self.assertEqual(set(r), set(scrape.SEARCH_ROW_FIELDS))

    def test_unknowns_default_to_none_not_to_a_value(self):
        row = scrape._search_row(item_id="1")
        for field in ("ship_from", "listing_age", "free_shipping", "variant_count",
                      "stock_left", "currency"):
            self.assertIsNone(row[field], field)

    def test_a_fallback_parse_says_which_facts_it_cannot_supply(self):
        note = catalog._degraded_source_note([{"data_source": "legacy"}])
        self.assertIn("warehouse country", note)
        self.assertIn("unavailable here, not absent", note)

    def test_the_healthy_path_says_nothing(self):
        self.assertIsNone(catalog._degraded_source_note([{"data_source": "ssr"}]))

    def test_a_mixed_parse_is_described_as_partial(self):
        note = catalog._degraded_source_note(
            [{"data_source": "ssr"}, {"data_source": "html"}])
        self.assertIn("Some rows", note)

    def test_the_note_reaches_the_rendered_output(self):
        rows = [scrape._search_row(item_id="1", title="Cable", price=9.0,
                                   currency="SEK", data_source="legacy")]
        self.assertIn("fallback parser", catalog._format_product_lines(rows, "H:"))


class TestLowStockSignal(unittest.TestCase):
    """The only remaining-stock figure on a search card, on 14 of 300 live cards."""

    @staticmethod
    def _card(text):
        return {"sellingPoints": [{"source": "earlyBird",
                                   "tagContent": {"tagText": text}}]}

    def test_reads_the_live_wordings(self):
        for text, want in [("Early bird deal, only 1 left", 1),
                           ("Early bird deal, only 2 left", 2),
                           ("Early bird deal, only 15 left", 15)]:
            with self.subTest(text=text):
                self.assertEqual(scrape._search_signals(self._card(text))["stock_left"], want)

    def test_other_badges_are_not_mistaken_for_stock(self):
        card = {"sellingPoints": [{"source": "npieces",
                                   "tagContent": {"tagText": "Offset duty: 3€ off"}}]}
        sig = scrape._search_signals(card)
        self.assertIsNone(sig["stock_left"])
        self.assertEqual(sig["duty_offset"], "Offset duty: 3€ off")

    def test_an_early_bird_badge_without_a_count_is_not_invented(self):
        self.assertIsNone(scrape._search_signals(self._card("Early bird deal"))["stock_left"])

    def test_no_badge_means_unknown_not_in_stock(self):
        self.assertIsNone(scrape._search_signals({"sellingPoints": []})["stock_left"])

    def test_it_is_rendered_as_a_warning(self):
        rows = [scrape._search_row(item_id="1", title="Cable", price=9.0,
                                   currency="SEK", stock_left=2, data_source="ssr")]
        self.assertIn("⚠ only 2 left", catalog._format_product_lines(rows, "H:"))

    def test_listing_age_is_rendered_as_the_listings_not_the_sellers(self):
        rows = [scrape._search_row(item_id="1", title="Cable", price=9.0,
                                   currency="SEK", listing_age="2.8y", data_source="ssr")]
        out = catalog._format_product_lines(rows, "H:")
        self.assertIn("listed 2.8y ago", out)
        self.assertNotIn("2.8y old", out)


class TestBrowserHeaderConsistency(unittest.TestCase):
    """
    Report #14: AliExpress's anti-bot risk engine (RGV587) is the worst
    failure mode this server has, and only a real browser challenge clears
    it — never waiting. The task specifically called out that a User-Agent
    claiming one Chrome version alongside client hints claiming another is a
    STRONGER bot signal than omitting the hints altogether, so the two must
    never be able to drift apart. These pin that relationship at the level
    the module docstring claims: derived from USER_AGENT, not hand-maintained
    separately.
    """

    def test_sec_ch_ua_major_version_matches_the_user_agent(self):
        m = re.search(r"Chrome/(\d+)\.", core.USER_AGENT)
        self.assertIsNotNone(m, "USER_AGENT must carry a Chrome/<major>. token")
        self.assertEqual(core.CHROME_MAJOR_VERSION, m.group(1))
        self.assertIn(f'"Google Chrome";v="{m.group(1)}"', core.SEC_CH_UA)
        self.assertIn(f'"Chromium";v="{m.group(1)}"', core.SEC_CH_UA)

    def test_platform_hint_matches_the_user_agent_os(self):
        """USER_AGENT declares "Macintosh" — sec-ch-ua-platform must agree."""
        self.assertIn("Macintosh", core.USER_AGENT)
        self.assertEqual(core.SEC_CH_UA_PLATFORM, '"macOS"')

    def test_mobile_hint_is_desktop_not_mobile(self):
        self.assertNotIn("Mobile", core.USER_AGENT)
        self.assertEqual(core.SEC_CH_UA_MOBILE, "?0")

    def test_accept_language_is_a_language_list_not_a_market_code(self):
        """
        This constant has been wrong twice, in opposite directions, so it is
        pinned against the capture rather than against a rule.

        It was hardcoded "en-CA" on an SE account. The obvious fix — derive
        "en-{COUNTRY}" — was then applied, and is also wrong: a real Chrome on
        this exact account sends `en-US,en;q=0.9,sv;q=0.8,de;q=0.7,es;q=0.6`.
        The header states which languages a person READS; it has no necessary
        relation to where their parcels ship. Both "en-CA" and "en-SE" are
        synthetic; only one of them looks synthetic in a new way.
        """
        self.assertNotIn("en-CA", core.ACCEPT_LANGUAGE)
        self.assertNotIn(f"en-{core.COUNTRY}", core.ACCEPT_LANGUAGE)
        # Must still be a well-formed preference list a browser could send.
        self.assertRegex(core.ACCEPT_LANGUAGE, r"^[a-z]{2}(-[A-Za-z]{2,4})?(,[^;]+;q=0\.\d)*$")

    def test_default_is_the_full_captured_weighted_list(self):
        """
        Pins the literal default, not just "not en-CA" — a truncated
        `"en-US,en;q=0.9"` would pass the test above (it's a well-formed
        preference list, and it's not en-CA/en-SE) while still being a
        weaker match than the real capture, which carries three more weighted
        languages. Regression guard for exactly that kind of quiet trim.
        """
        self.assertEqual(core.ACCEPT_LANGUAGE, "en-US,en;q=0.9,sv;q=0.8,de;q=0.7,es;q=0.6")

    def test_accept_language_is_overridable(self):
        """
        The default matches the browser whose cookies this server borrows, which
        is the whole point — but someone else's install reads different
        languages, so it has to be settable without editing source.
        """
        # A subprocess, not importlib.reload: reloading core swaps the module
        # object out from under cart.py and catalog.py, which imported its
        # functions by value at their own import time. Two header-order tests
        # failed that way before this was changed — the reload was louder than
        # the thing it was testing.
        import subprocess
        env = dict(os.environ, ALIEXPRESS_ACCEPT_LANGUAGE="fr-FR,fr;q=0.9")
        out = subprocess.run(
            [sys.executable, "-c",
             "from aliexpress_mcp import core; print(core.ACCEPT_LANGUAGE)"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "fr-FR,fr;q=0.9", out.stderr[-400:])

    def test_every_outbound_caller_uses_the_shared_constant(self):
        """
        Three separate places hardcoded "en-CA": get_client, mtop_call and
        _fetch_reviews. The first two were fixed together and the third was
        missed for hours because it lives in catalog.py on a different host.
        """
        import pathlib
        pkg = pathlib.Path(core.__file__).parent
        offenders = [p.name for p in pkg.glob("*.py")
                     if "en-CA,en" in p.read_text()]
        self.assertEqual(offenders, [], f"hardcoded Accept-Language in: {offenders}")


class TestGetClientHeaderOrder(unittest.TestCase):
    """
    Building a request never opens a socket — `httpx.Client.build_request`
    only constructs the object `.send()` would later transmit, which is
    exactly the boundary this needs: real headers, no network. Order was
    reverse-engineered from httpx's own merge logic (see the "Browser header
    profile" comment in core.py above USER_AGENT) and confirmed the same way
    here — read `request.headers.raw`, the actual wire representation.

    load_cookies() is mocked to a fixed value across this class rather than
    left to read the real credential file: this dev machine has real saved
    AliExpress cookies on disk, and without mocking, whether "Cookie" shows up
    in the header list (and where the order assertion's last element is)
    would depend on whichever machine happens to run the suite.
    """

    def setUp(self):
        self._cookies_patch = mock.patch.object(
            core, "load_cookies", return_value={"cna": "abc123"})
        self._cookies_patch.start()

    def tearDown(self):
        self._cookies_patch.stop()

    def test_header_order_matches_a_real_chrome_navigation(self):
        client = core.get_client()
        try:
            req = client.build_request("GET", "/w/wholesale-usb-c-cable.html")
        finally:
            client.close()
        order = [k.decode() for k, _v in req.headers.raw]
        self.assertEqual(order, [
            "Host", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
            "Upgrade-Insecure-Requests", "User-Agent", "Accept",
            "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-User", "Sec-Fetch-Dest",
            "Referer", "Priority", "Accept-Encoding", "Accept-Language",
            "Connection", "Cookie",
        ])

    def test_navigation_is_same_origin_with_a_referrer(self):
        """
        Every current caller navigates same-origin with `referer` defaulting
        to BASE_URL — "none" (no referrer at all) would be wrong here; that
        value is for a freshly typed URL, never this client's case.
        """
        client = core.get_client()
        try:
            req = client.build_request("GET", "/item/123.html")
        finally:
            client.close()
        self.assertEqual(req.headers["Sec-Fetch-Site"], "same-origin")
        self.assertEqual(req.headers["Sec-Fetch-Mode"], "navigate")
        self.assertEqual(req.headers["Sec-Fetch-Dest"], "document")

    def test_no_cookie_header_when_no_cookies_are_saved(self):
        """An empty Cookie header is itself a tell — omit the header, not send it blank."""
        self._cookies_patch.stop()
        self._cookies_patch = mock.patch.object(core, "load_cookies", return_value={})
        self._cookies_patch.start()
        client = core.get_client()
        try:
            req = client.build_request("GET", "/")
        finally:
            client.close()
        self.assertNotIn("Cookie", req.headers)


class TestMtopCallHeaderOrder(unittest.TestCase):
    """
    mtop_call() builds its own httpx.Client and calls .get()/.post() on it
    directly, so getting at the real outgoing request without a live network
    call means intercepting Client.send — the same extension point httpx's
    own MockTransport uses internally, just applied at the method level since
    mtop_call() doesn't expose a way to inject a transport. Nothing here ever
    opens a socket: `fake_send` never calls the original.
    """

    def setUp(self):
        self.captured: list[httpx.Request] = []
        self._orig_send = httpx.Client.send

        def fake_send(client_self, request, **kw):
            self.captured.append(request)
            return httpx.Response(
                200, json={"ret": ["SUCCESS::调用成功"], "data": {}}, request=request)

        httpx.Client.send = fake_send
        # _pace() adds a real time.sleep() between calls on the same channel
        # (by design, to stay under AliExpress's rate limit) — irrelevant to
        # what this test class checks and would only slow the suite down.
        self._orig_pace = core._pace
        core._pace = lambda *a, **kw: None

    def tearDown(self):
        httpx.Client.send = self._orig_send
        core._pace = self._orig_pace

    def _last_order(self):
        return [k.decode() for k, _v in self.captured[-1].headers.raw]

    def test_get_header_order_and_cross_origin_fetch_metadata(self):
        core.mtop_call(
            "mtop.aliexpress.trade.cart.render", "1.0", {"a": 1},
            cookies={"_m_h5_tk": "tok_123456789012345"},
            referer="https://www.aliexpress.com/p/shoppingcart/index.html")
        # Deliberately NOT an exact wire-order assertion.
        #
        # The capture this was built from is DevTools' `fetch()` export, and
        # that view lists headers ALPHABETICALLY — accept, accept-language,
        # content-type, priority, sec-ch-ua, sec-ch-ua-mobile, ... is a-to-z,
        # not the order Chrome put on the wire. So the capture cannot tell us
        # the order, and an exact-order test pins an invention while looking
        # like it pins evidence. That is the same trap as the checkbox
        # constants: confident-looking, unverified, and wrong.
        #
        # What IS checkable: the header SET, and that httpx did not silently
        # drop or reorder anything we listed (Host aside, which httpx always
        # injects first). Pin those.
        order = self._last_order()
        self.assertEqual(order[0], "Host")
        self.assertEqual(sorted(order[1:]), sorted([
            "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
            "User-Agent", "Accept", "Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode",
            "Sec-Fetch-Dest", "Referer", "Priority", "Accept-Encoding",
            "Accept-Language", "Connection", "Cookie",
        ]))
        self.assertEqual(len(order), len(set(order)), "a header was sent twice")
        req = self.captured[-1]
        # acs.aliexpress.com from www.aliexpress.com: different host, same
        # registrable site -> same-site, not same-origin and not cross-site.
        self.assertEqual(req.headers["Sec-Fetch-Site"], "same-site")
        self.assertEqual(req.headers["Sec-Fetch-Mode"], "cors")
        self.assertEqual(req.headers["Sec-Fetch-Dest"], "empty")
        # Both are navigation-only; an XHR/fetch call must never send them.
        self.assertNotIn("Sec-Fetch-User", req.headers)
        self.assertNotIn("Upgrade-Insecure-Requests", req.headers)

    def test_post_does_not_append_content_type_at_the_end(self):
        """
        The real defect this guards: `d["Content-Type"] = v` on an
        already-built dict appends at the very END, because a Python dict only
        reorders on a key's FIRST insertion. The POST path therefore builds its
        own ordered copy.

        It asserts "not last", not an exact index. The capture cannot settle
        the exact slot — DevTools' fetch() export is alphabetized — and an
        earlier version of this test asserted `index(Accept) + 1` while the
        code deliberately placed it after Accept-Language, so the test and the
        code it was guarding disagreed with each other and neither was evidence.
        """
        core.mtop_call(
            "mtop.aliexpress.trade.cart.async", "1.0", {"a": 1},
            cookies={"_m_h5_tk": "tok_123456789012345"}, method="POST")
        order = self._last_order()
        self.assertIn("Content-Type", order)
        self.assertLess(order.index("Content-Type"), len(order) - 1,
                        "Content-Type was appended last — the ordered copy was bypassed")
        # It belongs with the content-negotiation headers, ahead of the
        # transport ones httpx keeps at the tail.
        self.assertLess(order.index("Content-Type"), order.index("Connection"))

    def test_get_has_no_content_type(self):
        """No body on GET -> no Content-Type, matching what a real browser sends."""
        core.mtop_call(
            "mtop.aliexpress.trade.cart.render", "1.0", {"a": 1},
            cookies={"_m_h5_tk": "tok_123456789012345"})
        self.assertNotIn("Content-Type", self._last_order())

    def test_accept_is_bare_application_json(self):
        """
        Was `application/json, text/plain, */*` — a jQuery/axios default that
        looked like a reasonable guess and was wrong. The real capture shows
        bare `application/json`, no fallback types — a VALUE, not an ordering
        claim, so unaffected by the DevTools-alphabetizing caveat above.
        """
        core.mtop_call(
            "mtop.aliexpress.trade.cart.render", "1.0", {"a": 1},
            cookies={"_m_h5_tk": "tok_123456789012345"})
        self.assertEqual(self.captured[-1].headers["Accept"], "application/json")

    def test_sec_ch_ua_matches_the_captured_brand_list(self):
        """Pins the exact captured string, not just SEC_CH_UA's presence."""
        core.mtop_call(
            "mtop.aliexpress.trade.cart.render", "1.0", {"a": 1},
            cookies={"_m_h5_tk": "tok_123456789012345"})
        self.assertEqual(
            self.captured[-1].headers["sec-ch-ua"],
            '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"')


class TestSponsoredPlacements(unittest.TestCase):
    """
    Paid position rendered identically to earned rank, under a header claiming a
    sort order. Live pages ran 0/60 to 56/60 sponsored.
    """

    @staticmethod
    def _p4p_card():
        return {"p4p": {"clickUrl": "//us-click.aliexpress.com/ci_bb?ot=x"}}

    @staticmethod
    def _adtag_card():
        return {"allPlatformInfo": {"adTag": {"displayTagType": "text", "tagText": "Ad"}}}

    def test_both_markers_are_recognised(self):
        """They never co-occur on a card, so either alone has to be enough."""
        self.assertTrue(scrape._is_sponsored(self._p4p_card()))
        self.assertTrue(scrape._is_sponsored(self._adtag_card()))

    def test_an_organic_card_is_not_flagged(self):
        for card in ({}, {"p4p": None}, {"p4p": {}}, {"allPlatformInfo": {}},
                     {"allPlatformInfo": {"adTag": {}}},
                     {"allPlatformInfo": {"adTag": {"tagText": ""}}}):
            with self.subTest(card=card):
                self.assertFalse(scrape._is_sponsored(card))

    def test_malformed_shapes_do_not_raise(self):
        for card in ({"allPlatformInfo": "Ad"}, {"allPlatformInfo": {"adTag": "Ad"}},
                     {"p4p": ""}):
            with self.subTest(card=card):
                self.assertFalse(scrape._is_sponsored(card))

    def test_the_signal_reaches_the_row(self):
        self.assertTrue(scrape._search_signals(self._p4p_card())["sponsored"])
        self.assertFalse(scrape._search_signals({})["sponsored"])

    def _rows(self, flags):
        return [scrape._search_row(item_id=str(i), title="Cable", price=9.0,
                                   currency="SEK", sponsored=f, data_source="ssr")
                for i, f in enumerate(flags)]

    def test_marked_rows_are_kept_in_place_not_dropped_or_reordered(self):
        rows = self._rows([True, False, True])
        out = catalog._format_product_lines(rows, "H:").splitlines()
        body = [l for l in out if l.startswith("- ")]
        self.assertEqual(len(body), 3)                      # dropped nothing
        self.assertEqual([("sponsored" in l) for l in body], [True, False, True])

    def test_the_header_note_counts_only_the_rows_shown(self):
        rows = self._rows([True] * 3 + [False] * 30)
        note = catalog._format_product_lines(rows, "H:", limit=5).splitlines()[1]
        self.assertIn("3 of 5 rows below are", note)

    def test_an_all_sponsored_page_says_so_plainly(self):
        note = catalog._sponsored_note(self._rows([True, True]))
        self.assertIn("Every row below is", note)

    def test_the_count_agrees_with_its_verb(self):
        one = catalog._sponsored_note(self._rows([True, False]))
        many = catalog._sponsored_note(self._rows([True, True, False]))
        self.assertIn("1 of 2 rows below is a sponsored placement", one)
        self.assertIn("2 of 3 rows below are sponsored placements", many)

    def test_the_note_qualifies_the_sort_claim(self):
        note = catalog._sponsored_note(self._rows([True, False]))
        self.assertIn("not earned by the sort", note)

    def test_an_organic_page_gets_no_note(self):
        self.assertIsNone(catalog._sponsored_note(self._rows([False, False])))
        out = catalog._format_product_lines(self._rows([False]), "H:")
        self.assertNotIn("sponsored", out)

    def test_unknown_is_not_counted_as_organic(self):
        """A fallback parse can't tell, so it must not dilute the ratio."""
        rows = self._rows([True]) + [scrape._search_row(item_id="9", title="C", price=1.0,
                                                        data_source="legacy")]
        self.assertIn("Every row below is", catalog._sponsored_note(rows))

    def test_fallback_parsers_declare_sponsorship_unknown(self):
        for source in ("legacy", "html"):
            with self.subTest(source=source):
                self.assertIn("sponsored/organic", scrape.SEARCH_SOURCE_GAPS[source])
                self.assertIsNone(scrape._search_row(data_source=source)["sponsored"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
