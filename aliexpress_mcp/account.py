"""
Account domain: order history (read-only) and wishlist (read-only browsing,
plus the list/save/delete writes).

Moved verbatim out of aliexpress_mcp_server.py — see that file's module
docstring for the server-level overview.
"""

import json
import re
from typing import Optional

from aliexpress_mcp.core import (
    BASE_URL, COUNTRY, CURRENCY, LANG,
    CART_WRITE_MIN_INTERVAL, _pace, mtop_call, ret_problem,
    _fmt_money, _normalize_price, _strip_html, _fmt_epoch_ms, _cents_to_float,
    blocks, _iter_blocks, _block_by_prefix,
)


# ─── Orders (read-only) ───────────────────────────────────────────────────────
#
# Buyer order endpoints — names lifted from AliExpress's own JS bundle (Jul 2026):
#   mtop.aliexpress.trade.buyer.order.list    — order list (used here)
#   mtop.aliexpress.trade.buyer.order.detail  — returns async-loaded shells; the
#       parcel-trace events load via a further call, so we don't use it yet.
#   mtop.aliexpress.trade.buyer.order.count   — counts per status (unused)
# These require a FULL login session: the quick `document.cookie` copy omits the
# HttpOnly login cookies, so the API returns FAIL_SYS_SESSION_EXPIRED until those
# are added. The list response is a `data.data` block-map (like the cart): order
# blocks carry `fields.orderId` + `fields.orderLines`; prices are pre-formatted
# strings. Reverse-engineered live Jul 2026. We never touch `...order.operation`
# (pay/cancel/confirm) — this server is read-only.

ORDER_LIST_API = "mtop.aliexpress.trade.buyer.order.list"


def _extract_orders(resp: dict, max_orders: int) -> list[dict]:
    """
    Parse the order.list block-map. `data.data` is keyed by block id; order blocks
    are the ones whose `fields` carry both `orderId` and an `orderLines` list.
    Prices come as pre-formatted display strings (already localized).
    """
    orders: list[dict] = []
    for _bid, b in _iter_blocks(resp):
        f = b.get("fields")
        if not isinstance(f, dict) or not f.get("orderId") or not isinstance(f.get("orderLines"), list):
            continue

        items = []
        for ol in f["orderLines"]:
            if not isinstance(ol, dict):
                continue
            raw = ol.get("itemPriceText") or ol.get("formatPriceInfo")
            items.append({
                "title": ol.get("itemTitle"),
                "price_text": raw,                       # raw localized display string
                "price": _normalize_price(raw),          # normalized numeric amount
                "currency": ol.get("currencyCode"),      # ISO code for that amount
                "quantity": ol.get("quantity"),
                "product_id": ol.get("productId"),
            })

        total_raw = f.get("totalPriceText") or f.get("formatPriceInfo")
        orders.append({
            "order_id": str(f.get("orderId")),
            "status": f.get("statusText") or f.get("statusTitle"),
            "date": f.get("orderDateText"),
            "total_text": total_raw,
            "total": _normalize_price(total_raw),
            "currency": f.get("currencyCode"),
            "store": f.get("storeName"),
            "items": items,
        })
    return orders[:max_orders]


def _order_money(amount: Optional[float], currency: Optional[str], raw: Optional[str]) -> Optional[str]:
    """
    Prefer a normalized "amount CUR" string (self-describing, one glyph convention);
    fall back to the raw localized display text if we couldn't parse it. Order
    prices legitimately span currencies (each order was paid in its own currency —
    e.g. older orders in UAH, newer in USD) and can't be converted without FX, so
    we normalize the *format*, not the currency.
    """
    if amount is not None and currency:
        return _fmt_money(amount, currency)
    return raw


def _order_item_line(it: dict, bullet: str = "  • ") -> str:
    """Render one order line item as a compact text line."""
    seg = f"{bullet}{str(it.get('title') or '')[:100]}"
    qty = it.get("quantity")
    try:
        if qty and int(qty) != 1:
            seg += f" ×{qty}"
    except (TypeError, ValueError):
        pass
    money = _order_money(it.get("price"), it.get("currency"), it.get("price_text"))
    if money:
        seg += f" — {money}"
    # `productId` is parsed out of every order line and was then never printed, which
    # left order output as a dead end: no id meant no get_product_details, no
    # get_reviews and no re-order via add_to_cart. Name it `item_id` because that is
    # the parameter every product tool actually takes.
    if it.get("product_id"):
        seg += f"  [item_id: {it['product_id']}]"
    return seg


ORDERS_MAX_PAGES = 20


def _orders_fetch_all_pages(cookies: dict, first: dict, want: int) -> tuple[dict, Optional[str]]:
    """
    Walk the order list past page 1 and merge every page into `first`.

    `order.list` ignores top-level paging params. Further pages come from an
    Ultron/droplet POST whose `params` is a JSON *string* holding four more
    JSON *strings* (data / linkage / hierarchy / endpoint) plus `operator`.
    Nested-string encoding is load-bearing: passing objects gets an empty result.

    Returns (merged_response, warning); `warning` is set if the walk stopped
    early so the caller can say so instead of under-reporting silently. This
    includes the server replaying a page (0 net-new order blocks after a merge),
    which stops immediately instead of burning the rest of the page budget only
    to blame the cap.
    """
    merged = json.loads(json.dumps(first))
    body_id, body = _block_by_prefix(merged, "pc_om_list_body")
    if not body_id or not (body.get("fields") or {}).get("hasMore"):
        return merged, None

    head_id, head = _block_by_prefix(merged, "pc_om_list_header_action")
    page = int((body.get("fields") or {}).get("pageIndex") or 1)

    def order_count(r):
        return sum(1 for k in blocks(r) if k.startswith("pc_om_list_order_"))

    for _ in range(ORDERS_MAX_PAGES - 1):
        if order_count(merged) >= want:
            return merged, None
        data = merged.get("data") or {}
        page += 1

        next_body = json.loads(json.dumps(data["data"][body_id]))
        next_body.setdefault("fields", {})["pageIndex"] = page
        components = {body_id: next_body}
        if head_id:
            components[head_id] = data["data"][head_id]

        def s(obj):
            return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

        inner = {
            "data": s(components),
            "linkage": s(data.get("linkage") or {}),
            "hierarchy": s({"structure": (data.get("hierarchy") or {}).get("structure") or {}}),
            "endpoint": s(data.get("endpoint") or {}),
            "operator": body_id,
        }
        payload = {"params": s(inner), "shipToCountry": COUNTRY, "_lang": LANG}

        try:
            resp = mtop_call(
                ORDER_LIST_API, "1.0", payload, cookies=cookies,
                referer=f"{BASE_URL}/p/order/index.html", method="POST",
                extra_query={"post": "1", "isSec": "1", "ecode": "1",
                             "needLogin": "true", "method": "POST"},
            )
        except Exception as e:
            return merged, f"stopped after page {page - 1} — {e}"

        if ret_problem(resp):
            return merged, f"stopped after page {page - 1} — API returned {(resp.get('ret') or ['?'])[0]}"

        new_blocks = blocks(resp)
        new_orders = {k: v for k, v in new_blocks.items() if k.startswith("pc_om_list_order_")}
        if not new_orders:
            return merged, None

        before = order_count(merged)
        merged["data"]["data"].update(new_orders)
        if order_count(merged) <= before:
            return merged, f"stopped after page {page} — the server replayed a page"
        nb_id, nb = _block_by_prefix(resp, "pc_om_list_body")
        if nb_id:
            merged["data"]["data"][body_id] = nb
        if not (nb.get("fields") or {}).get("hasMore"):
            return merged, None

    return merged, f"stopped at the {ORDERS_MAX_PAGES}-page cap — older orders may exist"


# ─── Wishlist (read-only) ─────────────────────────────────────────────────────
#
# Saved / liked items come from `mtop.ae.wishlist.allItems.render` (name lifted
# from the wish-manage page bundle, Jul 2026). Needs a FULL login session (same
# gate as orders). Unlike cart/orders, the response uses the Ultron *container*
# envelope — items live one level deeper than the flat block-map:
#     resp.data.data.data    -> component map, item blocks tagged `wln_page_product`
#     resp.data.data.global  -> page globals (itemTotalCount)
# Per item: productBaseDTO (title/invalid/itemUrl; the real product id is in
# similarActionParams.productId, NOT the internal itemId), priceDTO.price /
# .crossPrice ({amount:{cent,currencyCode}}), and purchaseDTO.reducePriceText —
# an HTML snippet flagging a price drop since the item was saved.
# Read-only — we never call the wishlist add/remove endpoints.

WISHLIST_API = "mtop.ae.wishlist.allItems.render"
# Wishlist *groups* ("lists") are managed by a different API, at v2.0, which takes
# the group as a JSON string in `groupListString` rather than as an object.
WISHLIST_GROUP_API = "mtop.aliexpress.wishlist.group.update"
# Enumerating the lists is a *different* endpoint from reading their contents:
# `allItems.render` returns saved products and never names the groups, while this
# one returns the groups and never returns their products.
WISHLIST_GROUPS_API = "mtop.ae.wishlist.myList.render"
# Adds AND removes in one call: `addedItemIdStr` / `deletedItemIdStr` are JSON
# *strings*, and `currentGroupId` targets the list directly — so saving straight
# into a chosen list is one request, not save-then-move as the website does it.
WISHLIST_SAVE_API = "mtop.ae.wishlist.myList.saveItem"
# The ♡ itself. Saves a product into the wishlist (ungrouped); assigning it to a
# named list is the separate myList.saveItem call above.
WISHLIST_FAVOURITE_API = "mtop.aliexpress.wishlist.wishitem.save"
# The group list paginates at 6 per page, independently of the item pagination.
WISHLIST_GROUPS_PAGE_SIZE = 6
WISHLIST_GROUPS_MAX_PAGES = 10
_ITEM_URL_RE = re.compile(r"/item/(\d+)\.html")


def _wishlist_container(resp: dict) -> tuple[dict, dict]:
    """
    Return (components, globals) from the wishlist response, tolerant of shape.
    The live shape is the Ultron container (data.data.data / data.data.global);
    fall back to the flat block-map if a future response reverts to it.
    """
    outer = blocks(resp)
    if isinstance(outer, dict) and isinstance(outer.get("data"), dict):
        glob = outer.get("global")
        return outer["data"], (glob if isinstance(glob, dict) else {})
    comps = outer if isinstance(outer, dict) else {}
    glob = resp.get("data", {}).get("global")
    return comps, (glob if isinstance(glob, dict) else {})


def _extract_wishlist(resp: dict, max_items: int) -> dict:
    """Parse the wishlist allItems.render container into items + total + has_more."""
    comps, glob = _wishlist_container(resp)
    items: list[dict] = []
    has_more = False
    for bid, b in comps.items():
        if not isinstance(b, dict):
            continue
        tag = b.get("tag")
        if tag == "wln_paging" or "paging" in str(bid).lower():
            if (b.get("fields") or {}).get("hasMore"):
                has_more = True
            continue
        if tag != "wln_page_product":
            continue
        f = b.get("fields")
        if not isinstance(f, dict):
            continue
        pb = f.get("productBaseDTO") if isinstance(f.get("productBaseDTO"), dict) else {}
        if not pb:
            continue
        pr = f.get("priceDTO") if isinstance(f.get("priceDTO"), dict) else {}
        pu = f.get("purchaseDTO") if isinstance(f.get("purchaseDTO"), dict) else {}

        price, currency = _cents_to_float(pr.get("price"))
        if price is None:
            pnode = pr.get("price") if isinstance(pr.get("price"), dict) else {}
            price = _normalize_price(pnode.get("formattedAmount") or pnode.get("formatPriceInfo"))
            currency = currency or pnode.get("currency")
        original, _ = _cents_to_float(pr.get("crossPrice"))

        # Real product id lives in similarActionParams.productId / itemUrl — the
        # productBaseDTO.itemId is an internal wishlist id, not the /item/<id>.html one.
        sap = pb.get("similarActionParams") if isinstance(pb.get("similarActionParams"), dict) else {}
        item_url = pb.get("itemUrl")
        real_id = sap.get("productId")
        if not real_id and isinstance(item_url, str):
            m = _ITEM_URL_RE.search(item_url)
            if m:
                real_id = m.group(1)
        real_id = str(real_id or pb.get("itemId"))

        items.append({
            "item_id": real_id,
            "title": pb.get("title"),
            "price": price,
            "currency": currency,
            "original_price": original,
            "invalid": bool(pb.get("invalid")),
            "price_drop": _strip_html(pu.get("reducePriceText")),
            "added": _fmt_epoch_ms(pb.get("gmtCreate")),
            "url": item_url or f"{BASE_URL}/item/{real_id}.html",
        })

    return {"items": items[:max_items], "total": glob.get("itemTotalCount"), "has_more": has_more}


def _fetch_wishlist_groups(cookies: dict) -> tuple[list[dict], Optional[str]]:
    """
    Enumerate the account's wishlists (groups).

    Separate endpoint from the saved-items one: `allItems.render` returns products
    and never names the groups; this returns groups and never their products.
    Groups paginate at 6, independently of item pagination.

    Returns (groups, error).
    """
    groups: list[dict] = []
    for page in range(1, WISHLIST_GROUPS_MAX_PAGES + 1):
        try:
            resp = mtop_call(
                WISHLIST_GROUPS_API, "1.0",
                {"pageIndex": page, "locale": "en_US", "shipToCountry": COUNTRY,
                 "deviceType": "PC", "_lang": LANG, "_currency": CURRENCY},
                cookies=cookies, referer=f"{BASE_URL}/p/wish-manage/index.html",
            )
        except Exception as e:
            return groups, f"Wishlist group call failed: {e}"

        data = (resp.get("data") or {}).get("data") or {}
        if not (resp.get("data") or {}).get("succeed", True):
            return groups, f"AliExpress rejected the wishlist group request: {(resp.get('ret') or ['?'])[0]}"

        before = len(groups)
        for bid, block in (data.get("data") or {}).items():
            if not bid.startswith("wln_group_container_"):
                continue
            f = block.get("fields") or {}
            if f.get("groupId") is None:
                continue
            groups.append({
                "group_id": str(f["groupId"]),
                "name": f.get("name") or "(unnamed)",
                "item_count": f.get("itemCount"),
                # publishType "N" = private. `spreadId` is a share handle for a
                # private list — deliberately not surfaced.
                "public": f.get("publishType") == "Y",
            })
        paging = next((v.get("fields") for k, v in (data.get("data") or {}).items()
                       if k.startswith("wln_paging")), {}) or {}
        if len(groups) <= before or not paging.get("hasMore"):
            return groups, None

    return groups, f"stopped after {WISHLIST_GROUPS_MAX_PAGES} pages of lists"


def _resolve_wishlist_group(cookies: dict, wishlist: str):
    """
    Turn a list name or id into exactly one group. Returns (group, error).

    Refuses to guess: an unknown or ambiguous name is an error, never a silent
    fallback to the ungrouped default — items landing somewhere the user does not
    look is worse than a failed call.
    """
    groups, err = _fetch_wishlist_groups(cookies)
    if err and not groups:
        return None, err
    if not groups:
        return None, ("You have no wishlists yet — create one with create_wishlist first.")

    want = (wishlist or "").strip()
    exact_id = [g for g in groups if g["group_id"] == want]
    if exact_id:
        return exact_id[0], None

    matches = [g for g in groups if g["name"].casefold() == want.casefold()]
    if not matches:
        matches = [g for g in groups if want.casefold() in g["name"].casefold()]

    if not matches:
        listing = ", ".join(f"{g['name']!r}" for g in groups)
        return None, f"No wishlist matches {want!r}. Your lists: {listing}."
    if len(matches) > 1:
        listing = "\n".join(f"  - {g['name']!r} (id {g['group_id']})" for g in matches)
        return None, (f"{want!r} matches {len(matches)} lists — say which one "
                      f"(name exactly, or the id):\n{listing}")
    return matches[0], None


def _wishlist_saved_item_ids(cookies: dict, group_id: str = "0") -> set[str]:
    """Item ids already saved in the wishlist (group 0 = every saved item)."""
    try:
        resp = mtop_call(
            WISHLIST_API, "1.0",
            {"pageIndex": 1, "shipToCountry": COUNTRY, "locale": "en_US", "deviceType": "PC",
             "_lang": LANG, "_currency": CURRENCY, "wishGroupId": int(group_id)},
            cookies=cookies, referer=f"{BASE_URL}/p/wish-manage/index.html",
        )
    except Exception:
        return set()
    found: set[str] = set()
    for bid, block in blocks({"data": (resp.get("data") or {}).get("data") or {}}).items():
        if not bid.startswith("wln_page_product_"):
            continue
        base = ((block.get("fields") or {}).get("productBaseDTO") or {})
        if base.get("itemId") is not None:
            found.add(str(base["itemId"]))
    return found


def _wishlist_favourite(cookies: dict, item_id: str) -> str:
    """Save a product into the wishlist (the ♡). Lands ungrouped; returns MTOP ret."""
    _pace("cart_write", CART_WRITE_MIN_INTERVAL)
    resp = mtop_call(
        WISHLIST_FAVOURITE_API, "1.0",
        {"platform": "pc", "itemType": "product", "itemId": str(item_id)},
        cookies=cookies, referer=f"{BASE_URL}/item/{item_id}.html",
    )
    ret = (resp.get("ret") or ["?"])[0]
    data = resp.get("data") or {}
    if ret.startswith("SUCCESS") and data.get("succeed") is False:
        return f"FAILED::{data.get('message') or 'server reported no success'}"
    return ret


def _wishlist_delete_item(cookies: dict, item_id: str, group_id: str = "0") -> str:
    """
    Remove a saved item — `DELETE_PRODUCT` on the render endpoint.

    Different mechanism from `saveItem`: this is an Ultron/droplet operation on
    the *render* endpoint — echo the item's component back with
    `fields.operationType = "DELETE_PRODUCT"`, alongside the render's own linkage
    and hierarchy. `params` is a plain JSON string of nested OBJECTS here, unlike
    the orders pager which nests JSON *strings*.

    The verb is the same for both of AliExpress's two removals; only the SCOPE
    differs, and the scope is `wishGroupId` on the render AND on the operation:

      group_id "0"  -> the whole wishlist: deletes the item everywhere. Permanent.
      group_id <id> -> that one list only: un-groups it, the item stays saved.

    The UI names them separately ("Delete from my wishlist products" vs "Remove
    from collection"), which is the only hint that one call does both.
    """
    scope = str(group_id or "0")
    render = mtop_call(
        WISHLIST_API, "1.0",
        {"pageIndex": 1, "shipToCountry": COUNTRY, "locale": "en_US", "deviceType": "PC",
         "_lang": LANG, "_currency": CURRENCY, "wishGroupId": scope},
        cookies=cookies, referer=f"{BASE_URL}/p/wish-manage/index.html",
    )
    tree = (render.get("data") or {}).get("data") or {}
    comp_id = f"wln_page_product_I_{item_id}"
    component = (tree.get("data") or {}).get(comp_id)
    if not component:
        return ("NOTFOUND::item is not in your wishlist" if scope == "0"
                else "NOTFOUND::item is not in that list")

    operated = json.loads(json.dumps(component))
    operated.setdefault("fields", {})["operationType"] = "DELETE_PRODUCT"

    inner = {
        "endpoint": tree.get("endpoint") or {},
        "operator": comp_id,
        "linkage": tree.get("linkage") or {},
        "data": {comp_id: operated},
        "hierarchy": tree.get("hierarchy") or {},
    }
    payload = {
        "params": json.dumps(inner, separators=(",", ":"), ensure_ascii=False),
        "pageIndex": 1, "locale": "en_US", "shipToCountry": COUNTRY,
        "deviceType": "PC", "_lang": LANG, "_currency": CURRENCY, "wishGroupId": scope,
    }
    _pace("cart_write", CART_WRITE_MIN_INTERVAL)
    resp = mtop_call(WISHLIST_API, "1.0", payload, cookies=cookies,
                     referer=f"{BASE_URL}/p/wish-manage/index.html",
                     extra_query={"needLogin": "true"})
    ret = (resp.get("ret") or ["?"])[0]
    if ret.startswith("SUCCESS") and not (resp.get("data") or {}).get("succeed", True):
        return f"FAILED::{((resp.get('data') or {}).get('message')) or 'server reported no success'}"
    return ret


def _wishlist_delete_group(cookies: dict, group_id: str) -> str:
    """
    Delete a wishlist (group) — the inverse of `create_wishlist`.

    Note this does NOT use the v2.0 `group.update` API that creates lists, which
    has no delete opType. It is a droplet operation like the item delete, but on
    the *groups* render (`myList.render`): echo the group's own container
    component back with `fields.operationType = "DELETE_GROUP"`.

    Deletes the container only — items filed under it stay in the wishlist and
    become ungrouped. The caller verifies that rather than trusting the ack.
    """
    comp_id = f"wln_group_container_GH_{group_id}"
    tree = component = None
    for page in range(1, WISHLIST_GROUPS_MAX_PAGES + 1):
        render = mtop_call(
            WISHLIST_GROUPS_API, "1.0",
            {"pageIndex": page, "locale": "en_US", "shipToCountry": COUNTRY,
             "deviceType": "PC", "_lang": LANG, "_currency": CURRENCY},
            cookies=cookies, referer=f"{BASE_URL}/p/wish-manage/index.html",
        )
        tree = (render.get("data") or {}).get("data") or {}
        component = (tree.get("data") or {}).get(comp_id)
        if component:
            break
        paging = next((v.get("fields") for k, v in (tree.get("data") or {}).items()
                       if k.startswith("wln_paging")), {}) or {}
        if not paging.get("hasMore"):
            break
    if not component:
        return "NOTFOUND::no such wishlist"

    operated = json.loads(json.dumps(component))
    operated.setdefault("fields", {})["operationType"] = "DELETE_GROUP"

    inner = {
        "endpoint": tree.get("endpoint") or {},
        "operator": comp_id,
        "linkage": tree.get("linkage") or {},
        "data": {comp_id: operated},
        "hierarchy": tree.get("hierarchy") or {},
    }
    payload = {
        "params": json.dumps(inner, separators=(",", ":"), ensure_ascii=False),
        "pageIndex": 1, "locale": "en_US", "shipToCountry": COUNTRY,
        "deviceType": "PC", "_lang": LANG, "_currency": CURRENCY,
    }
    _pace("cart_write", CART_WRITE_MIN_INTERVAL)
    resp = mtop_call(WISHLIST_GROUPS_API, "1.0", payload, cookies=cookies,
                     referer=f"{BASE_URL}/p/wish-manage/index.html",
                     extra_query={"needLogin": "true"})
    ret = (resp.get("ret") or ["?"])[0]
    if ret.startswith("SUCCESS") and not (resp.get("data") or {}).get("succeed", True):
        return f"FAILED::{((resp.get('data') or {}).get('message')) or 'server reported no success'}"
    return ret


def _wishlist_save_item(cookies: dict, group_id: str, add: list[str], remove: list[str]) -> str:
    """
    Add and/or remove items in one list. Returns the MTOP `ret`.

    Item id arrays go as JSON *strings*, not arrays — the same nested-string
    encoding the cart and order protocols use.
    """
    payload = {
        "addedItemIdStr": json.dumps([int(i) for i in add], separators=(",", ":")),
        "deletedItemIdStr": json.dumps([int(i) for i in remove], separators=(",", ":")),
        "currentGroupId": int(group_id),
        "_lang": LANG,
        "_currency": CURRENCY,
    }
    _pace("cart_write", CART_WRITE_MIN_INTERVAL)
    resp = mtop_call(WISHLIST_SAVE_API, "1.0", payload, cookies=cookies,
                     referer=f"{BASE_URL}/p/wish-manage/index.html")
    ret = (resp.get("ret") or ["?"])[0]
    # This API family reports ret=SUCCESS while failing in `data`, so the inner
    # flag is the real verdict.
    if ret.startswith("SUCCESS") and not (resp.get("data") or {}).get("succeed"):
        return f"FAILED::{(resp.get('data') or {}).get('message') or 'server reported no success'}"
    return ret

