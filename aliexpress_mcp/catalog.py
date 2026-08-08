"""
Product catalog domain: search fetch+render, PDP fetch+extract+render,
seller/store, reviews, and the variant (SKU) table.

Moved verbatim out of aliexpress_mcp_server.py — see that file's module
docstring for the server-level overview.
"""

import json
import os
import re
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx

from aliexpress_mcp.core import (
    BASE_URL, COUNTRY, CURRENCY, LANG, USER_AGENT, logger,
    load_cookies, get_client, check_auth_redirect,
    AUTH_EXPIRED_MSG, _pace, mtop_call, ret_problem,
    _msrp_flag, _fmt_money, parse_price, _normalize_price, _strip_html,
    _parse_sold_count,
)
from aliexpress_mcp.scrape import parse_search_results


SORT_MAP = {
    "best_match": None,
    "orders": "total_tranpro_desc",
    "price_asc": "price_asc",
    "price_desc": "price_desc",
}


SEARCH_RENDER_ATTEMPTS = 3


def _search_total_results(html: str) -> Optional[int]:
    """Read the server's own result count, which is present even when the grid isn't."""
    m = re.search(r'"totalResults"\s*:\s*(\d+)', html)
    return int(m.group(1)) if m else None


def _search_fetch_parse(query: str, sort_by: str = "best_match",
                        ship_from: str = "") -> tuple[list[dict], Optional[int]]:
    """
    Fetch an AliExpress search results page and parse product cards.

    Returns (items, total_results). `total_results` comes from the page's own
    `pageInfo.totalResults` and is reported even when zero cards parse.

    AliExpress intermittently serves the results page WITHOUT the `mods.itemList`
    grid — same URL, same second, sometimes present and sometimes not. Parsing
    that as "no results" told the caller a 92,000-result query had no products,
    so an empty parse against a non-zero total is retried before believing it.

    Raises RuntimeError(AUTH_EXPIRED_MSG) if AliExpress bounces us to login.
    """
    slug = quote_plus(query.strip()).replace("+", "-")
    url_path = f"/w/wholesale-{slug}.html"
    params = {}
    if SORT_MAP.get(sort_by):
        params["SortType"] = SORT_MAP[sort_by]
    if ship_from:
        params["shipFromCountry"] = ship_from.strip().upper()

    total = None
    for attempt in range(SEARCH_RENDER_ATTEMPTS):
        client = get_client()
        try:
            resp = client.get(url_path, params=params)
            if check_auth_redirect(resp):
                raise RuntimeError(AUTH_EXPIRED_MSG)
            resp.raise_for_status()
            items = parse_search_results(resp.text)
            total = _search_total_results(resp.text)
        finally:
            client.close()

        if items or not total:
            return items, total
        logger.info("search grid missing for %r (total=%s), retry %d/%d",
                    query, total, attempt + 1, SEARCH_RENDER_ATTEMPTS - 1)
        _pace("search_retry", 1.0)

    return [], total


TITLE_MAX = 80


def _short_title(title: str, limit: int = TITLE_MAX) -> str:
    """
    Trim AliExpress's keyword-stuffed titles to their identifying head.

    Measured over 240 live cards (usb-c cable / mechanical keyboard / nvme ssd /
    desk lamp, Aug 2026): mean title 123 chars, max 179, and the tail is almost
    always the SEO compatibility run ("For Xiaomi Samsung Huawei Honor Realme
    OPPO"). Cutting at 80 on a word boundary produced ZERO new collisions on
    those 240 cards — every pair that rendered identically was already an
    identical full title. It does lose a trailing size token on 5 of 240
    ("... 1M 2M 3M"), which is why the ellipsis is kept: it tells the caller the
    title was cut and that get_product_details has the full one.
    """
    t = " ".join((title or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    sp = cut.rfind(" ")
    if sp >= limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,·-") + "…"


def apply_sort(products: list[dict], sort_by: str) -> list[dict]:
    """
    Enforce the requested ordering over the rows we actually return.

    AliExpress honours `SortType` on a plain search but silently drops it when
    `shipFromCountry` is also set — verified live: price_asc alone returns
    3.23, 3.48, 3.51…; the same call with ship_from=ES returns 149.91, 195.18,
    121.36 while the header still claimed price_asc. Rather than assert an
    ordering the server did not apply, sort the parsed rows ourselves. Unpriced
    rows sink to the end instead of being dropped or sorting as zero.
    """
    if sort_by == "price_asc":
        key, rev = lambda p: (p.get("price") is None, p.get("price") or 0), False
    elif sort_by == "price_desc":
        key, rev = lambda p: (p.get("price") is None, p.get("price") or 0), True
    else:
        return products
    return sorted(products, key=key, reverse=rev)


def _format_product_lines(products: list[dict], header: str, limit: int = 25) -> str:
    """Render parsed product dicts into the compact text shared by search + deals."""
    lines = [header]
    for p in products[:limit]:
        line = f"- {_short_title(p['title'])}"
        cur = p.get("currency") or CURRENCY
        if p["price"] is not None:
            line += f" — {_fmt_money(p['price'], cur)}"
        if p["original_price"] is not None and p["original_price"] > (p["price"] or 0):
            line += f" (was {p['original_price']:.2f})"
        if p["discount_pct"]:
            line += f" [-{p['discount_pct']}%]{_msrp_flag(p['discount_pct'])}"
            sd = p.get("seller_discount_pct")
            if sd is not None and round(sd) != p["discount_pct"]:
                line += f" (seller says -{round(sd)}%)"
        if p["rating"]:
            line += f" ★{p['rating']}"
        if p["sold_count"]:
            line += f" · {p['sold_count']}"
        # Warehouse country decides both transit time and whether customs gets
        # involved, so it belongs on every row, not behind a second call.
        if p.get("ship_from"):
            line += f" · ships from {p['ship_from']}"
        if p.get("is_choice"):
            line += " · Choice"
        if p.get("listing_age"):
            line += f" · {p['listing_age']}"
        # Only the minority lacking a free-shipping badge is worth a mark; the
        # rest would just repeat themselves 48 times.
        if p.get("free_shipping") is False:
            line += " · ⚠ no free-shipping badge"
        if p.get("duty_offset"):
            line += f" · {p['duty_offset']}"
        # One row = one line. The id used to sit on a second line under its own
        # "item_id:" label; the label is static per row, so it lives in the
        # docstring legend now and only the value is repeated 25 times.
        line += f" [{p['item_id']}]"
        lines.append(line)
    return "\n".join(lines)


def _fetch_pdp_mtop(item_id: str) -> Optional[dict]:
    """
    Call the PDP MTOP endpoint and return the raw response dict, or None on failure.
    Tries the PC endpoint first, then the msite endpoint as a fallback.
    """
    referer = f"{BASE_URL}/item/{item_id}.html"
    payload = {
        "productId": item_id,
        "_currency": CURRENCY,
        "_lang": LANG,
        "country": COUNTRY,
        "channel": "",
        "sourceType": "pc",
        "pdp_ext_f": "",
    }

    for api, version in [
        ("mtop.aliexpress.pdp.pc.query", "1.0"),
        ("mtop.aliexpress.itemdetail.pc.asyncPCDetail", "1.0"),
        ("mtop.aliexpress.itemdetail.msite", "1.0"),
    ]:
        try:
            resp = mtop_call(api, version, payload, referer=referer)
        except Exception as e:
            logger.debug("MTOP %s failed: %s", api, e)
            continue
        ret = resp.get("ret", [])
        if ret_problem(resp) is None:
            return resp
        # Some endpoints return data even without SUCCESS::API_SUCCESS in ret
        if resp.get("data") and isinstance(resp["data"], dict) and resp["data"]:
            return resp
        logger.debug("MTOP %s ret=%s", api, ret)
    return None


# Countries between which goods move without import charges. Only used to decide
# whether AliExpress's duty sentence tells us anything the warehouse country
# hasn't already: across 22 live items the two agreed every single time (14 CN →
# "Import charges will apply", 8 EU → "No extra duties"), so restating it is a
# wasted line. Note the failure direction is safe — we drop the clause only on an
# exact match, so an incomplete list here means we print more, never less.
DUTY_FREE_BLOCS = [
    {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
     "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
     "SI", "ES", "SE"},
]


def _duty_free_expected(ship_from_code: Optional[str]) -> Optional[bool]:
    """True/False if the warehouse country settles the duty question, else None."""
    if not ship_from_code:
        return None
    origin = ship_from_code.strip().upper()
    if not origin:
        return None
    if origin == COUNTRY.upper():
        return True
    for bloc in DUTY_FREE_BLOCS:
        if COUNTRY.upper() in bloc:
            return origin in bloc
    return False


def _informative_tax_note(tax_note: Optional[str], ship_from_code: Optional[str]) -> Optional[str]:
    """
    Drop the duty clause when it merely repeats what "Ships from: X" already says.

    AliExpress packs two facts into one string — "Price includes VAT | No extra
    duties". The VAT half is never derivable and is always kept; the duty half is
    kept only when it does NOT follow from the warehouse country, which is exactly
    when it is worth the caller's attention (a CN warehouse with duty prepaid, or
    an in-union warehouse that still attracts charges).
    """
    if not tax_note:
        return None
    expected = _duty_free_expected(ship_from_code)
    if expected is None:
        return tax_note
    kept = []
    for clause in re.split(r"\s*[|;]\s*", tax_note):
        c = clause.strip()
        if not c:
            continue
        low = c.lower()
        is_duty = any(w in low for w in ("dut", "import charge", "customs"))
        if is_duty:
            says_free = any(w in low for w in ("no extra", "no import", "no customs", "free of", "duty-free"))
            says_charge = any(w in low for w in ("will apply", "may apply", "applies"))
            if (expected and says_free) or (not expected and says_charge):
                continue  # exactly what the warehouse country already implied
        kept.append(c)
    return " | ".join(kept) if kept else None


def _sku_spec_for_id(result: dict, sku_id: str) -> Optional[str]:
    """
    Human-readable spec of one SKU ("DDR4 32GB · R7 5825U"), by id.

    Used to label the endpoints of a price range so "from 747.41" can say *which*
    configuration costs that. Reads SKU.skuPaths, the same source get_variants uses.
    """
    sku = result.get("SKU") if isinstance(result.get("SKU"), dict) else {}
    paths = sku.get("skuPaths") if isinstance(sku.get("skuPaths"), list) else []
    if not paths:
        return None
    prop_map = _sku_prop_map(sku)
    for p in paths:
        if not isinstance(p, dict):
            continue
        pid = p.get("skuIdStr") or (str(p.get("skuId")) if p.get("skuId") is not None else None)
        if pid and str(pid) == str(sku_id):
            return _sku_attr_spec(p.get("skuAttr", ""), prop_map)
    return None


def _lot_note(result: dict, selected_sku_id: Any = None) -> Optional[str]:
    """
    Describe a lot listing's unit content, e.g. "100 pcs, 1,17kr/pc" or "lot of 40".

    On a lot listing the quoted price buys a whole lot, not one piece, so the
    number in the price column is not comparable with a single-unit listing's
    until the caller knows the lot size. Two live shapes (Aug 2026):
      LOT.unitContentSkuMap  {"<skuId>": "100 pcs,1,17kr/pc"}  — AliExpress has
                             already done the per-unit division; use its string.
      LOT.numberPerLot/unitContent  {"numberPerLot": 40, "unitContent": "40Pieces"}
                             — lot size only, no per-unit figure supplied.
    """
    lot = result.get("LOT") if isinstance(result.get("LOT"), dict) else None
    if not lot:
        return None

    ucm = lot.get("unitContentSkuMap")
    if isinstance(ucm, dict) and ucm:
        val = None
        if selected_sku_id is not None:
            val = ucm.get(str(selected_sku_id))
        if not val:
            val = next((v for v in ucm.values() if isinstance(v, str) and v.strip()), None)
        if isinstance(val, str) and val.strip():
            # "100 pcs,1,17kr/pc" — the comma is both the field separator and the
            # SEK decimal mark, so split once from the left only, and print both
            # halves exactly as AliExpress formatted them.
            head, sep, tail = val.strip().partition(",")
            return f"{head.strip()}, {tail.strip()}" if sep and tail.strip() else val.strip()

    n = lot.get("numberPerLot")
    unit = lot.get("unitContent") or lot.get("itemUnitContent")
    # A "lot" of one is just a normal listing — saying so would be noise, and
    # would wrongly imply the price needs dividing.
    if isinstance(n, (int, float)) and n == 1:
        return None
    if isinstance(n, (int, float)) and n:
        return f"price is per lot of {unit or n}"
    if isinstance(unit, str) and unit.strip():
        return f"price is per lot of {unit.strip()}"
    return None


def _pdp_error_code(mtop_resp: dict) -> Optional[str]:
    """
    Detect the PDP "this listing isn't available here" response.

    A delisted / banned / region-blocked / nonexistent item still answers
    `SUCCESS::调用成功`, so `ret_problem` passes it. What actually changes is the
    payload: `data.result` collapses to GLOBAL_DATA alone, carrying
    `globalData.errorCode` (e.g. "SITEM_NOT_EXIST") and boilerplate i18n, with no
    PRICE / PRODUCT_TITLE / SHIPPING. Unchecked, the PDP tools fell through to the
    HTML scrape and emitted a plausible shell — title "# Aliexpress" and
    "Shipping: not available (AliExpress needs a saved delivery address)", which
    reads as a fixable config problem rather than a dead listing.

    (There is no `bigBossBan` field in this API version, and `errorCode` is absent
    on healthy items — those carry `itemStatus`/`offlineInfo` instead. Verified
    against live responses Aug 2026.)
    """
    result = (mtop_resp.get("data") or {}).get("result")
    if not isinstance(result, dict):
        return None
    gd = result.get("GLOBAL_DATA")
    gd = gd.get("globalData") if isinstance(gd, dict) else None
    code = gd.get("errorCode") if isinstance(gd, dict) else None
    return str(code) if code else None


def _pdp_unavailable_msg(item_id: str, code: str) -> str:
    """One wording for the dead-listing answer, shared by every PDP tool."""
    return (
        f"Item {item_id} is unavailable in {COUNTRY} (AliExpress returned {code}). "
        "It may be delisted, blocked for this region, or the id may be wrong — "
        "there is no price, shipping, or seller data to report. "
        "Check the item URL, or search for the product again."
    )


def _extract_pdp_fields(mtop_resp: dict, item_id: str) -> dict:
    """
    Pull fields we care about from an MTOP PDP response.

    The response uses a component layout keyed by uppercase section names:
    PRODUCT_TITLE, PRICE, PC_RATING, SHIPPING, SHOP_CARD_PC, HEADER_IMAGE_PC, …
    Field paths below were reverse-engineered from a live response (Apr 2026).
    """
    d: dict[str, Any] = {
        "item_id": item_id,
        "url": f"{BASE_URL}/item/{item_id}.html",
        "title": None,
        "price": None,
        "price_range": None,
        "original_price": None,
        "currency": None,
        "discount_pct": None,
        "seller_discount_pct": None,
        "price_low_spec": None,
        "price_high_spec": None,
        "lot_note": None,
        "rating": None,
        "review_count": None,
        "sold_count": None,
        "sold_count_num": None,
        "seller_name": None,
        "store_url": None,
        "seller_positive_rate": None,
        "seller_total_reviews": None,
        "shipping_cost": None,
        "shipping_free": None,
        "free_shipping_over": None,
        "shipping_alternatives": [],
        "shipping_estimate": None,
        "ship_from": None,
        "ship_from_code": None,
        "tax_note": None,
        "ship_unreachable": None,
        "ship_days_min": None,
        "ship_days_max": None,
    }

    result = mtop_resp.get("data", {}).get("result", {})
    if not isinstance(result, dict):
        return d

    # ── Title ──────────────────────────────────────────────────────────
    pt = result.get("PRODUCT_TITLE")
    if isinstance(pt, dict):
        d["title"] = (
            pt.get("text")          # the real location
            or pt.get("subject")
            or pt.get("title")
            or pt.get("displayTitle")
        )
    gd = result.get("GLOBAL_DATA", {}).get("globalData", {})
    if not isinstance(gd, dict):
        gd = {}
    if not d["title"]:
        d["title"] = gd.get("subject")

    # ── Currency ───────────────────────────────────────────────────────
    # GLOBAL_DATA.globalData.currencyCode is the authoritative page currency
    # (the one the amounts below are actually rendered in — may be UAH, USD,
    # CAD, … depending on the account, NOT necessarily the configured default).
    d["currency"] = gd.get("currencyCode")

    # ── Price ──────────────────────────────────────────────────────────
    # Real paths (seen live Apr 2026):
    #   PRICE.targetSkuPriceInfo.salePriceString         e.g. "C$9.76"
    #   PRICE.targetSkuPriceInfo.originalPrice.value     e.g. 12.19
    # For variant listings, PRICE.skuPriceInfoMap is a dict of per-SKU prices;
    # we report the min/max range when targetSku isn't enough.
    pn = result.get("PRICE")
    if isinstance(pn, dict):
        tsp = pn.get("targetSkuPriceInfo") or {}
        if isinstance(tsp, dict):
            sp = tsp.get("salePriceString")
            if isinstance(sp, str):
                d["price"] = _normalize_price(sp)
            op = tsp.get("originalPrice")
            if isinstance(op, dict):
                if not d["currency"] and op.get("currency"):
                    d["currency"] = op.get("currency")
                v = op.get("value")
                if isinstance(v, (int, float)):
                    d["original_price"] = float(v)
                elif op.get("formatedAmount"):
                    d["original_price"] = _normalize_price(op["formatedAmount"])

        # If this is a variant listing, compute a price range from skuPriceInfoMap
        sku_map = pn.get("skuPriceInfoMap")
        if isinstance(sku_map, dict) and sku_map:
            prices = []
            by_price: list[tuple[float, str]] = []
            for sku_id, sku in sku_map.items():
                if isinstance(sku, dict):
                    sp = sku.get("salePriceString")
                    if isinstance(sp, str):
                        p = _normalize_price(sp)
                        if p is not None:
                            prices.append(p)
                            by_price.append((p, str(sku_id)))
            if prices:
                lo, hi = min(prices), max(prices)
                if lo != hi:
                    d["price_range"] = (lo, hi)
                    # Name the configuration at each end. The headline "from"
                    # price is routinely a stripped or non-functional SKU ("No Ram
                    # No Storage"), and the top end is often a placeholder the
                    # seller uses to mark a variant unavailable — a bare
                    # "747.41–1221432.03 SEK" tells the caller neither.
                    d["price_low_spec"] = _sku_spec_for_id(result, min(by_price)[1])
                    d["price_high_spec"] = _sku_spec_for_id(result, max(by_price)[1])
                if d["price"] is None:
                    d["price"] = lo

    if d["discount_pct"] is None and d["price"] and d["original_price"] and d["original_price"] > d["price"]:
        d["discount_pct"] = round((1 - d["price"] / d["original_price"]) * 100)

    # The seller's own declared discount rate, as shown on the site. Ours is
    # derived from sale-vs-was and AliExpress floors where we round, so the two
    # differ by exactly 1pp on ~18% of live items. Keep both: the ⚠ MSRP? flag
    # stays on the derived figure, this is the raw claim beside it.
    dr = ((gd.get("eventInfo") or {}).get("clcEvent") or {}).get("discountRate") \
        if isinstance(gd.get("eventInfo"), dict) else None
    if isinstance(dr, (int, float)):
        d["seller_discount_pct"] = round(dr)

    # Lot listings quote the price PER LOT, so a 116.71 SEK row and a 2.67 SEK row
    # are not comparable until you know one is 100 pieces. AliExpress ships the
    # per-unit breakdown itself; print its string rather than recomputing money.
    d["lot_note"] = _lot_note(result, pn.get("selectedSkuId") if isinstance(pn, dict) else None)

    # ── Rating / sold count ────────────────────────────────────────────
    rating_mod = result.get("PC_RATING")
    if isinstance(rating_mod, dict):
        r = rating_mod.get("rating")
        if r:
            try:
                d["rating"] = float(r)
            except (TypeError, ValueError):
                pass
        rc = rating_mod.get("totalValidNum")
        if rc is not None:
            try:
                d["review_count"] = int(rc)
            except (TypeError, ValueError):
                pass
        other = rating_mod.get("otherText") or ""
        sold_text, sold_n = _parse_sold_count(other)
        if sold_text:
            d["sold_count"] = sold_text
            d["sold_count_num"] = sold_n

    # ── Seller / store ─────────────────────────────────────────────────
    shop = result.get("SHOP_CARD_PC")
    if isinstance(shop, dict):
        d["seller_name"] = shop.get("storeName")
        si = shop.get("sellerInfo") or {}
        store_url = si.get("storeURL") or shop.get("storeHomePage")
        if isinstance(store_url, str):
            if store_url.startswith("//"):
                store_url = "https:" + store_url
            d["store_url"] = store_url
        pr = shop.get("sellerPositiveRate")
        if pr:
            try:
                d["seller_positive_rate"] = float(pr)
            except (TypeError, ValueError):
                pass
        tn = shop.get("sellerTotalNum")
        if tn is not None:
            try:
                d["seller_total_reviews"] = int(tn)
            except (TypeError, ValueError):
                pass

    # ── Shipping ──────────────────────────────────────────────────────
    # AliExpress computes the duty position per item for the configured
    # destination — "Import charges will apply" for a CN warehouse vs "No extra
    # duties" for an in-union one. That is the landed-cost answer straight from
    # the source, so read it rather than inferring customs rules ourselves.
    tax_info = (result.get("PRICE_EXTEND") or {}).get("taxInfo") or {}
    if isinstance(tax_info, dict) and tax_info.get("content"):
        d["tax_note"] = _strip_html(tax_info["content"])

    ship = result.get("SHIPPING")
    if isinstance(ship, dict):
        # bizData lives inside originalLayoutResultList[0] or deliveryLayoutInfo[0]
        layouts = ship.get("originalLayoutResultList") or ship.get("deliveryLayoutInfo") or []
        biz = None
        if isinstance(layouts, list) and layouts:
            l0 = layouts[0]
            if isinstance(l0, dict):
                biz = l0.get("bizData")
        if isinstance(biz, dict):
            amt = biz.get("displayAmount")
            if amt is not None:
                try:
                    d["shipping_cost"] = float(amt)
                except (TypeError, ValueError):
                    d["shipping_cost"] = parse_price(str(amt))
            # A genuinely free option carries shippingFee="free" and NO
            # displayAmount (8 of 22 live items). Nothing read shippingFee, so
            # those items fell through to "shipping cost not available", which
            # both hid the best fact about them and blamed the caller's setup.
            # The pairing is exact in the sample: free ⇔ no displayAmount,
            # charge ⇔ displayAmount present.
            fee = biz.get("shippingFee")
            if isinstance(fee, str) and fee.strip().lower() == "free":
                d["shipping_free"] = True
                if d["shipping_cost"] is None:
                    d["shipping_cost"] = 0.0
            # `logisticsComposeThreshold` is the FREE-SHIPPING THRESHOLD, never the
            # freight price: it is a flat per-site figure ("100,00kr" on every SE
            # listing, "C$10.00" on every CA one) sitting right next to a real
            # freight of 18,68kr / C$3.08, and it is absent on listings whose
            # freight is unusual. It used to be assigned to `shipping_cost`, which
            # would print "Shipping: 100.00 SEK" for an item that ships for 18.68.
            # Keep it, but only ever as what it is. Verified live Aug 2026.
            thresh = biz.get("logisticsComposeThreshold")
            if thresh:
                d["free_shipping_over"] = _strip_html(thresh) or str(thresh)
            eta_min = biz.get("displayEtaMinDate")
            eta_max = biz.get("displayEtaMaxDate")
            if eta_min and eta_max:
                d["shipping_estimate"] = f"{eta_min} – {eta_max}"
            elif eta_min:
                d["shipping_estimate"] = eta_min
            d["ship_from"] = biz.get("shipFrom")
            d["ship_from_code"] = biz.get("shipFromCode")
            if biz.get("unreachable"):
                d["ship_unreachable"] = True
            dmin = biz.get("deliveryDayMin")
            dmax = biz.get("deliveryDayMax")
            if dmin is not None:
                try:
                    d["ship_days_min"] = int(dmin)
                except (TypeError, ValueError):
                    pass
            if dmax is not None:
                try:
                    d["ship_days_max"] = int(dmax)
                except (TypeError, ValueError):
                    pass

        # Faster-but-paid couriers live in the remaining layouts and were dropped
        # entirely. One live item offers free 9–17 days, DHL 417.65 in 4–9, and
        # Fedex 531.92 in 4–17 — a real speed/price trade the caller never saw.
        # Only keep options that differ from the default in price OR day range:
        # another live item lists 8 carriers, 7 of them identical.
        if isinstance(layouts, list) and len(layouts) > 1:
            seen_opts = {(d.get("shipping_cost"), d.get("ship_days_min"), d.get("ship_days_max"))}
            for lay in layouts[1:]:
                if not isinstance(lay, dict):
                    continue
                b = lay.get("bizData")
                if not isinstance(b, dict):
                    continue
                cost = None
                a = b.get("displayAmount")
                if a is not None:
                    try:
                        cost = float(a)
                    except (TypeError, ValueError):
                        cost = parse_price(str(a))
                elif isinstance(b.get("shippingFee"), str) and b["shippingFee"].strip().lower() == "free":
                    cost = 0.0

                def _as_int(v):
                    try:
                        return int(v) if v is not None else None
                    except (TypeError, ValueError):
                        return None

                lo, hi = _as_int(b.get("deliveryDayMin")), _as_int(b.get("deliveryDayMax"))
                key = (cost, lo, hi)
                if key in seen_opts:
                    continue
                seen_opts.add(key)
                d["shipping_alternatives"].append({
                    "company": b.get("company"),
                    "cost": cost,
                    "days_min": lo,
                    "days_max": hi,
                })

    return d


def _delivery_days(d: dict) -> Optional[str]:
    """
    Render the numeric delivery-day range ("19–25 days", "60 days") on its own.

    AliExpress frequently returns deliveryDayMin/deliveryDayMax with NO
    displayEtaMinDate/MaxDate — 9 of 48 live items sampled Aug 2026. Both PDP tools
    used to reach the day range only *through* the display dates, so for those
    listings the day range was extracted and then thrown away, and the tools
    reported no delivery information at all. Test with `is not None`, not
    truthiness: a same-day `0` is a real answer.
    """
    lo, hi = d.get("ship_days_min"), d.get("ship_days_max")
    if lo is not None and hi is not None:
        return f"{lo} days" if lo == hi else f"{lo}–{hi} days"
    one = lo if lo is not None else hi
    return f"{one} days" if one is not None else None


# ─── Reviews ──────────────────────────────────────────────────────────────────
#
# Product reviews come from a separate, *unsigned* JSON endpoint (not MTOP):
#   GET https://feedback.aliexpress.com/pc/searchEvaluation.do
# Confirmed live Jul 2026. Per-review `buyerEval` is on a 0–100 scale
# (100 = 5 stars); `productEvaluationStatistic` carries the aggregate breakdown.

FEEDBACK_URL = "https://feedback.aliexpress.com/pc/searchEvaluation.do"


def _fetch_reviews(item_id: str, page: int = 1, page_size: int = 20, filt: str = "all",
                    cookies: Optional[dict[str, str]] = None) -> Optional[dict]:
    """Fetch one page of reviews. Returns the response `data` block, or None."""
    if cookies is None:
        cookies = load_cookies()
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-CA,en;q=0.9",
        "Referer": f"{BASE_URL}/item/{item_id}.html",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str
    params = {
        "productId": item_id,
        "lang": "en_US",
        "country": COUNTRY,
        "page": str(page),
        "pageSize": str(page_size),
        "filter": filt or "all",
        "sort": "complex_default",
    }
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        resp = c.get(FEEDBACK_URL, params=params, headers=headers)
        try:
            return resp.json().get("data")
        except (json.JSONDecodeError, AttributeError):
            return None


def _extract_reviews(data: dict, max_reviews: int) -> dict:
    """Pull aggregate stats + a capped list of individual reviews."""
    out: dict[str, Any] = {"stats": None, "reviews": [], "total": None}
    if not isinstance(data, dict):
        return out

    stat = data.get("productEvaluationStatistic") or {}
    if isinstance(stat, dict) and stat:
        stats = {
            "average": stat.get("evarageStar"),
            "total": stat.get("totalNum"),
            "positive_rate": stat.get("positiveRate"),
            "neutral_rate": stat.get("neutralRate"),
            "negative_rate": stat.get("negativeRate"),
            "stars": {
                5: stat.get("fiveStarNum"),
                4: stat.get("fourStarNum"),
                3: stat.get("threeStarNum"),
                2: stat.get("twoStarNum"),
                1: stat.get("oneStarNum"),
            },
        }
        has_value = any(v is not None for k, v in stats.items() if k != "stars") or any(
            v is not None for v in stats["stars"].values()
        )
        if has_value:
            out["stats"] = stats
    out["total"] = data.get("totalNum") or (out["stats"] or {}).get("total")

    for ev in (data.get("evaViewList") or [])[:max_reviews]:
        if not isinstance(ev, dict):
            continue
        raw = ev.get("buyerEval")
        stars = round(raw / 20, 1) if isinstance(raw, (int, float)) else None  # 0–100 → 0–5
        text = ev.get("buyerTranslationFeedback") or ev.get("buyerFeedback") or ""
        out["reviews"].append({
            "stars": stars,
            "country": ev.get("buyerCountry"),
            "date": ev.get("evalDate"),
            "sku": ev.get("skuInfo"),
            "text": " ".join(text.split()),          # collapse whitespace / nbsp
            "up_votes": ev.get("upVoteCount"),
            "logistics": " ".join(str(ev.get("logistics") or "").split()) or None,
        })
    return out


REVIEW_BODY_MAX = 250


def _clip_review(text: str, limit: int = REVIEW_BODY_MAX) -> str:
    """
    Keep the first `limit` characters of a review body, cut on a word boundary.

    Buyer reviews have no length ceiling — one live review on item 1005004992957146
    ran 1,003 characters and spent the last 700 restating its first sentence. The
    verdict, and any concrete defect, is essentially always in the opening lines;
    the tail is elaboration. The marker says how much was dropped so the caller
    knows a long review is being summarised rather than quoted whole.
    """
    # The "(+N chars)" marker costs ~13 chars, so clipping a 255-char review would
    # make the output longer. Only clip once there is something real to save.
    if len(text) <= limit + 25:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    if sp >= limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,.;:—-") + f"… (+{len(text) - len(cut)} chars)"


def _common_sku_affixes(skus: list[str]) -> tuple[str, str]:
    """
    Longest common prefix/suffix of the shown reviews' skuInfo, on token boundaries.

    Every review of an item that sells one colour in one warehouse repeats the whole
    variant string — "Color:Light Grey Ships From:Poland Plug Type:EU" appeared on 7
    of 10 shown reviews of one live item. The part that repeats is worth stating once;
    the part that differs is what tells you a 1★ review is about a different SKU than
    the 5★ ones, so it stays on the row.

    skuInfo is NOT safely splittable into its axes — both axis names and values may
    contain spaces ("Ships From:Poland"), so "Color:Light Grey Ships From:…" has more
    than one valid reading. Plain string prefix/suffix needs no such guess. Both are
    then pulled back to a space or ':' so a shared "Color:Bl" can never split
    "Blue"/"Black" into "ue"/"ack".
    """
    if len(skus) < 2:
        return "", ""
    if len(set(skus)) == 1:
        # Whole string is common; return it intact so no residual is left over.
        return skus[0], ""
    pre = os.path.commonprefix(skus)
    cut = max(pre.rfind(" "), pre.rfind(":"))
    pre = pre[:cut + 1] if cut >= 0 else ""

    rev = [s[::-1] for s in skus]
    suf = os.path.commonprefix(rev)[::-1]
    # A suffix must begin at a token boundary, else "…1m"/"…2m" hoists a bare "m".
    sp = suf.find(" ")
    suf = suf[sp:] if sp >= 0 else ""
    # Never let the two affixes overlap on the shortest string.
    if pre and suf and len(pre) + len(suf) > min(len(s) for s in skus):
        suf = ""
    return pre, suf


# ─── Seller / store ───────────────────────────────────────────────────────────

def _extract_seller(shop: dict) -> dict:
    """Pull store/seller info from a PDP `SHOP_CARD_PC` block (live Jul 2026)."""
    d: dict[str, Any] = {
        "store_name": None, "positive_rate": None, "positive_num": None,
        "total_reviews": None, "score": None, "level": None, "opened": None,
        "opened_years": None, "country": None, "top_rated": None,
        "local_seller": None, "store_url": None, "store_id": None,
    }
    if not isinstance(shop, dict):
        return d
    d["store_name"] = shop.get("storeName")
    pr = shop.get("sellerPositiveRate")
    if pr not in (None, ""):
        try:
            d["positive_rate"] = float(pr)
        except (TypeError, ValueError):
            pass
    d["positive_num"] = shop.get("sellerPositiveNum")
    d["total_reviews"] = shop.get("sellerTotalNum")
    d["score"] = shop.get("sellerScore")
    d["level"] = shop.get("sellerLevel")

    si = shop.get("sellerInfo") or {}
    if isinstance(si, dict):
        d["opened"] = si.get("formatOpenTime")
        d["opened_years"] = si.get("openedYear")
        d["country"] = si.get("countryCompleteName")
        d["top_rated"] = si.get("topRatedSeller")
        d["local_seller"] = si.get("localSeller")
        d["store_id"] = si.get("storeNum")
        su = si.get("storeURL") or shop.get("storeHomePage")
        if isinstance(su, str):
            if su.startswith("//"):
                su = "https:" + su
            d["store_url"] = su
    return d


# ─── Variants / SKU table ─────────────────────────────────────────────────────
#
# A listing's `SKU` component exposes `skuPaths` (one entry per buyable config,
# each with a human-readable `skuAttr` like "14:193#DDR4 16GB 500GB SSD;…" plus a
# `skuId`), and `PRICE.skuPriceInfoMap` gives each skuId its own price. Joining
# them yields a config→price table — the thing a bare price range can't tell you.
# Reverse-engineered live Jul 2026.

def _sku_prop_map(sku: dict) -> dict[str, str]:
    """
    Build {"<propId>:<valueId>": "Display Name"} from SKU.skuProperties.

    Needed because some listings encode skuAttr as bare id pairs
    ("200007763:203372089") with no inline "#name", so the human-readable
    value ("China Mainland", "DDR4 32GB", …) only exists in skuProperties.
    """
    prop_map: dict[str, str] = {}
    for prop in (sku.get("skuProperties") or []):
        if not isinstance(prop, dict):
            continue
        pid = prop.get("skuPropertyId")
        for v in (prop.get("skuPropertyValues") or []):
            if not isinstance(v, dict):
                continue
            vid = v.get("propertyValueIdLong") or v.get("propertyValueId")
            name = v.get("propertyValueDisplayName") or v.get("propertyValueName")
            if pid is not None and vid is not None and name:
                prop_map[f"{pid}:{vid}"] = str(name).strip()
    return prop_map


def _sku_prop_details(sku: dict) -> dict[str, dict[str, Optional[str]]]:
    """
    Build {"<propId>:<valueId>": {"axis", "value", "raw_value", "image"}}.

    Richer sibling of `_sku_prop_map`. The extra fields matter because AliExpress
    sellers routinely sell unrelated products through one axis — a "Color" whose
    values are "10pcs 40P male" / "5Sets male female". Exposing the axis name and
    the per-variant image lets the caller notice that; the display value alone
    hides it.
    """
    details: dict[str, dict[str, Optional[str]]] = {}
    for prop in (sku.get("skuProperties") or []):
        if not isinstance(prop, dict):
            continue
        pid = prop.get("skuPropertyId")
        axis = prop.get("skuPropertyName")
        for v in (prop.get("skuPropertyValues") or []):
            if not isinstance(v, dict):
                continue
            vid = v.get("propertyValueIdLong") or v.get("propertyValueId")
            if pid is None or vid is None:
                continue
            details[f"{pid}:{vid}"] = {
                "axis": str(axis).strip() if axis else None,
                "value": (v.get("propertyValueDisplayName") or v.get("propertyValueName") or "").strip() or None,
                "raw_value": (v.get("propertyValueName") or "").strip() or None,
                "image": v.get("skuPropertyImagePath") or None,
            }
    return details


def _sku_attr_detail_parts(sku_attr: str, details: dict) -> tuple[list[str], Optional[str]]:
    """
    Resolve a skuAttr into ["Axis: Value", ...] plus the variant's image, if any.

    Falls back to the bare value when the axis name is unknown, so listings that
    only carry the inline "#name" encoding still render sensibly.
    """
    labels: list[str] = []
    image: Optional[str] = None
    for part in (sku_attr or "").split(";"):
        part = part.strip()
        if not part:
            continue
        key = part.split("#", 1)[0].strip()
        meta = details.get(key) or {}
        value = meta.get("value") or (part.split("#", 1)[1].strip() if "#" in part else None)
        if not value:
            continue
        axis = meta.get("axis")
        labels.append(f"{axis}: {value}" if axis else value)
        if image is None and meta.get("image"):
            image = meta["image"]
    return labels, image


def _sku_attr_parts(sku_attr: str, prop_map: Optional[dict[str, str]] = None) -> list[str]:
    """
    Resolve a skuAttr into its human-readable value components. Handles both encodings:
      inline  "14:193#DDR4 16GB;200000828:x#R7 5825U" -> ["DDR4 16GB", "R7 5825U"]
      id-only "200007763:203372089"                    -> ["China Mainland"]  (via prop_map)
    """
    prop_map = prop_map or {}
    parts: list[str] = []
    for part in (sku_attr or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if "#" in part:
            name = part.split("#", 1)[1].strip()
        else:
            name = prop_map.get(part)
        if name:
            parts.append(name)
    return parts


def _sku_attr_spec(sku_attr: str, prop_map: Optional[dict[str, str]] = None) -> Optional[str]:
    """Join a skuAttr's resolved value components into "A · B · C" (or None)."""
    parts = _sku_attr_parts(sku_attr, prop_map)
    return " · ".join(parts) if parts else None


def _extract_variants(result: dict) -> list[dict]:
    """Join SKU.skuPaths with PRICE.skuPriceInfoMap into a config→price table."""
    sku = result.get("SKU") if isinstance(result.get("SKU"), dict) else {}
    price = result.get("PRICE") if isinstance(result.get("PRICE"), dict) else {}
    price_map = price.get("skuPriceInfoMap") if isinstance(price.get("skuPriceInfoMap"), dict) else {}
    paths = sku.get("skuPaths") if isinstance(sku.get("skuPaths"), list) else []
    prop_map = _sku_prop_map(sku)
    prop_details = _sku_prop_details(sku)
    gd = result.get("GLOBAL_DATA", {}).get("globalData", {})
    page_currency = gd.get("currencyCode") if isinstance(gd, dict) else None

    variants: list[dict] = []
    for p in paths:
        if not isinstance(p, dict):
            continue
        sku_id = p.get("skuIdStr") or (str(p.get("skuId")) if p.get("skuId") is not None else None)
        if not sku_id:
            continue
        pinfo = price_map.get(sku_id) if isinstance(price_map, dict) else None
        price_val = original = None
        currency = page_currency
        if isinstance(pinfo, dict):
            price_val = _normalize_price(pinfo.get("salePriceString"))
            op = pinfo.get("originalPrice")
            if isinstance(op, dict):
                original = op.get("value") if isinstance(op.get("value"), (int, float)) else _normalize_price(op.get("formatedAmount"))
                currency = op.get("currency") or page_currency
        spec_parts = _sku_attr_parts(p.get("skuAttr", ""), prop_map)
        label_parts, image = _sku_attr_detail_parts(p.get("skuAttr", ""), prop_details)
        variants.append({
            "sku_id": sku_id,
            "spec": " · ".join(spec_parts) if spec_parts else None,
            "spec_parts": spec_parts,
            "label_parts": label_parts,
            "image": image,
            "price": price_val,
            "original_price": original,
            "currency": currency,
            "in_stock": bool(p.get("salable")),
            "stock": p.get("skuStock"),
        })

    # Collapse indistinguishable rows: some listings carry an extra unnamed
    # dimension (e.g. plug/region) that duplicates the same visible spec + price.
    #
    # Rows that collide here are NOT the same product — they are distinct SKUs whose
    # difference AliExpress declined to label. Keeping only the first one and
    # dropping the rest handed the caller one sku_id out of several and no hint that
    # a choice was made for them, so `add_to_cart` could ship a different plug or
    # region than intended. Live example (item 1005011654394254, Aug 2026): sku
    # 12000056161378548 was silently dropped in favour of 12000056161378552 — same
    # rendered spec "Poland · EU", same price, different colour code (14:193 vs
    # 14:496) that resolves to no name. So keep the collapse for readability but
    # carry every sku_id it covers; the renderer prints them all.
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for v in variants:
        key = (v["spec"], round(v["price"], 2) if v["price"] is not None else None)
        entry = (v["sku_id"], v["in_stock"], v.get("stock"))
        if key in merged:
            if v["in_stock"]:
                merged[key]["in_stock"] = True
            merged[key]["covered_skus"].append(entry)
        else:
            v["covered_skus"] = [entry]
            merged[key] = v
            order.append(key)
    return [merged[k] for k in order]
