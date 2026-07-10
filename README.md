# AliExpress MCP Server

An MCP server that wraps AliExpress search and product detail scraping for a Canadian shopper — prices in CAD, shipping to Vancouver, BC.

Read-only by design: it searches, fetches product details, and checks shipping. It does **not** add to cart, check out, or pay.

## Tools

- `search_products(query, min_rating, max_price, sort_by)` — searches the public wholesale search URL and parses the embedded JSON.
- `find_deals(query, min_discount, max_price, min_rating, sort_by)` — same search, filtered to discounted listings and sorted by discount depth.
- `get_product_details(item_id | url)` — title, price (a range for multi-config listings), discount, rating, sold count, seller, shipping cost & ETA.
- `get_variants(item_id | url)` — the full per-configuration (SKU) price table, e.g. "DDR4 32GB 1TB SSD · R7 5825U" → 916.63; the price→spec map a bare range can't give. Out-of-stock configs flagged.
- `get_shipping_estimate(item_id | url)` — shipping cost + ETA to the configured country (freight is computed against your saved delivery address; may report "unreachable" without one).
- `get_reviews(item_id | url, max_reviews, filter_by)` — rating breakdown (positive / neutral / negative) + individual reviews via the unsigned `feedback.aliexpress.com/pc/searchEvaluation.do` endpoint.
- `get_seller(item_id | url)` — store rating, positive-feedback rate, seller level, age, and store link (read from the PDP `SHOP_CARD_PC` block).
- `compare_sellers(title | item_id | url, max_candidates)` — when several storefronts sell the same item (originals, relisters, dropshippers), searches the title, inspects the top hits' sellers, and ranks them **most-established first** (store age → feedback volume → positive rate) so you can prefer the long-running seller over a brand-new relister. Costs one search + one lookup per candidate.
- `view_cart()` — current cart contents via the signed `mtop.aliexpress.trade.cart.render` endpoint, grouped by seller, with a computed subtotal over the shown items. Read-only. AliExpress paginates the cart and the API exposes only the first page, so a large cart is shown as "N of M items" (see Known limitations).
- `list_orders(max_orders)` / `get_order(order_id)` — your recent orders and per-order status + line items via `mtop.aliexpress.trade.buyer.order.list`. Prices are normalized to `<amount> <ISO code>` (each order keeps the currency it was paid in). Read-only (never pays/cancels). **Needs a full login session** — see the note under Known limitations.
- `get_wishlist(max_items)` — your saved / liked items via `mtop.ae.wishlist.allItems.render`, flagging items that **dropped in price** since you saved them (normalized to `<amount> <CUR> off`) and sold-out ones, with each item's saved-on date. Read-only. **Needs a full login session** (same as orders). Like the cart, the API only returns the first page (~16 items), so a larger wishlist is shown as "N of M".

All `item_id | url` tools also accept short **`a.aliexpress.com/_xxx`** share links (the mobile share-sheet format) — they're resolved by following the redirect. Steep discounts (≥ 60%) are tagged **⚠ MSRP?** since AliExpress's strike-through "was" price is frequently fabricated.

## How it works

The initial product page is a client-side-rendered shell (`window._d_c_.isCSR = true`, `window.runParams = {}`) — real product data loads over AJAX from AliExpress's MTOP API after the JS runs. So HTML scraping doesn't work for PDP data.

Instead, this server replicates what `mtop.js` does in the browser: it makes signed calls to `mtop.aliexpress.pdp.pc.query` using the session's `_m_h5_tk` cookie as the HMAC token. The signing algorithm:

```
sign = md5(token + "&" + t_ms + "&" + "12574478" + "&" + json_payload)
```

where `token` is the prefix of `_m_h5_tk` before the underscore, and `12574478` is AliExpress's public web `appKey`. Token refresh on `FAIL_SYS_TOKEN_EXPIRED` is handled automatically.

Search uses the SSR path — no signed calls needed. Product data is parsed from the search page's embedded `window._dida_config_._init_data_` payload (items at `…root.fields.mods.itemList.content`, each with a structured `prices.salePrice.minPrice` + `currencyCode`).

**Currency is self-describing, not uniform.** AliExpress renders each surface in a different currency and this is not something the server can force to agree: search and PDP come back in your **account** site currency (e.g. UAH for a UA account), the **cart** renders in each item's listing currency (often USD), **orders** are shown in whatever currency each order was actually paid in (so a single response can legitimately mix UAH and USD), and the **wishlist** honours the `ALIEXPRESS_CURRENCY` you request. Rather than fight that, every amount the server prints carries its own ISO code read from that response (`212.90 USD`, `808.96 UAH`, `82.44 CAD`), so the caller can always tell what a number means and convert if needed. If you want fewer currencies in play, set `ALIEXPRESS_CURRENCY` to match your account and/or change your AliExpress site currency, then re-save cookies.

Token refresh is handled automatically: a stale `_m_h5_tk` makes MTOP return `FAIL_SYS_TOKEN_EXOIRED` (yes, AliExpress misspells "EXPIRED") together with a fresh token cookie; the client detects any `FAIL_SYS_TOKEN*` and retries once with the new token.

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Install the [MCP Auth Bridge](https://github.com/justinritchie/mcp-auth-bridge) Chrome extension. The `aliexpress` entry is already in its `sites.json`.

3. Open `https://www.aliexpress.com`, log in, then click **Save AliExpress** in the extension popup. This writes cookies to `~/.mcp-credentials/aliexpress.json`.

4. Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "aliexpress": {
         "command": "python3",
         "args": ["/Users/YOUR_USER/justinritchie-mcp-servers/aliexpress-mcp-server/aliexpress_mcp_server.py"],
         "env": {
           "ALIEXPRESS_CREDENTIALS": "~/.mcp-credentials/aliexpress.json",
           "ALIEXPRESS_COUNTRY": "CA",
           "ALIEXPRESS_CURRENCY": "CAD"
         }
       }
     }
   }
   ```

5. Restart Claude Desktop.

### Auth without the extension (manual cookie paste)

You don't need the Chrome extension — the server reads whatever cookies live in the
credential file. To populate it by hand:

1. Log into `https://www.aliexpress.com` in Chrome.
2. Open DevTools (⌥⌘I) → **Console**, paste this, and press Enter. It copies a ready-made
   `{"cookies": {…}}` blob to your clipboard:

   ```js
   copy(JSON.stringify({cookies:Object.fromEntries(document.cookie.split('; ').map(c=>{const i=c.indexOf('=');return[c.slice(0,i),c.slice(i+1)]}))},null,2))
   ```

3. Paste it over the contents of `~/.mcp-credentials/aliexpress.json` and save.

This captures `_m_h5_tk` (the MTOP signing token — the important one) plus the JS-readable
session cookies. If `view_cart` still reports empty/expired afterward, a few auth cookies are
`HttpOnly` (invisible to `document.cookie`) — copy those from DevTools → **Application →
Cookies → aliexpress.com** (the rows with a ✓ in the HttpOnly column) into the JSON by hand.
Re-run the snippet whenever tools start returning "session expired" — the token rotates.

## Known limitations

- **Low-volume / brand-new listings** sometimes return empty MTOP responses (endpoint returns `SUCCESS` but an empty data block). Likely a region/visibility gate. Search results still show the listing fine.
- **Cookies expire / lag the cart.** When tool calls start returning "session expired" — or `view_cart` reports an empty cart despite having items — re-open aliexpress.com in Chrome and click **Save AliExpress** again to capture fresh session cookies. The `_m_h5_tk` token and cart state rotate together; stale cookies show an empty server-side cart.
- **Rate limiting** is the user's responsibility. The server sends realistic Chrome headers but doesn't throttle; don't hammer the search.
- **Orders and the wishlist need a full login session.** `list_orders` / `get_order` / `get_wishlist` call APIs that require the HttpOnly login cookies the quick `document.cookie` snippet can't capture — otherwise they return a "full login session" message. Add the HttpOnly rows (DevTools → **Application → Cookies → aliexpress.com**, the ✓ HttpOnly ones) to `~/.mcp-credentials/aliexpress.json`.
- **The cart shows only its first page.** AliExpress paginates the cart with an opaque, gzip-compressed append cursor (`linkage.common.queryParams`); rather than reverse-engineer a brittle cursor, `view_cart` renders the first page and labels the result "showing N of M items" so nothing is silently dropped. The computed subtotal is over the shown items; the server's own "estimated total" reflects only the checkbox-*selected* lines and is labelled as such.
- **`get_product_details` and `get_variants` prices can differ by a hair.** They're two separate point-in-time MTOP calls reading the same SKU price map, so a price refresh (or a coupon applied to one path) between them can leave the ranges ~0.5% apart. `get_variants` is the more granular, authoritative per-SKU view.

## Architecture

Follows the pattern of [dekudeals-mcp-server](../dekudeals-mcp-server/) — FastMCP + httpx + BeautifulSoup — with added MTOP client code in `aliexpress_mcp_server.py`. Session cookies come from the shared credential file written by the [MCP Auth Bridge](https://github.com/justinritchie/mcp-auth-bridge) Chrome extension.

The MTOP response field paths (`PRODUCT_TITLE.text`, `PRICE.targetSkuPriceInfo.salePriceString`, `PC_RATING.rating`, `SHIPPING.originalLayoutResultList[0].bizData.displayAmount`, etc.) were reverse-engineered live from a real response in April 2026 and are documented inline in `_extract_pdp_fields()`. If AliExpress reorganizes the component layout, that function is the place to update.
