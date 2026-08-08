"""
Shared foundation for the AliExpress MCP server: configuration, cookie/session
handling, the signed MTOP HTTP client, and small formatting helpers used
across the search/catalog/cart/account modules.

Moved verbatim out of aliexpress_mcp_server.py — see that file's module
docstring for the server-level overview.

This module must NOT import `mcp` (avoids a server/domain import cycle) and
must NOT import `bs4` (BeautifulSoup parsing lives only in scrape.py).
"""

import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

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

# AliExpress's anti-bot risk engine (RGV587) answers with FAIL_SYS_USER_VALIDATE
# when it wants a human to clear a challenge — this is a different failure mode
# from an expired session/token above, and looks like a raw platform error code
# if surfaced verbatim (report item #10). Testing that report established two
# things: it is NOT time-based (retries at 45s and 90s both still failed), and
# it cleared ONLY once a human completed the challenge in a logged-in browser
# tab. So the message has to say plainly that waiting won't help — the natural
# agent instinct to retry with exponential backoff just burns calls and
# prolongs the block instead of fixing anything.
CHALLENGE_MSG = (
    "AliExpress is holding this request behind a human-verification challenge "
    "(anti-bot check — FAIL_SYS_USER_VALIDATE / RGV587_ERROR). Waiting will NOT "
    "clear it — do not retry with backoff. Open aliexpress.com in the browser "
    "tab you're logged into, complete the challenge there, then retry."
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

        # This must NOT also swallow FAIL_SYS_USER_VALIDATE / RGV587_ERROR (the
        # anti-bot human-verification challenge, report item #10) — that failure
        # doesn't clear with time, confirmed by testing 45s and 90s retries that
        # both still failed, so silently retrying here would just burn calls and
        # prolong the block instead of fixing anything. Checked: neither string
        # contains "FAIL_SYS_TOKEN", so today's substring match already excludes
        # them; this comment is here so that exclusion stays intentional if the
        # match is ever broadened. See ret_problem()'s CHALLENGE_MSG for the
        # caller-facing message — callers must surface it, not loop on it.
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


# AliExpress abbreviates high sales volumes — "100K+ sold", "5.7K+ sold", "1M+ sold"
# — and on some listings says "sales" instead ("10,000+ sales ౹ 50,000+ cross-platform
# sales"). A digits-only pattern therefore lost exactly the best-selling listings:
# every one of the top hits for `usb c cable` sorted by orders reads "100K+ sold" and
# matched nothing, so get_product_details printed no Sold line at all while
# search_products (which reads a different field) printed "100K+ sold". Verified live
# Aug 2026. The leading alternative must stay first so "10,000+ sales" wins over the
# broader "cross-platform sales" figure later in the same string.
_SOLD_RX = re.compile(r"(\d[\d,.]*)\s*([KM])?\+?\s*(?:sold|sales)\b", re.IGNORECASE)


def _sold_to_int(digits: str, suffix: Optional[str]) -> Optional[int]:
    """Best-effort integer for a sold-count figure; None when it can't be read safely."""
    t = digits.strip().rstrip(".,").replace(",", "")
    if suffix:
        try:
            return int(float(t) * (1000 if suffix.upper() == "K" else 1_000_000))
        except ValueError:
            return None
    if "." in t:
        # Bare "1.234" is a thousands separator in some locales and a decimal in
        # others — guessing would invent a number, so report only the display text.
        return None
    try:
        return int(t)
    except ValueError:
        return None


def _parse_sold_count(text: Any) -> tuple[Optional[str], Optional[int]]:
    """
    Find the first sold/sales figure in `text`.

    Returns (display text as AliExpress wrote it, integer volume or None) — the
    integer is what makes "100K+" comparable against "5,000+" without the consumer
    having to parse an abbreviation.
    """
    if not isinstance(text, str) or not text:
        return None, None
    mt = _SOLD_RX.search(text)
    if not mt:
        return None, None
    return mt.group(0).strip(), _sold_to_int(mt.group(1), mt.group(2))


# ─── Block-map helpers ──────────────────────────────────────────────────────

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
    if "FAIL_SYS_USER_VALIDATE" in ret_str or any("RGV587" in r for r in ret):
        # If the server handed back a challenge/verification URL alongside the
        # error, surface it — but only if it's actually there; never fabricate
        # one. Seen in practice on the cart-add endpoint as `data.url`; other
        # endpoints may omit it, and the message above stands on its own either way.
        data = resp.get("data") if isinstance(resp, dict) else None
        url = data.get("url") if isinstance(data, dict) else None
        return CHALLENGE_MSG + (f" Challenge URL: {url}" if url else "")
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
