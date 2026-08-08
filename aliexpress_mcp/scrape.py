"""
AliExpress search-results-page and product-detail-page HTML parsing.

The ONLY BeautifulSoup consumer in this package — every other module works
off signed MTOP JSON responses, not scraped HTML. Moved verbatim out of
aliexpress_mcp_server.py — see that file's module docstring for the
server-level overview.
"""

import base64
import gzip
import json
import re
from datetime import datetime
from typing import Any, Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup

from aliexpress_mcp.core import BASE_URL, ITEM_ID_RE, parse_price, _parse_sold_count

# A search card advertises free shipping through one of these selling-point
# sources. 48 of 60 live hits carry one, so the *absence* is the notable case —
# tagging the majority would be pure bloat. Verified live Aug 2026.
FREE_SHIPPING_SOURCES = {"platformFreeShipping_atm", "Free_Shipping_atm"}


def _listing_age(lunch_time: Any) -> Optional[str]:
    """
    Render a search card's `lunchTime` (the listing's publish date, sic) as a
    compact age: "8d", "14mo", "5.5y".

    Age is a relister/dropshipper tell that no other field carries: a ★4.7 built
    from 21 orders on a listing published four days ago is a very different bet
    from the same rating on a three-year-old listing. Live values span 8 days to
    5.5 years, and every one of 120 sampled cards had the field.

    This is the LISTING's age, not the store's — the search payload carries no
    store age at all. The bare number is returned without a trailing "old" so the
    renderer can say "listed 5.5y ago"; the previous "5.5y old" was read as the
    seller's age in a review of this tool, which is the one wrong conclusion it
    must not invite.
    """
    if not isinstance(lunch_time, str) or not lunch_time.strip():
        return None
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(lunch_time.strip(), fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    days = (datetime.now() - dt).days
    if days < 0:
        return None
    if days < 90:
        return f"{days}d"
    if days < 730:
        return f"{days // 30}mo"
    return f"{days / 365.0:.1f}y"


def _is_sponsored(it: dict) -> bool:
    """
    Whether this card is a paid placement rather than an earned rank.

    AliExpress marks bought positions two different ways and — measured over 514
    live cards, Aug 2026 — NEVER both on the same card, so either one alone is the
    signal and the union is the count:

      `p4p`                          the ad click-tracking payload (Alibaba's ad
                                     product is literally "pay for placement"); its
                                     clickUrl points at us-click.aliexpress.com.
      `allPlatformInfo.adTag`        the badge the site paints on the card. Its
                                     tagText read "Ad" on all 96 archived cards
                                     carrying it — no other value appeared.

    How much of a page this covers varies enormously and is the reason it must be
    stated per search rather than assumed: across 8 live result pages the sponsored
    share ran 0/60, 0/60, 0/34, 1/60, 22/60, 27/60, 32/60 and 56/60 — that last one
    ("usb c cable") is 93% of the page. A consumer reasoning about "the top result"
    under a header claiming a sort order has no way to know which it is looking at.
    """
    if it.get("p4p"):
        return True
    api = it.get("allPlatformInfo")
    tag = api.get("adTag") if isinstance(api, dict) else None
    return bool(isinstance(tag, dict) and tag.get("tagText"))


def _sku_count(it: dict) -> Optional[int]:
    """
    How many buyable configurations the listing has, from `extraParams.sku_images`.

    That field is a gzip+base64 blob holding "<axisPropId>:<valueId>:<img>;<valueId>:<img>;…"
    — one entry per SKU, images repeated where several SKUs share a picture. Counting
    the entries matched `SKU.skuPaths` in the PDP response EXACTLY on all 15 items where
    both were captured (Aug 2026), across 1, 3, 4, 8, 10, 16, 21, 24, 28, 64 and 100 SKUs.
    The two remaining pairs carried no `sku_images` at all — both single-SKU legacy
    listings (32811041093, 1005012630102114) — so an absent field reads as unknown, not 1.

    This is the only thing on a search card that says the row's price is one
    configuration out of many; see `_format_product_lines` for why that matters.
    """
    raw = (it.get("extraParams") or {}).get("sku_images") if isinstance(it.get("extraParams"), dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        blob = gzip.decompress(base64.b64decode(raw)).decode("utf-8", "replace")
    except Exception:
        return None
    # Drop the leading "<axisPropId>:" header before splitting on the row separator.
    body = blob.split(":", 1)[1] if ":" in blob else blob
    n = len([p for p in body.split(";") if p.strip()])
    return n or None


def _search_signals(it: dict) -> dict:
    """
    Pull the decision-relevant signals AliExpress hides in a search card's
    tracking payload (`trace.pdpParams` / `trace.utLogMap`) and selling points.

    Everything here is already on the wire for every card — we were dropping it:

      ship_from     `pdp_cdi.shipFrom`, the warehouse country. The single most
                    decision-relevant field on the card: an EU warehouse means
                    days rather than weeks AND no import charges.
      is_choice     `utLogMap.isChoice` — changes who fulfils and who handles returns.
      sold_exact    `utLogMap.real_trade_count`, the exact order count. `trade.tradeDesc`
                    buckets it, and the buckets are coarse enough to be useless at
                    the top: 34 of 60 hits for one query all read "50,000+ sold"
                    and 17 more all read "100K+ sold", so the listing ranked #1 and
                    the one ranked #17 were indistinguishable. Live range 1–232,302.
      listing_age   from `lunchTime` — see `_listing_age`.
      duty_offset   `sellingPoints[source="npieces"]`, e.g. "Offset duty: 3€ off" —
                    a real landed-cost input, present on 6 of 60 hits.
      free_shipping whether any free-shipping badge is present (None if the card
                    carries no selling points at all, i.e. unknown rather than no).
      sponsored     whether the row is a paid placement — see `_is_sponsored`.
      variant_count how many SKUs the listing has — see `_sku_count`.
      price_sku_id  `prices.skuId`, the SKU the card's price actually belongs to.
                    Kept because it is the proof that the card quotes ONE
                    configuration: `prices.salePrice.minPrice` is named like a
                    listing minimum but is not one.
    """
    out: dict[str, Any] = {
        "ship_from": None, "is_choice": False, "sold_exact": None,
        "listing_age": None, "duty_offset": None, "free_shipping": None,
        "variant_count": None, "price_sku_id": None, "stock_left": None,
        "sponsored": False,
    }

    out["sponsored"] = _is_sponsored(it)
    trace = it.get("trace") if isinstance(it.get("trace"), dict) else {}

    out["variant_count"] = _sku_count(it)
    prices = it.get("prices") if isinstance(it.get("prices"), dict) else {}
    sku_id = prices.get("skuId")
    if sku_id is not None and str(sku_id).strip():
        out["price_sku_id"] = str(sku_id).strip()

    pdp = trace.get("pdpParams") if isinstance(trace.get("pdpParams"), dict) else {}
    raw = pdp.get("pdp_cdi")
    if isinstance(raw, str) and raw:
        # Doubly-encoded: a URL-escaped JSON blob inside a tracking param.
        try:
            cdi = json.loads(unquote(raw))
        except (json.JSONDecodeError, ValueError):
            cdi = None
        if isinstance(cdi, dict):
            sf = cdi.get("shipFrom")
            if isinstance(sf, str) and sf.strip():
                out["ship_from"] = sf.strip().upper()

    ut = trace.get("utLogMap") if isinstance(trace.get("utLogMap"), dict) else {}
    out["is_choice"] = str(ut.get("isChoice", "")).strip().lower() == "true"
    rtc = ut.get("real_trade_count")
    if rtc is not None:
        try:
            out["sold_exact"] = int(str(rtc).strip())
        except (TypeError, ValueError):
            pass

    out["listing_age"] = _listing_age(it.get("lunchTime"))

    sps = it.get("sellingPoints") if isinstance(it.get("sellingPoints"), list) else []
    sources: set = set()
    for sp in sps:
        if not isinstance(sp, dict):
            continue
        sources.add(sp.get("source"))
        tc = sp.get("tagContent") if isinstance(sp.get("tagContent"), dict) else {}
        txt = tc.get("tagText")
        txt = txt.strip() if isinstance(txt, str) and txt.strip() else None
        if sp.get("source") == "npieces" and txt:
            out["duty_offset"] = txt
        # The ONLY remaining-stock figure anywhere on a search card. It rides on
        # the "Early bird deal" badge rather than any inventory field: 14 of 300
        # live cards carried one, reading "only 1 left" (5), "only 2 left" (4),
        # "only 4 left" (4) and "only 15 left" (1). Small enough to change what
        # you order first, and invisible until now.
        if sp.get("source") == "earlyBird" and txt:
            m = re.search(r"only\s+(\d[\d,]*)\s+left", txt, re.IGNORECASE)
            if m:
                try:
                    out["stock_left"] = int(m.group(1).replace(",", ""))
                except ValueError:
                    pass
    if sps:
        out["free_shipping"] = bool(sources & FREE_SHIPPING_SOURCES)
    return out


def _extract_embedded_json(html: str, var_names: list[str]) -> Optional[dict]:
    """
    AliExpress embeds product/search data in <script> tags like:
        window.runParams = {...}
        window._d_c_._hycdylydh = function() { ... data: {...} }
    Try to grab the JSON blob.
    """
    for name in var_names:
        pattern = re.compile(
            r"window\." + re.escape(name) + r"\s*=\s*(\{.*?\});?\s*\n",
            re.DOTALL,
        )
        m = pattern.search(html)
        if m:
            blob = m.group(1)
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                pass

    # Generic: look for `data: {...}` inside the runParams pattern
    m = re.search(r"window\.runParams\s*=\s*(\{.*?\});", html, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
    return None


def _search_init_data(html: str) -> Optional[dict]:
    """
    Extract the modern search SSR payload:
        window._dida_config_._init_data_ = { data: {<JSON>} }
    Anchor on the *assignment* (`_init_data_ =`) — a bare substring match would
    catch the earlier __INIT_DATA_CALLBACK__ reference instead. The outer
    `{ data: … }` wrapper has an unquoted key (not valid JSON), so we brace-match
    the *inner* object after `data:` and parse that. The matcher is string-aware so
    a `{`/`}` inside a product title can't throw off the depth count. Returns the
    inner dict (keys: hierarchy / data / global), or None.
    """
    mt = re.search(r"window\._dida_config_\._init_data_\s*=", html)
    if not mt:
        return None
    di = html.find("data:", mt.end())
    if di < 0:
        return None
    start = html.find("{", di)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for k in range(start, len(html)):
        c = html[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:k + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _walk_for_items(data: Optional[dict]) -> list[dict]:
    """
    Find the product grid inside an already-parsed SSR payload. Items live at
    data.root.fields.mods.itemList.content (reverse-engineered live Jul 2026); we
    locate them structurally (the longest list of dicts carrying productId+prices)
    so a layout-key rename doesn't silently break us.

    Split out of `_extract_search_items` so `classify_search_render` can reuse it
    against a `data` dict it already parsed, instead of re-running
    `_search_init_data`'s brace-matching scan over the same HTML a second time.
    """
    if not data:
        return []
    found: list[list] = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            if o and isinstance(o[0], dict) and "productId" in o[0] and "prices" in o[0]:
                found.append(o)
            else:
                for v in o:
                    walk(v)

    walk(data)
    return max(found, key=len) if found else []


def _extract_search_items(html: str) -> list[dict]:
    """Pull the product grid out of the SSR payload. See `_walk_for_items`."""
    return _walk_for_items(_search_init_data(html))


# report item #3: catalog.py's SEARCH_RENDER_ATTEMPTS retry loop treats "no
# items parsed, total_results > 0" as one fact — "AliExpress rendered the page
# without its grid" — and retries against that single diagnosis. That is why
# the retry advice it produced ("Retry the same query") was wrong often enough
# to get reported: an empty parse has at least three different real causes and
# only one of them is "AliExpress dropped the grid this time":
#
#   1. the `_dida_config_._init_data_` assignment is missing from the HTML
#      outright (AliExpress served the page with no SSR payload at all);
#   2. the assignment is present but the JSON after it didn't parse — brace
#      match failed or the text between `start` and the matching `}` isn't
#      valid JSON (truncated response, escaping we don't expect, etc.) — this
#      is at least as likely to be OUR parser choking as it is AliExpress's
#      fault, and unlike (1) it is NOT necessarily fixed by an identical
#      resubmit;
#   3. the payload parsed cleanly and genuinely contains no productId+prices
#      list (the page rendered, the grid module just wasn't populated).
#
# `parse_search_results` had no way to tell these apart because it only ever
# returns a list, so a caller retrying on "items == []" was retrying blind
# regardless of which of the three happened. This function exposes which one
# it was; it does not itself decide what to do about it — that judgment call
# (retry vs. not, what to tell the caller) belongs to whichever module owns
# the HTTP retry loop.
SSR_NO_PAYLOAD = "no_ssr_payload"
SSR_UNPARSEABLE = "unparseable_ssr_payload"
SSR_NO_ITEM_LIST = "no_item_list"
SSR_OK = "ok"


def classify_search_render(html: str) -> str:
    """
    Diagnose an empty `parse_search_results` result: SSR_NO_PAYLOAD,
    SSR_UNPARSEABLE, SSR_NO_ITEM_LIST, or SSR_OK (a non-empty grid was found).

    Cheapest check first: a bare regex search for the assignment before paying
    for `_search_init_data`'s brace-matching scan, which only runs once here —
    see `_walk_for_items` for why this doesn't duplicate `_extract_search_items`'s
    own parse.
    """
    if not re.search(r"window\._dida_config_\._init_data_\s*=", html or ""):
        return SSR_NO_PAYLOAD
    data = _search_init_data(html)
    if data is None:
        return SSR_UNPARSEABLE
    return SSR_OK if _walk_for_items(data) else SSR_NO_ITEM_LIST


# Every field a parsed search row can carry, with "not known" as the default.
#
# There are three parsers below and they used to emit three different key sets:
# the SSR path returned warehouse, listing age, free-shipping and the rest, while
# the two fallbacks returned eight keys and nothing else. Nothing crashed — the
# renderer reads the extras with .get() — so a fallback parse simply produced rows
# that looked like items with no warehouse and no age, which is indistinguishable
# from items whose warehouse and age AliExpress didn't state. One is a fact about
# the listing, the other a fact about which parser ran, and the caller could not
# tell them apart. Now every row has every key, and `data_source` says which
# parser produced it so the renderer can name the difference.
SEARCH_ROW_FIELDS: dict[str, Any] = {
    "item_id": None, "title": None, "url": None,
    "price": None, "original_price": None, "discount_pct": None,
    "seller_discount_pct": None, "currency": None, "price_sku_id": None,
    "rating": None, "sold_count": None, "sold_count_num": None,
    "ship_from": None, "is_choice": False, "listing_age": None,
    "duty_offset": None, "free_shipping": None, "variant_count": None,
    "stock_left": None, "sponsored": None, "data_source": None,
}

# What each parser cannot supply, named so the renderer can say which facts are
# unavailable rather than letting them read as absent. The SSR path supplies
# everything, so it is not listed.
SEARCH_SOURCE_GAPS = {
    "legacy": ("warehouse country", "listing age", "free-shipping badge",
               "variant count", "stock", "sponsored/organic"),
    "html": ("warehouse country", "listing age", "free-shipping badge",
             "variant count", "stock", "currency", "sponsored/organic"),
}


def _search_row(**fields: Any) -> dict:
    """One search row, with every key present and unknowns left as None."""
    row = dict(SEARCH_ROW_FIELDS)
    row.update(fields)
    return row


def parse_search_results(html: str) -> list[dict]:
    """
    Parse product cards from an AliExpress search results page.

    Rows always carry the full `SEARCH_ROW_FIELDS` key set regardless of which of
    the three parsers produced them; `data_source` records which one did.
    """
    products: list[dict] = []
    seen_ids: set[str] = set()

    # Approach 0 (primary): the modern _dida_ SSR payload. Prices here are
    # structured and in the *session's* currency (e.g. UAH for a UA account) —
    # we surface that real currency rather than assuming the configured one, which
    # is what caused earlier listings to render local amounts as inflated "$".
    for it in _extract_search_items(html):
        pid = it.get("productId")
        title = it.get("title")
        if isinstance(title, dict):
            title = title.get("displayTitle")
        if not pid or not title:
            continue
        pid = str(pid)
        if pid in seen_ids:
            continue

        prices = it.get("prices") if isinstance(it.get("prices"), dict) else {}
        sp = prices.get("salePrice") if isinstance(prices.get("salePrice"), dict) else {}
        op = prices.get("originalPrice") if isinstance(prices.get("originalPrice"), dict) else {}
        # `minPrice` is a misnomer: the card quotes the ONE SKU named in
        # `prices.skuId`, not the listing's cheapest. Measured against the PDP
        # price map on 15 items (Aug 2026), it equalled that SKU's price on 13
        # (the other two were captured minutes apart and moved ≤1.2%), and it was
        # NOT the listing minimum twice — item 1005007010293617 quoted 190.39
        # against a 48.10–190.39 span (the DEAREST config) and 1005007129679040
        # quoted 189.83 inside a 44.58–180,935.69 span, matching neither end.
        price = sp.get("minPrice")
        currency = sp.get("currencyCode") or op.get("currencyCode")
        original_price = op.get("minPrice")
        if price is None and original_price is not None:
            # Some cards only carry an original price — use it rather than blanking.
            price, original_price = original_price, None

        discount_pct = None
        if isinstance(price, (int, float)) and isinstance(original_price, (int, float)) and original_price > price:
            discount_pct = round((1 - price / original_price) * 100)

        # The seller's own declared discount, straight from the card. Ours is
        # derived from the two prices and AliExpress *floors* where we round, so
        # the two disagree by exactly 1pp on ~14% of live cards (never more). The
        # ⚠ MSRP? flag stays keyed to the derived number; this is the raw claim
        # shown next to it so the caller can see both.
        seller_discount_pct = sp.get("discount") if isinstance(sp, dict) else None
        if not isinstance(seller_discount_pct, (int, float)):
            seller_discount_pct = None

        rating = None
        ev = it.get("evaluation")
        if isinstance(ev, dict) and ev.get("starRating") is not None:
            try:
                rating = float(ev["starRating"])
            except (TypeError, ValueError):
                rating = None

        trade = it.get("trade")
        sold_count = trade.get("tradeDesc") if isinstance(trade, dict) else None

        sig = _search_signals(it)
        # Prefer the exact order count over AliExpress's bucketed label — same
        # line length on average, but it actually separates the top listings.
        if sig["sold_exact"] is not None:
            sold_count = f"{sig['sold_exact']:,} sold"

        seen_ids.add(pid)
        products.append(_search_row(
            item_id=pid,
            title=str(title).strip(),
            price=price,
            original_price=original_price,
            discount_pct=discount_pct,
            seller_discount_pct=seller_discount_pct,
            currency=currency,
            rating=rating,
            sold_count=sold_count,
            sold_count_num=sig["sold_exact"],
            ship_from=sig["ship_from"],
            is_choice=sig["is_choice"],
            listing_age=sig["listing_age"],
            duty_offset=sig["duty_offset"],
            free_shipping=sig["free_shipping"],
            variant_count=sig["variant_count"],
            price_sku_id=sig["price_sku_id"],
            stock_left=sig["stock_left"],
            sponsored=sig["sponsored"],
            data_source="ssr",
            url=f"{BASE_URL}/item/{pid}.html",
        ))

    if products:
        return products

    # Approach 1: legacy embedded JSON (window.runParams) — fallback if the SSR
    # payload above ever disappears. Prices fall back to the configured currency.
    data = _extract_embedded_json(html, ["runParams", "_dida_"])
    if data:
        # Walk the structure looking for lists of items
        items_pool: list[dict] = []

        def walk(obj):
            if isinstance(obj, dict):
                # Common keys where items live
                for k in ("mods", "items", "itemList", "resultList"):
                    v = obj.get(k)
                    if isinstance(v, list):
                        for it in v:
                            if isinstance(it, dict):
                                items_pool.append(it)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(data)

        for it in items_pool:
            # Heuristic: needs a productId or itemId
            pid = (
                it.get("productId")
                or it.get("itemId")
                or it.get("product_id")
                or it.get("id")
            )
            title = (
                it.get("title", {}).get("displayTitle")
                if isinstance(it.get("title"), dict)
                else it.get("title")
            ) or it.get("subject") or it.get("name")
            if not pid or not title:
                continue
            pid = str(pid)
            if pid in seen_ids:
                continue

            price_info = it.get("prices") or it.get("price") or {}
            sale_price = None
            original_price = None
            if isinstance(price_info, dict):
                sp = price_info.get("salePrice") or price_info.get("formattedPrice")
                op = price_info.get("originalPrice") or price_info.get("strikePrice")
                if isinstance(sp, dict):
                    sale_price = sp.get("minPrice") or parse_price(sp.get("formattedPrice", ""))
                elif sp:
                    sale_price = parse_price(str(sp))
                if isinstance(op, dict):
                    original_price = op.get("minPrice") or parse_price(op.get("formattedPrice", ""))
                elif op:
                    original_price = parse_price(str(op))

            discount_pct = None
            d = it.get("discount") or (price_info.get("discount") if isinstance(price_info, dict) else None)
            if d:
                dm = re.search(r"(\d+)", str(d))
                if dm:
                    discount_pct = int(dm.group(1))

            trade = it.get("trade") or {}
            sold_count = trade.get("tradeDesc") if isinstance(trade, dict) else None

            rating = None
            ev = it.get("evaluation") or {}
            if isinstance(ev, dict):
                r = ev.get("starRating") or ev.get("rating")
                if r:
                    try:
                        rating = float(r)
                    except (TypeError, ValueError):
                        rating = None

            seen_ids.add(pid)
            products.append(_search_row(
                item_id=pid,
                title=title.strip(),
                price=sale_price,
                original_price=original_price,
                discount_pct=discount_pct,
                rating=rating,
                sold_count=sold_count,
                data_source="legacy",
                url=f"{BASE_URL}/item/{pid}.html",
            ))

        if products:
            return products

    # Approach 2: HTML fallback
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.select("a[href*='/item/']"):
        href = link.get("href", "")
        m = ITEM_ID_RE.search(href)
        if not m:
            continue
        pid = m.group(1)
        if pid in seen_ids:
            continue

        # Climb up to the card container
        card = link.find_parent(["div", "article", "li"]) or link
        title_el = card.select_one("h1, h2, h3, [class*='title'], [class*='Title']")
        title = title_el.get_text(strip=True) if title_el else link.get("title") or ""
        if not title:
            continue

        card_text = card.get_text(" ", strip=True)
        sale_price = parse_price(card_text)

        # Try to find a second (higher) price for original
        original_price = None
        all_prices = [parse_price(t) for t in re.findall(r"[A-Z]{0,2}\$?\s*[\d,]+\.\d{2}", card_text)]
        all_prices = [p for p in all_prices if p is not None]
        if len(all_prices) >= 2:
            sale_price = min(all_prices)
            original_price = max(all_prices)

        discount_pct = None
        dm = re.search(r"-?(\d{1,2})%", card_text)
        if dm:
            discount_pct = int(dm.group(1))

        sold_count, _ = _parse_sold_count(card_text)

        rating = None
        rmatch = re.search(r"\b([0-4]\.\d|5\.0)\b", card_text)
        if rmatch:
            try:
                rating = float(rmatch.group(1))
            except ValueError:
                pass

        seen_ids.add(pid)
        products.append(_search_row(
            item_id=pid,
            title=title[:200],
            price=sale_price,
            original_price=original_price,
            discount_pct=discount_pct,
            rating=rating,
            sold_count=sold_count,
            data_source="html",
            url=f"{BASE_URL}/item/{pid}.html",
        ))

    return products


def parse_product_detail(html: str, item_id: str) -> dict:
    """Parse a product detail page."""
    soup = BeautifulSoup(html, "html.parser")

    details = {
        "item_id": item_id,
        "url": f"{BASE_URL}/item/{item_id}.html",
        "title": None,
        "price": None,
        "original_price": None,
        "discount_pct": None,
        "rating": None,
        "sold_count": None,
        "seller_name": None,
        "shipping_cost": None,
        "shipping_estimate": None,
        "variants": [],
    }

    # Title
    if soup.title:
        t = soup.title.get_text(strip=True)
        # AliExpress titles often have " | AliExpress" or similar suffix
        t = re.sub(r"\s*[|–-]\s*(AliExpress|aliexpress).*$", "", t, flags=re.IGNORECASE)
        details["title"] = t or None

    h1 = soup.select_one("h1")
    if h1 and h1.get_text(strip=True):
        details["title"] = h1.get_text(strip=True)

    # Try embedded JSON first
    data = _extract_embedded_json(html, ["runParams"])
    if data:
        # Walk for useful nuggets
        def find_first(obj, keys):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in keys and v not in (None, "", {}):
                        return v
                    r = find_first(v, keys)
                    if r is not None:
                        return r
            elif isinstance(obj, list):
                for v in obj:
                    r = find_first(v, keys)
                    if r is not None:
                        return r
            return None

        price_node = find_first(data, {"priceModule", "price", "priceInfo"})
        if isinstance(price_node, dict):
            sp = price_node.get("minActivityAmount") or price_node.get("formatedActivityPrice") or price_node.get("formatedPrice")
            op = price_node.get("formatedPrewarmingPrice") or price_node.get("maxAmount")
            if isinstance(sp, dict):
                details["price"] = sp.get("value") or parse_price(sp.get("formattedPrice", ""))
            elif sp:
                details["price"] = parse_price(str(sp))
            if isinstance(op, dict):
                details["original_price"] = op.get("value")
            elif op:
                details["original_price"] = parse_price(str(op))

        title_node = find_first(data, {"title", "subject"})
        if isinstance(title_node, str) and not details["title"]:
            details["title"] = title_node

        seller_node = find_first(data, {"storeName", "sellerName", "storeInfo"})
        if isinstance(seller_node, str):
            details["seller_name"] = seller_node
        elif isinstance(seller_node, dict):
            details["seller_name"] = seller_node.get("name") or seller_node.get("storeName")

        ship_node = find_first(data, {"shippingModule", "logistics"})
        if isinstance(ship_node, dict):
            details["shipping_cost"] = parse_price(str(ship_node.get("formattedFreight", "")))
            details["shipping_estimate"] = ship_node.get("deliveryDate") or ship_node.get("etd")

    # Fallbacks from rendered HTML body text
    body_text = soup.get_text(" ", strip=True)

    if details["price"] is None:
        # Look for a currency-prefixed price near the top
        price_el = soup.select_one("[class*='price'], [class*='Price']")
        if price_el:
            details["price"] = parse_price(price_el.get_text(" ", strip=True))

    # Discount
    dmatch = re.search(r"-(\d{1,2})%", body_text)
    if dmatch:
        details["discount_pct"] = int(dmatch.group(1))

    # Rating
    rmatch = re.search(r"\b([0-4]\.\d|5\.0)\s*(?:out of|/)\s*5", body_text)
    if rmatch:
        try:
            details["rating"] = float(rmatch.group(1))
        except ValueError:
            pass
    elif details["rating"] is None:
        rmatch = re.search(r"\b([0-4]\.\d|5\.0)\b\s*\(?\d", body_text)
        if rmatch:
            try:
                details["rating"] = float(rmatch.group(1))
            except ValueError:
                pass

    # Sold count
    details["sold_count"], details["sold_count_num"] = _parse_sold_count(body_text)

    return details
