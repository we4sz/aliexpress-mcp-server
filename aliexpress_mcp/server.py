"""
The FastMCP server singleton and all 21 @mcp.tool() functions.

This is the only module in the package that imports `mcp` / registers tools —
FastMCP registers via decorator side effects, so keeping one registration site
avoids an import-order puzzle. Tool bodies are thin: validate input, call into
the domain module (core/scrape/catalog/cart/account), and render the result.

Moved verbatim out of aliexpress_mcp_server.py — see that file's original
module docstring below for the server-level overview:

AliExpress MCP Server

Search AliExpress, pull clean product details, check shipping to the
configured country (ALIEXPRESS_COUNTRY, default CA), and manage your cart, orders, and wishlist.

Auth: Session cookies from MCP Auth Bridge extension at
~/.mcp-credentials/aliexpress.json
"""

import json
import re
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from aliexpress_mcp.core import (
    COUNTRY, CURRENCY, LANG, BASE_URL, logger,
    load_cookies, get_client, check_auth_redirect,
    AUTH_EXPIRED_MSG,
    CART_WRITE_MIN_INTERVAL, _pace,
    MTOP_CART_APP_KEY, mtop_call,
    _msrp_flag, _fmt_money, _resolve_item_id,
    _normalize_price, ret_problem,
)
from aliexpress_mcp.scrape import parse_product_detail
from aliexpress_mcp.catalog import (
    apply_sort,
    SEARCH_RENDER_ATTEMPTS, search_with_notes, search_by_title,
    _relevant_fraction, RELEVANCE_FLOOR,
    normalize_ship_from, _format_product_lines,
    _fetch_pdp_mtop, _informative_tax_note, _lot_note,
    _pdp_error_code, _pdp_unavailable_msg, _extract_pdp_fields, _delivery_days,
    _fetch_reviews, _extract_reviews, _clip_review, _common_sku_affixes,
    _extract_seller, _extract_variants,
)
from aliexpress_mcp.cart import (
    _extract_cart_droplet, _extract_cart_summary,
    _cart_droplet_render, _cart_operate, _cart_lines, _cart_fetch_all_pages,
    _extract_cart, _resolve_sku_for_cart, _resolve_cart_target, _cart_set_selected,
)
from aliexpress_mcp.account import (
    ORDER_LIST_API, _extract_orders, _order_money, _order_item_line,
    _orders_fetch_all_pages,
    WISHLIST_API, WISHLIST_GROUP_API,
    _extract_wishlist, _fetch_wishlist_groups, _resolve_wishlist_group,
    _wishlist_saved_item_ids, _wishlist_favourite, _wishlist_delete_item,
    _wishlist_delete_group, _wishlist_save_item,
)


# ─── MCP Server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    "aliexpress",
    dependencies=["httpx", "beautifulsoup4"],
    instructions=(
        "Browse, search, and manage an AliExpress account: search products, compare "
        "sellers, check reviews/shipping/variants, and inspect your cart, orders, and "
        "wishlist. Nine tools write to the real, signed-in account (add_to_cart, "
        "add_many_to_cart, set_cart_quantity, set_cart_selection, remove_from_cart, "
        "add_to_wishlist, remove_from_wishlist, create_wishlist, delete_wishlist) but "
        "none of them ever checks out, places an order, or pays — checkout is not "
        "implemented. Add several items with add_many_to_cart rather than looping "
        "add_to_cart: rapid repeated writes trip an anti-bot block that does NOT clear "
        "by waiting. "
        "Note AliExpress orders only the TICKED cart lines: an un-ticked line stays "
        "visible in the cart and simply never arrives, which view_cart flags and "
        "set_cart_selection fixes."
    ),
)



# When AliExpress replaces the query rather than filtering it (see
# catalog._relevant_fraction), the rows are not near-misses — they are a
# different product category entirely: e-bike motors returned for a heat-shrink
# query. Printing 25 of those costs the caller a screenful of context to learn
# one fact the warning already stated. Show enough to prove the point and stop.
BROADENED_ROW_LIMIT = 3


def _row_limit(products: list, query: str, requested: int) -> tuple[int, Optional[str]]:
    """(rows to print, note) — collapses the listing when the query was replaced."""
    share = _relevant_fraction(products, query)
    if share is not None and share < RELEVANCE_FLOOR and len(products) > BROADENED_ROW_LIMIT:
        return BROADENED_ROW_LIMIT, (
            f"Showing only {BROADENED_ROW_LIMIT} of {len(products)} rows: at "
            f"{share:.0%} keyword match these are a different product category, "
            "not near misses. Re-run without ship_from, or with different "
            "keywords, to get real results.")
    return requested, None


@mcp.tool(
    title="Search Products",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def search_products(
    query: str,
    min_rating: float = 0,
    max_price: float = 0,
    sort_by: str = "best_match",
    ship_from: Any = "",
    max_results: int = 25,
) -> str:
    """
    Search AliExpress for products.

    Each result is one line ending in `[<item_id>]` — pass that number as
    `item_id` to get_product_details / get_variants / get_reviews / get_seller.
    Titles are trimmed to ~80 chars; a trailing "…" means AliExpress's full title
    is longer, and get_product_details returns it in full.

    Args:
        query: Search term (e.g., "groudon plush", "usb c cable")
        min_rating: Minimum rating (0-5, e.g., 4.5). 0 disables filter.
        max_price: Maximum price, compared against the prices printed on the rows
            below — the same number, same currency, no conversion. That is your
            AliExpress site currency, which is stated in the output next to every
            price, so read it off a first unfiltered search if unsure. 0 disables.
        sort_by: One of "best_match", "orders", "price_asc", "price_desc"
        ship_from: Warehouse country to restrict results to — a two-letter code
            ("ES", "PL", "CZ"), a list or comma-separated string of them, or
            "EU"/"EEA" for the whole customs union. Shipping from inside your own
            customs union arrives in days rather than weeks and avoids import
            charges. Empty = any warehouse.
        max_results: How many rows to print (default 25, capped at 60). Lowered
            automatically when AliExpress has replaced your query rather than
            filtered it, since those rows are a different product category and
            printing 25 of them just costs context.
    """
    try:
        products, total_results, notes = search_with_notes(query, sort_by, ship_from)
        products = apply_sort(products, sort_by)
    except RuntimeError as e:
        return str(e)

    if min_rating > 0:
        products = [p for p in products if p.get("rating") and p["rating"] >= min_rating]
    if max_price > 0:
        products = [p for p in products if p.get("price") is not None and p["price"] <= max_price]

    if not products:
        filters = []
        if min_rating > 0:
            filters.append(f"rating ≥ {min_rating}")
        if max_price > 0:
            # No currency symbol: max_price is in the search's own currency (the
            # account's site currency), which this code does not know. Printing
            # "$15.00" in an SEK session states a currency we never established.
            filters.append(f"price ≤ {max_price:.2f}")
        suffix = f" with {', '.join(filters)}" if filters else ""
        # Distinguish "AliExpress has nothing" from "AliExpress did not render the
        # grid". Reporting the second as the first told callers a 92,000-result
        # query was empty, and they believed it.
        if total_results and not filters:
            # This used to end "Retry the same query." A user followed that
            # advice and the identical retry failed again, twice in a row —
            # the same class of error as the anti-bot message that once said
            # "wait a minute or two" when waiting never helped. Do not advise
            # an action without evidence it works.
            #
            # Deliberately does NOT suggest changing sort_by: adding a SortType
            # alongside shipFromCountry was measured returning total=0 with no
            # grid at all, so that "fix" can empty the result set outright.
            advice = ("Dropping ship_from is worth trying — a warehouse filter narrows "
                      "the query and may be correlated." if ship_from else
                      "Trying slightly different keywords is worth a go.")
            return (
                f"AliExpress reports {total_results:,} results for '{query}' but did not "
                f"return the results grid after {SEARCH_RENDER_ATTEMPTS} attempts. This is "
                "an intermittent server-side render failure, not an empty catalogue — but "
                "retrying the identical query is NOT reliably fixing it. "
                f"{advice} If it keeps failing for this exact query, report it as-is "
                "rather than retrying in a loop."
            )
        if total_results:
            return (f"No products matched {', '.join(filters)} for '{query}' "
                    f"(AliExpress reports {total_results:,} results before filtering).")
        return f"No products found for '{query}'{suffix}."

    limit, broad_note = _row_limit(products, query, max(1, min(int(max_results or 25), 60)))
    if broad_note:
        notes = list(notes) + [broad_note]
    shown = min(len(products), limit)
    header = f"Showing {shown} of {len(products)} parsed"
    if total_results:
        header += f" ({total_results:,} total)"
    header += f" for '{query}' (sort: {sort_by}"
    # normalize_ship_from, not .upper() — ship_from accepts a list and "EU", and
    # calling .upper() on a list raises.
    where = "/".join(normalize_ship_from(ship_from))
    header += f", ships from {where}" if where else ""
    header += "):"
    body = _format_product_lines(products, header, limit=limit)
    # Notes describe ways the rows differ from what was literally asked for — a
    # warehouse filter that quietly replaced the keywords, for instance. They are
    # the difference between a caller seeing 25 speed bumps under a header that
    # says "fan cable" and knowing why, so they print above the rows rather than
    # below, where a truncated read would miss them.
    return "\n".join(notes + [""] + [body]) if notes else body


@mcp.tool(
    title="Find Deals",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def find_deals(
    query: str,
    min_discount: int = 0,
    max_price: float = 0,
    min_rating: float = 0,
    sort_by: str = "orders",
    ship_from: Any = "",
    max_results: int = 25,
) -> str:
    """
    Search AliExpress and surface the most-discounted listings for a query.

    Same underlying search as `search_products`, but keeps only items with a
    visible discount and sorts by discount depth (biggest first).

    Each result is one line ending in `[<item_id>]` — pass that number as
    `item_id` to the per-product tools. Titles are trimmed to ~80 chars; a
    trailing "…" means get_product_details has the full one.

    Args:
        query: Search term (e.g., "mechanical keyboard").
        min_discount: Minimum discount percent to include (e.g., 40). 0 keeps any discount.
        max_price: Maximum price, in the search's local currency (your AliExpress
            site currency, e.g. UAH — not necessarily ALIEXPRESS_CURRENCY). 0 disables.
        min_rating: Minimum rating (0-5). 0 disables filter.
        sort_by: Search sort ("orders", "best_match", "price_asc", "price_desc").
        ship_from: Two-letter warehouse country (e.g. "ES", "PL", "CN"). Empty = any.
    """
    try:
        products, total_results, notes = search_with_notes(query, sort_by, ship_from)
        products = apply_sort(products, sort_by)
    except RuntimeError as e:
        return str(e)

    deals = [p for p in products if p.get("discount_pct")]
    if min_discount > 0:
        deals = [p for p in deals if p["discount_pct"] >= min_discount]
    if max_price > 0:
        deals = [p for p in deals if p.get("price") is not None and p["price"] <= max_price]
    if min_rating > 0:
        deals = [p for p in deals if p.get("rating") and p["rating"] >= min_rating]

    if not deals:
        floor = f" ≥ {min_discount}%" if min_discount > 0 else ""
        return f"No discounted listings{floor} found for '{query}'."

    deals.sort(key=lambda p: p["discount_pct"], reverse=True)
    # Say what is actually printed. The header claimed "Found 58 deal(s)" while the
    # body carried 25 — the same header-disagrees-with-body bug already fixed in
    # search_products, still living in its sibling.
    limit, broad_note = _row_limit(deals, query, max(1, min(int(max_results or 25), 60)))
    if broad_note:
        notes = list(notes) + [broad_note]
    shown = min(len(deals), limit)
    head = (f"Showing {shown} of {len(deals)} deal(s) for '{query}'"
            if shown < len(deals) else f"Found {len(deals)} deal(s) for '{query}'")
    where = "/".join(normalize_ship_from(ship_from))
    if where:
        head += f", ships from {where}"
    body = _format_product_lines(deals, head + " (biggest discount first):", limit=limit)
    return "\n".join(notes + [""] + [body]) if notes else body


@mcp.tool(
    title="Get Product Details",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def get_product_details(item_id: str = "", url: str = "") -> str:
    """
    Get detailed info for a specific AliExpress product.

    A `Price:` line showing a range means the listing has several configurations —
    call get_variants for the per-config price table. The store's own page URL is
    not returned: no tool here accepts one, and get_seller answers store questions
    from the same item_id.

    Args:
        item_id: AliExpress item ID (e.g., "1005007655628250")
        url: Full product URL (alternative to item_id)
    """
    item_id = _resolve_item_id(item_id, url)
    if not item_id:
        return "Provide a valid item_id or AliExpress product URL (short a.aliexpress.com links work too)."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    # Primary path: MTOP signed API
    d: Optional[dict] = None
    try:
        resp = _fetch_pdp_mtop(item_id)
        if resp:
            err = _pdp_error_code(resp)
            if err:
                return _pdp_unavailable_msg(item_id, err)
            d = _extract_pdp_fields(resp, item_id)
    except Exception as e:
        logger.warning("MTOP PDP fetch failed: %s", e)

    # Fallback: HTML scrape (rarely useful since PDP is CSR, but kept for robustness)
    if not d or not (d.get("title") or d.get("price")):
        client = get_client(referer=f"{BASE_URL}/")
        try:
            r = client.get(f"/item/{item_id}.html")
            if check_auth_redirect(r):
                return AUTH_EXPIRED_MSG
            if r.status_code == 404:
                return f"Product not found: item_id {item_id}."
            r.raise_for_status()
            html_d = parse_product_detail(r.text, item_id)
            if not d:
                d = html_d
            else:
                for k, v in html_d.items():
                    if not d.get(k) and v:
                        d[k] = v
        finally:
            client.close()

    if not d or not (d.get("title") or d.get("price")):
        return (
            f"Could not extract product data for item {item_id}. "
            "The MTOP API returned no data — session may be expired or the item "
            "may be region-restricted. Try re-saving credentials."
        )

    cur = d.get("currency")
    lines = [f"# {d.get('title') or item_id}"]
    lines.append(f"URL: {d['url']}")
    if d.get("price") is not None:
        if d.get("price_range"):
            lo, hi = d["price_range"]
            line = f"Price: {_fmt_money(lo, cur)}–{_fmt_money(hi, cur)}"
        else:
            line = f"Price: {_fmt_money(d['price'], cur)}"
        if d.get("original_price") and d["original_price"] > d["price"]:
            line += f" (was {d['original_price']:.2f}"
            if d.get("discount_pct"):
                line += f", -{d['discount_pct']}%"
            line += ")" + _msrp_flag(d.get("discount_pct"))
            sd = d.get("seller_discount_pct")
            if sd is not None and d.get("discount_pct") is not None and sd != d["discount_pct"]:
                line += f" (seller says -{sd}%)"
        lines.append(line)
        if d.get("lot_note"):
            lines.append(f"  Lot listing — {d['lot_note']}")
        if d.get("price_range"):
            # Say which configuration sits at each end: the cheap end is often a
            # stripped SKU nobody wants and the dear end is often a placeholder.
            lo_spec, hi_spec = d.get("price_low_spec"), d.get("price_high_spec")
            if lo_spec or hi_spec:
                lines.append(f"  Cheapest config: {lo_spec or 'unnamed'}"
                             f"   ·   Dearest: {hi_spec or 'unnamed'}")
    if d.get("rating"):
        rating_line = f"Rating: ★{d['rating']}"
        if d.get("review_count"):
            rating_line += f" ({d['review_count']} reviews)"
        lines.append(rating_line)
    if d.get("sold_count"):
        sold_line = f"Sold: {d['sold_count']}"
        # Spell the number out only for the abbreviated forms ("100K+"), where a
        # reader comparing against a plain "5,000+" could get the magnitude wrong.
        if d.get("sold_count_num") and re.search(r"[KM]", d["sold_count"], re.IGNORECASE):
            sold_line += f" (≈{d['sold_count_num']:,})"
        lines.append(sold_line)
    if d.get("seller_name"):
        seller_line = f"Seller: {d['seller_name']}"
        if d.get("seller_positive_rate"):
            seller_line += f" — {d['seller_positive_rate']}% positive feedback"
        if d.get("seller_total_reviews"):
            seller_line += f" ({d['seller_total_reviews']} seller feedbacks)"
        # Store age was the one figure this block lacked, so a research session
        # comparing sellers had to spend a second call per item on get_seller
        # purely to learn it. It is also the figure that most changes how the
        # rest reads: 100% across 10 feedbacks means something very different on
        # a store opened last month than on a five-year-old one.
        if d.get("seller_opened"):
            age = f", {d['seller_opened_years']} yr" if d.get("seller_opened_years") else ""
            seller_line += f" — opened {d['seller_opened']}{age}"
        lines.append(seller_line)
        if d.get("seller_aggregated"):
            lines.append(
                f"  ⚠ Aggregation listing: the shop card names "
                f"{d.get('seller_listed_name') or 'another store'}; the seller of record "
                "per the page's EU trader disclosure is the store above. Feedback and "
                "age figures describe the pooled listing, not this merchant, so they "
                "are omitted here rather than misattributed.")
    if d.get("ship_unreachable"):
        lines.append(f"Shipping: does not ship to {COUNTRY}")
    elif d.get("shipping_cost") is not None:
        lines.append("Shipping: " + ("Free" if d["shipping_cost"] == 0 else _fmt_money(d["shipping_cost"], cur)))
    else:
        lines.append("Shipping: not available (AliExpress needs a saved delivery address to quote it)")
    for alt in d.get("shipping_alternatives") or []:
        bits = []
        if alt.get("cost") is not None:
            bits.append("Free" if alt["cost"] == 0 else _fmt_money(alt["cost"], cur))
        span = _delivery_days({"ship_days_min": alt.get("days_min"), "ship_days_max": alt.get("days_max")})
        if span:
            bits.append(span)
        if bits:
            lines.append(f"  Alt: {alt.get('company') or 'other carrier'} — " + ", ".join(bits))
    # Printed verbatim as AliExpress formatted it — it is an order-level threshold,
    # not this item's freight, so it must never read as the shipping cost.
    if d.get("free_shipping_over"):
        lines.append(f"  Free shipping on orders over {d['free_shipping_over']}")
    eta_days = _delivery_days(d)
    if d.get("shipping_estimate"):
        eta_line = f"Estimated delivery: {d['shipping_estimate']}"
        if eta_days:
            eta_line += f" ({eta_days})"
        lines.append(eta_line)
    elif eta_days:
        lines.append(f"Estimated delivery: {eta_days}")
    if d.get("ship_from"):
        origin = f"Ships from: {d['ship_from']}"
        if d.get("ship_from_code"):
            origin += f" ({d['ship_from_code']})"
        lines.append(origin)
    tax_note = _informative_tax_note(d.get("tax_note"), d.get("ship_from_code"))
    if tax_note:
        # Once the duty clause is dropped as redundant, only the VAT half is left
        # and "Duties:" would be the wrong word for it.
        label = "Duties" if any(w in tax_note.lower() for w in ("dut", "import charge", "customs")) else "Tax"
        lines.append(f"{label}: {tax_note}")
    return "\n".join(lines)


@mcp.tool(
    title="Get Shipping Estimate",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def get_shipping_estimate(item_id: str = "", url: str = "") -> str:
    """
    Check shipping time and cost for a product to the configured country.

    Note: AliExpress computes freight against the delivery address saved in your
    session; without one it may default elsewhere and report the item as unreachable.

    Args:
        item_id: AliExpress item ID (e.g., "1005007655628250").
        url: Full or short AliExpress product URL (alternative to item_id).
    """
    item_id = _resolve_item_id(item_id, url)
    if not item_id:
        return "Provide a valid item_id or AliExpress product URL (short a.aliexpress.com links work too)."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    try:
        resp = _fetch_pdp_mtop(item_id)
    except Exception as e:
        return f"MTOP call failed: {e}"

    if not resp:
        return (
            f"Could not fetch shipping data for item {item_id} — MTOP returned "
            "no usable response. Try re-saving AliExpress credentials."
        )

    err = _pdp_error_code(resp)
    if err:
        return _pdp_unavailable_msg(item_id, err)

    d = _extract_pdp_fields(resp, item_id)
    # A day range on its own is real shipping information, so it must both keep the
    # tool from bailing out below and get printed — this tool had no day-range
    # branch at all, and answered "set a delivery address" for listings that had
    # told us "19–25 days".
    eta_days = _delivery_days(d)
    if d.get("shipping_cost") is None and not d.get("shipping_estimate") and not eta_days:
        if d.get("ship_unreachable"):
            return (
                f"Item {item_id} does not ship to {COUNTRY} (AliExpress marked the "
                "destination unreachable). Ship-from: " + (d.get("ship_from") or "unknown") + "."
            )
        return (
            f"Shipping info not present in API response for item {item_id}. "
            "AliExpress computes freight against your saved delivery address — set one "
            "on the site and re-save cookies if this persists."
        )

    lines = [f"Shipping to {COUNTRY} for item {item_id}:"]
    if d.get("shipping_cost") is not None:
        lines.append("  Cost: " + ("Free" if d["shipping_cost"] == 0 else _fmt_money(d["shipping_cost"], d.get("currency"))))
    if d.get("free_shipping_over"):
        lines.append(f"  Free shipping on orders over {d['free_shipping_over']}")
    # Paid express options sit in the later layouts; without them the caller
    # cannot see that 4-day delivery is even purchasable.
    for alt in d.get("shipping_alternatives") or []:
        bits = []
        if alt.get("cost") is not None:
            bits.append("Free" if alt["cost"] == 0 else _fmt_money(alt["cost"], d.get("currency")))
        span = _delivery_days({"ship_days_min": alt.get("days_min"), "ship_days_max": alt.get("days_max")})
        if span:
            bits.append(span)
        if bits:
            lines.append(f"  Alt: {alt.get('company') or 'other carrier'} — " + ", ".join(bits))
    if d.get("shipping_estimate"):
        lines.append(f"  Estimated delivery: {d['shipping_estimate']}"
                     + (f" ({eta_days})" if eta_days else ""))
    elif eta_days:
        lines.append(f"  Estimated delivery: {eta_days}")
    if d.get("ship_from"):
        lines.append(f"  Ships from: {d['ship_from']}")
    return "\n".join(lines)


@mcp.tool(
    title="Get Reviews",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def get_reviews(item_id: str = "", url: str = "", max_reviews: int = 10, filter_by: str = "all") -> str:
    """
    Fetch buyer reviews and the rating breakdown for an AliExpress product.

    Reading the output: the `5★:… 4★:…` histogram is the full aggregate — positive
    / neutral / negative percentages are just that histogram re-divided, so they are
    not repeated. If an "All shown reviews are for: …" line appears, it is the
    variant string every shown review shares, and the `…` in it is filled in by the
    `(…)` on each review below. A review body ending in "… (+N chars)" was clipped
    at ~250 characters.

    Args:
        item_id: AliExpress item ID (e.g., "1005007655628250").
        url: Full product URL (alternative to item_id).
        max_reviews: How many individual reviews to include (default 10).
        filter_by: Review filter — "all" (default), "additional", "local",
                   "with_picture". Unknown values fall back to all reviews.
    """
    item_id = _resolve_item_id(item_id, url)
    if not item_id:
        return "Provide a valid item_id or AliExpress product URL (short a.aliexpress.com links work too)."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    page_size = min(max(max_reviews, 10), 50)
    data = _fetch_reviews(item_id, page=1, page_size=page_size, filt=filter_by, cookies=cookies)
    if data is None:
        return f"Could not fetch reviews for item {item_id} (no usable response)."

    r = _extract_reviews(data, max_reviews)
    if not r["stats"] and not r["reviews"]:
        return f"No reviews found for item {item_id}."

    lines = [f"Reviews for item {item_id}:"]
    st = r["stats"]
    if st:
        head = f"★{st['average']} average"
        if st.get("total") is not None:
            head += f" from {st['total']} ratings"
        lines.append(head)
        sd = st.get("stars") or {}
        breakdown = "  ".join(f"{k}★:{sd[k]}" for k in (5, 4, 3, 2, 1) if sd.get(k) is not None)
        # The positive/neutral/negative percentages are AliExpress's own arithmetic
        # over the very histogram printed on the next line — 466+21 of 502 IS the
        # 97.0% positive it reports, and 5/502 IS the 1.0% neutral. Printing both is
        # printing the same five numbers twice, and the histogram is the more
        # informative of the two (it separates 1★ from 2★). The rate line is kept
        # only when the histogram is missing, where it is the sole aggregate left.
        if breakdown:
            lines.append("  " + breakdown)
        elif st.get("positive_rate") is not None:
            neu = st.get("neutral_rate")
            neu_str = f" · {neu}% neutral (3★)" if neu is not None else ""
            lines.append(f"  {st['positive_rate']}% positive{neu_str} · {st.get('negative_rate', 0)}% negative")

    if r["reviews"]:
        skus = [rv["sku"] for rv in r["reviews"] if rv.get("sku")]
        pre, suf = _common_sku_affixes(skus) if len(skus) == len(r["reviews"]) else ("", "")
        if len(pre) + len(suf) < 8:
            pre = suf = ""
        if pre or suf:
            if len(set(skus)) == 1:
                lines.append(f"All shown reviews are for: {skus[0]}")
            else:
                lines.append(f"All shown reviews are for: {pre.rstrip()}…{suf}")
        lines.append("")
        for rv in r["reviews"]:
            star = f"★{rv['stars']}" if rv["stars"] is not None else "★?"
            head = f"- {star}"
            if rv.get("country"):
                head += f" · {rv['country']}"
            if rv.get("date"):
                head += f" · {rv['date']}"
            lines.append(head)
            if rv.get("sku"):
                residual = rv["sku"][len(pre):len(rv["sku"]) - len(suf) if suf else None].strip()
                if residual:
                    lines.append(f"  ({residual})")
            if rv.get("text"):
                lines.append(f"  {_clip_review(rv['text'])}")
            if rv.get("up_votes"):
                lines.append(f"  👍 {rv['up_votes']}")
    return "\n".join(lines)


@mcp.tool(
    title="Get Seller",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def get_seller(item_id: str = "", url: str = "") -> str:
    """
    Get the store / seller profile behind an AliExpress product: positive-feedback
    rate, feedback volume, how long the store has been open, and where it ships from.

    AliExpress's own "seller level" and "seller score" are deliberately not
    reported — it publishes no scale for either, so they cannot be compared.

    Args:
        item_id: AliExpress item ID (e.g., "1005007655628250").
        url: Full product URL (alternative to item_id).
    """
    item_id = _resolve_item_id(item_id, url)
    if not item_id:
        return "Provide a valid item_id or AliExpress product URL (short a.aliexpress.com links work too)."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    try:
        resp = _fetch_pdp_mtop(item_id)
    except Exception as e:
        return f"MTOP call failed: {e}"
    if not resp:
        return (
            f"Could not fetch seller info for item {item_id} — MTOP returned no data. "
            "Try re-saving AliExpress credentials."
        )

    err = _pdp_error_code(resp)
    if err:
        return _pdp_unavailable_msg(item_id, err)

    # The WHOLE result, not just SHOP_CARD_PC. On an "aggregation" listing the
    # shop card names a shell store that never sees the order, and the real
    # merchant is only in the EU trader-identification block — so a parser handed
    # the shop card alone cannot possibly get this right. Passing the narrower
    # object is exactly how this tool reported six unrelated products as all
    # coming from one 10-feedback store while the user's cart named six different
    # merchants; the cart was right, and money was spent on the wrong answer.
    d = _extract_seller(resp.get("data", {}).get("result", {}))
    if not d["store_name"]:
        return f"No seller info found for item {item_id}."

    lines = [f"Seller for item {item_id}:", f"Store: {d['store_name']}"]
    if d.get("aggregated"):
        lines.append(
            f"  ⚠ Aggregation listing: the product page's shop card names "
            f"{d.get('listed_store_name') or 'another store'}, but the seller of "
            "record — per the page's own EU trader disclosure — is the store above. "
            "The rating and feedback figures on such a page describe the pooled "
            "listing, not this merchant, so they are omitted rather than "
            "misattributed.")
        # There is no substitute figure to offer: the merchant's own rating,
        # volume and age live on their store page, and that page returns no
        # payload this server can parse (probed live Aug 2026 — the store name
        # is in the HTML, the structured profile is not). Rather than invent a
        # number or stay silent, hand over the one thing that does answer the
        # question: the URL. This is the case where "is this seller legit"
        # matters MOST, and the honest answer is currently "open this yourself".
        if d.get("store_url"):
            lines.append(f"  Judge the merchant here: {d['store_url']}")
    if d.get("positive_rate") is not None:
        pr = f"Positive feedback: {d['positive_rate']}%"
        if d.get("total_reviews") is not None:
            pr += f" (across {d['total_reviews']} seller feedbacks)"
        lines.append(pr)
    elif d.get("total_reviews") is not None:
        lines.append(f"Seller feedbacks: {d['total_reviews']}")
    # `sellerLevel` and `sellerScore` are dropped, not merely hidden: AliExpress
    # publishes no scale for either, and the live values make that plain — level
    # came back as the string "23-s" on one store and "0" on another, score as
    # 4271 with no stated maximum. A number nobody can place on a scale is not a
    # number a shopper can reason with. Positive-feedback %, feedback volume and
    # store age below are all self-describing, and they stay.
    if d.get("opened"):
        age = f" ({d['opened_years']} yr)" if d.get("opened_years") else ""
        lines.append(f"Opened: {d['opened']}{age}")
    if d.get("country"):
        lines.append(f"Ships from: {d['country']}")
    flags = []
    if d.get("top_rated"):
        flags.append("Top-rated seller")
    if d.get("local_seller"):
        flags.append("Local seller")
    if flags:
        lines.append(" · ".join(flags))
    return "\n".join(lines)


@mcp.tool(
    title="Compare Sellers",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def compare_sellers(title: str = "", item_id: str = "", url: str = "", max_candidates: int = 6) -> str:
    """
    Find which SELLERS offer the same product and rank them by how established each
    store is — longest-running, then highest feedback volume, then best positive rate.
    Read-only.

    AliExpress lists the same item under many storefronts (the original brand store,
    relisters, dropshippers). This surfaces the oldest / highest-volume seller so you
    can prefer them over a brand-new relister. It costs one search plus one lookup per
    candidate, so keep max_candidates modest.

    Candidates are matched by a TITLE SEARCH, so confirm each listing is the exact
    product and configuration you want before buying — same title does not mean
    same SKU. Each row ends with `item <item_id>`; store page URLs are not returned
    because no tool here takes one.

    Args:
        title: Product title / keywords to search (e.g. "AOOSTAR GEM10 mini pc").
        item_id: Alternatively, an item whose title is used as the search query.
        url: Full or short AliExpress product URL (alternative to item_id).
        max_candidates: How many top search hits to inspect (default 6, capped at 10).
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    query = (title or "").strip()
    if not query and (item_id or url):
        rid = _resolve_item_id(item_id, url)
        if rid:
            try:
                resp = _fetch_pdp_mtop(rid)
                if resp:
                    query = (_extract_pdp_fields(resp, rid).get("title") or "").strip()
            except Exception:
                query = ""
    if not query:
        return "Provide a product title, or an item_id/url whose title I can look up."

    max_candidates = min(max(max_candidates, 1), 10)
    try:
        # Titles here are usually the keyword-stuffed originals, and AliExpress
        # 404s a long enough one outright — a real 2600-piece resistor-pack title
        # returned "No listings found" while a shorter form of the SAME phrase
        # returned six near-identical hits. search_by_title walks down a ladder of
        # progressively shorter queries and reports which rung answered.
        products, query, notes = search_by_title(query)
    except RuntimeError as e:
        return str(e)
    if not products:
        # Name the query that actually ran: told "nothing found" for the full
        # title, a caller cannot tell an absent product from an over-long query.
        return f"No listings found for '{query}'."

    # Inspect the top hits: read each one's seller, keep its listing price. Collapse
    # multiple listings from the same store, keeping that store's cheapest.
    sellers: dict[str, dict] = {}
    inspected = 0
    for p in products[:max_candidates]:
        try:
            resp = _fetch_pdp_mtop(p["item_id"])
        except Exception:
            continue
        if not resp:
            continue
        inspected += 1
        s = _extract_seller(resp.get("data", {}).get("result", {}))
        if not s.get("store_name"):
            continue
        key = str(s.get("store_id") or s["store_name"])
        cand = {"seller": s, "title": p["title"], "price": p.get("price"),
                "currency": p.get("currency"), "item_id": p["item_id"]}
        rec = sellers.get(key)
        if rec is None or (cand["price"] is not None and (rec["price"] is None or cand["price"] < rec["price"])):
            sellers[key] = cand

    if not sellers:
        return (
            f"Searched '{query}' but couldn't read seller info for the top {max_candidates} "
            "hits (they may be region-gated). Try get_seller on a specific item_id."
        )

    def rank_key(c: dict) -> tuple:
        s = c["seller"]
        yrs = s.get("opened_years") or 0
        vol = s.get("total_reviews") or s.get("positive_num") or 0
        pos = s.get("positive_rate") or 0
        return (-float(yrs), -float(vol), -float(pos))

    ranked = sorted(sellers.values(), key=rank_key)
    # `query` is whatever rung of the shortening ladder actually answered, not
    # necessarily what the caller passed — naming it here is what lets them see
    # that the match was made on a trimmed phrase.
    lines = list(notes) + ([""] if notes else []) + [
        f'Sellers offering "{query[:60]}" '
        f"({len(ranked)} store(s) across the top {inspected} hits, most-established first):",
        "",
    ]
    for i, c in enumerate(ranked):
        s = c["seller"]
        tag = "  ✅ most established" if i == 0 and len(ranked) > 1 else ""
        lines.append(f"- {s['store_name']}{tag}")
        stats = []
        if s.get("opened_years"):
            stats.append(f"{s['opened_years']} yr old")
        elif s.get("opened"):
            stats.append(f"since {s['opened']}")
        if s.get("total_reviews") is not None:
            stats.append(f"{s['total_reviews']} feedbacks")
        if s.get("positive_rate") is not None:
            stats.append(f"{s['positive_rate']}% positive")
        if s.get("top_rated"):
            stats.append("top-rated")
        if stats:
            lines.append("  " + " · ".join(stats))
        price_str = _fmt_money(c["price"], c.get("currency")) if c.get("price") is not None else "price N/A"
        lines.append(f"  {price_str} — item {c['item_id']}")
    return "\n".join(lines)


@mcp.tool(
    title="Get Variants",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def get_variants(item_id: str = "", url: str = "") -> str:
    """
    List every buyable configuration (SKU) of an AliExpress product with its own
    price — e.g. "DDR4 32GB 1TB SSD · R7 5825U" → 749.36. This is the price→spec
    map that `get_product_details`' price range can't give you.

    Reading the output: `[sku_id: …]` is what add_to_cart needs to pick that exact
    config. `← DEFAULT` marks the config add_to_cart buys when no sku_id is
    passed — it is usually the cheapest and rarely the one the user meant, so
    pass an explicit sku_id whenever size, colour, length or gender matters. A
    trailing `img#N` appears only when the configs do not all share one photo;
    rows with the same N share a photo, so a listing whose "Color" values each
    carry their own N is often a grab-bag of unrelated products sold under one
    item_id. A discount identical on every config is stated once under the
    header instead of on every row.

    Args:
        item_id: AliExpress item ID (e.g., "1005009686220027").
        url: Full or short AliExpress product URL (alternative to item_id).
    """
    item_id = _resolve_item_id(item_id, url)
    if not item_id:
        return "Provide a valid item_id or AliExpress product URL (short a.aliexpress.com links work too)."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    try:
        resp = _fetch_pdp_mtop(item_id)
    except Exception as e:
        return f"MTOP call failed: {e}"
    if not resp:
        return (
            f"Could not fetch variants for item {item_id} — MTOP returned no data. "
            "Try re-saving AliExpress credentials."
        )

    err = _pdp_error_code(resp)
    if err:
        return _pdp_unavailable_msg(item_id, err)

    result = resp.get("data", {}).get("result", {})
    variants = _extract_variants(result)
    if not variants:
        return f"No variant/SKU data for item {item_id} (likely a single-configuration listing)."

    variants.sort(key=lambda v: (v["price"] is None, v["price"] or 0))

    # Drop spec dimensions that are identical across every variant — they carry no
    # distinguishing info (e.g. "China Mainland" on all 12 rows) and just add noise.
    # Keep them when they vary (e.g. ship-from being the only difference).
    part_sets = [set(v.get("spec_parts") or []) for v in variants]
    common = set.intersection(*part_sets) if len(part_sets) > 1 else set()
    for v in variants:
        kept = [p for p in (v.get("spec_parts") or []) if p not in common]
        v["display_spec"] = " · ".join(kept) if kept else (v.get("spec") or v["sku_id"])
        # Prefer the "Axis: Value" form — it reveals when a seller is pushing
        # unrelated products through a dimension like Color.
        labels = v.get("label_parts") or []
        kept_labels = [l for l in labels if l.split(": ", 1)[-1] not in common]
        if kept_labels:
            v["display_spec"] = " · ".join(kept_labels)

    # Per-variant images only help when they actually differ; on a normal listing
    # every SKU shares one image and printing it 20 times is pure noise.
    #
    # The consumer of this string is blind and cannot open a URL, so the URL text
    # itself was never the payload — the payload is *which rows share a photo*,
    # which is how a grab-bag listing (unrelated products hidden behind "Color")
    # shows itself. Measured over 22 live listings: images varied on 14 of them,
    # costing 177 lines / 16,269 chars of URLs. Numbering the distinct photos
    # keeps the whole grouping for ~7 chars a row instead of ~92.
    img_ix: dict[str, int] = {}
    for v in variants:
        im = v.get("image")
        if im and im not in img_ix:
            img_ix[im] = len(img_ix) + 1
    images_vary = len(img_ix) > 1

    priced = [v["price"] for v in variants if v["price"] is not None]
    cur = next((v["currency"] for v in variants if v.get("currency")), None) or CURRENCY
    total_skus = sum(len(v.get("covered_skus") or [v["sku_id"]]) for v in variants)
    header = f"Variants for item {item_id} ({len(variants)} configs"
    if total_skus > len(variants):
        # Say so rather than letting "9 configs" stand for 10 real SKUs.
        header += f" covering {total_skus} SKUs"
    if priced:
        header += f", {_fmt_money(min(priced), cur)}–{_fmt_money(max(priced), cur)}"
    header += "):"

    # Nearly every listing gives every config the same discount off the same kind
    # of crossed-out price (9 of 22 live listings sampled had one identical
    # percentage on every discounted row). Where that holds, "(was 44.99, -50%)"
    # 64 times is one fact repeated 64 times, so state it once. Where the
    # percentages actually differ, the per-row form stays — a config at -70%
    # beside one at -20% is exactly the comparison worth spending tokens on.
    discounts = [
        round((1 - v["price"] / v["original_price"]) * 100)
        for v in variants
        if v.get("original_price") and v.get("price") and v["original_price"] > v["price"]
    ]
    uniform_discount = discounts[0] if len(discounts) > 1 and len(set(discounts)) == 1 else None

    lines = [header]
    if uniform_discount is not None:
        scope = "every config" if len(discounts) == len(variants) else f"{len(discounts)} of {len(variants)} configs"
        lines.append(f"  {scope} listed at -{uniform_discount}% off the crossed-out price"
                     f"{_msrp_flag(uniform_discount)}")
    # On a lot listing every price below buys a LOT, not one piece, so the rows
    # are not comparable with a single-unit listing until the lot size is stated.
    # AliExpress supplies the per-unit breakdown per SKU; use its own strings.
    lot_block = result.get("LOT") if isinstance(result.get("LOT"), dict) else {}
    lot_map = lot_block.get("unitContentSkuMap") if isinstance(lot_block.get("unitContentSkuMap"), dict) else {}
    if not lot_map:
        whole_lot_note = _lot_note(result)
        if whole_lot_note:
            lines.append(f"  ⓘ Lot listing — {whole_lot_note}; prices below are per lot.")
    for v in variants:
        vc = v.get("currency") or cur
        price_str = _fmt_money(v["price"], vc) if v["price"] is not None else "price N/A"
        line = f"- {v.get('display_spec') or v['sku_id']} — {price_str}"
        lot_unit = lot_map.get(str(v["sku_id"])) if lot_map else None
        if isinstance(lot_unit, str) and lot_unit.strip():
            head, sep, tail = lot_unit.strip().partition(",")
            line += f" ({head.strip()}, {tail.strip()})" if sep and tail.strip() else f" ({lot_unit.strip()})"
        if (uniform_discount is None and v.get("original_price") and v["price"]
                and v["original_price"] > v["price"]):
            disc = round((1 - v["price"] / v["original_price"]) * 100)
            line += f" (was {v['original_price']:.2f}, -{disc}%){_msrp_flag(disc)}"
        covered = v.get("covered_skus") or [(v["sku_id"], v["in_stock"], v.get("stock"))]
        if not v["in_stock"]:
            line += " ⚠ out of stock"
        elif len(covered) == 1 and v.get("stock") == 0:
            line += " ⚠ out of stock"
        elif len(covered) == 1 and isinstance(v.get("stock"), int):
            line += f", {v['stock']} in stock"
        # The sku_id is what add_to_cart needs to pick this exact configuration —
        # without it the caller can only ever add the item's preselected variant.
        if len(covered) == 1:
            line += f"  [sku_id: {v['sku_id']}]"
        else:
            line += "  [sku_ids: " + ", ".join(s for s, _ok, _st in covered) + "]"
        if images_vary and v.get("image"):
            line += f" img#{img_ix[v['image']]}"
        # Which config add_to_cart buys when no sku_id is passed. Without this
        # the fallback is invisible: the caller cannot tell which row it would
        # get, and it is usually the cheapest rather than the intended one —
        # that is how one session added 28 AWG wire instead of 22 and male
        # headers instead of female. On a collapsed row the default may not be
        # the row's own sku_id, so name the exact id rather than implying it.
        if v.get("is_default"):
            line += " ← DEFAULT"
            if v.get("default_sku_id") and v["default_sku_id"] != v["sku_id"]:
                line += f" (sku {v['default_sku_id']})"
        lines.append(line)
        if len(covered) > 1:
            # Never let one of these read as "the" sku_id for the row: they are
            # different SKUs, and add_to_cart would buy whichever one it is given.
            def _stock_note(ok, st):
                if not ok or st == 0:
                    return "out of stock"
                return f"{st} in stock" if isinstance(st, int) else "in stock"
            lines.append(
                f"    ⚠ {len(covered)} distinct SKUs share this spec and price — AliExpress "
                "did not label what differs between them (usually plug, region or an "
                "unnamed colour). Pick one only if you know which; otherwise check the "
                "product page:"
            )
            lines.append("      " + " · ".join(f"{s} ({_stock_note(ok, st)})" for s, ok, st in covered))

    return "\n".join(lines)


def _add_one_to_cart(cookies: dict, item_id: str, sku_id: str = "",
                     quantity: int = 1) -> dict:
    """
    Add a single line to the cart and report structurally what happened.

    Split out of add_to_cart so the bulk tool runs the exact same code rather
    than a parallel near-copy — the two paths drifting apart is how the anti-bot
    handling ended up implemented twice and inconsistently.

    Returns {ok, challenged, descr, text, cart_num, cart_id}. `challenged` is
    called out separately from ordinary failure because it is the one condition
    where continuing is actively harmful: the block does not lift on its own, so
    a caller in a loop must stop rather than spend the rest of its items
    confirming the same wall.
    """
    out = {"ok": False, "challenged": False, "descr": f"item {item_id}",
           "text": "", "cart_num": None, "cart_id": None, "warn": None}

    sku_id, service, spec, unit_price, unit_currency, err, warn = _resolve_sku_for_cart(item_id, sku_id)
    if err:
        out["text"] = err
        return out
    out["warn"] = warn

    # Confirm in terms a person can check against what they meant to buy. A bare
    # sku_id cannot be checked at all: one real session made three wrong-variant
    # adds in a row — 28 AWG wire instead of 22, male headers instead of female,
    # 10x 1mm drill bits instead of a graduated set — and every one of them was
    # invisible in a confirmation that only echoed the id back. The spec/price
    # lookup is best-effort (see _resolve_sku_for_cart), so fall back to the
    # bare id rather than failing an add that actually succeeded.
    descr = f"variant {sku_id}"
    if spec:
        descr = f'"{spec}"'
        if unit_price is not None:
            descr += f" — {_fmt_money(unit_price, unit_currency)}"
        descr += f" [sku {sku_id}]"
    out["descr"] = descr

    add_item: dict[str, Any] = {
        "itemId": str(item_id),
        "skuId": str(sku_id),
        "quantity": quantity,
        "attributes": {"carAdditionalInfo": "{}", "sourceType": ""},
    }
    if service:
        add_item["fulfillmentservice"] = service

    payload = {
        "_saasRegion": "AEG",
        "_currency": CURRENCY,
        "state": "",
        "city": "",
        "shipToCountry": COUNTRY,
        "currency": CURRENCY,
        "locale": "en_US",
        "language": "en",
        "system": "pc",
        "bizParams": json.dumps({"platformType": "DESKTOP"}, separators=(",", ":")),
        "addItems": json.dumps([add_item], separators=(",", ":")),
        "addFrom": "main_detail",
    }

    # Writes are the rate-limited surface, so hold them further apart than reads.
    #
    # There used to be a 6s/15s backoff loop here that absorbed the anti-bot
    # challenge "transparently", and a message telling the caller it "clears by
    # itself — wait a minute or two". Both were wrong, and measured: 45s and 90s
    # waits were tested and BOTH failed; the block cleared only once a human
    # completed the challenge in a logged-in browser tab. So retrying spends the
    # user's rate-limit budget prolonging a block that waiting cannot lift, and
    # the message sent the caller to do exactly that. Fail immediately instead
    # and let ret_problem() say what actually works.
    _pace("cart_write", CART_WRITE_MIN_INTERVAL)
    try:
        resp = mtop_call(
            "mtop.aliexpress.trade.cart.add", "1.0", payload,
            cookies=cookies,
            referer=f"{BASE_URL}/item/{item_id}.html",
            app_key=MTOP_CART_APP_KEY,
        )
    except Exception as e:
        out["text"] = f"MTOP call failed: {e}"
        return out

    ret = (resp.get("ret") or [""])[0]
    data = resp.get("data") or {}

    # Challenge detection lives in ret_problem() alone now — it used to be
    # string-matched here as well, which is how the two copies drifted apart.
    problem = ret_problem(resp)
    if problem is not None:
        out["challenged"] = "human-verification challenge" in problem
        out["text"] = problem
        return out

    if data.get("addFailed"):
        out["text"] = f"AliExpress said: {ret or 'no status returned'}"
        return out

    out["ok"] = True
    out["cart_num"] = data.get("cartNum")
    out["cart_id"] = data.get("cartId")
    return out


@mcp.tool(
    title="Add to Cart",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False,
        openWorldHint=True
    ),
    structured_output=False,
)
def add_to_cart(item_id: str = "", url: str = "", sku_id: str = "", quantity: int = 1) -> str:
    """
    Add an item to your AliExpress cart. This WRITES to your real account.

    Does not buy anything — the item sits in the cart until you check out on the
    site yourself. To remove an item you no longer want, use remove_from_cart.

    Args:
        item_id: AliExpress item ID (e.g., "1005007655628250").
        url: Full or short AliExpress product URL (alternative to item_id).
        sku_id: Specific variant to add, as printed by get_variants
            (`[sku_id: ...]`). Defaults to the item's preselected variant, which
            is what the product page shows — pass one explicitly whenever the
            options matter (size, colour, length, male/female).
        quantity: How many to add (default 1).
    """
    item_id = _resolve_item_id(item_id, url)
    if not item_id:
        return "Provide a valid item_id or AliExpress product URL (short a.aliexpress.com links work too)."
    if quantity < 1:
        return "quantity must be at least 1."
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    r = _add_one_to_cart(cookies, item_id, sku_id, quantity)
    if not r["ok"]:
        return f"Item {item_id} was NOT added to your cart. {r['text']}"

    lines = [f"Added item {item_id}: {r['descr']} ×{quantity} to your cart."]
    if r["cart_num"] is not None:
        # AliExpress's own running count, echoed as it came back. Under parallel
        # adds it arrives out of order — a five-call batch reported 15, 18, 19,
        # 16, 17 in that sequence, every add having landed. Labelled rather than
        # suppressed: it is useful when calls are serial, and misleading only if
        # you assume it is monotonic.
        lines.append(f"  Cart now holds {r['cart_num']} item(s) "
                     "(AliExpress's count at the moment of this add; not "
                     "ordered under parallel calls).")
    if r["cart_id"] is not None:
        lines.append(f"  Cart line ID: {r['cart_id']}")
    if r.get("warn"):
        lines.append(f"  ⚠ {r['warn']}.")
    lines.append("  Nothing has been ordered or paid for.")
    return "\n".join(lines)


@mcp.tool(
    title="Add Many to Cart",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False,
        openWorldHint=True
    ),
    structured_output=False,
)
def add_many_to_cart(items: list[dict]) -> str:
    """
    Add several items to your cart in one call, paced. This WRITES.

    Prefer this over looping add_to_cart. Twenty-plus rapid single adds is the
    pattern that trips AliExpress's anti-bot check, and that block does NOT
    clear by waiting — only completing a challenge in a logged-in browser does.
    This spaces the writes and, if a challenge does land, STOPS immediately and
    reports exactly which items made it in, so nothing is added twice on a retry.

    Buys nothing. Each line is confirmed by variant name and price, not just an
    id, so a wrong variant is visible straight away.

    No batch size limit is imposed here, and none has been confirmed on
    AliExpress's side — but a user reported large lists appearing to stop around
    20. Nothing is silent either way: the reply names every item that was added,
    every one that failed, and every one never attempted, so a short result is
    visible rather than assumed. If you do hit a wall, re-run with only the
    items listed as not added.

    Args:
        items: List of items to add. Each entry is an object with:
            item_id (or url), and optionally sku_id (from get_variants — pass it
            whenever size/colour/length matters) and quantity (default 1).
            A bare list of item-id strings is also accepted.
    """
    if not items or not isinstance(items, list):
        return ("Provide a list of items, e.g. "
                '[{"item_id": "1005006", "sku_id": "12000039", "quantity": 2}].')

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    # Snapshot the checkout selection once before and once after the batch.
    # Cart MUTATIONS disturb it, not just selection writes: a session that made
    # no set_cart_selection calls at all watched un-ticked lines go 10 -> 14
    # across one remove and two adds. Two renders per batch is a proportionate
    # price for noticing; doing it per item would double the write-path cost of
    # a twenty-item batch against a rate limiter whose block does not clear by
    # waiting. Nothing is re-asserted automatically — that would be more writes
    # into the same churn; the caller is told, and set_cart_selection converges.
    def _sel_snapshot():
        try:
            merged, _ = _cart_fetch_all_pages(cookies, _cart_droplet_render(cookies))
            return {str(i.get("cart_id")): (bool(i.get("selected")), i.get("title"))
                    for i in _extract_cart_droplet(merged)["items"]}
        except Exception:
            return None

    sel_before = _sel_snapshot()

    added, failed, skipped = [], [], []
    challenge_msg = None

    for n, raw in enumerate(items, 1):
        entry = {"item_id": raw} if isinstance(raw, str) else (raw or {})
        if not isinstance(entry, dict):
            failed.append(f"  #{n}: not an item id or object — {raw!r}")
            continue

        # Once challenged, every remaining call is a guaranteed failure that
        # also deepens the block. Record the rest as untried rather than
        # burning them, so a retry knows exactly where to resume.
        if challenge_msg:
            skipped.append(f"  {entry.get('item_id') or entry.get('url') or f'#{n}'}")
            continue

        item_id = _resolve_item_id(str(entry.get("item_id") or ""), str(entry.get("url") or ""))
        if not item_id:
            failed.append(f"  #{n}: no valid item_id or URL — {raw!r}")
            continue
        try:
            qty = int(entry.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        if qty < 1:
            failed.append(f"  #{n} ({item_id}): quantity must be at least 1")
            continue

        try:
            r = _add_one_to_cart(cookies, item_id, str(entry.get("sku_id") or ""), qty)
        except Exception as e:
            failed.append(f"  {item_id}: {e}")
            continue

        if r["ok"]:
            note = f"  ⚠ {r['warn']}" if r.get("warn") else ""
            added.append(f"  {item_id}: {r['descr']} ×{qty}{note}")
        elif r["challenged"]:
            challenge_msg = r["text"]
            failed.append(f"  {item_id}: blocked by the verification challenge")
        else:
            failed.append(f"  {item_id}: {r['text']}")

    drift = []
    sel_after = _sel_snapshot() if sel_before is not None else None
    if sel_before and sel_after:
        drift = [(cid, sel_before[cid][1]) for cid in sel_before
                 if cid in sel_after and sel_before[cid][0] and not sel_after[cid][0]]

    out = [f"Added {len(added)} of {len(items)} item(s) to your cart."]
    if added:
        out += ["", "Added:"] + added
    if failed:
        out += ["", "Not added:"] + failed
    if skipped:
        out += ["", f"Not attempted ({len(skipped)}) — stopped after the challenge:"] + skipped
    if challenge_msg:
        out += ["", challenge_msg,
                "Re-run with only the items listed as not added or not attempted."]
    if drift:
        out += ["", f"⚠ {len(drift)} line(s) that were ticked for checkout became "
                    "UN-ticked while this batch ran, without being asked to. They stay "
                    "in the cart but will NOT be ordered:"]
        out += [f"    {str(t or c)[:70]}" for c, t in drift]
        out += ["    Run set_cart_selection(selected=True, all_lines=True) to put them "
                "back — it re-reads and converges."]
    out += ["", "Nothing has been ordered or paid for."]
    return "\n".join(out)


# Rounds a converging selection change will attempt before giving up. Each
# round re-reads the cart and rewrites only the lines still wrong, so a stable
# cart finishes in one. More than a handful means the server is fighting us and
# saying so beats looping.
SELECTION_MAX_ROUNDS = 4


@mcp.tool(
    title="Set Cart Selection",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True,
        openWorldHint=True
    ),
    structured_output=False,
)
def set_cart_selection(selected: bool, item_id: str = "", url: str = "",
                       cart_id: str = "", sku_id: str = "",
                       cart_ids: Optional[list] = None,
                       all_lines: bool = False) -> str:
    """
    Tick or un-tick cart lines for checkout. This WRITES to your real account.

    AliExpress orders ONLY the ticked lines. An un-ticked line stays visibly in
    the cart and simply does not arrive, which is not recoverable after
    checkout — so this is how you make sure something you intend to buy is
    actually included. `view_cart` shows which lines are currently un-ticked.

    Three ways to target, cheapest first:
      all_lines=True  — every line in the cart.
      cart_ids=[...]  — several specific lines.
      cart_id / item_id — a single line.

    The list forms CONVERGE: they re-read the cart, re-diff against what you
    asked for, and re-write only what is still wrong, up to a bounded number of
    rounds. That is not belt-and-braces — setting one line's checkbox reliably
    flips others off, so a single pass genuinely does not land. The result
    reports what actually held, verified against a fresh read, and says plainly
    when something refused to stick rather than claiming success.

    Prefer the list forms over calling this once per line: the rate limiter
    guarding these endpoints answers with a challenge that does NOT clear by
    waiting, so every extra write is another chance to get locked out of your
    own cart. One call also reports the whole outcome together.

    Buys nothing and pays nothing; it only changes what a future checkout would
    include. One product can occupy several cart lines (one per variant), so an
    ambiguous `item_id` lists the candidate `cart_id`s and changes nothing.

    Args:
        selected: True to tick (include), False to un-tick.
        item_id: AliExpress item ID of the line to change.
        url: Full or short AliExpress product URL (alternative to item_id).
        cart_id: The cart LINE id, from view_cart — the unambiguous way to
            target a line when one product appears more than once.
        sku_id: Variant id, to disambiguate between lines of the same item.
        cart_ids: Several cart LINE ids to set to the same state in one call.
        all_lines: Set EVERY line in the cart, in a single request.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    # all_lines resolves to the full list of cart_ids and then takes exactly the
    # same per-line path. The cart header does expose a real "select all"
    # checkbox and it would be one write instead of N — but two attempts to
    # drive it failed, the first un-ticking an entire 22-line cart in response
    # to a request to TICK it. A single write is not worth an operation that
    # can silently empty a checkout, so this trades requests for predictability.
    if all_lines:
        try:
            merged, _ = _cart_fetch_all_pages(cookies, _cart_droplet_render(cookies))
            everything = _extract_cart_droplet(merged)["items"]
        except Exception as e:
            return f"Could not read the cart to enumerate its lines: {e}"
        # Target EVERY line, not just the ones currently wrong. The convergence
        # loop below already skips lines that are correct, so this costs nothing
        # extra on a stable cart — but it is the difference between fixing the
        # cart and fixing five lines while five others get knocked off behind
        # you. Targeting only the initially-wrong set did exactly that: it
        # converged its five and returned "5 of 5" on a cart that was still
        # 20 of 25.
        cart_ids = [str(i["cart_id"]) for i in everything if i.get("cart_id")]
        if not cart_ids:
            return "The cart has no lines to select."
        if all(bool(i.get("selected")) == bool(selected)
               for i in everything if i.get("cart_id")):
            state = "ticked" if selected else "un-ticked"
            return (f"All {len(everything)} cart line(s) are already {state} — "
                    "nothing to change, no writes made.")

    if cart_ids:
        # CONVERGE, don't fire-and-hope.
        #
        # Setting one line's checkbox reliably flips OTHER lines off — reported
        # across a whole session and reproduced here (tick 2, watch 2 unrelated
        # lines go un-ticked; the count stays put while the SET churns). It is
        # not our payload: decoded browser captures of real ticks and ours is
        # byte-shape-identical, same two components, same operationType, same
        # checkbox object. Paginated reads were the other suspect and are
        # innocent — page-1 state is unchanged across them.
        #
        # So the churn is server-side and a single pass cannot be trusted to
        # land. Re-read, re-diff and re-write only what is still wrong, until
        # it converges or stops improving. Bounded, because a cart that will
        # not converge must say so rather than loop forever.
        want = bool(selected)
        targets = {str(c) for c in cart_ids}
        done, failed, rounds = set(), [], 0
        prev_wrong = None
        for rounds in range(1, SELECTION_MAX_ROUNDS + 1):
            try:
                merged, _ = _cart_fetch_all_pages(cookies, _cart_droplet_render(cookies))
                state = {str(i.get("cart_id")): i for i in _extract_cart_droplet(merged)["items"]}
            except Exception as e:
                failed.append(f"  could not re-read the cart: {e}")
                break
            wrong = [c for c in targets
                     if c in state and bool(state[c].get("selected")) != want]
            if not wrong:
                break
            # No progress since the last round means writing again will not help.
            if prev_wrong is not None and len(wrong) >= prev_wrong:
                failed.append(
                    f"  stopped after round {rounds}: {len(wrong)} line(s) still wrong and "
                    "the last round changed nothing — AliExpress is not holding this state.")
                break
            prev_wrong = len(wrong)
            for cid in wrong:
                try:
                    line, err, _coll = _cart_set_selected(cookies, "", cid, "", want)
                except Exception as e:
                    failed.append(f"  {cid}: {e}")
                    continue
                if err:
                    failed.append(f"  {cid}: {err}")
                else:
                    done.add(cid)

        try:
            merged, _ = _cart_fetch_all_pages(cookies, _cart_droplet_render(cookies))
            final = {str(i.get("cart_id")): i for i in _extract_cart_droplet(merged)["items"]}
        except Exception:
            final = {}
        landed = [c for c in targets if c in final and bool(final[c].get("selected")) == want]
        stuck = [c for c in targets if c in final and bool(final[c].get("selected")) != want]
        gone = sorted(targets - set(final))

        verb = "Ticked" if want else "Un-ticked"
        out = [f"{verb} {len(landed)} of {len(targets)} line(s), verified against a fresh "
               f"read after {rounds} round(s)."]
        if stuck:
            out += ["", f"⚠ NOT {verb.lower()} — these did not hold:"]
            out += [f"    {str(final[c].get('title') or c)[:70]}" for c in stuck]
        if gone:
            out += ["", "Not in the cart at all:"] + [f"    {c}" for c in gone]
        if failed:
            out += ["", "Problems:"] + failed
        total_ticked = sum(1 for i in final.values() if i.get("selected"))
        if final:
            out += ["", f"Cart now: {total_ticked} of {len(final)} line(s) ticked for checkout."]
        return "\n".join(out)

    if not (item_id or url or cart_id):
        return ("Provide a cart_id (from view_cart), an item_id / product URL, "
                "a cart_ids list, or all_lines=True.")
    if item_id or url:
        item_id = _resolve_item_id(item_id, url) or ""

    try:
        line, err, collateral = _cart_set_selected(
            cookies, item_id, cart_id, sku_id, bool(selected))
    except Exception as e:
        return f"Cart selection change failed: {e}"

    # Never swallow a collateral change, even on the error path. An un-ticked
    # line stays visible in the cart and simply never arrives, so this is the
    # one failure the caller cannot discover for themselves until the parcel is
    # short one item.
    warn = ""
    if collateral:
        warn = ("\n\n⚠ OTHER cart lines changed selection during this call, which should "
                "not happen — re-check view_cart before ordering:\n" + "\n".join(
                    f"    {t or cid}: {'ticked' if b else 'un-ticked'} → "
                    f"{'ticked' if a else 'un-ticked'}"
                    for cid, t, b, a in collateral))
    if err:
        return err + warn

    where = line.get("title") or f"line {line.get('cart_id')}"
    if selected:
        return (f"Ticked for checkout: {where}\n"
                f"  cart_id: {line.get('cart_id')} — it will be included when you order." + warn)
    return (f"Un-ticked: {where}\n"
            f"  cart_id: {line.get('cart_id')} — it stays in your cart but will NOT be ordered." + warn)


@mcp.tool(
    title="Set Cart Quantity",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True,
        openWorldHint=True
    ),
    structured_output=False,
)
def set_cart_quantity(quantity: int, item_id: str = "", url: str = "",
                      cart_id: str = "", sku_id: str = "") -> str:
    """
    Change how many of one cart line you have. This WRITES to your real account.

    Sets an absolute quantity, not a delta — quantity=3 means "three of these",
    whatever it was before. AliExpress caps this per listing; exceeding the cap
    is reported back rather than silently clamped.

    Args:
        quantity: The new absolute quantity (1 or more). To remove a line
            entirely use remove_from_cart, not quantity=0.
        item_id: AliExpress item ID of the line to change.
        url: Full or short AliExpress product URL (alternative to item_id).
        cart_id: Exact cart line id from view_cart — the unambiguous way to
            target one variant when an item occupies several lines.
        sku_id: Variant id, to disambiguate between lines of the same item.
    """
    if quantity < 1:
        return "quantity must be at least 1 — use remove_from_cart to delete a line."
    if not cart_id:
        item_id = _resolve_item_id(item_id, url)
        if not item_id:
            return "Provide a cart_id, or an item_id / AliExpress product URL."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    resp, target, err = _resolve_cart_target(cookies, item_id, cart_id, sku_id)
    if err:
        return err
    if target["qty"] == quantity:
        return f"Cart line {target['cart_id']} is already ×{quantity} — nothing to do."

    try:
        ret = _cart_operate(cookies, resp, target["component_id"], "update_quantity",
                            quantity=quantity)
    except Exception as e:
        return f"Quantity change failed: {e}"
    if ret_problem({"ret": [ret]}) is not None:
        return f"Could not change cart line {target['cart_id']} — AliExpress said: {ret}"

    # A wrong operationType still returns SUCCESS while doing nothing, so the ack
    # alone proves nothing — confirm against a fresh read.
    try:
        after = _cart_lines(_cart_fetch_all_pages(cookies, _cart_droplet_render(cookies))[0])
        now = next((r for r in after if r["cart_id"] == target["cart_id"]), None)
        if not now:
            return f"Cart line {target['cart_id']} disappeared after the change — check view_cart."
        if now["qty"] != quantity:
            return (f"AliExpress accepted the request but cart line {target['cart_id']} is still "
                    f"×{now['qty']}, not ×{quantity}. It may be capped at {now['qty']} for this listing.")
        return (f"Set {target['title']!r} (cart line {target['cart_id']}) "
                f"from ×{target['qty']} to ×{quantity}.")
    except Exception:
        return f"Changed cart line {target['cart_id']} to ×{quantity} (could not re-read to confirm)."


@mcp.tool(
    title="Remove from Cart",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False,
        openWorldHint=True
    ),
    structured_output=False,
)
def remove_from_cart(item_id: str = "", url: str = "", cart_id: str = "", sku_id: str = "") -> str:
    """
    Remove one line from your AliExpress cart. This WRITES to your real account
    and cannot be undone from here — the item would have to be re-added.

    The same product can occupy several cart lines (one per variant), so when
    `item_id` alone matches more than one line this refuses to guess and lists
    the candidates with their cart_id values instead.

    Args:
        item_id: AliExpress item ID of the line to remove.
        url: Full or short AliExpress product URL (alternative to item_id).
        cart_id: Exact cart line id, as shown by view_cart. Wins over item_id
            and is the unambiguous way to target a specific variant.
        sku_id: Variant id, to disambiguate when one item has several lines.
    """
    if not cart_id:
        item_id = _resolve_item_id(item_id, url)
        if not item_id:
            return "Provide a cart_id, or an item_id / AliExpress product URL."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    resp, target, err = _resolve_cart_target(cookies, item_id, cart_id, sku_id)
    if err:
        return err

    try:
        ret = _cart_operate(cookies, resp, target["component_id"], "delete")
    except Exception as e:
        return f"Remove failed: {e}"
    if ret_problem({"ret": [ret]}) is not None:
        return f"Could not remove cart line {target['cart_id']} — AliExpress said: {ret}"

    # Destructive, so confirm against a fresh read rather than trusting the ack.
    try:
        after = _cart_lines(_cart_fetch_all_pages(cookies, _cart_droplet_render(cookies))[0])
        if any(r["cart_id"] == target["cart_id"] for r in after):
            return (f"AliExpress reported success but cart line {target['cart_id']} is still "
                    "present — nothing was removed. Try again or remove it on the site.")
        return (f"Removed {target['title']!r} (cart line {target['cart_id']}) from your cart.\n"
                f"  Cart now holds {len(after)} line(s).")
    except Exception:
        return f"Removed cart line {target['cart_id']} (could not re-read the cart to confirm)."


def _subtotal_by_currency(items: list[dict],
                          default_currency: Optional[str] = None) -> dict[Optional[str], float]:
    """
    Sum priced cart lines, grouped by each line's OWN currency.

    AliExpress renders a line in its listing's currency, so one cart can
    legitimately mix them. Adding USD to SEK produces a figure that is wrong in
    every currency while looking authoritative — the worst kind of number to
    hand someone reconciling a basket, and a real session lost time to a ~310 kr
    discrepancy that did not exist. Grouping makes the mix visible instead.

    `default_currency` is only used for lines that state none themselves; when
    that is also None the key is None, meaning "AliExpress never said".
    """
    out: dict[Optional[str], float] = {}
    for it in items:
        if it.get("price") is None:
            continue
        cur = it.get("currency") or default_currency
        try:
            qty = int(it.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        out[cur] = round(out.get(cur, 0.0) + it["price"] * qty, 2)
    return out


@mcp.tool(
    title="View Cart",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def view_cart() -> str:
    """
    View current AliExpress cart contents (read-only).

    Reading the output: an "Unless a line says otherwise: …" line gives the
    shipping and/or delivery that applies to every line that does not state its
    own. `cart_id` identifies the LINE and is what set_cart_quantity /
    remove_from_cart need; `item_id` identifies only the product, and the same
    product appears on several lines when it is in the cart under more than one
    variant. Store page URLs are not returned — no tool here accepts one.

    Calls the signed MTOP endpoint `mtop.aliexpress.trade.cart.render` v1.0.
    Requires a fresh session — if you see an empty cart despite having items,
    re-save AliExpress cookies via the MCP Auth Bridge extension.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    try:
        resp = mtop_call(
            "mtop.aliexpress.trade.cart.render",
            "1.0",
            {"platformType": "DESKTOP"},
            cookies=cookies,
            referer=f"{BASE_URL}/p/shoppingcart/index.html",
        )
    except Exception as e:
        return f"Cart MTOP call failed: {e}"

    ret = resp.get("ret", [])
    ret_str = ret[0] if ret else ""
    if ret_problem(resp):
        return (
            f"Cart API returned: {ret_str}. "
            "If this says TOKEN_EXPIRED, re-save AliExpress cookies via the MCP Auth Bridge."
        )

    legacy_cart = _extract_cart(resp)
    page_warning = None
    summary: dict[str, Any] = {}
    used_droplet = False

    # The legacy render only ever returns page 1. Re-render in the droplet shape,
    # which is the only one carrying pagination, and walk the rest. Fall back to
    # the legacy result if that path yields nothing, so a shape change upstream
    # degrades to the old behaviour instead of an empty cart.
    try:
        droplet = mtop_call(
            "mtop.aliexpress.trade.cart.render", "1.0",
            {"_saasRegion": "AEG", "_currency": CURRENCY, "shipToCountry": COUNTRY,
             "locale": "en_US", "language": "en", "system": "pc",
             "bizParams": json.dumps({"platformType": "DESKTOP", "pcChoiceNewCart": 1},
                                     separators=(",", ":"))},
            cookies=cookies, referer=f"{BASE_URL}/p/shoppingcart/index.html",
        )
        if ret_problem(droplet) is None:
            droplet, page_warning = _cart_fetch_all_pages(cookies, droplet)
            paged = _extract_cart_droplet(droplet)
            if len(paged["items"]) > len(legacy_cart["items"]):
                # Item prices below now come from the droplet (pay currency). The
                # legacy render's subtotal/shipping/total are USD no matter what
                # `_currency` says, so they are NOT carried over — printing them
                # under this cart's currency label is exactly the bug. The server
                # item count is the one legacy figure that is currency-free and
                # still the more complete of the two.
                paged["count"] = legacy_cart.get("count") or paged.get("count")
                legacy_cart = paged
                used_droplet = True
            summary = _extract_cart_summary(droplet)
    except Exception as e:
        logger.info("cart pagination unavailable, using first page only: %s", e)
        page_warning = f"pagination failed ({e})"

    cart = legacy_cart
    items = cart["items"]
    count = cart["count"]

    if not items:
        if count == 0:
            return (
                "Your AliExpress cart is empty (server count: 0). "
                "If you just added items, re-save AliExpress cookies via the "
                "MCP Auth Bridge extension to refresh the session, then retry."
            )
        return (
            f"Cart render succeeded but no item blocks matched. Server count: {count}. "
            "The cart-line block shape may have changed."
        )

    # The currency AliExpress actually stated for this cart — deliberately NOT
    # defaulted to the configured CURRENCY. The cart renders each line in its
    # listing's own currency, which is often not the configured one, so a
    # fallback here stamps e.g. "SEK" onto a figure the server reported in USD.
    # A real session hit exactly that: a cart reconciled against USD values
    # under a SEK label produced a ~310 kr discrepancy that did not exist, and
    # the same cart read as USD early in the session and SEK later because the
    # droplet and legacy paths labelled money differently. None means "unstated"
    # here, and every place below that prints money copes with that.
    currency = cart.get("currency") or (summary.get("currency") if used_droplet else None)
    n = len(items)

    def _money(amount: float, cur: Optional[str] = None) -> str:
        """Never label an amount with a currency the response did not state."""
        cur = cur or currency
        return _fmt_money(amount, cur) if cur else f"{amount:.2f} (currency unstated)"

    # Honest header. AliExpress paginates the cart (append / infinite-scroll behind
    # an opaque cursor); the render API exposes only the first page, so the shown
    # count can be less than the server's total — say so instead of losing items.
    truncated = bool(count and count > n)
    if truncated:
        header = (
            f"Cart — showing {n} of {count} items.\n"
            f"  ⚠ {count - n} more item(s) aren't listed here"
            + (f" — {page_warning}." if page_warning else
               " — the cart API stopped returning pages before the full count was reached.")
        )
    else:
        header = f"Cart ({n} item(s)):"
    lines = [header, ""]

    # Group items under their shop (first-appearance order) instead of printing
    # a decoupled shop list and item list.
    seen_shops: list[str] = []
    grouped: dict[str, list] = {}
    for it in items:
        k = it.get("shop_name") or "Other seller"
        if k not in grouped:
            grouped[k] = []
            seen_shops.append(k)
        grouped[k].append(it)

    # Shipping and delivery are the same answer on nearly every line of a real
    # cart — the live 24-item cart sampled Aug 2026 said "shipping: Free" on 20
    # lines and "delivery: Aug 16 - 22" on 20 — so state the prevailing value once
    # and let each line override it. Nothing is dropped: a line that differs still
    # carries its own value, and a line AliExpress gave no freight quote for says
    # so explicitly rather than silently inheriting "Free", which is the one way
    # this could mislead.
    def _ship_str(it: dict) -> Optional[str]:
        sc = it.get("shipping_cost")
        if sc is None:
            return None
        return "Free" if sc == 0 else _money(sc, it.get("currency"))

    def _prevailing(vals: list[Optional[str]]) -> Optional[str]:
        """Value shared by enough lines that hoisting it is a net saving."""
        known = [v for v in vals if v]
        if not known:
            return None
        top = max(set(known), key=known.count)
        n = known.count(top)
        # 4 lines and 70% keeps the header plus its exception markers strictly
        # cheaper than repeating the value on every line, at every cart size.
        return top if n >= 4 and n >= 0.7 * len(vals) else None

    common_ship = _prevailing([_ship_str(it) for it in items])
    common_date = _prevailing([it.get("delivery_date") for it in items])
    if common_ship or common_date:
        bits = []
        if common_ship:
            bits.append(f"shipping {common_ship}")
        if common_date:
            bits.append(f"delivery {common_date}")
        lines.append("Unless a line says otherwise: " + " · ".join(bits))
        lines.append("")

    for shop_name in seen_shops:
        group = grouped[shop_name]
        # The store page URL is not printed: no tool in this server accepts one,
        # and the shop name above already groups and identifies the seller.
        lines.append(f"▸ {shop_name}")
        for it in group:
            line = f"  - {it['title']}"
            if it.get("price") is not None:
                line += f" — {_money(it['price'], it.get('currency'))}"
            if it.get("original_price") and it["original_price"] > (it.get("price") or 0):
                line += f" (was {it['original_price']:.2f})"
            qty = it.get("quantity") or 1
            try:
                if int(qty) != 1:
                    line += f" × {qty}"
            except (TypeError, ValueError):
                pass
            if it.get("sku_info"):
                line += f"\n      variant: {it['sku_info']}"
            # AliExpress only orders TICKED lines, and nothing in this output used
            # to say which those were: one real cart showed "Checkout (17)" against
            # 18 lines, so an item the user believed they were buying would simply
            # not have arrived. Only the un-ticked state is called out — marking
            # every normal line "selected" would bury the exception it matters to
            # see. `selected is None` means the response didn't say, which is not
            # the same as un-ticked and must not be rendered as a warning.
            if it.get("selected") is False:
                line += "\n      ⚠ NOT ticked for checkout — will NOT be ordered"
            detail = []
            ship_str = _ship_str(it)
            if common_ship is None:
                if ship_str is not None:
                    detail.append(f"shipping: {ship_str}")
            elif ship_str != common_ship and it.get("valid", True):
                # Never let a missing quote inherit the hoisted "Free". Skipped on
                # an already-unavailable line, which has no freight to quote.
                detail.append(f"shipping: {ship_str or 'not quoted'}")
            date = it.get("delivery_date")
            if date and date != common_date:
                detail.append(f"delivery: {date}")
            if detail:
                line += "\n      " + " · ".join(detail)
            if not it.get("valid", True):
                line += f"\n      ⚠️ unavailable — {it.get('unavailable_reason') or 'sold out or removed'}"
            # cart_id identifies the LINE, item_id only the product — the same
            # product sits on several lines whenever it's in the cart under more
            # than one variant, and set_cart_quantity / remove_from_cart refuse to
            # act on an ambiguous item_id. Their docstrings promise this value
            # comes "from view_cart", so it has to actually be here.
            line += f"\n      item_id: {it['item_id']}"
            if it.get("cart_id"):
                line += f"  ·  cart_id: {it['cart_id']}"
            lines.append(line)
        lines.append("")

    # Computed subtotal over the shown, priced items — always meaningful. The
    # server's own subtotal/total reflect only the checkbox-*selected* lines
    # (that's why a cart of priced items can report an "Estimated total" of 0.00),
    # so we compute ours and label the server figure honestly.
    #
    # Grouped by each line's OWN currency. AliExpress renders a line in its
    # listing's currency, so one cart can legitimately mix them, and adding USD
    # to SEK yields a figure that is wrong in every currency while looking
    # authoritative — the worst kind of number to hand a caller reconciling a
    # basket. When every line agrees (the normal case) this prints exactly as
    # it did before; when they don't, it refuses to fake a single total.
    # Say it once, loudly, as well as per line: an un-ticked line is an item the
    # user thinks they are buying and will not receive, and that is not
    # recoverable after checkout.
    unticked = [it for it in items if it.get("selected") is False]
    # Cross-check our own parse against AliExpress's count of ticked lines. They
    # can legitimately disagree: a line whose checkbox we couldn't read comes
    # back `selected is None`, which is unknown, not un-ticked. Reporting only
    # what we identified would then understate the problem by exactly the lines
    # we understand least — so when the arithmetic doesn't close, say so.
    #
    # This is not hypothetical. Two renders of the SAME 24-line cart minutes
    # apart parsed 5 and then 4 un-ticked lines, while the server's own
    # selectItemNum said 19 ticked (i.e. 5) both times — one line's checkbox is
    # not reliably present across the page merge. Trusting our own count alone
    # would have silently told the user 4 items were excluded when 5 were.
    server_selected = summary.get("selected_count") if used_droplet else None
    excluded = (len(items) - server_selected
                if isinstance(server_selected, int) and not truncated else None)
    if unticked or excluded:
        headline = (f"⚠ {excluded} of {len(items)} shown line(s) are NOT ticked for "
                    "checkout and will not be ordered"
                    if excluded is not None else
                    f"⚠ {len(unticked)} of {len(items)} shown line(s) are NOT ticked "
                    "for checkout and will not be ordered")
        lines.append(headline + (":" if unticked else "."))
        for it in unticked:
            lines.append(f"    - {it['title'][:70]}")
        if excluded is not None and excluded != len(unticked):
            lines.append(
                f"    (AliExpress reports {server_selected} ticked, so {excluded} are "
                f"excluded, but only {len(unticked)} could be identified line-by-line "
                "— check the cart on the site before ordering.)")
        lines.append("")

    priced = [it for it in items if it.get("price") is not None]
    by_currency = _subtotal_by_currency(priced, currency)
    if priced:
        # Say what the number actually covers. Unpriced lines (unavailable items
        # carry no price) were silently dropped from the sum while the label still
        # claimed "all items shown" — a total narrower than its own description.
        unpriced = len(items) - len(priced)
        if truncated and unpriced:
            scope = f"{len(priced)} of {n} shown items; {unpriced} unpriced"
        elif truncated:
            scope = f"{n} shown items"
        elif unpriced:
            scope = f"{len(priced)} of {len(items)} items; {unpriced} unpriced"
        else:
            scope = "all items shown"
        if len(by_currency) == 1:
            only = next(iter(by_currency))
            lines.append(f"Subtotal ({scope}): {_money(by_currency[only], only)}")
        else:
            lines.append(f"Subtotal ({scope}) — this cart mixes currencies, so there "
                         "is no single total; they cannot be added without an FX rate:")
            for c, amount in sorted(by_currency.items(), key=lambda kv: str(kv[0])):
                lines.append(f"  {_money(amount, c)}")
    # AliExpress's own totals. On the droplet path they come from that response's
    # summary component, already formatted in its pay currency, and are printed
    # verbatim — never re-formatted under a label from somewhere else. On the
    # legacy-only fallback path the legacy totals are printed with the legacy
    # cart's own currency, which is self-consistent because the item prices above
    # came from that same response.
    if used_droplet:
        if summary.get("lines"):
            sel = summary.get("selected_count")
            scope = (f"{sel} selected line(s)" if isinstance(sel, int)
                     else "selected items only")
            lines.append(f"AliExpress checkout estimate ({scope}):")
            for row in summary["lines"]:
                label = row.get("label")
                lines.append(f"  {label}: {row['text']}" if label else f"  {row['text']}")
        else:
            lines.append(
                "AliExpress checkout estimate: unavailable "
                "(the cart summary block was missing from the response)."
            )
    elif cart.get("total") is not None:
        lines.append(
            f"AliExpress checkout estimate (selected items only): {_money(cart['total'])}"
        )

    return "\n".join(lines).rstrip()


@mcp.tool(
    title="List Orders",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def list_orders(max_orders: int = 10) -> str:
    """
    List your recent AliExpress orders (read-only): status, date, store, items, total.

    Requires a FULL login session in the credential file — the quick cookie snippet
    misses HttpOnly login cookies (see README). Does not expose shipping addresses.

    Pages back through your history as needed — AliExpress returns 10 orders per
    page, so asking for more fetches more pages (one request each). Orders going
    back years are reachable this way.

    Args:
        max_orders: Max number of orders to list (default 10, one page).
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        # The payload the site actually sends. The previous {"page", "pageSize"}
        # was accepted with SUCCESS and silently ignored — verified against live
        # responses, which returned an identical page 1 for every variation.
        resp = mtop_call(
            ORDER_LIST_API, "1.0",
            {"statusTab": None, "renderType": "init", "clientPlatform": "pc",
             "shipToCountry": COUNTRY, "_lang": LANG, "timeZone": "GMT+0200"},
            cookies=cookies, referer=f"{BASE_URL}/p/order/index.html",
        )
    except Exception as e:
        return f"Order list call failed: {e}"

    problem = ret_problem(resp)
    if problem:
        return problem

    page_warning = None
    if max_orders > 10:
        resp, page_warning = _orders_fetch_all_pages(cookies, resp, max_orders)

    orders = _extract_orders(resp, max_orders)
    if not orders:
        return (
            "Order API succeeded but no order blocks were recognized. The order-list "
            "shape may have changed — update `_extract_orders`."
        )

    head_line = f"Recent orders ({len(orders)}"
    if len(orders) < max_orders:
        # The walk was bound and its outcome thrown away, so a truncated history
        # looked identical to a complete one.
        head_line += (f", history walk {page_warning}" if page_warning
                      else "; that is all AliExpress returned")
    head_line += "):"
    lines = [head_line]
    for o in orders:
        lines.append("")
        head = f"- Order {o['order_id']}"
        if o.get("status"):
            head += f" — {o['status']}"
        lines.append(head)
        if o.get("date"):
            lines.append(f"  {o['date']}")
        if o.get("store"):
            lines.append(f"  store: {o['store']}")
        items = o.get("items", [])
        for it in items[:6]:
            lines.append(_order_item_line(it))
        # The cut used to be silent — a 9-line order rendered as 6 with nothing
        # saying the rest existed. get_order prints them all.
        if len(items) > 6:
            lines.append(f"  … +{len(items) - 6} more line(s) — get_order({o['order_id']}) for all of them")
        money = _order_money(o.get("total"), o.get("currency"), o.get("total_text"))
        if money:
            lines.append(f"  total: {money}")
    return "\n".join(lines)


@mcp.tool(
    title="Get Order",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def get_order(order_id: str) -> str:
    """
    Show one AliExpress order in full (read-only): status, date, store, line items, total.

    Requires a full login session (see README). Looks the order up in your recent
    order list. Detailed parcel-tracking events aren't exposed yet.

    Args:
        order_id: The AliExpress order id (from `list_orders`).
    """
    if not order_id:
        return "Provide an order_id (see list_orders)."
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        # `order.list` ignores {page,pageSize} — the old payload here asked for 20 and
        # always got page 1's ten, so every order older than that reported "not found"
        # even though list_orders(max_orders=40) listed it. Send the payload the site
        # sends, then walk the same droplet pagination list_orders uses.
        resp = mtop_call(
            ORDER_LIST_API, "1.0",
            {"statusTab": None, "renderType": "init", "clientPlatform": "pc",
             "shipToCountry": COUNTRY, "_lang": LANG, "timeZone": "GMT+0200"},
            cookies=cookies, referer=f"{BASE_URL}/p/order/index.html",
        )
    except Exception as e:
        return f"Order lookup failed: {e}"

    problem = ret_problem(resp)
    if problem:
        return problem

    ALL = 10 ** 6  # never truncate: we are looking for one id, not listing a page
    orders = _extract_orders(resp, ALL)
    match = next((o for o in orders if o["order_id"] == str(order_id)), None)
    page_warning = None
    if not match:
        try:
            resp, page_warning = _orders_fetch_all_pages(cookies, resp, ALL)
        except Exception as e:
            page_warning = f"stopped while paging back — {e}"
        orders = _extract_orders(resp, ALL)
        match = next((o for o in orders if o["order_id"] == str(order_id)), None)
    if not match:
        msg = f"Order {order_id} not found in the {len(orders)} orders reachable on your account"
        msg += (f" — the history walk {page_warning}, so older orders may exist."
                if page_warning else ". Run list_orders to see what's available.")
        return msg

    lines = [f"Order {match['order_id']}:"]
    if match.get("status"):
        lines.append(f"Status: {match['status']}")
    if match.get("date"):
        lines.append(f"Date: {match['date']}")
    if match.get("store"):
        lines.append(f"Store: {match['store']}")
    if match.get("items"):
        lines.append("Items:")
        for it in match["items"]:
            lines.append(_order_item_line(it))
    money = _order_money(match.get("total"), match.get("currency"), match.get("total_text"))
    if money:
        lines.append(f"Total: {money}")
    return "\n".join(lines)


@mcp.tool(
    title="List Wishlists",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def list_wishlists() -> str:
    """
    List the wishlists (saved-item collections) on your AliExpress account.

    Returns each list's name, id and item count. Use this before add_to_wishlist
    or remove_from_wishlist, which need a list to target. Note this is separate
    from get_wishlist, which returns saved *items* rather than the lists.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    groups, err = _fetch_wishlist_groups(cookies)
    if err and not groups:
        return err
    if not groups:
        return "You have no wishlists. Create one with create_wishlist."

    lines = [f"Wishlists ({len(groups)}):"]
    for g in groups:
        count = g["item_count"]
        lines.append(f"  - {g['name']}"
                     + (f" — {count} item(s)" if isinstance(count, int) else "")
                     + ("  [public]" if g["public"] else "")
                     + f"  [id: {g['group_id']}]")
    if err:
        lines.append(f"  ⚠ {err}")
    return "\n".join(lines)


@mcp.tool(
    title="Add To Wishlist",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True,
        openWorldHint=True
    ),
    structured_output=False,
)
def add_to_wishlist(wishlist: str, item_id: str = "", url: str = "") -> str:
    """
    File a product under one of your wishlists, saving it first if needed. WRITES.

    AliExpress models these as two separate actions and this tool does both: the
    ♡ that saves a product into the wishlist ungrouped, then the list assignment
    that moves it into a named list. Its list API only assigns items already in
    the wishlist, so an unsaved product takes two calls rather than one.

    Note that lists are folders, not tags: an item lives in at most one, so
    filing it into a second list MOVES it out of the first, and the result says
    so.

    The list is required — there is no default. Items landing in the ungrouped
    bucket is how they end up somewhere you never look, so an unknown or ambiguous
    list name is an error rather than a guess.

    Args:
        wishlist: Which list to move it into — name (case-insensitive) or id.
        item_id: AliExpress item ID (e.g. "1005007655628250").
        url: Full or short AliExpress product URL (alternative to item_id).
    """
    item_id = _resolve_item_id(item_id, url)
    if not item_id:
        return "Provide a valid item_id or AliExpress product URL."
    if not (wishlist or "").strip():
        return "Provide the wishlist to save into (name or id) — run list_wishlists to see them."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    group, err = _resolve_wishlist_group(cookies, wishlist)
    if err:
        return err

    # Two steps, because AliExpress separates them: `wishitem.save` puts the
    # product in the wishlist (ungrouped), and only then can `myList.saveItem`
    # file it under a list. Calling the second on an unsaved item returns SUCCESS
    # and does nothing, so the order matters.
    newly_saved = False
    if str(item_id) not in _wishlist_saved_item_ids(cookies):
        try:
            fav = _wishlist_favourite(cookies, str(item_id))
        except Exception as e:
            return f"Could not save item {item_id} to your wishlist: {e}"
        if not fav.startswith("SUCCESS"):
            return f"Could not save item {item_id} to your wishlist — AliExpress said: {fav}"
        newly_saved = True

    try:
        ret = _wishlist_save_item(cookies, group["group_id"], [item_id], [])
    except Exception as e:
        return f"Wishlist save failed: {e}"
    if not ret.startswith("SUCCESS"):
        return f"Could not save item {item_id} to {group['name']!r} — AliExpress said: {ret}"

    # SUCCESS from this family has been observed on no-ops, so confirm by re-reading.
    in_group = _wishlist_saved_item_ids(cookies, group["group_id"])
    if str(item_id) not in in_group:
        return (f"AliExpress reported success but item {item_id} is not in "
                f"{group['name']!r}" + (" (it is saved to your wishlist, just ungrouped)."
                                        if newly_saved else "."))
    verb = "Saved" if newly_saved else "Moved"
    return (f"{verb} item {item_id} to wishlist {group['name']!r} "
            f"({len(in_group)} item(s) in that list).")


@mcp.tool(
    title="Remove From Wishlist",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True,
        openWorldHint=True
    ),
    structured_output=False,
)
def remove_from_wishlist(item_id: str = "", url: str = "", wishlist: str = "",
                         permanent: bool = False) -> str:
    """
    Remove a saved item from a wishlist, or delete it outright. This WRITES.

    AliExpress has two separate removals and this tool will not choose between
    them for you — a bare call removes nothing and tells you the options:

      wishlist="My list" -> takes it out of THAT list only. The item stays saved
                            (ungrouped) and can be re-filed with add_to_wishlist.
      permanent=True     -> DELETES it from your wishlist entirely, out of every
                            list. Cannot be undone.

    The destructive form used to be what you got by simply omitting an argument,
    which is the wrong way round: the irreversible action should be the one you
    have to ask for.

    Args:
        item_id: AliExpress item ID to remove.
        url: Full or short AliExpress product URL (alternative to item_id).
        wishlist: List to take it out of — name (case-insensitive) or id.
            Keeps the item saved.
        permanent: Set True to delete the item from the wishlist altogether.
            Ignored when `wishlist` is given, since that is the scoped removal.
    """
    item_id = _resolve_item_id(item_id, url)
    if not item_id:
        return "Provide a valid item_id or AliExpress product URL."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    saved = _wishlist_saved_item_ids(cookies)
    if saved and str(item_id) not in saved:
        return f"Item {item_id} is not in your wishlist — nothing to remove."

    # Neither scope given: refuse rather than guess, and say where the item
    # actually sits so the caller can name a list without a second lookup.
    if not (wishlist or "").strip() and not permanent:
        groups, _ = _fetch_wishlist_groups(cookies)
        holding = [g for g in groups
                   if str(item_id) in _wishlist_saved_item_ids(cookies, g["group_id"])]
        where = (f"It is currently in {holding[0]['name']!r}." if len(holding) == 1
                 else "It is saved but not filed under any list."
                 if not holding else
                 "It appears in: " + ", ".join(repr(g["name"]) for g in holding) + ".")
        return (
            f"Nothing removed — say which removal you mean for item {item_id}.\n"
            f"{where}\n"
            f"  • To take it out of a list but keep it saved: "
            f"remove_from_wishlist(item_id={item_id!r}, wishlist='<list name>')\n"
            f"  • To delete it from your wishlist entirely (cannot be undone): "
            f"remove_from_wishlist(item_id={item_id!r}, permanent=True)"
        )

    group = None
    if (wishlist or "").strip():
        group, err = _resolve_wishlist_group(cookies, wishlist)
        if err:
            return err
        if str(item_id) not in _wishlist_saved_item_ids(cookies, group["group_id"]):
            return (f"Item {item_id} is not in wishlist {group['name']!r} — nothing to "
                    "remove. (Omit `wishlist` to delete it from your wishlist entirely.)")

    scope = group["group_id"] if group else "0"
    try:
        ret = _wishlist_delete_item(cookies, str(item_id), group_id=scope)
    except Exception as e:
        return f"Wishlist removal failed: {e}"
    if ret.startswith("NOTFOUND"):
        return f"Item {item_id} is not in your wishlist — nothing to remove."
    if not ret.startswith("SUCCESS"):
        return f"Could not remove item {item_id} — AliExpress said: {ret}"

    # This API family acks no-ops, so confirm against a fresh read either way.
    after = _wishlist_saved_item_ids(cookies)
    if group:
        if str(item_id) in _wishlist_saved_item_ids(cookies, group["group_id"]):
            return (f"AliExpress reported success but item {item_id} is still in "
                    f"{group['name']!r} — nothing changed.")
        kept = " It is still in your wishlist, just ungrouped." if str(item_id) in after else ""
        return f"Removed item {item_id} from wishlist {group['name']!r}.{kept}"
    if str(item_id) in after:
        return (f"AliExpress reported success but item {item_id} is still in your "
                "wishlist — nothing was deleted.")
    return f"Deleted item {item_id} from your wishlist ({len(after)} item(s) left)."


@mcp.tool(
    title="Create Wishlist",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False,
        openWorldHint=True
    ),
    structured_output=False,
)
def create_wishlist(name: str, public: bool = False) -> str:
    """
    Create a new (empty) wishlist on your AliExpress account. This WRITES.

    Creates the list itself, empty; use add_to_wishlist to put products in it.
    Names are not checked for duplicates: AliExpress will happily create a second
    list with the same name, so this reports the new list's id. delete_wishlist
    is the inverse, and removes only the container — items filed under a deleted
    list stay in the wishlist, ungrouped.

    Args:
        name: Name for the new list, e.g. "3D printer parts".
        public: Whether the list is visible to others (default private).
    """
    name = (name or "").strip()
    if not name:
        return "Provide a name for the wishlist."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    payload = {
        "_lang": LANG,
        "_currency": CURRENCY,
        # Nested JSON string, not an object — the API rejects a real array here.
        "groupListString": json.dumps(
            [{"id": "", "name": name, "isPublic": "Y" if public else "N"}],
            separators=(",", ":"), ensure_ascii=False),
        "opType": "add",
    }
    try:
        _pace("cart_write", CART_WRITE_MIN_INTERVAL)
        resp = mtop_call(WISHLIST_GROUP_API, "2.0", payload, cookies=cookies,
                         referer=f"{BASE_URL}/p/wish-manage/index.html")
    except Exception as e:
        return f"Wishlist create failed: {e}"

    ret = (resp.get("ret") or ["?"])[0]
    data = resp.get("data") or {}
    if ret_problem(resp) is not None or not data.get("succeed"):
        return f"Could not create wishlist {name!r} — AliExpress said: {ret}"

    groups = data.get("data") or []
    made = next((g for g in groups if isinstance(g, dict) and g.get("name") == name), None)
    if not made:
        return f"AliExpress reported success but returned no list named {name!r} — check the site."
    return (f"Created wishlist {made.get('name')!r} "
            f"({'public' if made.get('isPublic') == 'Y' else 'private'}).\n"
            f"  List ID: {made.get('id')}")


@mcp.tool(
    title="Delete Wishlist",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True,
        openWorldHint=True
    ),
    structured_output=False,
)
def delete_wishlist(wishlist: str) -> str:
    """
    Delete one of your wishlists. This WRITES and CANNOT be undone.

    Deletes the list itself, not the products in it: anything filed under it
    stays in your wishlist and becomes ungrouped. To remove a single item from a
    list instead, use remove_from_wishlist with its `wishlist` argument.

    Args:
        wishlist: Which list to delete — name (case-insensitive) or id. An
            ambiguous name is refused rather than guessed.
    """
    if not (wishlist or "").strip():
        return "Provide the wishlist to delete (name or id) — run list_wishlists to see them."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    group, err = _resolve_wishlist_group(cookies, wishlist)
    if err:
        return err

    try:
        ret = _wishlist_delete_group(cookies, group["group_id"])
    except Exception as e:
        return f"Wishlist deletion failed: {e}"
    if ret.startswith("NOTFOUND"):
        return f"No wishlist {wishlist!r} — nothing to delete."
    if not ret.startswith("SUCCESS"):
        return f"Could not delete wishlist {group['name']!r} — AliExpress said: {ret}"

    # Acks are unreliable in this API family — confirm the list is actually gone.
    after, _ = _fetch_wishlist_groups(cookies)
    if any(g["group_id"] == group["group_id"] for g in after):
        return (f"AliExpress reported success but wishlist {group['name']!r} is still "
                "there — nothing was deleted.")
    freed = group.get("item_count") or 0
    kept = (f" Its {freed} item(s) are still in your wishlist, now ungrouped."
            if freed else "")
    return (f"Deleted wishlist {group['name']!r}.{kept} "
            f"({len(after)} list(s) left.)")


@mcp.tool(
    title="Get Wishlist",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
def get_wishlist(max_items: int = 25) -> str:
    """
    List your saved / liked AliExpress items (wishlist), read-only. Flags items
    that dropped in price since you saved them (⚠ price drop) and sold-out ones.

    Requires a FULL login session in the credential file — the quick cookie snippet
    misses HttpOnly login cookies (see README).

    Args:
        max_items: Max number of saved items to list (default 25).
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    # The wishlist endpoint ignores pageIndex and locks pageSize at ~16 — it's an
    # append/infinite-scroll surface behind an opaque cursor (same as the cart), so
    # asking for "page 2" just replays page 1. We therefore fetch the single server
    # page and dedupe defensively rather than looping (which replayed items before).
    payload = {
        "_lang": LANG, "_currency": CURRENCY, "country": COUNTRY,
        "pageIndex": 1, "pageSize": max(max_items, 20), "groupId": "0",
    }
    try:
        resp = mtop_call(
            WISHLIST_API, "1.0", payload, cookies=cookies,
            referer=f"{BASE_URL}/p/wish-manage/index.html",
        )
    except Exception as e:
        return f"Wishlist call failed: {e}"
    problem = ret_problem(resp)  # same full-login gate as orders
    if problem:
        return problem

    chunk = _extract_wishlist(resp, max_items)
    total = chunk["total"]
    seen: set[str] = set()
    items: list[dict] = []
    for it in chunk["items"]:
        if it["item_id"] in seen:
            continue
        seen.add(it["item_id"])
        items.append(it)

    if not items:
        # We only reach here on a SUCCESS response (auth/token problems return
        # earlier), so distinguish a genuinely empty wishlist from a parse failure
        # instead of blaming throttling/cookies.
        if total and total > 0:
            return (
                f"The wishlist endpoint succeeded and reports {total} saved item(s), but "
                "none could be parsed — the wishlist component shape likely changed. This "
                "is a parser issue (not auth or throttling); `_extract_wishlist` needs an "
                "update against the current response."
            )
        if total == 0:
            return "Your AliExpress wishlist is empty (server reports 0 saved items)."
        return (
            "The wishlist endpoint returned no items and no count — its response shape may "
            "have changed. Update `_extract_wishlist` against a fresh response."
        )

    n = len(items)
    header = f"Wishlist ({n} shown" + (f" of {total}" if total is not None else "") + "):"
    lines = [header]
    if total and total > n:
        lines.append(
            f"  ⚠ AliExpress returns only the first page of the wishlist via the API, "
            f"so {total - n} more saved item(s) aren't listed here."
        )
    lines.append("")
    for it in items:
        line = f"- {it['title']}"
        cur = it.get("currency")
        if it.get("price") is not None:
            line += f" — {_fmt_money(it['price'], cur)}"
        if it.get("original_price") and it["price"] and it["original_price"] > it["price"]:
            line += f" (was {it['original_price']:.2f})"
        if it.get("invalid"):
            line += "  ⚠ sold out / unavailable"
        # Normalize the price-drop signal to "<amount> <CUR> off" — the raw server
        # string mixes glyphs/decimal styles ("грн.3,085.93 off" vs "C$91.21 off").
        if it.get("price_drop"):
            drop_amt = _normalize_price(it["price_drop"])
            if drop_amt is not None and cur:
                line += f"\n  ⚠ price drop: {_fmt_money(drop_amt, cur)} off since saved"
            else:
                line += f"\n  ⚠ price drop: {it['price_drop']}"
        meta = [f"item_id: {it['item_id']}"]
        if it.get("added"):
            meta.append(f"saved: {it['added']}")
        line += "\n  " + " · ".join(meta)
        lines.append(line)
    return "\n".join(lines)
