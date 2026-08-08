"""
Cart domain: render/extract in both response shapes (legacy and the
Ultron/droplet shape that carries pagination and writes), pagination, and
the write operations (add / set-quantity / remove).

Moved verbatim out of aliexpress_mcp_server.py — see that file's module
docstring for the server-level overview.
"""

import base64
import gzip
import json
import re
from typing import Any, Optional

from aliexpress_mcp.core import (
    BASE_URL, COUNTRY, CURRENCY,
    CART_WRITE_MIN_INTERVAL, _pace, mtop_call, ret_problem,
    parse_price, _strip_html, _cents_to_float,
    blocks, _iter_blocks, _block_by_prefix,
)
from aliexpress_mcp.catalog import _fetch_pdp_mtop, _sku_spec_for_id, _extract_variants


# --- Cart-line selection (checkbox) -----------------------------------------
#
# AliExpress only checks out lines whose checkbox is ticked. A line can be
# fully valid, in stock, and sitting in the cart, and still silently NOT be
# part of the next order because its checkbox got cleared — a bulk "deselect"
# after browsing a promo, an accidental tap on "select shop" twice, etc. A
# live cart was seen showing "Checkout (17)" against 18 rendered lines with no
# way to tell which one wouldn't ship. `selected` below is the read side of
# that; `_cart_set_selected` near the bottom of this file is the write side.
#
# Read side (CONFIRMED against a real render, Aug 2026 — see the
# `cart_render.json` fixture / tests/test_units.py): the *legacy* shape
# (`hierarchy`/`linkage`, tag "product_item_component", no itemView) carries
# it at `fields.checkbox` = {"enable": bool, "selected": bool}. 10 of 24 real
# cart lines had `selected: false`, and the count matched the render's own
# `summary_component.fields.summary.selectItemNum` exactly (10) — cross-checked,
# not just present.
#
# The *droplet* shape (itemView-nested, `quantityView`/`priceViews`/
# `logisticsView`/`shopView` siblings — see `_extract_cart_droplet`) is what
# `_cart_operate` actually writes against. It was originally assumed to rename
# this field to "checkboxView", by analogy with quantity -> quantityView and
# prices -> priceViews. It does NOT: a live droplet render (Aug 2026) carries
# a plain `checkbox: {"enable": bool, "selected": bool}` on the product
# component, exactly as the legacy shape does, alongside all the *View
# siblings. The "View" rename is not the universal rule it looked like.
#
# Reading was correct anyway only because the lookup fell through to the
# legacy name — worth remembering as a case where a fallback hid a wrong
# primary for as long as nobody checked which branch was firing.
CART_SELECT_FIELD = "checkbox"

# Write side operationType, sent as `fields.operationType` through the same
# `mtop.aliexpress.trade.cart.async` endpoint `_cart_operate` already drives
# for "update_quantity" and "delete".
#
# VERIFIED from a browser capture of a real tick, Aug 2026. It is the bare
# noun "checkbox" — not "update_checkbox", which was the natural guess from
# its sibling "update_quantity" and which AliExpress answered with
# ret=SUCCESS while changing nothing, the house pattern of acknowledging a
# no-op. Two guesses, both plausible, both wrong: the verb was not derivable
# because a render never echoes an operationType back (it is send-only), so
# there was nothing to read it off.
#
# `_cart_set_selected` re-reads and compares the flag rather than trusting
# `ret`, which is the only reason the wrong guess was caught rather than
# shipped as a working feature.
CART_OP_SELECT = "checkbox"


def _cart_line_selected(fields: dict) -> Optional[bool]:
    """
    Pull a cart line's checkbox state out of its `fields` dict.

    Both render shapes name this field the same way, so there is nothing to
    disambiguate — see CART_SELECT_FIELD. Returns None when the field is
    missing or malformed: "we could not read it" is not the same fact as "the
    user un-ticked it", and only one of those is worth warning about.
    """
    cb = fields.get(CART_SELECT_FIELD)
    return cb.get("selected") if isinstance(cb, dict) else None


def _extract_cart_droplet(resp: dict) -> dict:
    """
    Parse the cart's *droplet* render shape into the same dict `_extract_cart`
    returns, so the renderer doesn't care which shape it came from.

    `cart.render` answers in two different shapes depending on the payload: the
    legacy one (`hierarchy`/`linkage`, first page only) and this Ultron/droplet
    one (`components`/`page`), which is the only shape carrying pagination.
    """
    result: dict[str, Any] = {"items": [], "shops": {}, "count": None, "currency": None,
                              "subtotal": None, "shipping_fee": None, "total": None}

    for bid, block in blocks(resp).items():
        if "product_component" not in bid or not isinstance(block, dict):
            continue
        f = block.get("fields") or {}
        iv = f.get("itemView") or {}
        item_id = iv.get("itemId")

        # Unavailable lines (sold out, delisted, no longer shippable) use a
        # different shape: no itemView, fields promoted to the top level, plus an
        # `invalidText` saying why. Skipping them hid real cart contents — the
        # caller needs to know something it may have planned around is gone.
        if not item_id and f.get("itemId"):
            result["items"].append({
                "item_id": str(f["itemId"]),
                "title": str(f.get("itemTitle") or "")[:200],
                "sku_id": f.get("skuId"),
                "url": f"{BASE_URL}/item/{f['itemId']}.html",
                "valid": False,
                "status": f.get("status"),
                "cart_id": f.get("cartId"),
                "quantity": (f.get("quantityView") or {}).get("current") or 1,
                "unavailable_reason": _strip_html(f.get("invalidText")) or "no longer available",
                "selected": _cart_line_selected(f),
            })
            continue

        if not item_id:
            continue

        item: dict[str, Any] = {
            "item_id": str(item_id),
            "title": str(iv.get("title") or "")[:200],
            "sku_info": (iv["sku"].get("skuInfo") if isinstance(iv.get("sku"), dict)
                         else iv.get("sku") if isinstance(iv.get("sku"), str) else None),
            "sku_id": iv.get("skuId"),
            "url": f"{BASE_URL}/item/{item_id}.html",
            "valid": iv.get("valid", True),
            "status": iv.get("status"),
            "cart_id": iv.get("cartId"),
            "quantity": (f.get("quantityView") or {}).get("current") or 1,
            "selected": _cart_line_selected(f),
        }

        for pv in (f.get("priceViews") or []):
            if not isinstance(pv, dict):
                continue
            if pv.get("priceType") == "showPrice" and isinstance(pv.get("value"), (int, float)):
                item["price"] = pv["value"]
                item["currency"] = pv.get("currency") or (pv.get("amount") or {}).get("currencyCode")
            elif pv.get("priceType") == "crossedPrice" and isinstance(pv.get("value"), (int, float)):
                item["original_price"] = pv["value"]

        lv = f.get("logisticsView") or {}
        if lv.get("freeShipping"):
            item["shipping_cost"] = 0.0
        elif lv.get("freightCost"):
            item["shipping_cost"] = parse_price(str(lv["freightCost"]))
        if lv.get("deliveryText"):
            item["delivery_date"] = re.sub(r"<[^>]+>", "", str(lv["deliveryText"])).replace("Delivery:", "").strip()

        shop = f.get("shopView") or {}
        if shop.get("name"):
            item["shop_name"] = shop["name"]
        if shop.get("homeUrl"):
            url = str(shop["homeUrl"])
            item["shop_url"] = "https:" + url if url.startswith("//") else url
        if shop.get("sellerId") is not None:
            item["seller_id"] = str(shop["sellerId"])

        result["items"].append(item)

    result["count"] = len(result["items"]) or None
    result["currency"] = next((i.get("currency") for i in result["items"] if i.get("currency")), None)
    return result


def _extract_cart_summary(resp: dict) -> dict:
    """
    Pull the cart's own totals out of the droplet render's summary component.

    These are the ONLY totals denominated in the session's pay currency. The
    legacy `platformType=DESKTOP` render ignores `_currency`/`shipToCountry` and
    always answers in USD, so merging its subtotal/total into the droplet result
    printed a USD number under the droplet's SEK label — a 67.63 USD estimate
    rendered as "67.63 SEK" against a cart whose real total was 1 366,66kr.

    Every amount here is a string AliExpress already formatted in the pay
    currency ("1 366,66kr", "- 1 021,35kr", "Free", "VAT included"). We pass them
    through verbatim rather than parsing to float and re-formatting, so a figure
    can never be relabelled into a currency it wasn't denominated in.

    Shape (live Aug 2026), block id `app_cart_summary_component_summary`:
        fields.payCurrencyCode                          "SEK"
        fields.summaryTabVO.priceBlockList[0]
            .summaryLines[]  {type, title.title, content.content}
            .selectItemNum                              20
        fields.totalSummaryLines[]                      (same row shape, fallback)
    """
    out: dict[str, Any] = {"currency": None, "lines": [], "selected_count": None}

    for bid, block in blocks(resp).items():
        if "summary" not in bid.lower() or not isinstance(block, dict):
            continue
        f = block.get("fields")
        if not isinstance(f, dict):
            continue
        out["currency"] = f.get("payCurrencyCode") or out["currency"]

        # priceBlockList[0] is the itemised breakdown (items total, discounts,
        # shipping, tax, estimated total); totalSummaryLines is the two-line
        # summary shown on the button. Prefer the breakdown, fall back to the
        # short form if the shape changes.
        rows: list = []
        tab = f.get("summaryTabVO")
        if isinstance(tab, dict):
            pbl = tab.get("priceBlockList")
            if isinstance(pbl, list) and pbl and isinstance(pbl[0], dict):
                pb = pbl[0]
                if isinstance(pb.get("selectItemNum"), int):
                    out["selected_count"] = pb["selectItemNum"]
                if isinstance(pb.get("summaryLines"), list):
                    rows = pb["summaryLines"]
        if not rows and isinstance(f.get("totalSummaryLines"), list):
            rows = f["totalSummaryLines"]

        for row in rows:
            if not isinstance(row, dict):
                continue
            content = row.get("content")
            text = content.get("content") if isinstance(content, dict) else None
            if not text:
                continue
            title = row.get("title")
            label = title.get("title") if isinstance(title, dict) else None
            out["lines"].append({
                "type": row.get("type"),
                "label": _strip_html(label) if label else None,
                "text": _strip_html(text) or str(text),
            })
        if out["lines"]:
            break

    return out


CART_PAGINATION_COMPONENT = "app_cart_pagination_container_page"
CART_MAX_PAGES = 10


def _cart_droplet_render(cookies: dict) -> dict:
    """Render the cart in the droplet shape (the only one supporting paging/writes)."""
    return mtop_call(
        "mtop.aliexpress.trade.cart.render", "1.0",
        {"_saasRegion": "AEG", "_currency": CURRENCY, "shipToCountry": COUNTRY,
         "locale": "en_US", "language": "en", "system": "pc",
         "bizParams": json.dumps({"platformType": "DESKTOP", "pcChoiceNewCart": 1},
                                 separators=(",", ":"))},
        cookies=cookies, referer=f"{BASE_URL}/p/shoppingcart/index.html",
    )


def _cart_operate(cookies: dict, resp: dict, component_id: str, operation: str,
                  quantity: Optional[int] = None, selected: Optional[bool] = None,
                  select_all: Optional[bool] = None) -> str:
    """
    Run an Ultron/droplet operation against one cart line and return the MTOP `ret`.

    The browser sends a deliberately small envelope: only the operated component
    (carrying `fields.operationType`) and the page root, never the whole tree —
    sending everything is rejected with AE-CART-PARSE-PARAM-ERROR.

    For "update_quantity" it also *replaces* `fields.quantityView` with a bare
    `{"current": N}` rather than editing the rendered object in place. Sending the
    full quantityView back is accepted with SUCCESS but silently does nothing.

    `selected` does the same replacement for the checkbox field (see the
    CART_SELECT_FIELD / CART_OP_SELECT comments above _extract_cart_droplet) —
    UNVERIFIED against a live checkbox toggle, unlike the quantity path above.
    """
    tree = resp.get("data") or {}
    # Named `components`, not `blocks`: a local `blocks` here shadows the imported
    # blocks() helper for the rest of this function, which would break any later
    # call to it silently.
    components = tree.get("data") or {}
    root = (tree.get("page") or {}).get("root")

    comp = json.loads(json.dumps(components[component_id]))
    comp.setdefault("fields", {})["operationType"] = operation
    if quantity is not None:
        comp["fields"]["quantityView"] = {"current": int(quantity)}
    if selected is not None:
        # Unlike quantityView — which is replaced with a bare {"current": N},
        # and silently no-ops if you echo the whole rendered object back — the
        # checkbox is sent COMPLETE, with `enable` preserved and only `selected`
        # flipped. Captured from the browser Aug 2026:
        #     "checkbox": {"enable": true, "selected": false}
        # Mirroring the quantity idiom here was wrong in both directions: wrong
        # verb and wrong payload shape.
        existing = comp["fields"].get(CART_SELECT_FIELD)
        checkbox = dict(existing) if isinstance(existing, dict) else {"enable": True}
        checkbox["selected"] = bool(selected)
        comp["fields"][CART_SELECT_FIELD] = checkbox
    if select_all is not None:
        # The cart HEADER carries TWO selection fields meaning different things,
        # and the obvious one is not the operative one. `checkbox` is the
        # pre-click display state; `checkBoxSelected` is the intent. A captured
        # "select all" click sent:
        #     checkbox: {"enable": true, "selected": false}, checkBoxSelected: true
        # i.e. it echoed the OLD state and stated the new one separately.
        #
        # Setting `checkbox` here instead — which is exactly what the per-line
        # path correctly does, so it looks right — leaves checkBoxSelected at
        # whatever the render held, and un-ticked a whole 22-line cart in
        # response to a request to tick it. Verified the hard way.
        comp["fields"]["checkBoxSelected"] = bool(select_all)
    comp["needSubmit"] = True
    data = {component_id: comp}
    if root and root in components:
        root_comp = json.loads(json.dumps(components[root]))
        root_comp["needSubmit"] = True
        data[root] = root_comp

    outer = {
        "config": {"fromDroplet": True, "pageName": "cart_droplet2_web", "protocolVersion": "1.0"},
        "operator": component_id,
        "asyncHandler": tree.get("asyncHandler") or {},
        "data": data,
        "page": tree.get("page") or {},
    }
    payload = {
        "compress": True,
        "params": base64.b64encode(gzip.compress(
            json.dumps(outer, separators=(",", ":"), ensure_ascii=False).encode())).decode(),
        "bizParams": json.dumps({"platformType": "DESKTOP", "pcChoiceNewCart": 1},
                                separators=(",", ":")),
        "_saasRegion": "aeg", "_currency": CURRENCY, "shipToCountry": COUNTRY,
        "_state": "", "_city": "", "locale": "en_US", "nextPage": False,
    }
    _pace("cart_write", CART_WRITE_MIN_INTERVAL)
    resp2 = mtop_call("mtop.aliexpress.trade.cart.async", "1.0", payload, cookies=cookies,
                      referer=f"{BASE_URL}/p/shoppingcart/index.html", method="POST")
    return (resp2.get("ret") or ["?"])[0]


def _cart_lines(resp: dict) -> list[dict]:
    """Flatten a rendered cart into [{component_id, item_id, sku_id, cart_id, title, qty}]."""
    rows = []
    for bid, block in blocks(resp).items():
        if "product_component" not in bid or not isinstance(block, dict):
            continue
        f = block.get("fields") or {}
        iv = f.get("itemView") or {}
        if not iv.get("itemId"):
            continue
        rows.append({
            "component_id": bid,
            "item_id": str(iv.get("itemId")),
            "sku_id": str(iv.get("skuId")) if iv.get("skuId") is not None else None,
            "cart_id": str(iv.get("cartId")) if iv.get("cartId") is not None else None,
            "title": str(iv.get("title") or "")[:70],
            "qty": (f.get("quantityView") or {}).get("current"),
        })
    return rows


def _cart_fetch_all_pages(cookies: dict, first: dict) -> tuple[dict, Optional[str]]:
    """
    Walk the cart's remaining pages and merge them into `first`.

    `cart.render` has no page parameter — it always returns page 1. Further pages
    come from the Ultron/droplet endpoint: POST the rendered component tree back
    with the pagination component as `operator` and `nextPage: true`, which
    returns the next slice ("strategy": "append"). Merging the block maps lets
    the ordinary cart extractor see every line.

    The pagination component's id carries a server-assigned numeric suffix (like
    order-list block ids), so it's found by prefix match on CART_PAGINATION_COMPONENT
    and that discovered id is reused as `operator` for the whole walk, rather than
    assuming the bare constant is the exact id.

    Returns (merged_response, warning). `warning` is set when the walk stopped
    early, so the caller can say so rather than silently under-reporting — this
    includes the server replaying a page (0 net-new cart-line blocks after a
    merge), which stops immediately instead of burning the rest of the page
    budget only to blame the cap.
    """
    merged = json.loads(json.dumps(first))
    page_id, state = _block_by_prefix(merged, CART_PAGINATION_COMPONENT)
    if not state.get("hasMore"):
        return merged, None

    def cart_line_count(r):
        return sum(1 for k in blocks(r) if "product_component" in k)

    for _ in range(CART_MAX_PAGES - 1):
        tree = merged.get("data") or {}
        outer = {
            "config": tree.get("config") or {
                "fromDroplet": True, "pageName": "cart_droplet2_web", "protocolVersion": "1.0"},
            "operator": page_id,
            "asyncHandler": tree.get("asyncHandler") or {},
            "data": tree.get("data") or {},
            "page": tree.get("page") or {},
        }
        blob = base64.b64encode(gzip.compress(
            json.dumps(outer, separators=(",", ":"), ensure_ascii=False).encode())).decode()
        payload = {
            "compress": True,
            "params": blob,
            "bizParams": json.dumps({"platformType": "DESKTOP", "pcChoiceNewCart": 1},
                                    separators=(",", ":")),
            "_saasRegion": "aeg", "_currency": CURRENCY, "shipToCountry": COUNTRY,
            "_state": "", "_city": "", "locale": "en_US", "nextPage": True,
        }
        try:
            resp = mtop_call(
                "mtop.aliexpress.trade.cart.async", "1.0", payload,
                cookies=cookies, referer=f"{BASE_URL}/p/shoppingcart/index.html",
                method="POST",
            )
        except Exception as e:
            return merged, f"stopped paging after an error: {e}"

        if ret_problem(resp):
            return merged, f"stopped paging — the cart API returned {(resp.get('ret') or ['?'])[0]}"

        new_blocks = blocks(resp)
        if not new_blocks:
            return merged, None
        before = cart_line_count(merged)
        merged.setdefault("data", {}).setdefault("data", {}).update(new_blocks)
        if cart_line_count(merged) <= before:
            return merged, "stopped paging — the server replayed a page"

        # Carry the response's own asyncHandler/page forward. The handler encodes
        # where the cursor is, so reusing the first page's copy asks for "the page
        # after page 1" every time — which silently caps the walk at two pages.
        for key in ("asyncHandler", "page", "config"):
            if (resp.get("data") or {}).get(key):
                merged["data"][key] = (resp.get("data") or {})[key]

        # The response carries a refreshed pagination component; trust it for the
        # loop condition so a cart that grows mid-walk still terminates.
        _, page_state = _block_by_prefix(resp, CART_PAGINATION_COMPONENT)
        if not page_state.get("hasMore"):
            return merged, None

    return merged, f"stopped after {CART_MAX_PAGES} pages — some items may be missing"


def _extract_cart(render_response: dict) -> dict:
    """
    Pull structured cart content from a `mtop.aliexpress.trade.cart.render` v1.0
    response. The response's `data.data` is a flat block-map keyed by block ID.
    We recognize these tags:

        product_item_component  → a cart line
        store_title_component   → seller/shop heading
        summary_component       → totals / checkout summary
        cart_header_component   → the top bar (contains `count`)

    Each item block's `itemId` is the real product ID; prices live in
    `prices.children.retailPrice.amount.{cent,currencyCode}`; quantity is
    `quantity.current`; variant text is `sku.skuInfo`.
    """
    result: dict[str, Any] = {
        "items": [],
        "shops": {},              # sellerId -> shop dict
        "count": None,
        "currency": None,
        "subtotal": None,
        "shipping_fee": None,
        "total": None,
    }

    for bid, b in _iter_blocks(render_response):
        tag = b.get("tag") or ""
        fields = b.get("fields") or {}
        if not isinstance(fields, dict):
            continue

        if tag.startswith("cart_header_component"):
            c = fields.get("count")
            if c is not None:
                try:
                    result["count"] = int(c)
                except (TypeError, ValueError):
                    pass

        elif tag.startswith("store_title_component"):
            sid = fields.get("sellerId") or fields.get("shopId")
            url = fields.get("url") or ""
            if isinstance(url, str) and url.startswith("//"):
                url = "https:" + url
            result["shops"][str(sid)] = {
                "name": fields.get("title"),
                "url": url,
                "shop_id": fields.get("shopId"),
                "seller_id": sid,
                # cart-line ids belonging to this shop — the link back to items
                "product_ids": [str(x) for x in (fields.get("productIds") or [])],
            }

        elif tag.startswith("product_item_component"):
            item_id = fields.get("itemId") or fields.get("productId")
            title = fields.get("title")
            if not item_id or not title:
                continue

            item: dict[str, Any] = {
                "item_id": str(item_id),
                "title": str(title)[:200],
                "sku_info": (fields.get("sku") or {}).get("skuInfo") if isinstance(fields.get("sku"), dict) else None,
                "sku_id": fields.get("skuId"),
                "url": f"{BASE_URL}/item/{item_id}.html",
                "valid": fields.get("valid", True),
                "status": fields.get("status"),
                "cart_id": fields.get("cartId"),
                # Whether this line's checkout checkbox is ticked — see the
                # CART_SELECT_FIELD comment above _extract_cart_droplet. This is
                # the shape it was actually confirmed against (fields.checkbox).
                "selected": _cart_line_selected(fields),
            }

            # Quantity
            q = fields.get("quantity") or {}
            if isinstance(q, dict):
                item["quantity"] = q.get("current") or q.get("origin") or 1
            else:
                try:
                    item["quantity"] = int(q)
                except (TypeError, ValueError):
                    item["quantity"] = 1

            # Prices: current sale price
            prices_node = fields.get("prices") or {}
            children = prices_node.get("children") if isinstance(prices_node, dict) else None
            if isinstance(children, dict):
                retail = children.get("retailPrice") or children.get("salePrice")
                if isinstance(retail, dict):
                    p, c = _cents_to_float(retail)
                    item["price"] = p
                    if c and not item.get("currency"):
                        item["currency"] = c

            # Original (strike) price
            orig = fields.get("originalPrice")
            if isinstance(orig, dict):
                fmt = orig.get("formattedAmount")
                if fmt:
                    item["original_price"] = parse_price(fmt)

            # Freight
            freight = fields.get("freightInfo") or {}
            svc_list = freight.get("availableFreightServices") if isinstance(freight, dict) else None
            if isinstance(svc_list, list) and svc_list:
                chosen = next((s for s in svc_list if s.get("chosen")), svc_list[0])
                if isinstance(chosen, dict):
                    fcost = chosen.get("freightCost") or ""
                    # Format: "Shipping: $1.99" or "Free shipping"
                    if isinstance(fcost, str):
                        if "free" in fcost.lower():
                            item["shipping_cost"] = 0.0
                        else:
                            item["shipping_cost"] = parse_price(fcost)
                    item["delivery_date"] = chosen.get("deliveryDate")

            result["items"].append(item)

        elif tag.startswith("summary_component"):
            # Real structure:
            #   fields.summary.priceMap.total
            #   fields.summary.priceDetail[*].priceMap.{subTotal, total, shippingFee}
            # Also fallback: fields.checkout.orderDetail[*].priceMap.*
            summary = fields.get("summary") or {}
            pm = summary.get("priceMap") if isinstance(summary, dict) else None
            if isinstance(pm, dict):
                t = pm.get("total")
                if isinstance(t, dict):
                    v, c = _cents_to_float(t)
                    result["total"] = v
                    if c:
                        result["currency"] = c

            # Walk priceDetail groups — they have subTotal/shippingFee per order group
            pd = summary.get("priceDetail") if isinstance(summary, dict) else None
            if isinstance(pd, list):
                for grp in pd:
                    grp_pm = grp.get("priceMap") if isinstance(grp, dict) else None
                    if not isinstance(grp_pm, dict):
                        continue
                    if result["subtotal"] is None and "subTotal" in grp_pm:
                        result["subtotal"] = _cents_to_float(grp_pm["subTotal"])[0]
                    if result["shipping_fee"] is None and "shippingFee" in grp_pm:
                        result["shipping_fee"] = _cents_to_float(grp_pm["shippingFee"])[0]
                    if result["total"] is None and "total" in grp_pm:
                        v, c = _cents_to_float(grp_pm["total"])
                        result["total"] = v
                        if c and not result.get("currency"):
                            result["currency"] = c

            # Last-ditch: checkout.orderDetail
            checkout = fields.get("checkout") or {}
            if isinstance(checkout, dict):
                for grp in checkout.get("orderDetail") or []:
                    grp_pm = grp.get("priceMap") if isinstance(grp, dict) else None
                    if not isinstance(grp_pm, dict):
                        continue
                    if result["total"] is None and "total" in grp_pm:
                        v, c = _cents_to_float(grp_pm["total"])
                        result["total"] = v
                        if c and not result.get("currency"):
                            result["currency"] = c

    # Attach each cart line to its shop via store_title.productIds ↔ item.cart_id,
    # so the caller can group items under their seller instead of two decoupled lists.
    line_to_shop: dict[str, dict] = {}
    for shop in result["shops"].values():
        for pid in shop.get("product_ids", []):
            line_to_shop[pid] = shop
    for it in result["items"]:
        shop = line_to_shop.get(str(it.get("cart_id")))
        if shop:
            it["shop_name"] = shop.get("name")
            it["shop_url"] = shop.get("url")
            it["seller_id"] = shop.get("seller_id")

    # Currency fallback: if the summary didn't carry one, use the items'.
    if not result.get("currency"):
        result["currency"] = next((it.get("currency") for it in result["items"] if it.get("currency")), None)

    return result


def _cart_variant_label(result: dict, sku_id: str) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """
    Human-readable (spec, price, currency) for one sku_id, given an
    already-fetched PDP `result` dict — pure, no I/O, so a caller that already
    has a PDP response in hand (like _resolve_sku_for_cart, below) doesn't pay
    for a second MTOP round-trip just to label a variant.

    "Added item 1005007805734021 (variant 12000042262236139) x1" gives no way
    to catch a wrong-variant add — the wrong length, the wrong colour, the
    wrong plug — until the mistake shows up later. This is the data for
    something checkable instead: 'Silicone hookup wire - "28 AWG 60m" -
    106.58 SEK'.

    Reuses catalog._sku_spec_for_id (a direct per-id lookup against
    SKU.skuPaths — always finds the id if it's a real variant of this item)
    for the spec, and catalog._extract_variants for price/currency rather than
    re-deriving either. _extract_variants collapses variants that share an
    identical (spec, price) into one row and files the rest under
    `covered_skus` (see its docstring) — this sku_id may only appear there,
    not as the row's own top-level sku_id, so both are checked.
    """
    spec = _sku_spec_for_id(result, sku_id)
    price = currency = None
    for v in _extract_variants(result):
        ids = {v.get("sku_id")} | {str(cs[0]) for cs in (v.get("covered_skus") or [])}
        if str(sku_id) in ids:
            price, currency = v.get("price"), v.get("currency")
            break
    return spec, price, currency


def _resolve_sku_for_cart(item_id: str, sku_id: str = "") -> tuple[
        Optional[str], Optional[str], Optional[str], Optional[float], Optional[str],
        Optional[str], Optional[str]]:
    """
    Pull the fields `cart.add` needs but the caller shouldn't have to know.

    Always resolves the shipping solution code (the browser sends it on every
    add, including when a variant is chosen explicitly). When `sku_id` is given
    it is validated against the item's real SKU list instead of being trusted —
    a wrong id would otherwise be sent to AliExpress verbatim.

    Also resolves the variant's human-readable spec and unit price/currency
    (see _cart_variant_label) off the SAME PDP fetch this function already
    makes, at zero extra MTOP cost — so a caller confirming a cart write can
    print something a person can check against what they meant to add,
    instead of only a sku_id.

    Returns (sku_id, fulfillment_service, spec, price, currency, error). On
    success `error` is None; `spec`/`price`/`currency` can still individually
    be None (the label lookup is best-effort — a listing with unparsable SKU
    data shouldn't fail the add itself) without that meaning failure.
    """
    try:
        resp = _fetch_pdp_mtop(item_id)
    except Exception as e:
        return None, None, None, None, None, f"MTOP call failed: {e}", None
    if not resp:
        return None, None, None, None, None, f"Could not fetch item {item_id} — MTOP returned no usable response.", None

    result = (resp.get("data") or {}).get("result") or resp.get("result") or {}
    sku = result.get("SKU") or {}
    layouts = (result.get("SHIPPING") or {}).get("originalLayoutResultList") or []
    biz = (layouts[0].get("bizData") if layouts and isinstance(layouts[0], dict) else {}) or {}
    service = biz.get("deliveryOptionCode")

    if sku_id:
        paths = sku.get("skuPaths") if isinstance(sku.get("skuPaths"), list) else []
        known = {
            str(p.get("skuIdStr") or p.get("skuId"))
            for p in paths if isinstance(p, dict) and (p.get("skuIdStr") or p.get("skuId")) is not None
        }
        if known and str(sku_id) not in known:
            return None, None, None, None, None, (
                f"sku_id {sku_id} is not a variant of item {item_id}. "
                "Run get_variants on this item and use one of the listed sku_id values."
            ), None
        resolved_id = str(sku_id)
        spec, price, currency = _cart_variant_label(result, resolved_id)
        return resolved_id, service, spec, price, currency, None, None

    default_id = sku.get("selectedSkuIdStr") or sku.get("selectedSkuId")
    if default_id is None:
        return None, None, None, None, None, (
            f"Item {item_id} exposes no default SKU — pass sku_id explicitly "
            "(get_variants lists them)."
        ), None
    if sku.get("selectedSkuSaleable") is False:
        return None, None, None, None, None, f"The default variant of item {item_id} is not saleable (out of stock).", None
    resolved_id = str(default_id)
    spec, price, currency = _cart_variant_label(result, resolved_id)

    # How many configurations does this listing actually have? A caller who
    # omitted sku_id on a multi-config listing is buying whichever one the
    # seller happens to preselect, and across ~15 multi-variant listings checked
    # in one real session that was the wanted config approximately NEVER: 80mm
    # fan packs defaulting for a 120mm need, 8x8cm for 12x12cm, 1.5mm solder for
    # 0.8mm, single-size drill packs where a set was wanted. The add still goes
    # through — refusing would be worse, since some listings genuinely have one
    # real choice — but the caller is told, by name, what it picked for them.
    paths = sku.get("skuPaths") if isinstance(sku.get("skuPaths"), list) else []
    n_configs = len({str(pth.get("skuIdStr") or pth.get("skuId"))
                     for pth in paths
                     if isinstance(pth, dict) and (pth.get("skuIdStr") or pth.get("skuId")) is not None})
    if n_configs > 1:
        warn = (f"no sku_id was given, so this is the seller's preselected config "
                f"of {n_configs} — run get_variants on {item_id} and pass sku_id if "
                "size, colour, length or pack count matters")
        return resolved_id, service, spec, price, currency, None, warn
    return resolved_id, service, spec, price, currency, None, None


def _resolve_cart_target(cookies: dict, item_id: str, cart_id: str, sku_id: str):
    """
    Find the single cart line a write should act on.

    Returns (resp, target, error). Refuses to guess when one item_id spans
    several lines — that happens whenever the same product is in the cart under
    two variants, and picking wrong would edit the wrong thing.
    """
    try:
        resp = _cart_droplet_render(cookies)
        if ret_problem(resp):
            return None, None, f"Could not read the cart: {(resp.get('ret') or ['?'])[0]}"
        resp, _ = _cart_fetch_all_pages(cookies, resp)
    except Exception as e:
        return None, None, f"Cart MTOP call failed: {e}"

    rows = _cart_lines(resp)
    if cart_id:
        matches = [r for r in rows if r["cart_id"] == str(cart_id)]
    else:
        matches = [r for r in rows if r["item_id"] == str(item_id)]
        if sku_id:
            matches = [r for r in matches if r["sku_id"] == str(sku_id)]

    if not matches:
        which = f"cart_id {cart_id}" if cart_id else f"item {item_id}"
        return None, None, f"No cart line matches {which}. Run view_cart to see what's there."
    if len(matches) > 1:
        listing = "\n".join(
            f"  - cart_id {m['cart_id']} · variant {m['sku_id']} · ×{m['qty']} · {m['title']}"
            for m in matches)
        return None, None, (
            f"Item {item_id} occupies {len(matches)} cart lines — refusing to guess which "
            f"one you mean. Re-run with the cart_id:\n{listing}")
    return resp, matches[0], None


# The cart header carries its own "select all" checkbox and IS a real control —
# a captured click operates on it with the same `operationType: "checkbox"`,
# differing only in which component is the operator. It is deliberately NOT
# used here. Two attempts to drive it failed: setting its `checkbox.selected`
# un-ticked an entire 22-line cart in response to a request to TICK it, and
# setting its `checkBoxSelected` (the field the capture actually carries the
# intent in) was an inert no-op. One write instead of N is not worth an
# operation that can silently empty a checkout, so `all_lines` fans out over
# the per-line path instead — which IS verified, and whose payload is
# byte-shape-identical to what Chrome sends (same two components: the operated
# product and the `global_cart_*` page root).
#
# For anyone picking this back up: the header is `app_cart_head_component*`,
# and its two selection fields mean different things — `checkbox` is the
# pre-click display state, `checkBoxSelected` is the intent (a capture of a
# "select all" click sends checkbox.selected=false alongside
# checkBoxSelected=true). Knowing that still was not enough to make it work.


def _selection_map(resp: dict) -> dict:
    """{cart_id: (selected, title)} for every line in a rendered cart."""
    return {str(i.get("cart_id")): (i.get("selected"), i.get("title"))
            for i in _extract_cart_droplet(resp).get("items", [])}


def _cart_set_selected(cookies: dict, item_id: str, cart_id: str, sku_id: str,
                       selected: bool) -> tuple[Optional[dict], Optional[str], list]:
    """
    Tick or untick one cart line's checkout checkbox — the write side of the
    `selected` field on cart items (see CART_SELECT_FIELD / CART_OP_SELECT).

    Mirrors set_cart_quantity/remove_from_cart's shape: resolve the target via
    _resolve_cart_target, fire the operation, then re-read and check the ACTUAL
    state rather than trusting `ret`, because this API returns SUCCESS for
    no-ops as readily as for real changes — which is exactly how the original
    wrong operationType was caught.

    It also diffs the WHOLE selection set, not just the target. A user report
    described ticking lines and watching untouched ones silently lose their
    tick; that could not be reproduced here (single ticks, consecutive ticks,
    quantity changes and add/remove were all clean, and the server's
    selectItemNum agreed with the parse exactly), but the failure it describes
    is the worst kind this tool can have: an un-ticked line stays visible in
    the cart and simply never arrives, so nobody finds out until the parcel is
    short. If it ever does happen, it should be loud rather than discovered
    after the order. Hence the third return value.

    Returns (line, error, collateral):
      - (None, msg, [])   — resolution or the write failed outright.
      - (line, msg, ...)  — SUCCESS returned but the target's flag did not move.
      - (line, None, ...) — confirmed; the target now matches the request.
    `collateral` is [(cart_id, title, before, after)] for every OTHER line whose
    selection changed during the call — normally empty.
    """
    resp, target, err = _resolve_cart_target(cookies, item_id, cart_id, sku_id)
    if err:
        return None, err, []
    before = _selection_map(resp)

    try:
        ret = _cart_operate(cookies, resp, target["component_id"], CART_OP_SELECT,
                            selected=selected)
    except Exception as e:
        return None, f"Selection change failed: {e}", []
    if ret_problem({"ret": [ret]}) is not None:
        return None, f"Could not update cart line {target['cart_id']} — AliExpress said: {ret}", []

    try:
        fresh, _ = _cart_fetch_all_pages(cookies, _cart_droplet_render(cookies))
    except Exception as e:
        return None, f"Selection request sent (ret={ret}), but re-reading the cart failed: {e}", []

    cart = _extract_cart_droplet(fresh)
    # Every line EXCEPT the target that changed selection state during the call.
    after = _selection_map(fresh)
    collateral = [
        (cid, before[cid][1], before[cid][0], after[cid][0])
        for cid in before
        if cid != target["cart_id"] and cid in after and before[cid][0] != after[cid][0]
    ]
    line = next((i for i in cart["items"] if str(i.get("cart_id")) == target["cart_id"]), None)
    if line is None:
        return None, f"Cart line {target['cart_id']} disappeared after the change — check view_cart.", collateral

    now = line.get("selected")
    if now is None:
        return line, (
            f"AliExpress accepted the request (ret={ret}) but the re-read cart carries no "
            f"selection field to confirm against — CART_SELECT_FIELD ({CART_SELECT_FIELD!r}) is "
            "probably the wrong guess for this shape. Needs a live capture to fix."
        ), collateral
    if now != selected:
        return line, (
            f"AliExpress accepted the request (ret={ret}) but cart line {target['cart_id']} is "
            f"still {'selected' if now else 'unselected'}, not "
            f"{'selected' if selected else 'unselected'} as requested — CART_OP_SELECT "
            f"({CART_OP_SELECT!r}) is probably the wrong guess."
        ), collateral
    return line, None, collateral
