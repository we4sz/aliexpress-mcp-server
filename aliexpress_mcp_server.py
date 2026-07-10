#!/usr/bin/env python3
"""
AliExpress MCP Server

Search AliExpress, pull clean product details, check shipping to Canada,
and peek at the current cart — all read-only.

Auth: Session cookies from MCP Auth Bridge extension at
~/.mcp-credentials/aliexpress.json
"""

import json
import os
import re
import time
import hashlib
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

# ─── Configuration ──────────────────────────────────────────────────────────

CREDENTIALS_PATH = Path(
    os.environ.get("ALIEXPRESS_CREDENTIALS", "~/.mcp-credentials/aliexpress.json")
).expanduser()

COUNTRY = os.environ.get("ALIEXPRESS_COUNTRY", "CA")
CURRENCY = os.environ.get("ALIEXPRESS_CURRENCY", "CAD")

BASE_URL = "https://www.aliexpress.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aliexpress-mcp")


# ─── Auth ───────────────────────────────────────────────────────────────────

def load_cookies() -> dict[str, str]:
    """Load session cookies from the credential file written by MCP Auth Bridge."""
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
        return data.get("cookies", {})
    except (json.JSONDecodeError, KeyError):
        return {}


def get_client(referer: str = BASE_URL) -> httpx.Client:
    """Create an HTTP client with session cookies and realistic browser headers."""
    cookies = load_cookies()
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items()) if cookies else ""

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str

    return httpx.Client(
        base_url=BASE_URL,
        headers=headers,
        follow_redirects=True,
        timeout=30.0,
    )


def check_auth_redirect(response: httpx.Response) -> bool:
    """Detect a redirect to the AliExpress login page (expired session)."""
    url = str(response.url).lower()
    return "login.aliexpress" in url or "/login.htm" in url or "passport" in url


AUTH_EXPIRED_MSG = (
    "AliExpress session expired or missing. Open aliexpress.com in Chrome, "
    "log in, and click 'Save AliExpress' in the MCP Auth Bridge extension."
)

# Order APIs need a *full* login session. The quick `document.cookie` copy misses
# the HttpOnly login cookies, so those endpoints return FAIL_SYS_SESSION_EXPIRED.
FULL_AUTH_MSG = (
    "AliExpress order data needs a full login session. The quick-copy cookie snippet "
    "misses the HttpOnly login cookies — open DevTools → Application → Cookies → "
    "aliexpress.com and add the HttpOnly rows to ~/.mcp-credentials/aliexpress.json, "
    "then retry."
)


# ─── MTOP API Client ────────────────────────────────────────────────────────
#
# The AliExpress product detail page is client-side-rendered — window.runParams
# is empty in the initial HTML. Real data loads via signed MTOP AJAX calls.
# We replicate the signing algorithm the `mtop.js` library uses in-browser:
#
#   token   = cookie `_m_h5_tk` split on `_` → first segment
#   appKey  = "12574478" (the public web app key)
#   t       = current timestamp in ms as a string
#   data    = JSON string of the request payload
#   sign    = md5( token + "&" + t + "&" + appKey + "&" + data )
#
# The response is wrapped in an HTTP envelope. When ret[0] starts with
# "FAIL_SYS_TOKEN_EMPTY" or "FAIL_SYS_TOKEN_EXPIRED" the server returns fresh
# `_m_h5_tk` cookies — the real browser retries; we can too.

MTOP_APP_KEY = "12574478"
MTOP_BASE = "https://acs.aliexpress.com"


def _mtop_sign(token: str, t_ms: str, app_key: str, data: str) -> str:
    raw = f"{token}&{t_ms}&{app_key}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _h5_token_prefix(cookies: dict[str, str]) -> Optional[str]:
    """Extract the signing token prefix from the _m_h5_tk cookie."""
    raw = cookies.get("_m_h5_tk", "")
    if not raw:
        return None
    # Cookie shape: <token>_<expiry-ms>
    return raw.split("_", 1)[0] or None


def mtop_call(
    api: str,
    version: str,
    payload: dict[str, Any],
    *,
    cookies: Optional[dict[str, str]] = None,
    retries: int = 1,
    referer: Optional[str] = None,
) -> dict:
    """
    Make a signed MTOP request. Returns the parsed JSON response dict on success.

    Raises RuntimeError on signing/network failure.
    """
    if cookies is None:
        cookies = load_cookies()

    token = _h5_token_prefix(cookies)
    if not token:
        raise RuntimeError(
            "No _m_h5_tk cookie found. Re-save AliExpress credentials via the "
            "MCP Auth Bridge extension."
        )

    data_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    t_ms = str(int(time.time() * 1000))
    sign = _mtop_sign(token, t_ms, MTOP_APP_KEY, data_str)

    params = {
        "jsv": "2.6.2",
        "appKey": MTOP_APP_KEY,
        "t": t_ms,
        "sign": sign,
        "api": api,
        "v": version,
        "type": "originaljson",
        "dataType": "json",
        "timeout": "20000",
        "AntiCreep": "true",
        "AntiFlood": "true",
        "data": data_str,
    }

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-CA,en;q=0.9",
        "Referer": referer or f"{BASE_URL}/",
        "Origin": BASE_URL,
        "Cookie": cookie_str,
    }
    url = f"{MTOP_BASE}/h5/{api}/{version}/"

    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        resp = c.get(url, params=params, headers=headers)

        # MTOP sometimes wraps valid JSON inside `mtopjsonp1({...})` even when
        # we request originaljson. Strip the wrapper if present.
        text = resp.text.strip()
        if text.startswith("mtopjsonp") and text.endswith(")"):
            inner = text.split("(", 1)[1][:-1]
            text = inner

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"MTOP returned non-JSON: {text[:200]}") from e

        ret = data.get("ret", [])
        ret_str = ret[0] if ret else ""

        # Retry once if the token is empty/expired (the server sets a fresh
        # `_m_h5_tk` cookie on that first response — the standard MTOP two-step
        # handshake). NOTE: AliExpress's live error string is the *misspelled*
        # `FAIL_SYS_TOKEN_EXOIRED` ("EXOIRED", not "EXPIRED") — matching only the
        # correct spelling silently disables the refresh, so any `FAIL_SYS_TOKEN*`
        # triggers the single refresh-and-retry.
        # httpx.Response.cookies iterates as cookie-name strings, so pull values by key.
        if retries > 0 and "FAIL_SYS_TOKEN" in ret_str:
            new_cookies = dict(cookies)
            for name in resp.cookies:
                val = resp.cookies.get(name)
                if val is not None:
                    new_cookies[name] = val
            return mtop_call(api, version, payload, cookies=new_cookies, retries=retries - 1, referer=referer)

        return data


# ─── Parsing Helpers ────────────────────────────────────────────────────────

PRICE_RE = re.compile(r"(?:C\$|CA\$|US\$|\$|CAD|USD)\s*([\d,]+\.\d{2}|[\d,]+)")
ITEM_ID_RE = re.compile(r"/item/(\d+)\.html")

# Discounts at/above this percent often reflect AliExpress's fabricated MSRP
# (an inflated strike-through "was" price) rather than a real markdown — flag them.
SUSPICIOUS_DISCOUNT = 60


def _msrp_flag(discount_pct: Optional[float]) -> str:
    """Warning marker for a discount steep enough to smell like a fabricated MSRP."""
    if isinstance(discount_pct, (int, float)) and discount_pct >= SUSPICIOUS_DISCOUNT:
        return " ⚠ MSRP?"
    return ""


def _fmt_money(amount: float, currency: Optional[str] = None) -> str:
    """Format a monetary amount as "12.34 CUR" — the single source for money glyphs."""
    return f"{amount:.2f} {currency or CURRENCY}"


def _resolve_item_id(item_id: str = "", url: str = "") -> Optional[str]:
    """
    Resolve an AliExpress item id from a raw numeric id, a full /item/<id>.html URL,
    or a short share link (a.aliexpress.com/_xxx, s.click.aliexpress.com/…) by
    following its redirect — that short form is what the mobile share sheet emits.
    Returns the numeric id string, or None.
    """
    if item_id and item_id.isdigit():
        return item_id
    candidate = (url or item_id or "").strip()
    if not candidate:
        return None
    m = ITEM_ID_RE.search(candidate)
    if m:
        return m.group(1)
    if candidate.startswith("http") or "aliexpress.com" in candidate:
        target = candidate if candidate.startswith("http") else "https://" + candidate
        try:
            with httpx.Client(
                follow_redirects=True, timeout=20.0, headers={"User-Agent": USER_AGENT}
            ) as c:
                r = c.get(target)
        except Exception:
            return None
        m = (
            ITEM_ID_RE.search(str(r.url))
            or re.search(r"/item/(\d+)\.html", r.text)
            or re.search(r'"productId"\s*:\s*"?(\d{10,})"?', r.text)
        )
        if m:
            return m.group(1)
    return None


def parse_price(text: str) -> Optional[float]:
    """Pull the first price-looking number out of a string."""
    if not text:
        return None
    m = PRICE_RE.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    # Fallback: bare number
    m = re.search(r"([\d,]+\.\d{2})", text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _normalize_price(text: Any) -> Optional[float]:
    """
    Parse a localized money string into a float, currency-symbol- and
    locale-agnostic. Handles both decimal conventions:
        "US $1.79"        -> 1.79
        "808,96 грн."     -> 808.96   (comma decimal)
        "C$1,192.72"      -> 1192.72  (comma thousands)
        "1.192,72 €"      -> 1192.72  (dot thousands, comma decimal)
    Returns None if no number is present. Use this for server-rendered display
    strings; pair the result with a currencyCode read from the same response.
    """
    if text is None:
        return None
    # Keep only digits and separators, then drop any leading/trailing separator
    # (e.g. the "." in "808,96 грн." must not be mistaken for a decimal point).
    s = re.sub(r"[^0-9.,]", "", str(text)).strip(".,")
    if not s:
        return None
    if "," in s and "." in s:
        # The right-most separator is the decimal point.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # Pure thousands grouping (1,234 / 1,234,567) vs comma-decimal (808,96).
        if re.fullmatch(r"\d{1,3}(,\d{3})+", s):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _strip_html(text: Any) -> Optional[str]:
    """Strip HTML tags from a server string (e.g. wishlist reducePriceText)."""
    if not isinstance(text, str):
        return None
    clean = re.sub(r"<[^>]+>", "", text).strip()
    return clean or None


def _fmt_epoch_ms(ms: Any) -> Optional[str]:
    """Format an epoch-millisecond timestamp as a YYYY-MM-DD date (local)."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ms / 1000.0))
    except (ValueError, OSError, OverflowError):
        return None


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


def _extract_search_items(html: str) -> list[dict]:
    """
    Pull the product grid out of the SSR payload. Items live at
    data.root.fields.mods.itemList.content (reverse-engineered live Jul 2026); we
    locate them structurally (the longest list of dicts carrying productId+prices)
    so a layout-key rename doesn't silently break us.
    """
    data = _search_init_data(html)
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


def parse_search_results(html: str) -> list[dict]:
    """Parse product cards from an AliExpress search results page."""
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
        price = sp.get("minPrice")
        currency = sp.get("currencyCode") or op.get("currencyCode")
        original_price = op.get("minPrice")
        if price is None and original_price is not None:
            # Some cards only carry an original price — use it rather than blanking.
            price, original_price = original_price, None

        discount_pct = None
        if isinstance(price, (int, float)) and isinstance(original_price, (int, float)) and original_price > price:
            discount_pct = round((1 - price / original_price) * 100)

        rating = None
        ev = it.get("evaluation")
        if isinstance(ev, dict) and ev.get("starRating") is not None:
            try:
                rating = float(ev["starRating"])
            except (TypeError, ValueError):
                rating = None

        trade = it.get("trade")
        sold_count = trade.get("tradeDesc") if isinstance(trade, dict) else None

        seen_ids.add(pid)
        products.append({
            "item_id": pid,
            "title": str(title).strip(),
            "price": price,
            "original_price": original_price,
            "discount_pct": discount_pct,
            "currency": currency,
            "rating": rating,
            "sold_count": sold_count,
            "url": f"{BASE_URL}/item/{pid}.html",
        })

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
            products.append({
                "item_id": pid,
                "title": title.strip(),
                "price": sale_price,
                "original_price": original_price,
                "discount_pct": discount_pct,
                "rating": rating,
                "sold_count": sold_count,
                "url": f"{BASE_URL}/item/{pid}.html",
            })

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

        sold_match = re.search(r"([\d,]+\+?)\s*sold", card_text, re.IGNORECASE)
        sold_count = sold_match.group(0) if sold_match else None

        rating = None
        rmatch = re.search(r"\b([0-4]\.\d|5\.0)\b", card_text)
        if rmatch:
            try:
                rating = float(rmatch.group(1))
            except ValueError:
                pass

        seen_ids.add(pid)
        products.append({
            "item_id": pid,
            "title": title[:200],
            "price": sale_price,
            "original_price": original_price,
            "discount_pct": discount_pct,
            "rating": rating,
            "sold_count": sold_count,
            "url": f"{BASE_URL}/item/{pid}.html",
        })

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
    sold_match = re.search(r"([\d,]+\+?)\s*sold", body_text, re.IGNORECASE)
    if sold_match:
        details["sold_count"] = sold_match.group(0)

    return details


# ─── MCP Server ─────────────────────────────────────────────────────────────

mcp = FastMCP("aliexpress", dependencies=["httpx", "beautifulsoup4"])


SORT_MAP = {
    "best_match": None,
    "orders": "total_tranpro_desc",
    "price_asc": "price_asc",
    "price_desc": "price_desc",
}


def _search_fetch_parse(query: str, sort_by: str = "best_match") -> list[dict]:
    """
    Fetch an AliExpress search results page and parse product cards.
    Raises RuntimeError(AUTH_EXPIRED_MSG) if AliExpress bounces us to login.
    """
    slug = quote_plus(query.strip()).replace("+", "-")
    url_path = f"/w/wholesale-{slug}.html"
    params = {}
    if SORT_MAP.get(sort_by):
        params["SortType"] = SORT_MAP[sort_by]

    client = get_client()
    try:
        resp = client.get(url_path, params=params)
        if check_auth_redirect(resp):
            raise RuntimeError(AUTH_EXPIRED_MSG)
        resp.raise_for_status()
        return parse_search_results(resp.text)
    finally:
        client.close()


def _format_product_lines(products: list[dict], header: str, limit: int = 25) -> str:
    """Render parsed product dicts into the compact text shared by search + deals."""
    lines = [header]
    for p in products[:limit]:
        line = f"- {p['title']}"
        cur = p.get("currency") or CURRENCY
        if p["price"] is not None:
            line += f" — {_fmt_money(p['price'], cur)}"
        if p["original_price"] is not None and p["original_price"] > (p["price"] or 0):
            line += f" (was {p['original_price']:.2f})"
        if p["discount_pct"]:
            line += f" [-{p['discount_pct']}%]{_msrp_flag(p['discount_pct'])}"
        if p["rating"]:
            line += f" ★{p['rating']}"
        if p["sold_count"]:
            line += f" · {p['sold_count']}"
        line += f"\n  item_id: {p['item_id']}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def search_products(
    query: str,
    min_rating: float = 0,
    max_price: float = 0,
    sort_by: str = "best_match",
) -> str:
    """
    Search AliExpress for products.

    Args:
        query: Search term (e.g., "groudon plush", "usb c cable")
        min_rating: Minimum rating (0-5, e.g., 4.5). 0 disables filter.
        max_price: Maximum price, in the search's local currency (your AliExpress
            site currency, e.g. UAH — not necessarily ALIEXPRESS_CURRENCY). 0 disables.
        sort_by: One of "best_match", "orders", "price_asc", "price_desc"
    """
    try:
        products = _search_fetch_parse(query, sort_by)
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
            filters.append(f"price ≤ ${max_price:.2f}")
        suffix = f" with {', '.join(filters)}" if filters else ""
        return f"No products found for '{query}'{suffix}."

    return _format_product_lines(
        products, f"Found {len(products)} result(s) for '{query}' (sort: {sort_by}):"
    )


@mcp.tool()
def find_deals(
    query: str,
    min_discount: int = 0,
    max_price: float = 0,
    min_rating: float = 0,
    sort_by: str = "orders",
) -> str:
    """
    Search AliExpress and surface the most-discounted listings for a query.

    Same underlying search as `search_products`, but keeps only items with a
    visible discount and sorts by discount depth (biggest first).

    Args:
        query: Search term (e.g., "mechanical keyboard").
        min_discount: Minimum discount percent to include (e.g., 40). 0 keeps any discount.
        max_price: Maximum price, in the search's local currency (your AliExpress
            site currency, e.g. UAH — not necessarily ALIEXPRESS_CURRENCY). 0 disables.
        min_rating: Minimum rating (0-5). 0 disables filter.
        sort_by: Search sort ("orders", "best_match", "price_asc", "price_desc").
    """
    try:
        products = _search_fetch_parse(query, sort_by)
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
    return _format_product_lines(
        deals, f"Found {len(deals)} deal(s) for '{query}' (biggest discount first):"
    )


def _fetch_pdp_mtop(item_id: str) -> Optional[dict]:
    """
    Call the PDP MTOP endpoint and return the raw response dict, or None on failure.
    Tries the PC endpoint first, then the msite endpoint as a fallback.
    """
    referer = f"{BASE_URL}/item/{item_id}.html"
    payload = {
        "productId": item_id,
        "_currency": CURRENCY,
        "_lang": "en_US",
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
        if ret and any("SUCCESS" in r for r in ret):
            return resp
        # Some endpoints return data even without SUCCESS::API_SUCCESS in ret
        if resp.get("data") and isinstance(resp["data"], dict) and resp["data"]:
            return resp
        logger.debug("MTOP %s ret=%s", api, ret)
    return None


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
        "rating": None,
        "review_count": None,
        "sold_count": None,
        "seller_name": None,
        "store_url": None,
        "seller_positive_rate": None,
        "seller_total_reviews": None,
        "shipping_cost": None,
        "shipping_estimate": None,
        "ship_from": None,
        "ship_unreachable": None,
        "ship_days_min": None,
        "ship_days_max": None,
        "image_url": None,
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
            for sku in sku_map.values():
                if isinstance(sku, dict):
                    sp = sku.get("salePriceString")
                    if isinstance(sp, str):
                        p = _normalize_price(sp)
                        if p is not None:
                            prices.append(p)
            if prices:
                lo, hi = min(prices), max(prices)
                if lo != hi:
                    d["price_range"] = (lo, hi)
                if d["price"] is None:
                    d["price"] = lo

    if d["discount_pct"] is None and d["price"] and d["original_price"] and d["original_price"] > d["price"]:
        d["discount_pct"] = round((1 - d["price"] / d["original_price"]) * 100)

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
        sm = re.search(r"([\d,]+\+?)\s*sold", other, re.IGNORECASE)
        if sm:
            d["sold_count"] = sm.group(0)

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
            if d["shipping_cost"] is None and biz.get("logisticsComposeThreshold"):
                # e.g., "C$0.00" — sometimes the "threshold" is actually the freight text
                d["shipping_cost"] = parse_price(biz["logisticsComposeThreshold"])
            eta_min = biz.get("displayEtaMinDate")
            eta_max = biz.get("displayEtaMaxDate")
            if eta_min and eta_max:
                d["shipping_estimate"] = f"{eta_min} – {eta_max}"
            elif eta_min:
                d["shipping_estimate"] = eta_min
            d["ship_from"] = biz.get("shipFrom")
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

    # ── Image ─────────────────────────────────────────────────────────
    hdr = result.get("HEADER_IMAGE_PC")
    if isinstance(hdr, dict):
        imgs = hdr.get("imagePathList") or hdr.get("imgList")
        if isinstance(imgs, list) and imgs and isinstance(imgs[0], str):
            d["image_url"] = imgs[0]

    return d


@mcp.tool()
def get_product_details(item_id: str = "", url: str = "") -> str:
    """
    Get detailed info for a specific AliExpress product.

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
        lines.append(line)
        if d.get("price_range"):
            lines.append("  (price varies by configuration — use get_variants for the per-config breakdown)")
    if d.get("rating"):
        rating_line = f"Rating: ★{d['rating']}"
        if d.get("review_count"):
            rating_line += f" ({d['review_count']} reviews)"
        lines.append(rating_line)
    if d.get("sold_count"):
        lines.append(f"Sold: {d['sold_count']}")
    if d.get("seller_name"):
        seller_line = f"Seller: {d['seller_name']}"
        if d.get("seller_positive_rate"):
            seller_line += f" — {d['seller_positive_rate']}% positive feedback"
        if d.get("seller_total_reviews"):
            seller_line += f" ({d['seller_total_reviews']} seller feedbacks)"
        lines.append(seller_line)
        if d.get("store_url"):
            lines.append(f"Store: {d['store_url']}")
    if d.get("ship_unreachable"):
        lines.append(f"Shipping: does not ship to {COUNTRY}")
    elif d.get("shipping_cost") is not None:
        lines.append("Shipping: " + ("Free" if d["shipping_cost"] == 0 else _fmt_money(d["shipping_cost"], cur)))
    else:
        lines.append("Shipping: not available (AliExpress needs a saved delivery address to quote it)")
    if d.get("shipping_estimate"):
        eta_line = f"Estimated delivery: {d['shipping_estimate']}"
        if d.get("ship_days_min") and d.get("ship_days_max"):
            eta_line += f" ({d['ship_days_min']}–{d['ship_days_max']} days)"
        lines.append(eta_line)
    if d.get("ship_from"):
        lines.append(f"Ships from: {d['ship_from']}")
    return "\n".join(lines)


@mcp.tool()
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

    d = _extract_pdp_fields(resp, item_id)
    if d.get("shipping_cost") is None and not d.get("shipping_estimate"):
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
    if d.get("shipping_estimate"):
        lines.append(f"  Estimated delivery: {d['shipping_estimate']}")
    if d.get("ship_from"):
        lines.append(f"  Ships from: {d['ship_from']}")
    return "\n".join(lines)


# ─── Reviews ──────────────────────────────────────────────────────────────────
#
# Product reviews come from a separate, *unsigned* JSON endpoint (not MTOP):
#   GET https://feedback.aliexpress.com/pc/searchEvaluation.do
# Confirmed live Jul 2026. Per-review `buyerEval` is on a 0–100 scale
# (100 = 5 stars); `productEvaluationStatistic` carries the aggregate breakdown.

FEEDBACK_URL = "https://feedback.aliexpress.com/pc/searchEvaluation.do"


def _fetch_reviews(item_id: str, page: int = 1, page_size: int = 20, filt: str = "all") -> Optional[dict]:
    """Fetch one page of reviews. Returns the response `data` block, or None."""
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
    if isinstance(stat, dict):
        out["stats"] = {
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


@mcp.tool()
def get_reviews(item_id: str = "", url: str = "", max_reviews: int = 10, filter_by: str = "all") -> str:
    """
    Fetch buyer reviews and the rating breakdown for an AliExpress product.

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

    page_size = min(max(max_reviews, 10), 50)
    data = _fetch_reviews(item_id, page=1, page_size=page_size, filt=filter_by)
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
        if st.get("positive_rate") is not None:
            neu = st.get("neutral_rate")
            neu_str = f" · {neu}% neutral (3★)" if neu is not None else ""
            lines.append(f"  {st['positive_rate']}% positive{neu_str} · {st.get('negative_rate', 0)}% negative")
        sd = st.get("stars") or {}
        breakdown = "  ".join(f"{k}★:{sd[k]}" for k in (5, 4, 3, 2, 1) if sd.get(k) is not None)
        if breakdown:
            lines.append("  " + breakdown)

    if r["reviews"]:
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
                lines.append(f"  ({rv['sku']})")
            if rv.get("text"):
                lines.append(f"  {rv['text']}")
            if rv.get("up_votes"):
                lines.append(f"  👍 {rv['up_votes']}")
    return "\n".join(lines)


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


@mcp.tool()
def get_seller(item_id: str = "", url: str = "") -> str:
    """
    Get the store / seller profile behind an AliExpress product: rating,
    positive-feedback rate, seller level, age, and store link.

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

    shop = resp.get("data", {}).get("result", {}).get("SHOP_CARD_PC", {})
    d = _extract_seller(shop)
    if not d["store_name"]:
        return f"No seller info found for item {item_id}."

    lines = [f"Seller for item {item_id}:", f"Store: {d['store_name']}"]
    if d.get("positive_rate") is not None:
        pr = f"Positive feedback: {d['positive_rate']}%"
        if d.get("total_reviews") is not None:
            pr += f" (across {d['total_reviews']} seller feedbacks)"
        lines.append(pr)
    elif d.get("total_reviews") is not None:
        lines.append(f"Seller feedbacks: {d['total_reviews']}")
    if d.get("level"):
        lines.append(f"Seller level: {d['level']}")
    if d.get("score") is not None:
        lines.append(f"Seller score: {d['score']}")
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
    if d.get("store_url"):
        lines.append(f"Store: {d['store_url']}")
    return "\n".join(lines)


@mcp.tool()
def compare_sellers(title: str = "", item_id: str = "", url: str = "", max_candidates: int = 6) -> str:
    """
    Find which SELLERS offer the same product and rank them by how established each
    store is — longest-running, then highest feedback volume, then best positive rate.
    Read-only.

    AliExpress lists the same item under many storefronts (the original brand store,
    relisters, dropshippers). This surfaces the oldest / highest-volume seller so you
    can prefer them over a brand-new relister. It costs one search plus one lookup per
    candidate, so keep max_candidates modest.

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
        products = _search_fetch_parse(query)
    except RuntimeError as e:
        return str(e)
    if not products:
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
        s = _extract_seller(resp.get("data", {}).get("result", {}).get("SHOP_CARD_PC", {}))
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
    lines = [
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
        if s.get("store_url"):
            lines.append(f"  {s['store_url']}")
    lines.append("")
    lines.append("Note: matches come from a title search — confirm each listing is the exact product/config you want.")
    return "\n".join(lines)


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
        variants.append({
            "sku_id": sku_id,
            "spec": " · ".join(spec_parts) if spec_parts else None,
            "spec_parts": spec_parts,
            "price": price_val,
            "original_price": original,
            "currency": currency,
            "in_stock": bool(p.get("salable")),
            "stock": p.get("skuStock"),
        })

    # Collapse indistinguishable rows: some listings carry an extra unnamed
    # dimension (e.g. plug/region) that duplicates the same visible spec + price.
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for v in variants:
        key = (v["spec"], round(v["price"], 2) if v["price"] is not None else None)
        if key in merged:
            if v["in_stock"]:
                merged[key]["in_stock"] = True
        else:
            merged[key] = v
            order.append(key)
    return [merged[k] for k in order]


@mcp.tool()
def get_variants(item_id: str = "", url: str = "") -> str:
    """
    List every buyable configuration (SKU) of an AliExpress product with its own
    price — e.g. "DDR4 32GB 1TB SSD · R7 5825U" → 749.36. This is the price→spec
    map that `get_product_details`' price range can't give you.

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

    variants = _extract_variants(resp.get("data", {}).get("result", {}))
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

    priced = [v["price"] for v in variants if v["price"] is not None]
    cur = next((v["currency"] for v in variants if v.get("currency")), None) or CURRENCY
    header = f"Variants for item {item_id} ({len(variants)} configs"
    if priced:
        header += f", {_fmt_money(min(priced), cur)}–{_fmt_money(max(priced), cur)}"
    header += "):"

    lines = [header]
    for v in variants:
        vc = v.get("currency") or cur
        price_str = _fmt_money(v["price"], vc) if v["price"] is not None else "price N/A"
        line = f"- {v.get('display_spec') or v['sku_id']} — {price_str}"
        if v.get("original_price") and v["price"] and v["original_price"] > v["price"]:
            disc = round((1 - v["price"] / v["original_price"]) * 100)
            line += f" (was {v['original_price']:.2f}, -{disc}%){_msrp_flag(disc)}"
        if not v["in_stock"] or v.get("stock") == 0:
            line += " ⚠ out of stock"
        lines.append(line)
    return "\n".join(lines)


def _iter_blocks(resp: dict):
    """
    Yield (block_id, block) for each dict block in a dida `data.data` block-map.
    Shared by the cart / orders / wishlist extractors, which all render this shape.
    """
    blocks = resp.get("data", {}).get("data", {})
    if isinstance(blocks, dict):
        for bid, b in blocks.items():
            if isinstance(b, dict):
                yield bid, b


def _cents_to_float(amt_dict: Any) -> tuple[Optional[float], Optional[str]]:
    """AliExpress cart prices are nested dicts: {amount: {cent: 804, currencyCode: 'USD'}}."""
    if not isinstance(amt_dict, dict):
        return None, None
    amount = amt_dict.get("amount") if "amount" in amt_dict else amt_dict
    if isinstance(amount, dict):
        cent = amount.get("cent")
        code = amount.get("currencyCode")
        if isinstance(cent, (int, float)):
            return round(cent / 100.0, 2), code
    fmt = amt_dict.get("formattedAmount")
    if isinstance(fmt, str):
        return parse_price(fmt), None
    return None, None


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
                "image_url": fields.get("img"),
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


@mcp.tool()
def view_cart() -> str:
    """
    View current AliExpress cart contents (read-only).

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
    if not any("SUCCESS" in r for r in ret):
        return (
            f"Cart API returned: {ret_str}. "
            "If this says TOKEN_EXPIRED, re-save AliExpress cookies via the MCP Auth Bridge."
        )

    cart = _extract_cart(resp)
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

    currency = cart.get("currency") or CURRENCY
    n = len(items)

    # Honest header. AliExpress paginates the cart (append / infinite-scroll behind
    # an opaque cursor); the render API exposes only the first page, so the shown
    # count can be less than the server's total — say so instead of losing items.
    truncated = bool(count and count > n)
    if truncated:
        header = (
            f"Cart — showing {n} of {count} items.\n"
            f"  ⚠ AliExpress paginates the cart; the API exposes only the first page, "
            f"so {count - n} more item(s) aren't listed here."
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

    for shop_name in seen_shops:
        group = grouped[shop_name]
        url = next((it.get("shop_url") for it in group if it.get("shop_url")), None)
        lines.append(f"▸ {shop_name}" + (f"  ({url})" if url else ""))
        for it in group:
            line = f"  - {it['title']}"
            if it.get("price") is not None:
                line += f" — {_fmt_money(it['price'], it.get('currency') or currency)}"
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
            if it.get("shipping_cost") is not None:
                ship_str = "Free" if it["shipping_cost"] == 0 else _fmt_money(it["shipping_cost"], it.get("currency") or currency)
                line += f"\n      shipping: {ship_str}"
            if it.get("delivery_date"):
                line += f"\n      delivery: {it['delivery_date']}"
            if not it.get("valid", True):
                line += "\n      ⚠️ invalid (sold out or removed)"
            line += f"\n      item_id: {it['item_id']}"
            lines.append(line)
        lines.append("")

    # Computed subtotal over the shown, priced items — always meaningful. The
    # server's own subtotal/total reflect only the checkbox-*selected* lines
    # (that's why a cart of priced items can report an "Estimated total" of 0.00),
    # so we compute ours and label the server figure honestly.
    priced = [(it["price"], int(it.get("quantity") or 1)) for it in items if it.get("price") is not None]
    if priced:
        shown_subtotal = round(sum(p * q for p, q in priced), 2)
        scope = f"{n} shown items" if truncated else "all items shown"
        lines.append(f"Subtotal ({scope}): {_fmt_money(shown_subtotal, currency)}")
    if cart.get("total") is not None:
        lines.append(
            f"AliExpress checkout estimate (selected items only): {_fmt_money(cart['total'], currency)}"
        )

    return "\n".join(lines).rstrip()


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


def _order_ret_problem(resp: dict) -> Optional[str]:
    """Return a friendly message if the order response isn't a success, else None."""
    ret = resp.get("ret", []) if isinstance(resp, dict) else []
    ret_str = ret[0] if ret else ""
    if any("SUCCESS" in r for r in ret):
        return None
    if any(s in ret_str for s in ("SESSION_EXPIRED", "NEED_LOGIN", "ILLEGAL_ACCESS", "NO_LOGIN")):
        return FULL_AUTH_MSG
    if "TOKEN" in ret_str:
        return AUTH_EXPIRED_MSG
    return f"AliExpress API returned: {ret_str or 'unknown error'}."


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
    return seg


@mcp.tool()
def list_orders(max_orders: int = 10) -> str:
    """
    List your recent AliExpress orders (read-only): status, date, store, items, total.

    Requires a FULL login session in the credential file — the quick cookie snippet
    misses HttpOnly login cookies (see README). Does not expose shipping addresses.

    Args:
        max_orders: Max number of orders to list (default 10).
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        resp = mtop_call(
            ORDER_LIST_API, "1.0", {"page": 1, "pageSize": max(max_orders, 10)},
            cookies=cookies, referer=f"{BASE_URL}/p/order/index.html",
        )
    except Exception as e:
        return f"Order list call failed: {e}"

    problem = _order_ret_problem(resp)
    if problem:
        return problem

    orders = _extract_orders(resp, max_orders)
    if not orders:
        return (
            "Order API succeeded but no order blocks were recognized. The order-list "
            "shape may have changed — update `_extract_orders`."
        )

    lines = [f"Recent orders ({len(orders)}):"]
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
        for it in o.get("items", [])[:6]:
            lines.append(_order_item_line(it))
        money = _order_money(o.get("total"), o.get("currency"), o.get("total_text"))
        if money:
            lines.append(f"  total: {money}")
    return "\n".join(lines)


@mcp.tool()
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
        resp = mtop_call(
            ORDER_LIST_API, "1.0", {"page": 1, "pageSize": 20},
            cookies=cookies, referer=f"{BASE_URL}/p/order/index.html",
        )
    except Exception as e:
        return f"Order lookup failed: {e}"

    problem = _order_ret_problem(resp)
    if problem:
        return problem

    orders = _extract_orders(resp, 100)
    match = next((o for o in orders if o["order_id"] == str(order_id)), None)
    if not match:
        return (
            f"Order {order_id} not found in your 20 most recent orders — it may be "
            "older. Run list_orders to see what's available."
        )

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
_ITEM_URL_RE = re.compile(r"/item/(\d+)\.html")


def _wishlist_container(resp: dict) -> tuple[dict, dict]:
    """
    Return (components, globals) from the wishlist response, tolerant of shape.
    The live shape is the Ultron container (data.data.data / data.data.global);
    fall back to the flat block-map if a future response reverts to it.
    """
    outer = resp.get("data", {}).get("data", {})
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


@mcp.tool()
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
        "_lang": "en_US", "_currency": CURRENCY, "country": COUNTRY,
        "pageIndex": 1, "pageSize": max(max_items, 20), "groupId": "0",
    }
    try:
        resp = mtop_call(
            WISHLIST_API, "1.0", payload, cookies=cookies,
            referer=f"{BASE_URL}/p/wish-manage/index.html",
        )
    except Exception as e:
        return f"Wishlist call failed: {e}"
    problem = _order_ret_problem(resp)  # same full-login gate as orders
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


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
