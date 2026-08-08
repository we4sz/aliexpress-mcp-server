"""
Product catalog domain: search fetch+render, PDP fetch+extract+render,
seller/store, reviews, and the variant (SKU) table.

Moved verbatim out of aliexpress_mcp_server.py — see that file's module
docstring for the server-level overview.
"""

import json
import os
import random
import re
import time
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx

from aliexpress_mcp.core import (
    BASE_URL, COUNTRY, CURRENCY, LANG, USER_AGENT, ACCEPT_LANGUAGE, HTTP2, logger,
    load_cookies, get_client, check_auth_redirect,
    AUTH_EXPIRED_MSG, mtop_call, ret_problem,
    _msrp_flag, _fmt_money, parse_price, _normalize_price, _strip_html,
    _parse_sold_count,
)
from aliexpress_mcp.scrape import (
    parse_search_results, SEARCH_SOURCE_GAPS,
    classify_search_render, SSR_UNPARSEABLE,
)


SORT_MAP = {
    "best_match": None,
    "orders": "total_tranpro_desc",
    "price_asc": "price_asc",
    "price_desc": "price_desc",
}


SEARCH_RENDER_ATTEMPTS = 3

# Seconds to wait before each search retry, one entry per transition (3 attempts
# = 2 transitions). Longer than the value they replace because the thing being
# waited out is AliExpress deciding to render the grid, not our own politeness.
#
# These used to go through `_pace("search_retry", 1.0)`, which did not sleep at
# all before the first retry. `_pace` computes
# `wait = _last_call_at.get(channel, 0.0) + interval - time.monotonic()`, so on a
# channel never used in this process `last` defaults to 0.0 against a monotonic
# clock measured in days-since-boot and `wait` is hugely negative. Measured:
#
#     call 0 on a cold channel slept 0.000s
#     call 1                   slept 1.441s
#     call 2                   slept 1.169s
#
# That default is RIGHT for rate limiting — the first request in a process has
# nothing to be spaced from — and wrong for backoff, where the whole point is to
# wait before repeating something that just failed. The observable symptom was
# the reported one: attempt 0→1 was an immediate identical resubmit, which failed
# again, and only 1→2 ever backed off. `_pace` is left alone; core.py's cold-start
# behaviour is correct for its actual job.
#
# Jittered on use for the same reason `_pace` jitters: a fixed cadence is a
# fingerprint, and this path only runs when AliExpress is already unhappy with us.
SEARCH_RETRY_BACKOFF = (1.5, 3.0)


def _search_backoff(transition: int) -> float:
    """Seconds to sleep before the next search attempt, jittered."""
    base = SEARCH_RETRY_BACKOFF[min(transition, len(SEARCH_RETRY_BACKOFF) - 1)]
    return base + random.uniform(0, 0.4)


def _search_total_results(html: str) -> Optional[int]:
    """Read the server's own result count, which is present even when the grid isn't."""
    m = re.search(r'"totalResults"\s*:\s*(\d+)', html)
    return int(m.group(1)) if m else None


def _search_slug(query: str) -> str:
    """
    Build the `/w/wholesale-<slug>.html` path segment for a query.

    Punctuation must be REMOVED, not percent-encoded. `quote_plus` turns "1/4W 1%"
    into "1%2F4W-1%25", and AliExpress answers that path with a redirect to
    /s/error/404 — a 13KB page with no grid and no `totalResults`, which parses as
    zero cards and renders as "No listings found". Verified live Aug 2026 with
    "2600pcs Metal Film Resistors Assorted Pack 130 Values 1/4W 1%": the raw title
    404s, while the same words with the punctuation blanked return 1,849 results
    with the exact product ranked #1.

    Punctuation becomes a space rather than nothing so "1/4W" reads as "1 4W"
    (two tokens AliExpress can match) instead of the meaningless "14W".
    """
    cleaned = re.sub(r"[^0-9A-Za-zÀ-￿]+", " ", query or "")
    return quote_plus(" ".join(cleaned.split())).replace("+", "-")


# Order the EU expansion by how much stock AliExpress actually warehouses in each
# country, because only the FIRST entry is sent to the server (see
# `_search_fetch_parse`) and a rare warehouse makes a useless probe. Counted over
# 600 unfaceted live search cards (Aug 2026): PL 77, DE 53, FR 7, ES 4, and every
# other member zero — against CN 459. Alphabetical order would have probed AT,
# which never appeared once. Members with no observed stock keep a stable
# alphabetical tail; they still filter correctly, they are just poor probes.
EU_PROBE_ORDER = ("PL", "DE", "FR", "ES", "CZ")


def normalize_ship_from(ship_from: Any) -> list[str]:
    """
    Resolve a warehouse filter into a list of country codes.

    Accepts a single code ("PL"), a comma string ("PL,CZ,ES"), a list, or the alias
    "EU" for the whole customs union — "anywhere in the EU" is the usual intent, and
    naming 27 countries by hand is not something a caller should have to do.

    Order is preserved and meaningful: the caller's first code is the one the server
    is asked for, so an explicit ["ES","PL"] probes ES.
    """
    if not ship_from:
        return []
    raw = ship_from if isinstance(ship_from, (list, tuple, set)) else str(ship_from).split(",")
    out: list[str] = []
    for entry in raw:
        code = str(entry or "").strip().upper()
        if not code:
            continue
        if code in ("EU", "EEA"):
            # DUTY_FREE_BLOCS[0] is the EU set already maintained for the duty
            # logic, so there is one list, not two.
            bloc = DUTY_FREE_BLOCS[0]
            for c in list(EU_PROBE_ORDER) + sorted(bloc):
                if c in bloc and c not in out:
                    out.append(c)
        elif code not in out:
            out.append(code)
    return out


# A returned row "matches" when it carries at least half the query's tokens; the
# result set is judged degraded when fewer than this fraction of rows do so.
RELEVANCE_FLOOR = 0.5


def _query_tokens(query: str) -> list[str]:
    """Query words worth matching on — 2+ chars, punctuation split out."""
    return [t for t in re.split(r"[^0-9a-z]+", (query or "").lower()) if len(t) >= 2]


def _relevant_fraction(products: list[dict], query: str) -> Optional[float]:
    """
    Fraction of rows whose title carries at least half the query's tokens.

    This exists to catch AliExpress silently REPLACING the query. Asking for
    "4 pin PWM fan extension cable" with shipFromCountry=PL returned 34 cards of
    cable protector ramps, mains extension cords and rubber speed bumps — the
    server answers a thin keyword×warehouse intersection by quietly broadening the
    keywords instead of returning few results. Measured over 5 live result sets
    (Aug 2026) the separation is total: healthy sets score 0.95, 0.97, 1.00, 1.00,
    1.00, the broadened set scores 0.00, and not one of its 34 cards reached even
    0.40 coverage. Anything near the 0.5 floor has never been observed.
    """
    tokens = _query_tokens(query)
    if not tokens or not products:
        return None
    need = len(tokens) / 2.0
    hits = 0
    for p in products:
        body = " " + re.sub(r"[^0-9a-z]+", " ", str(p.get("title") or "").lower()) + " "
        if sum(1 for t in tokens if f" {t} " in body or t in body) >= need:
            hits += 1
    return hits / len(products)


def _search_fetch_parse(query: str, sort_by: str = "best_match",
                        ship_from: Any = "") -> tuple[list[dict], Optional[int]]:
    """
    (items, total_results) — `search_with_notes` without the notes.

    Kept as the two-value form so existing callers keep working unchanged; they
    still get the warehouse intersection and the relevance guard, they just don't
    print the sentences explaining them. Prefer `search_with_notes` in new code.
    """
    items, total, _notes = search_with_notes(query, sort_by, ship_from)
    return items, total


def search_with_notes(query: str, sort_by: str = "best_match",
                      ship_from: Any = "") -> tuple[list[dict], Optional[int], list[str]]:
    """
    Fetch an AliExpress search results page and parse product cards.

    Returns (items, total_results, notes). `total_results` comes from the page's own
    `pageInfo.totalResults` and is reported even when zero cards parse. `notes` are
    caller-facing sentences about how the result set was actually produced — they
    must be printed, because each one describes a way the rows differ from what was
    literally asked for.

    AliExpress intermittently serves the results page WITHOUT the `mods.itemList`
    grid — same URL, same second, sometimes present and sometimes not. Parsing
    that as "no results" told the caller a 92,000-result query had no products,
    so an empty parse against a non-zero total is retried before believing it.

    On the warehouse filter, three things are true and all three are handled here.
    It is worth keeping despite them: for "usb c cable" the facet returned 4,929
    PL-warehouse results while ZERO of the plain search's top 60 shipped from PL,
    so filtering the unfaceted search client-side would have found nothing.
      1. It is not exact. A PL request returned 27 PL cards plus ES 3, FR 2, DE 1
         and CZ 1, so the rows are intersected against the requested set here.
      2. It can replace the query outright when stock is thin — see
         `_relevant_fraction`, which detects that and says so rather than letting
         34 speed bumps pass as fan cables.
      3. The server takes exactly ONE country: `shipFromCountry=PL,ES,DE` collapsed
         "usb c cable" from 4,929 results to 60, of which 58 shipped from CN. So a
         multi-country request asks the server for the first code and keeps any
         warehouse in the set — which is precisely what leak (1) supplies.

    Raises RuntimeError(AUTH_EXPIRED_MSG) if AliExpress bounces us to login.
    """
    wanted = normalize_ship_from(ship_from)
    url_path = f"/w/wholesale-{_search_slug(query)}.html"
    params = {}
    if SORT_MAP.get(sort_by):
        params["SortType"] = SORT_MAP[sort_by]
    if wanted:
        params["shipFromCountry"] = wanted[0]

    total = None
    items: list[dict] = []
    render_notes: list[str] = []
    for attempt in range(SEARCH_RENDER_ATTEMPTS):
        client = get_client()
        try:
            resp = client.get(url_path, params=params)
            if check_auth_redirect(resp):
                raise RuntimeError(AUTH_EXPIRED_MSG)
            resp.raise_for_status()
            items = parse_search_results(resp.text)
            total = _search_total_results(resp.text)
            html = resp.text
        finally:
            client.close()

        if items or not total:
            break

        # An empty parse against a non-zero total has more than one cause, and
        # only some of them are worth repeating the request over. SSR_UNPARSEABLE
        # means the payload WAS there and we failed to read it — our bug, not a
        # dropped render — so an identical resubmit gets an identical failure and
        # burns anti-bot budget for nothing. Stop, and say which it was, because
        # "retry the same query" is actively wrong advice in that case.
        why = classify_search_render(html)
        if why == SSR_UNPARSEABLE:
            logger.info("search payload present but unparseable for %r (total=%s)",
                        query, total)
            render_notes.append(
                f"⚠ AliExpress returned {total:,} results for this query but the page "
                "payload could not be read here, so no listings are shown. This is a "
                "parsing failure on our side, not an empty result set — retrying the "
                "same query will not fix it. The listings do exist on the site."
            )
            break

        if attempt + 1 >= SEARCH_RENDER_ATTEMPTS:
            break
        logger.info("search grid missing for %r (total=%s, %s), retry %d/%d",
                    query, total, why, attempt + 1, SEARCH_RENDER_ATTEMPTS - 1)
        time.sleep(_search_backoff(attempt))

    items, total, notes = _finish_search(items, total, query, wanted)
    return items, total, render_notes + notes


def _finish_search(items: list[dict], total: Optional[int], query: str,
                   wanted: list[str]) -> tuple[list[dict], Optional[int], list[str]]:
    """Apply the client-side warehouse intersection and describe what was done."""
    notes: list[str] = []
    if not wanted or not items:
        return items, total, notes

    share = _relevant_fraction(items, query)
    if share is not None and share < RELEVANCE_FLOOR:
        notes.append(
            f"⚠ AliExpress appears to have broadened the query: only {share:.0%} of the "
            f"{len(items)} returned listings match your keywords. It does this when a "
            f"keyword/warehouse combination has little stock. Treat these as loosely "
            f"related, and re-run without ship_from to see the relevant listings."
        )

    # The fallback parse paths carry no warehouse signal at all; filtering on a
    # field nobody populated would silently empty the list.
    if not any(p.get("ship_from") for p in items):
        notes.append(
            f"Could not verify the warehouse country of these listings, so the "
            f"{'/'.join(wanted[:4])} filter was NOT applied — AliExpress's own filter "
            "is the only thing narrowing them."
        )
        return items, total, notes

    kept = [p for p in items if (p.get("ship_from") or "") in wanted]
    dropped = len(items) - len(kept)
    where = "/".join(wanted) if len(wanted) <= 4 else f"{len(wanted)} countries"
    note = (f"Warehouse filter: asked AliExpress for {wanted[0]} stock, then kept only "
            f"listings shipping from {where}, checked here against each listing's own "
            f"warehouse field.")
    if dropped:
        note += f" {dropped} of {len(items)} were dropped as shipping from elsewhere."
    notes.append(note)
    return kept, total, notes


# Where to cut a keyword-stuffed title when searching for the same product under
# other storefronts. Measured on the reported failure — a 61-char, 10-token title
# whose full form returned nothing while its first 7 tokens rank the exact product
# #1 — plus the 240-card title survey behind `_short_title` (mean 123 chars, and
# the tail is SEO filler). Descending, so the loop stops at the first rung that
# answers and the broad ones are only ever reached on a total miss.
#
# It stops at 5 deliberately. Five tokens of a title head is still the product
# ("2600pcs Metal Film Resistors Assorted"); three is its category ("2600pcs Metal
# Film"), which would always return something and so would turn "no listings
# found" into a confident list of the wrong product — worse than the miss.
TITLE_LADDER_TOKENS = (10, 7, 5)


def _title_query_ladder(title: str) -> list[str]:
    """
    Progressively shorter keyword subsets of a product title, longest first.

    AliExpress titles are keyword-stuffed by convention — "2600pcs Metal Film
    Resistors Assorted Pack 130 Values 1/4W 1%" is a *short* one — so searching a
    title verbatim is the case that needs to work, not an edge case. Each rung
    keeps the head of the title, which is where the identifying words are; the tail
    is the compatibility run ("For Xiaomi Samsung Huawei…") that only narrows the
    match to nothing.

    The complete title is always rung one — with punctuation no longer able to 404
    the URL (`_search_slug`) it is both the most specific query available and, for
    the reported failure, one that works. Only then do the cuts descend.

    Deduplicated and never empty: a two-word title yields exactly one query.
    """
    tokens = [t for t in " ".join((title or "").split()).split(" ") if t]
    if not tokens:
        return []
    out = [" ".join(tokens)]
    for n in TITLE_LADDER_TOKENS:
        rung = " ".join(tokens[:n])
        if rung and rung not in out:
            out.append(rung)
    return out


def search_by_title(title: str, sort_by: str = "best_match",
                    ship_from: Any = "") -> tuple[list[dict], str, list[str]]:
    """
    Search for a product title, shortening the query until AliExpress answers.

    Returns (products, query_actually_used, notes). Callers must report the query
    that was used: a caller told "no listings found" for the full title, when the
    hits were really found under its first 7 words, cannot tell a genuinely absent
    product from a query that was merely too long.
    """
    notes: list[str] = []
    # Notes from rungs that returned nothing. `_finish_search` produces its
    # warehouse and relevance sentences only when there ARE rows, so on an empty
    # rung the only thing that can come back is an explanation of WHY it was empty
    # — which is precisely what a caller about to be told "no listings found"
    # needs. Kept as the last one seen rather than accumulated, because the same
    # sentence otherwise repeats once per rung.
    failure_note: list[str] = []
    rungs = _title_query_ladder(title)
    for i, rung in enumerate(rungs):
        products, _total, rung_notes = search_with_notes(rung, sort_by, ship_from)
        if products:
            if i:
                notes.append(
                    f'Searched the first {len(rung.split())} words — "{rung}" — because '
                    "the full title returned nothing. Confirm each hit is the same product."
                )
            return products, rung, notes + rung_notes
        if rung_notes:
            failure_note = rung_notes[-1:]
        if i + 1 < len(rungs):
            # Same explicit wait as the render retry above, and for the same
            # reason: `_pace` on a cold channel would not have paused before the
            # second rung, so the ladder fired two searches back to back.
            time.sleep(_search_backoff(i))
    return [], (rungs[0] if rungs else ""), notes + failure_note


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

    AliExpress honours `SortType` on a plain search but not when
    `shipFromCountry` is also set. Two different failures were measured live, and
    the second is worse than the first:

      · the sort is ignored and rows still come back — "usb c cable" price_asc
        alone returns 3.23, 3.48, 3.51…; the same call with ship_from=ES returns
        149.91, 195.18, 121.36 while the header still claims price_asc;
      · the result set is ZEROED — "DS18B20" with `shipFromCountry=PL` plus
        `SortType=price_asc` returned total=0 and no grid at all, where the same
        request without SortType returns results.

    So the pair is not merely unreliable, it can be empty-making. That rules out
    "vary the sort and try again" as a retry mitigation: on the second failure it
    manufactures the very "no listings found" it was meant to work around.

    Rather than assert an ordering the server did not apply, sort the parsed rows
    ourselves. Unpriced rows sink to the end instead of being dropped or sorting
    as zero.

    Note the descending case negates the price rather than passing reverse=True:
    reversing would flip the "unpriced" flag too and float those rows to the very
    top of a most-expensive-first list, which is the one place they are most
    likely to be read as the answer.
    """
    if sort_by == "price_asc":
        key = lambda p: (p.get("price") is None, p.get("price") or 0)      # noqa: E731
    elif sort_by == "price_desc":
        key = lambda p: (p.get("price") is None, -(p.get("price") or 0))   # noqa: E731
    else:
        return products
    return sorted(products, key=key)


def _sponsored_note(products: list[dict]) -> Optional[str]:
    """
    Qualify the header's sort claim with how many of the shown rows were paid for.

    The header says things like "(sort: orders)", which a reader takes to mean every
    row earned its position. On live pages the sponsored share ran from 0/60 to
    56/60, so on some searches that reading is almost entirely wrong and on others
    it is exactly right — which is why this is counted per search rather than left
    to a static caveat in the docstring. Rows are neither dropped nor reordered:
    the product may still be the one the caller wants.
    """
    shown = [p for p in products if p.get("sponsored") is not None]
    ads = [p for p in shown if p.get("sponsored")]
    if not ads:
        return None
    if len(ads) == len(shown):
        head = "Every row below is a sponsored placement"
    elif len(ads) == 1:
        head = f"1 of {len(shown)} rows below is a sponsored placement"
    else:
        head = f"{len(ads)} of {len(shown)} rows below are sponsored placements"
    return (f"⚑ {head} (AliExpress labels these \"Ad\"). Their position was paid for, "
            "not earned by the sort — they are kept in place and marked · sponsored.")


def _format_product_lines(products: list[dict], header: str, limit: int = 25) -> str:
    """Render parsed product dicts into the compact text shared by search + deals."""
    lines = [header]
    ad_note = _sponsored_note(products[:limit])
    if ad_note:
        lines.append(ad_note)
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
        # Bought position, not earned rank. One word rather than AliExpress's own
        # "Ad" badge text: this output is read by an agent with no visual card to
        # anchor it, and beside "Choice" a bare "Ad" is easy to skim as another
        # programme name. The per-search count sits under the header.
        if p.get("sponsored"):
            line += " · sponsored"
        # The price above belongs to ONE configuration — the SKU the card names in
        # `prices.skuId` — and the field it comes from is called `minPrice` without
        # being one: on item 1005007010293617 the card quoted 190.39 while the
        # listing spans 48.10–190.39, i.e. the DEAREST config. Reported as a
        # cross-tool contradiction (search said 50.26, get_product_details said
        # 36.57–85.58); both were right. Marking the count is what makes the single
        # figure readable as a sample rather than the listing's price. The legend
        # for it lives in the tool docstring — it is identical on every row.
        vc = p.get("variant_count")
        if isinstance(vc, int) and vc > 1:
            line += f" · {vc} variants"
        # "listed 2.8y ago", never "2.8y old" — the latter was read as the SELLER's
        # age in a review of this tool, and the search payload carries no seller
        # age at all, so that reading is always wrong.
        if p.get("listing_age"):
            line += f" · listed {p['listing_age']} ago"
        # The only stock figure a search card ever carries (see `_search_signals`).
        # It rides on a promo badge, so its absence says nothing about stock —
        # which is why there is no "in stock" counterpart here.
        if isinstance(p.get("stock_left"), int):
            line += f" · ⚠ only {p['stock_left']} left"
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

    gaps = _degraded_source_note(products[:limit])
    if gaps:
        lines.append(gaps)
    return "\n".join(lines)


def _degraded_source_note(products: list[dict]) -> Optional[str]:
    """
    Name the fields a fallback parser could not supply, when one was used.

    Printed only when it applies, which is close to never: the SSR parser handles
    every live page seen so far and the fallbacks exist for the day it stops. That
    is exactly when the caller most needs telling, because rows missing a warehouse
    and an age otherwise look like listings that simply have neither.
    """
    used = {p.get("data_source") for p in products}
    degraded = sorted(s for s in used if s in SEARCH_SOURCE_GAPS)
    if not degraded:
        return None
    missing = sorted({f for s in degraded for f in SEARCH_SOURCE_GAPS[s]})
    scope = "Some rows" if len(used) > len(degraded) else "These rows"
    return (f"⚠ {scope} came from a fallback parser because AliExpress's usual search "
            f"payload was missing. Not reported for them: {', '.join(missing)} — "
            "unavailable here, not absent from the listing.")


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


# ─── Shipping states ──────────────────────────────────────────────────────────
#
# Three outcomes, and the whole point of the split is that the third exists.
#
#   SHIP_OK           AliExpress returned a delivery option for the configured
#                     country. Freight may still be unpriced; reachability and
#                     cost are separate questions.
#   SHIP_UNREACHABLE  AliExpress said, explicitly, that there is no option.
#   SHIP_UNKNOWN      we could not determine it. Carries a reason.
#
# It used to be two states inferred from one field, and the missing third was
# reported as the second: `bizData.unreachable` set "does not ship to SE", and a
# response with no SHIPPING block set nothing, which the renderer's else-branch
# turned into "AliExpress needs a saved delivery address" — a confident diagnosis
# of a configuration problem, produced by a response that had told us nothing.
# Five consecutive lookups across five sellers were reported as unshippable to SE;
# the same items quoted real freight and delivery windows minutes later.
#
# Two facts from 30 captured live responses (Aug 2026) shape this. First, the
# string "unreachable" appears in NONE of them — every healthy response carries
# shipFrom/shippingFee/shipToCode and simply omits the key — so the confirmed-
# negative branch is close to unexercised, which is exactly why a missing block
# must never be routed into it. Second, `bizData.shipToCode` is present on all 35
# delivery layouts and reads "SE" on every one, so the destination AliExpress
# actually quoted is checkable rather than assumed. That check is what catches a
# recurrence of the `_lang` bug (MTOP resolves the destination from `_lang`, and
# quoting for the wrong country is what made reachable items look unreachable)
# no matter which cause brings it back.
SHIP_OK = "ships"
SHIP_UNREACHABLE = "unreachable"
SHIP_UNKNOWN = "unknown"

# Why a shipping answer is unknown. Phrased as sentence fragments because they are
# printed to the caller, who otherwise cannot tell a blocked response from a
# listing that genuinely has no courier.
SHIP_UNKNOWN_REASONS = {
    "no_data": "AliExpress returned no product data at all (a degraded or blocked response)",
    "no_block": "the response carried no shipping section",
    "no_layouts": "the response carried an empty shipping section",
    "no_option": "the shipping section carried no delivery option",
    "wrong_destination": "AliExpress quoted delivery to {quoted}, not {country}",
}


def _shipping_unknown(d: dict, reason: str, quoted: Optional[str] = None) -> None:
    """Record that shipping could not be determined, and why."""
    d["ship_status"] = SHIP_UNKNOWN
    d["ship_status_reason"] = reason
    d["ship_status_detail"] = SHIP_UNKNOWN_REASONS.get(reason, reason).format(
        quoted=quoted or "somewhere else", country=COUNTRY)


def shipping_line(d: dict) -> str:
    """
    The one-line shipping verdict for a PDP dict, in the caller's words.

    Lives here rather than in the renderer because the wording is load-bearing: an
    unknown must not be able to read as a negative, so the sentence that says we
    do not know is written next to the code that decides we do not know.
    """
    status = d.get("ship_status")
    cost = d.get("shipping_cost")
    if status == SHIP_UNREACHABLE:
        return (f"Shipping: does not ship to {COUNTRY} — AliExpress returned no "
                "delivery option for this destination.")
    if status == SHIP_UNKNOWN:
        detail = d.get("ship_status_detail") or "the response did not say"
        if d.get("ship_status_reason") == "wrong_destination":
            # Not a retryable blip: the session is resolving to another country, so
            # every freight figure on the page is for that country until it is fixed.
            return (f"Shipping: UNKNOWN — {detail}, so its freight and delivery "
                    f"estimates were discarded rather than reported as {COUNTRY} "
                    "figures. Check the delivery address saved on the site.")
        return (f"Shipping: UNKNOWN — {detail}. This is not a report that the item "
                f"cannot ship to {COUNTRY}; retry, and if it persists check that the "
                "session still has a delivery address saved.")
    if cost is not None:
        return "Shipping: " + ("Free" if cost == 0 else _fmt_money(cost, d.get("currency")))
    if status == SHIP_OK:
        return (f"Shipping: ships to {COUNTRY}, but AliExpress quoted no freight price "
                "(a delivery estimate may still appear below).")
    return (f"Shipping: UNKNOWN — no shipping data was returned. This is not a report "
            f"that the item cannot ship to {COUNTRY}.")


# ─── Placeholder prices ───────────────────────────────────────────────────────
#
# Sellers mark a config unbuyable by pricing it absurdly rather than by removing
# it, and a single such row poisons the whole listing's price range. Item
# 1005007791813945 (KF301 terminal blocks, 35 configs) carries three out-of-stock
# rows at 1,809,373.19 SEK — the converted form of a round six-figure placeholder
# — and rendered as "Price: 11.44 SEK–1809373.19 SEK". The real spread is
# 11.44–257.51.
#
# The hard part is that wide spreads are often REAL: one item_id routinely covers
# a single component and a bulk reel. So the threshold was calibrated against live
# listings (Aug 2026) rather than guessed, measuring each listing's dearest config
# as a multiple of the median of its CHEAPER HALF. That anchor is used instead of
# the plain median because the plain median is itself contaminated once placeholder
# rows are numerous — on a two-config listing [11.44, 1809373.19] the median sits
# at 904,692 and the glitch measures 2.0x its own average, invisible.
#
#   LEGITIMATE, widest first
#     1005002565791543  LED strip 5m–100m, 240 configs   37.70–22,503.43 SEK   124x
#     1005001677403255  LED strip + controller, 75        134.30–4,590.10        34x
#     1005006989290299  LED strip 1m–100m, 40             142.02–3,772.48         8x
#     1005008406340177  lever connectors 10–75pc, 5        21.15–111.81           5x
#     1005007301884080  screw assortment 50–1000pc, 224    63.66–516.27           4x
#     1005003766577753  screw assortment 500–1000pc, 58   276.70–880.32           3x
#     1005010037316351  waterproof boxes, 19               33.12–105.09           3x
#     1005008819293735  USB-C cable 1m–3m, 6               16.77–37.28            2x
#
#   GLITCH
#     1005007791813945  terminal blocks, 35            11.44–1,809,373.19     69,484x
#
# 1000x sits 8x above the widest genuine span found and 69x below the glitch — two
# orders of magnitude of clearance on the side that matters. A 100x rule, the
# obvious first guess, would have thrown away the LED strip listing's real top end.
#
# Suppression applies ONLY to the range. The row stays in the variants table,
# flagged: the price is what AliExpress reports, and a caller comparing against the
# site should see the same number we did.

PRICE_GLITCH_RATIO = 1000


def _price_glitch_cutoff(prices: list) -> Optional[float]:
    """
    Price above which a config is treated as a placeholder, or None if undecidable.

    Anchored on the median of the cheaper half so the measure survives a listing
    whose placeholders outnumber its real configs.
    """
    usable = sorted(p for p in prices if isinstance(p, (int, float)) and p > 0)
    if len(usable) < 2:
        # One price is a point, not a range, and nothing to compare it against.
        return None
    lower = usable[:max(1, len(usable) // 2)]
    mid = len(lower) // 2
    anchor = lower[mid] if len(lower) % 2 else (lower[mid - 1] + lower[mid]) / 2
    if anchor <= 0:
        return None
    return anchor * PRICE_GLITCH_RATIO


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
        "price_suspect_count": None,
        "price_suspect_max": None,
        "lot_note": None,
        "rating": None,
        "review_count": None,
        "sold_count": None,
        "sold_count_num": None,
        "seller_name": None,
        "store_url": None,
        "seller_positive_rate": None,
        "seller_total_reviews": None,
        "seller_opened": None,
        "seller_opened_years": None,
        "seller_aggregated": None,
        "seller_listed_name": None,
        "shipping_cost": None,
        "shipping_free": None,
        "free_shipping_over": None,
        "shipping_alternatives": [],
        "shipping_estimate": None,
        "ship_from": None,
        "ship_from_code": None,
        "tax_note": None,
        # True ONLY when AliExpress itself said the destination is unreachable.
        # Never set from an absent or empty response — see the shipping states above.
        "ship_unreachable": None,
        "ship_status": None,
        "ship_status_reason": None,
        "ship_status_detail": None,
        "ship_to_code": None,
        "ship_days_min": None,
        "ship_days_max": None,
    }

    result = mtop_resp.get("data", {}).get("result", {})
    if not isinstance(result, dict) or not result:
        # An empty `result` is the shape an anti-bot soft-block leaves behind
        # (FAIL_SYS_USER_VALIDATE answers with the envelope and nothing in it).
        # It is a dict, so an isinstance check alone waved it through to be
        # reported as "no shipping section" — a statement about the listing, when
        # the truth is that we never got a listing.
        _shipping_unknown(d, "no_data")
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
                # Placeholder configs are excluded from the range but counted, so
                # the caller learns the listing has unbuyable rows rather than
                # seeing them vanish. See `_price_glitch_cutoff`.
                cutoff = _price_glitch_cutoff(prices)
                if cutoff is not None:
                    suspect = [p for p in prices if p > cutoff]
                    if suspect:
                        d["price_suspect_count"] = len(suspect)
                        d["price_suspect_max"] = max(suspect)
                        prices = [p for p in prices if p <= cutoff]
                        by_price = [t for t in by_price if t[0] <= cutoff]
                lo, hi = min(prices), max(prices)
                if lo != hi:
                    d["price_range"] = (lo, hi)
                    # Name the configuration at each end. The headline "from"
                    # price is routinely a stripped or non-functional SKU ("No Ram
                    # No Storage"), and the top end is the dearest config that
                    # survived the placeholder filter — a bare "747.41–1221432.03
                    # SEK" told the caller neither.
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
    # Routed through `_extract_seller` on the FULL result rather than reading
    # SHOP_CARD_PC directly, so this shares the aggregation check documented at
    # that function — the two tools disagreeing about who sells an item is the
    # bug that made this one call instead of two parses.
    # Unconditional: a page can carry the supplier disclosure without a usable shop
    # card, and that is exactly the case where the disclosure is the only thing that
    # knows who the seller is.
    s = _extract_seller(result)
    d["seller_name"] = s["store_name"]
    d["store_url"] = s["store_url"]
    d["seller_aggregated"] = s["aggregated"]
    d["seller_listed_name"] = s["listed_store_name"]
    d["seller_opened"] = s["opened"]
    d["seller_opened_years"] = s["opened_years"]
    if s["positive_rate"]:
        try:
            d["seller_positive_rate"] = float(s["positive_rate"])
        except (TypeError, ValueError):
            pass
    if s["total_reviews"] is not None:
        try:
            d["seller_total_reviews"] = int(s["total_reviews"])
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
    if not isinstance(ship, dict):
        _shipping_unknown(d, "no_block")
    else:
        # bizData lives inside originalLayoutResultList[0] or deliveryLayoutInfo[0]
        layouts = ship.get("originalLayoutResultList") or ship.get("deliveryLayoutInfo") or []
        biz = None
        if isinstance(layouts, list) and layouts:
            l0 = layouts[0]
            if isinstance(l0, dict):
                biz = l0.get("bizData")
        if not isinstance(layouts, list) or not layouts:
            _shipping_unknown(d, "no_layouts")
        elif not isinstance(biz, dict):
            _shipping_unknown(d, "no_option")
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

            quoted_to = biz.get("shipToCode")
            d["ship_to_code"] = str(quoted_to).strip().upper() if quoted_to else None
            if biz.get("unreachable"):
                d["ship_status"] = SHIP_UNREACHABLE
                d["ship_unreachable"] = True
            elif d["ship_to_code"] and d["ship_to_code"] != COUNTRY.upper():
                # The freight, the ETA and the courier all belong to whatever
                # destination MTOP resolved. Reporting them as the configured
                # country's is how the `_lang` bug produced confident wrong answers,
                # so a mismatch invalidates the quote rather than annotating it.
                _shipping_unknown(d, "wrong_destination", d["ship_to_code"])
                d["shipping_cost"] = None
                d["shipping_free"] = None
                d["shipping_estimate"] = None
                d["free_shipping_over"] = None
            elif (d["shipping_cost"] is not None or d["shipping_free"]
                  or d["shipping_estimate"] or biz.get("deliveryDayMin") is not None):
                d["ship_status"] = SHIP_OK
            else:
                _shipping_unknown(d, "no_option")
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

    # Every number in this block describes one destination. If that destination was
    # not ours, none of them may survive — a leftover "5–10 days" under an UNKNOWN
    # verdict reads as the answer and would be a quote for the wrong country.
    if d["ship_status_reason"] == "wrong_destination":
        d["ship_days_min"] = d["ship_days_max"] = None
        d["shipping_alternatives"] = []

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
        # Was a third hardcoded "en-CA" — the reviews endpoint is unsigned and
        # on a different host, so it was missed when the other two were fixed.
        "Accept-Language": ACCEPT_LANGUAGE,
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
    with httpx.Client(timeout=30.0, follow_redirects=True, http2=HTTP2) as c:
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
#
# SHOP_CARD_PC IS NOT ALWAYS THE SELLER. AliExpress runs "aggregation" listings —
# one item_id whose reviews and sales volume are pooled across several overseas
# merchants — and on those the shop card names a shell store that never sees the
# order. Reading it as the seller is how this tool told a user six unrelated
# products all came from "Stone's Store — 100.0% positive (10 feedbacks), opened
# Mar 1, 2024" while their own cart listed six different merchants. The cart was
# right. Money was spent on the strength of the wrong answer.
#
# The page carries the truth in its EU trader-identification block. Live Aug 2026,
# four affected items, every one with SHOP_CARD_PC = Stone's Store / storeNum
# 1103573332 / sellerId 2678280160 — the same shell on terminal blocks, lever
# connectors, drill bits and waterproof boxes:
#
#   item 1005007791813945 → Luyanmaoyi Store                       (1102764714)
#   item 1005006784660115 → DeFeng Tools Store                     (1102575030)
#   item 1005008406340177 → Electrical Hardware Tools Store        (1105626261)
#   item 1005010037316351 → Wenzhou Xiangheng Electric Technology  (1104022056)
#
# each named in COMPLIANCE_PC.complianceList under the title "Explanation of the
# Supplier": "This special page helps aggregate consumer reviews and sales volume
# of similar items offered by multiple overseas merchants… The seller of this item
# is <a href=…showcredential.htm?storeNum=1102764714…>Luyanmaoyi Store</a>". All
# four names matched what view_cart reported for the same item_ids.
#
# Note the same block ALSO carries a second, contradictory sentence — "Sold by
# Stone's Store. Logistics by AliExpress." — so the boilerplate cannot be trusted
# by prose matching. The credential ANCHOR is what we key on: it is the regulated
# trader disclosure, it carries a store id rather than a name, and it does not
# move with `_lang`.
#
# Detector: a `showcredential.htm?...storeNum=N` link INSIDE COMPLIANCE_PC whose N
# differs from the shop card's. Scoping to COMPLIANCE_PC is load-bearing — the same
# URL appears elsewhere on ordinary pages carrying the shop card's own id. Across
# the 12 live PDPs dumped Aug 2026 it separated the two populations cleanly: 8
# ordinary listings had no credential link in COMPLIANCE_PC at all, and the 4
# aggregation pages had exactly one, always a different store.

_CREDENTIAL_STORE_RE = re.compile(r"showcredential\.htm\?[^\"'<>\s]*?storeNum=(\d+)")
_CREDENTIAL_ANCHOR_RE = re.compile(
    r"<a\b[^>]*showcredential\.htm\?[^\"'<>\s]*?storeNum=(\d+)[^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)


def _supplier_disclosure(result: dict) -> Optional[dict]:
    """
    Read the EU trader-identification block: {"store_name", "store_id"} or None.

    Returns the merchant AliExpress legally names as the seller of this item,
    which on an aggregation listing is NOT the store in SHOP_CARD_PC.
    """
    if not isinstance(result, dict):
        return None
    block = result.get("COMPLIANCE_PC")
    if not isinstance(block, dict):
        return None
    entries = block.get("complianceList")
    if not isinstance(entries, list):
        return None
    for e in entries:
        if not isinstance(e, dict):
            continue
        content = e.get("content")
        if not isinstance(content, str) or "showcredential.htm" not in content:
            continue
        m = _CREDENTIAL_ANCHOR_RE.search(content)
        if m:
            name = _strip_html(m.group(2))
            if name:
                return {"store_name": name, "store_id": int(m.group(1))}
        # The id alone is still worth having: it proves the page is aggregated,
        # which is the part that must not be swallowed. Better a store we can
        # only number than a store name we know to be wrong.
        m = _CREDENTIAL_STORE_RE.search(content)
        if m:
            return {"store_name": None, "store_id": int(m.group(1))}
    return None


def _extract_seller(shop: dict) -> dict:
    """
    Pull store/seller info from a PDP response (live Aug 2026).

    Accepts EITHER the full `data.result` dict or a bare `SHOP_CARD_PC` block. Pass
    the full result: it is the only form that can consult the supplier disclosure
    above, and a bare shop card silently loses the aggregation check — the exact
    failure this function exists to prevent. Handed one anyway, the result carries
    `disclosure_checked: False` and NO claim that the store is the seller.
    """
    d: dict[str, Any] = {
        "store_name": None, "positive_rate": None, "positive_num": None,
        "total_reviews": None, "score": None, "level": None, "opened": None,
        "opened_years": None, "country": None, "top_rated": None,
        "local_seller": None, "store_url": None, "store_id": None,
        # Aggregation state. `aggregated` is True only when AliExpress itself
        # disclosed a different merchant; None means we were never in a position
        # to look, which is not the same as "no".
        "aggregated": None,
        "disclosure_checked": False,
        "listed_store_name": None,
        "listed_store_id": None,
        # False whenever the feedback figures below describe some store other than
        # the one named in `store_name`. Renderers must not print stats when this
        # is False — see the numbers in the block comment above: a shell store's
        # "100.0% positive (10 feedbacks)" reads as a glowing seller.
        "stats_describe_seller": True,
    }
    if not isinstance(shop, dict):
        return d

    # Sniff which of the two shapes we were handed. The key sets are disjoint: a
    # PDP result is keyed by uppercase component names, a shop card by camelCase
    # fields. Checking for the component rather than for `storeName` means an
    # aggregation page with a malformed shop card is still recognised as a result.
    result: dict = {}
    if "SHOP_CARD_PC" in shop or "GLOBAL_DATA" in shop or "COMPLIANCE_PC" in shop:
        result = shop
        shop = result.get("SHOP_CARD_PC") if isinstance(result.get("SHOP_CARD_PC"), dict) else {}

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

    if not result:
        # A bare shop card. We cannot rule out an aggregation page, so we do not
        # get to say it is not one.
        return d

    d["disclosure_checked"] = True
    d["listed_store_name"] = d["store_name"]
    d["listed_store_id"] = d["store_id"]
    disc = _supplier_disclosure(result)
    d["aggregated"] = bool(
        disc and (
            (disc["store_id"] is not None and d["store_id"] is not None
             and int(disc["store_id"]) != int(d["store_id"]))
            or (disc["store_id"] is not None and d["store_id"] is None)
        )
    )
    if not d["aggregated"]:
        return d

    # From here the shop card described the aggregation shell, so every figure it
    # supplied is about the wrong store. Drop them all rather than re-attributing
    # them: the shell's 100.0% / 10 feedbacks / "Mar 1, 2024" is a profile a
    # shopper would read as "new but flawless", and it belongs to nobody they are
    # buying from. We know the merchant's NAME and ID and nothing else about them,
    # so that is all we say. Fetching the real store's feedback would need a second
    # endpoint; until that exists, silence is the honest answer.
    d["store_name"] = disc["store_name"]
    d["store_id"] = disc["store_id"]
    d["store_url"] = f"{BASE_URL}/store/{disc['store_id']}" if disc["store_id"] else None
    d["stats_describe_seller"] = False
    for k in ("positive_rate", "positive_num", "total_reviews", "score", "level",
              "opened", "opened_years", "country", "top_rated", "local_seller"):
        d[k] = None
    return d


def seller_detail_lines(d: dict) -> list[str]:
    """
    Render the `Seller:` block of `get_product_details` from `_extract_pdp_fields`.

    Carries the opening date, which used to be reachable only via `get_seller` —
    a second live call per item, on the tool a research session runs most. The
    date is the one figure here that does not move: a store opened last month with
    a 100% rate has ten reviews behind it, and that is worth a line.
    """
    if d.get("seller_aggregated"):
        listed = d.get("seller_listed_name") or "another store"
        return [
            f"Seller: {d.get('seller_name') or 'not disclosed'}",
            f"  ⚠ Aggregation listing — the product page advertises {listed}, which "
            "is not the seller. No feedback rate or store age is available for the "
            "actual merchant.",
        ]
    if not d.get("seller_name"):
        return []
    line = f"Seller: {d['seller_name']}"
    if d.get("seller_positive_rate"):
        line += f" — {d['seller_positive_rate']}% positive feedback"
    if d.get("seller_total_reviews"):
        line += f" ({d['seller_total_reviews']} seller feedbacks)"
    if d.get("seller_opened"):
        age = f", {d['seller_opened_years']} yr" if d.get("seller_opened_years") else ""
        line += f", opened {d['seller_opened']}{age}"
    return [line]


def seller_report(d: dict, item_id: str) -> str:
    """
    Render `_extract_seller` output as the `get_seller` body.

    Lives here rather than in the tool so the aggregation warning cannot be
    rendered without the parse that produces it, and so both are testable offline.
    """
    if d.get("aggregated"):
        # Loud on purpose. The quiet version of this — a bare store name with no
        # numbers — reads as a thin profile rather than as a different store from
        # the one the page advertises, and that ambiguity is what cost money.
        named = d.get("store_name") or (
            f"store {d['store_id']}" if d.get("store_id") else None
        )
        lines = [
            f"Seller for item {item_id}:",
            f"Store: {named or 'not disclosed'}",
            "",
            "⚠ This is an AGGREGATION listing: one item_id pooling reviews and sales "
            "volume across several merchants. The store on the product page "
            f"({d.get('listed_store_name') or 'unnamed'}) is not the seller — "
            f"AliExpress's own supplier disclosure names {named or 'a different store'}.",
            "No feedback rate, feedback volume or opening date is reported: the "
            "figures on the page belong to the pooling store, not to this merchant, "
            "and there is no second source for the merchant's own.",
        ]
        if d.get("store_url"):
            lines.append(f"Store page: {d['store_url']}")
        return "\n".join(lines)
    if not d.get("store_name"):
        return f"No seller info found for item {item_id}."

    lines = [f"Seller for item {item_id}:", f"Store: {d['store_name']}"]
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
    if not d.get("disclosure_checked"):
        lines.append("⚠ Aggregation not checked — this store may not be the seller.")
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
            "is_default": False,
            "default_sku_id": None,
            # Set below, once every config's price is known.
            "price_suspect": False,
        })

    # Flag placeholder prices without removing them — the row is real data and the
    # number is what AliExpress served; it just must not set the listing's range.
    # See `_price_glitch_cutoff` for the threshold and the live spans behind it.
    cutoff = _price_glitch_cutoff([v["price"] for v in variants])
    if cutoff is not None:
        for v in variants:
            if v["price"] is not None and v["price"] > cutoff:
                v["price_suspect"] = True

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
    rows = [merged[k] for k in order]

    # Mark the config `add_to_cart` buys when it is given no sku_id. Read from the
    # same expression cart.py's `_resolve_sku_for_cart` uses, so the marker cannot
    # drift from the behaviour it describes.
    #
    # Worth marking because the default is not the neutral choice it reads as. On
    # 20 multi-config listings (Aug 2026) it was the cheapest row on 17 — which is
    # how three wrong-variant adds happened in one session, the caller assuming the
    # default was the config they had been discussing. The other 3 are the reason
    # "cheapest" can't just be assumed either: item 1005007010293617 defaults to the
    # DEAREST of its 17 price levels (190.39) and 1005007129679040 to #15 of 16.
    #
    # The default may also be one of several SKUs collapsed into one row, in which
    # case the row's own sku_id is NOT the one that would be bought — so the exact
    # id is carried separately. That did not occur in the 20 sampled listings, but
    # it is reachable: collapsed rows exist (item 1005011654394254 has them) and
    # nothing makes AliExpress prefer the survivor as its default.
    default_id = sku.get("selectedSkuIdStr") or sku.get("selectedSkuId")
    if default_id is not None:
        default_id = str(default_id)
        for v in rows:
            covered = [s for s, _ok, _st in (v.get("covered_skus") or [])]
            if v["sku_id"] == default_id or default_id in covered:
                v["is_default"] = True
                v["default_sku_id"] = default_id
                break
    return rows
