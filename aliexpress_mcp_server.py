#!/usr/bin/env python3
"""
AliExpress MCP Server

Search AliExpress, pull clean product details, check shipping to the
configured country (ALIEXPRESS_COUNTRY, default CA), and manage your cart, orders, and wishlist.

Auth: Session cookies from MCP Auth Bridge extension at
~/.mcp-credentials/aliexpress.json
"""

import base64
import gzip
import json
import os
import random
import re
import threading
import time
import hashlib
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# ─── Configuration ──────────────────────────────────────────────────────────

CREDENTIALS_PATH = Path(
    os.environ.get("ALIEXPRESS_CREDENTIALS", "~/.mcp-credentials/aliexpress.json")
).expanduser()

COUNTRY = os.environ.get("ALIEXPRESS_COUNTRY", "CA")
CURRENCY = os.environ.get("ALIEXPRESS_CURRENCY", "CAD")

# MTOP resolves the shipping destination from `_lang`, NOT from the `country`
# param — `country` is accepted and ignored. Sending a hardcoded "en_US" pins
# every freight quote to the United States and makes items look unreachable.
LANG = f"en_{COUNTRY}"

BASE_URL = "https://www.aliexpress.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aliexpress-mcp")


# ─── Auth ───────────────────────────────────────────────────────────────────

# MTOP hands out a fresh `_m_h5_tk` whenever the current one is stale, as an
# ordinary Set-Cookie on a 200 response whose body says FAIL_SYS_TOKEN_EXOIRED.
# The credential file on disk therefore goes stale within minutes of being saved.
# Without remembering the refreshed token, EVERY call paid the handshake twice —
# one throwaway request plus the real one — doubling request volume against an
# API that rate-limits on exactly that.
_SESSION_COOKIE_KEYS = ("_m_h5_tk", "_m_h5_tk_enc")
_cookie_lock = threading.Lock()
_session_cookies: dict[str, str] = {}


def remember_session_cookies(new: dict[str, str]) -> None:
    """Cache refreshed MTOP tokens for the life of the process."""
    fresh = {k: v for k, v in (new or {}).items() if k in _SESSION_COOKIE_KEYS and v}
    if not fresh:
        return
    with _cookie_lock:
        _session_cookies.update(fresh)


def load_cookies() -> dict[str, str]:
    """
    Load session cookies from the credential file written by MCP Auth Bridge,
    overlaid with any MTOP token refreshed during this process.
    """
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
        cookies = dict(data.get("cookies", {}))
    except (json.JSONDecodeError, KeyError):
        return {}
    with _cookie_lock:
        cookies.update(_session_cookies)
    return cookies


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

# ─── Pacing ─────────────────────────────────────────────────────────────────
#
# AliExpress's risk engine (RGV587) answers bursts of calls with
# FAIL_SYS_USER_VALIDATE — a short cooldown, not a ban. Nothing in the protocol
# avoids it; the only remedy anyone reports is not firing requests back to back.
# Writes are policed harder than reads, so they get their own longer floor.
MTOP_MIN_INTERVAL = float(os.environ.get("ALIEXPRESS_MIN_INTERVAL", "0.7"))
MTOP_JITTER = 0.6
CART_WRITE_MIN_INTERVAL = float(os.environ.get("ALIEXPRESS_CART_INTERVAL", "5.0"))

_pace_lock = threading.Lock()
_last_call_at: dict[str, float] = {}


def _pace(channel: str = "mtop", min_interval: float = MTOP_MIN_INTERVAL) -> None:
    """
    Sleep just long enough that consecutive calls on `channel` stay at least
    `min_interval` apart, plus jitter so the cadence isn't machine-regular.
    """
    with _pace_lock:
        now = time.monotonic()
        wait = (_last_call_at.get(channel, 0.0) + min_interval) - now
        if wait > 0:
            time.sleep(wait + random.uniform(0, MTOP_JITTER))
        _last_call_at[channel] = time.monotonic()


MTOP_APP_KEY = "12574478"
# The cart write endpoint is signed with a different appKey than the read APIs.
# The sign is md5(token & t & appKey & data), so this must match the appKey in
# the query string or the call fails with FAIL_SYS_ILLEGAL_ACCESS.
MTOP_CART_APP_KEY = "24815441"
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
    app_key: str = MTOP_APP_KEY,
    method: str = "GET",
    extra_query: Optional[dict[str, str]] = None,
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
    sign = _mtop_sign(token, t_ms, app_key, data_str)

    params = {
        "jsv": "2.6.2",
        "appKey": app_key,
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
    if extra_query:
        params.update(extra_query)

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

    _pace()
    with httpx.Client(timeout=40.0, follow_redirects=True) as c:
        if method.upper() == "POST":
            # Some payloads (the cart's compressed component tree) run to several
            # KB — far past the URL length MTOP's front end accepts, which answers
            # with "Http-Header-Length-Exceed". Those must go in the form body;
            # the signature still covers the same `data` string either way.
            post_headers = dict(headers)
            post_headers["Content-Type"] = "application/x-www-form-urlencoded"
            query = {k: v for k, v in params.items() if k != "data"}
            resp = c.post(url, params=query, headers=post_headers, data={"data": data_str})
        else:
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
        # Any response may hand back a refreshed token; keep it regardless of
        # whether this call needed a retry, so later calls skip the handshake.
        served = {}
        for name in resp.cookies:
            val = resp.cookies.get(name)
            if val is not None:
                served[name] = val
        remember_session_cookies(served)

        if retries > 0 and "FAIL_SYS_TOKEN" in ret_str:
            new_cookies = dict(cookies)
            new_cookies.update(served)
            return mtop_call(api, version, payload, cookies=new_cookies, retries=retries - 1,
                             referer=referer, app_key=app_key, method=method,
                             extra_query=extra_query)

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

mcp = FastMCP(
    "aliexpress",
    dependencies=["httpx", "beautifulsoup4"],
    instructions=(
        "Browse, search, and manage an AliExpress account: search products, compare "
        "sellers, check reviews/shipping/variants, and inspect your cart, orders, and "
        "wishlist. Four tools write to the real, signed-in account (add_to_cart, "
        "set_cart_quantity, remove_from_cart, create_wishlist) but none of them ever "
        "checks out, places an order, or pays — checkout is not implemented."
    ),
)


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
    ship_from: str = "",
) -> str:
    """
    Search AliExpress for products.

    Args:
        query: Search term (e.g., "groudon plush", "usb c cable")
        min_rating: Minimum rating (0-5, e.g., 4.5). 0 disables filter.
        max_price: Maximum price, in the search's local currency (your AliExpress
            site currency, e.g. UAH — not necessarily ALIEXPRESS_CURRENCY). 0 disables.
        sort_by: One of "best_match", "orders", "price_asc", "price_desc"
        ship_from: Two-letter warehouse country to restrict results to, e.g. "ES",
            "PL", "FR", "CZ", "IT", "UK" for EU stock or "CN" for mainland China.
            Shipping from inside your own customs union arrives in days rather than
            weeks and avoids import charges. Empty = any warehouse.
    """
    try:
        products, total_results = _search_fetch_parse(query, sort_by, ship_from)
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
        # Distinguish "AliExpress has nothing" from "AliExpress did not render the
        # grid". Reporting the second as the first told callers a 92,000-result
        # query was empty, and they believed it.
        if total_results and not filters:
            return (
                f"AliExpress reports {total_results:,} results for '{query}' but did not "
                f"return the results grid after {SEARCH_RENDER_ATTEMPTS} attempts — this is "
                "an intermittent server-side render failure, not an empty catalogue. "
                "Retry the same query."
            )
        if total_results:
            return (f"No products matched {', '.join(filters)} for '{query}' "
                    f"(AliExpress reports {total_results:,} results before filtering).")
        return f"No products found for '{query}'{suffix}."

    shown = min(len(products), 25)
    header = f"Showing {shown} of {len(products)} parsed"
    if total_results:
        header += f" ({total_results:,} total)"
    header += f" for '{query}' (sort: {sort_by}"
    header += f", ships from {ship_from.upper()}" if ship_from else ""
    header += "):"
    return _format_product_lines(products, header)


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
    ship_from: str = "",
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
        ship_from: Two-letter warehouse country (e.g. "ES", "PL", "CN"). Empty = any.
    """
    try:
        products, total_results = _search_fetch_parse(query, sort_by, ship_from)
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
        "rating": None,
        "review_count": None,
        "sold_count": None,
        "seller_name": None,
        "store_url": None,
        "seller_positive_rate": None,
        "seller_total_reviews": None,
        "shipping_cost": None,
        "free_shipping_over": None,
        "shipping_estimate": None,
        "ship_from": None,
        "ship_from_code": None,
        "tax_note": None,
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

    # ── Image ─────────────────────────────────────────────────────────
    hdr = result.get("HEADER_IMAGE_PC")
    if isinstance(hdr, dict):
        imgs = hdr.get("imagePathList") or hdr.get("imgList")
        if isinstance(imgs, list) and imgs and isinstance(imgs[0], str):
            d["image_url"] = imgs[0]

    return d


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
    # Printed verbatim as AliExpress formatted it — it is an order-level threshold,
    # not this item's freight, so it must never read as the shipping cost.
    if d.get("free_shipping_over"):
        lines.append(f"  Free shipping on orders over {d['free_shipping_over']}")
    if d.get("shipping_estimate"):
        eta_line = f"Estimated delivery: {d['shipping_estimate']}"
        if d.get("ship_days_min") and d.get("ship_days_max"):
            eta_line += f" ({d['ship_days_min']}–{d['ship_days_max']} days)"
        lines.append(eta_line)
    if d.get("ship_from"):
        origin = f"Ships from: {d['ship_from']}"
        if d.get("ship_from_code"):
            origin += f" ({d['ship_from_code']})"
        lines.append(origin)
    if d.get("tax_note"):
        lines.append(f"Duties: {d['tax_note']}")
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
    if d.get("free_shipping_over"):
        lines.append(f"  Free shipping on orders over {d['free_shipping_over']}")
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


@mcp.tool(
    title="Get Seller",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
    structured_output=False,
)
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

    err = _pdp_error_code(resp)
    if err:
        return _pdp_unavailable_msg(item_id, err)

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
        products, _ = _search_fetch_parse(query)
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
        # Prefer the "Axis: Value" form — it reveals when a seller is pushing
        # unrelated products through a dimension like Color.
        labels = v.get("label_parts") or []
        kept_labels = [l for l in labels if l.split(": ", 1)[-1] not in common]
        if kept_labels:
            v["display_spec"] = " · ".join(kept_labels)

    # Per-variant images only help when they actually differ; on a normal listing
    # every SKU shares one image and printing it 20 times is pure noise.
    seen_images = {v.get("image") for v in variants if v.get("image")}
    images_vary = len(seen_images) > 1

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
        elif isinstance(v.get("stock"), int):
            line += f", {v['stock']} in stock"
        # The sku_id is what add_to_cart needs to pick this exact configuration —
        # without it the caller can only ever add the item's preselected variant.
        line += f"  [sku_id: {v['sku_id']}]"
        lines.append(line)
        if images_vary and v.get("image"):
            lines.append(f"    image: {v['image']}")

    return "\n".join(lines)


def blocks(resp: dict) -> dict:
    """
    Return a response's `data.data` block-map ({} if the shape doesn't match).

    Cart, order-list, and wishlist renders all key their components this way —
    every read of that shape should go through here instead of retyping the dig.
    """
    b = (resp.get("data") or {}).get("data") or {}
    return b if isinstance(b, dict) else {}


def _iter_blocks(resp: dict):
    """
    Yield (block_id, block) for each dict block in a dida `data.data` block-map.
    Shared by the cart / orders / wishlist extractors, which all render this shape.
    """
    for bid, b in blocks(resp).items():
        if isinstance(b, dict):
            yield bid, b


def _block_by_prefix(resp: dict, prefix: str) -> tuple[Optional[str], dict]:
    """
    Find the first block whose id starts with `prefix`.

    Component ids carry a server-assigned numeric suffix (cart pagination,
    order-list body/header, …), so matching the bare name exactly stops working
    silently the moment AliExpress changes the suffix — prefix matching is the
    stable way to find them. Shared by the cart and order pagers.
    """
    for bid, block in blocks(resp).items():
        if bid.startswith(prefix) and isinstance(block, dict):
            return bid, block
    return None, {}


def ret_problem(resp: dict) -> Optional[str]:
    """
    Return a friendly message if an MTOP response's `ret` isn't a success, else
    None. Shared by the cart, order, and wishlist tools — they all see the same
    handful of failure shapes (expired quick-copy cookies, expired token, …).
    """
    ret = resp.get("ret", []) if isinstance(resp, dict) else []
    ret_str = ret[0] if ret else ""
    if any("SUCCESS" in r for r in ret):
        return None
    if any(s in ret_str for s in ("SESSION_EXPIRED", "NEED_LOGIN", "ILLEGAL_ACCESS", "NO_LOGIN")):
        return FULL_AUTH_MSG
    if "TOKEN" in ret_str:
        return AUTH_EXPIRED_MSG
    return f"AliExpress API returned: {ret_str or 'unknown error'}."


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
                "image_url": f.get("itemImageUrl"),
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
            "image_url": iv.get("imageUrl"),
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
    blocks = tree.get("data") or {}
    root = (tree.get("page") or {}).get("root")

    comp = json.loads(json.dumps(blocks[component_id]))
    comp.setdefault("fields", {})["operationType"] = operation
    if quantity is not None:
        comp["fields"]["quantityView"] = {"current": int(quantity)}
    comp["needSubmit"] = True
    data = {component_id: comp}
    if root and root in blocks:
        root_comp = json.loads(json.dumps(blocks[root]))
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

    sku_id, service, err = _resolve_sku_for_cart(item_id, sku_id)
    if err:
        return err

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

    def _is_challenged(r: dict) -> bool:
        rets = r.get("ret") or [""]
        return "FAIL_SYS_USER_VALIDATE" in rets[0] or any("RGV587" in str(x) for x in rets)

    # Writes are the rate-limited surface, so hold them further apart than reads
    # and absorb one cooldown transparently — it typically clears in seconds.
    resp = None
    for attempt, backoff in enumerate((6.0, 15.0, None)):
        _pace("cart_write", CART_WRITE_MIN_INTERVAL)
        try:
            resp = mtop_call(
                "mtop.aliexpress.trade.cart.add", "1.0", payload,
                cookies=cookies,
                referer=f"{BASE_URL}/item/{item_id}.html",
                app_key=MTOP_CART_APP_KEY,
            )
        except Exception as e:
            return f"MTOP call failed: {e}"
        if not _is_challenged(resp) or backoff is None:
            break
        logger.info("cart.add rate-limited; retrying in %.0fs (attempt %d)", backoff, attempt + 1)
        time.sleep(backoff + random.uniform(0, 2.0))

    ret_all = resp.get("ret") or [""]
    ret = ret_all[0]
    data = resp.get("data") or {}

    # AliExpress's risk engine (RGV587) challenges writes with a captcha. It is
    # scoped to the write endpoint — reads keep working — and clears once the
    # challenge is solved in a browser and fresh cookies are saved.
    if "FAIL_SYS_USER_VALIDATE" in ret or any("RGV587" in str(x) for x in ret_all):
        msg = [
            f"AliExpress rate-limited this write (anti-bot check) — item {item_id} "
            "was NOT added.",
            "",
            "This is usually a short cooldown triggered by adding several items in "
            "quick succession, and it clears by itself — wait a minute or two and "
            "retry. Searching and product lookups keep working meanwhile.",
            "",
            "Only if it persists across several minutes: open AliExpress in the "
            "browser you exported cookies from, add anything to the cart manually, "
            "then re-save credentials.",
        ]
        challenge = data.get("url")
        if challenge:
            msg += ["", f"(Verification URL, rarely needed: {challenge})"]
        return "\n".join(msg)

    if ret_problem(resp) is not None or data.get("addFailed"):
        return f"Could not add item {item_id} to cart — AliExpress said: {ret or 'no status returned'}."

    lines = [f"Added item {item_id} (variant {sku_id}) ×{quantity} to your cart."]
    if data.get("cartNum") is not None:
        lines.append(f"  Cart now holds {data['cartNum']} item(s).")
    if data.get("cartId") is not None:
        lines.append(f"  Cart line ID: {data['cartId']}")
    lines.append("  Nothing has been ordered or paid for.")
    return "\n".join(lines)


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

    currency = cart.get("currency") or (summary.get("currency") if used_droplet else None) or CURRENCY
    n = len(items)

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
    priced = [(it["price"], int(it.get("quantity") or 1)) for it in items if it.get("price") is not None]
    if priced:
        shown_subtotal = round(sum(p * q for p, q in priced), 2)
        scope = f"{n} shown items" if truncated else "all items shown"
        lines.append(f"Subtotal ({scope}): {_fmt_money(shown_subtotal, currency)}")
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
        resp = mtop_call(
            ORDER_LIST_API, "1.0", {"page": 1, "pageSize": 20},
            cookies=cookies, referer=f"{BASE_URL}/p/order/index.html",
        )
    except Exception as e:
        return f"Order lookup failed: {e}"

    problem = ret_problem(resp)
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


def _wishlist_delete_item(cookies: dict, item_id: str) -> str:
    """
    Permanently delete a saved item from the wishlist.

    Different mechanism from `saveItem`: this is an Ultron/droplet operation on
    the *render* endpoint — echo the item's component back with
    `fields.operationType = "DELETE_PRODUCT"`, alongside the render's own linkage
    and hierarchy. `params` is a plain JSON string of nested OBJECTS here, unlike
    the orders pager which nests JSON *strings*.

    AliExpress distinguishes this from un-grouping: removing from a collection
    keeps the item in the wishlist, deleting removes it everywhere. This is the
    destructive one.
    """
    render = mtop_call(
        WISHLIST_API, "1.0",
        {"pageIndex": 1, "shipToCountry": COUNTRY, "locale": "en_US", "deviceType": "PC",
         "_lang": LANG, "_currency": CURRENCY, "wishGroupId": 0},
        cookies=cookies, referer=f"{BASE_URL}/p/wish-manage/index.html",
    )
    tree = (render.get("data") or {}).get("data") or {}
    comp_id = f"wln_page_product_I_{item_id}"
    component = (tree.get("data") or {}).get(comp_id)
    if not component:
        return "NOTFOUND::item is not in your wishlist"

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
        "deviceType": "PC", "_lang": LANG, "_currency": CURRENCY, "wishGroupId": 0,
    }
    _pace("cart_write", CART_WRITE_MIN_INTERVAL)
    resp = mtop_call(WISHLIST_API, "1.0", payload, cookies=cookies,
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
    Put an ALREADY-SAVED item into one of your wishlists. This WRITES.

    IMPORTANT — this moves, it does not save. AliExpress's list API only assigns
    items that are already in your wishlist to a list; it cannot pull in a product
    you have not saved. Saving a new product is a separate action (the ♡ on the
    product page) that this server does not yet implement, so for an unsaved item
    this reports what is missing rather than silently doing nothing.

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
def remove_from_wishlist(item_id: str = "", url: str = "") -> str:
    """
    Permanently DELETE a saved item from your wishlist. This WRITES and CANNOT
    be undone — the item is removed from every list and from the wishlist itself.

    AliExpress separates two things this tool does NOT do: taking an item out of
    one list while keeping it saved, and moving it between lists. Use
    add_to_wishlist to file a saved item under a different list. This tool is the
    permanent one, so it takes no list argument — it deletes the item outright.

    Args:
        item_id: AliExpress item ID to delete from the wishlist.
        url: Full or short AliExpress product URL (alternative to item_id).
    """
    item_id = _resolve_item_id(item_id, url)
    if not item_id:
        return "Provide a valid item_id or AliExpress product URL."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    saved = _wishlist_saved_item_ids(cookies)
    if saved and str(item_id) not in saved:
        return f"Item {item_id} is not in your wishlist — nothing to delete."

    try:
        ret = _wishlist_delete_item(cookies, str(item_id))
    except Exception as e:
        return f"Wishlist deletion failed: {e}"
    if ret.startswith("NOTFOUND"):
        return f"Item {item_id} is not in your wishlist — nothing to delete."
    if not ret.startswith("SUCCESS"):
        return f"Could not delete item {item_id} — AliExpress said: {ret}"

    # Destructive and this API family acks no-ops, so confirm against a fresh read.
    after = _wishlist_saved_item_ids(cookies)
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

    Creates the list itself, not its contents — there is no tool yet for saving
    items into it. Names are not checked for duplicates: AliExpress will happily
    create a second list with the same name, so this reports the new list's id.

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


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
