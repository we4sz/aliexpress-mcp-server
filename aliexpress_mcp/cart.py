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
from aliexpress_mcp.catalog import _fetch_pdp_mtop


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
                  quantity: Optional[int] = None) -> str:
    """
    Run an Ultron/droplet operation against one cart line and return the MTOP `ret`.

    The browser sends a deliberately small envelope: only the operated component
    (carrying `fields.operationType`) and the page root, never the whole tree —
    sending everything is rejected with AE-CART-PARSE-PARAM-ERROR.

    For "update_quantity" it also *replaces* `fields.quantityView` with a bare
    `{"current": N}` rather than editing the rendered object in place. Sending the
    full quantityView back is accepted with SUCCESS but silently does nothing.
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


def _resolve_sku_for_cart(item_id: str, sku_id: str = "") -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Pull the fields `cart.add` needs but the caller shouldn't have to know.

    Always resolves the shipping solution code (the browser sends it on every
    add, including when a variant is chosen explicitly). When `sku_id` is given
    it is validated against the item's real SKU list instead of being trusted —
    a wrong id would otherwise be sent to AliExpress verbatim.

    Returns (sku_id, fulfillment_service, error). On success `error` is None.
    """
    try:
        resp = _fetch_pdp_mtop(item_id)
    except Exception as e:
        return None, None, f"MTOP call failed: {e}"
    if not resp:
        return None, None, f"Could not fetch item {item_id} — MTOP returned no usable response."

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
            return None, None, (
                f"sku_id {sku_id} is not a variant of item {item_id}. "
                "Run get_variants on this item and use one of the listed sku_id values."
            )
        return str(sku_id), service, None

    default_id = sku.get("selectedSkuIdStr") or sku.get("selectedSkuId")
    if default_id is None:
        return None, None, (
            f"Item {item_id} exposes no default SKU — pass sku_id explicitly "
            "(get_variants lists them)."
        )
    if sku.get("selectedSkuSaleable") is False:
        return None, None, f"The default variant of item {item_id} is not saleable (out of stock)."
    return str(default_id), service, None


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
