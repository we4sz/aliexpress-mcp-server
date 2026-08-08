# AliExpress MCP Server

An MCP server that wraps AliExpress search, product detail, cart, orders and wishlist. Destination country and currency are configurable via `ALIEXPRESS_COUNTRY` / `ALIEXPRESS_CURRENCY` (defaults `CA` / `CAD`); shipping, prices and delivery estimates all follow that setting.

Mostly read-only — it searches, fetches product details, checks shipping, and reads your cart/orders/wishlist — but four tools write to your account (`add_to_cart`, `set_cart_quantity`, `remove_from_cart`, `create_wishlist`); none of them ever checks out or pays.

## Tools

- `search_products(query, min_rating, max_price, sort_by)` — searches the public wholesale search URL and parses the embedded JSON.
- `find_deals(query, min_discount, max_price, min_rating, sort_by)` — same search, filtered to discounted listings and sorted by discount depth.
- `get_product_details(item_id | url)` — title, price (a range for multi-config listings), discount, rating, sold count, seller, shipping cost & ETA.
- `get_variants(item_id | url)` — the full per-configuration (SKU) price table, e.g. "DDR4 32GB 1TB SSD · R7 5825U" → 916.63; the price→spec map a bare range can't give. Out-of-stock configs flagged.
- `get_shipping_estimate(item_id | url)` — shipping cost + ETA to the configured country (freight is computed against your saved delivery address; may report "unreachable" without one).
- `get_reviews(item_id | url, max_reviews, filter_by)` — rating breakdown (positive / neutral / negative) + individual reviews via the unsigned `feedback.aliexpress.com/pc/searchEvaluation.do` endpoint.
- `get_seller(item_id | url)` — store rating, positive-feedback rate, seller level, age, and store link (read from the PDP `SHOP_CARD_PC` block).
- `compare_sellers(title | item_id | url, max_candidates)` — when several storefronts sell the same item (originals, relisters, dropshippers), searches the title, inspects the top hits' sellers, and ranks them **most-established first** (store age → feedback volume → positive rate) so you can prefer the long-running seller over a brand-new relister. Costs one search + one lookup per candidate.
- `add_to_cart(item_id | url, sku_id, quantity)` — **write.** Adds an item to your real cart via the signed `mtop.aliexpress.trade.cart.add` endpoint (which is signed with a *different* appKey than the read APIs — see `MTOP_CART_APP_KEY`). Buys nothing; the item waits in the cart until you check out on the site. `sku_id` defaults to the item's preselected variant, so pass one from `get_variants` when size/colour matters. To take something back out, use `remove_from_cart`.
- `set_cart_quantity(quantity, item_id | url | cart_id, sku_id)` — **write.** Sets an *absolute* quantity on one cart line via the same droplet endpoint, using `operationType = "update_quantity"` and replacing `fields.quantityView` with a bare `{"current": N}` — sending the full rendered `quantityView` back returns `SUCCESS` and silently does nothing, so the result is always re-read to confirm. Use `remove_from_cart` to delete a line; quantity 0 is rejected.
- `remove_from_cart(item_id | url | cart_id, sku_id)` — **destructive write.** Removes one cart line via the Ultron/droplet endpoint `mtop.aliexpress.trade.cart.async`: POST the operated component with `fields.operationType = "delete"` plus the page root (sending the whole component tree is rejected with `AE-CART-PARSE-PARAM-ERROR`). One product can occupy several cart lines (one per variant), so an ambiguous `item_id` lists the candidate `cart_id`s and removes nothing. Re-reads the cart afterwards to confirm.
- `view_cart()` — current cart contents via the signed `mtop.aliexpress.trade.cart.render` endpoint, grouped by seller, with a computed subtotal over the shown items. Read-only. AliExpress paginates the cart behind an opaque cursor, but `view_cart` walks it automatically (up to 10 pages), so most carts come back in full; only if paging fails partway or a cart exceeds that cap is it shown as "N of M items" (see Known limitations).
- `list_orders(max_orders)` / `get_order(order_id)` — your recent orders and per-order status + line items via `mtop.aliexpress.trade.buyer.order.list`. Prices are normalized to `<amount> <ISO code>` (each order keeps the currency it was paid in). Read-only (never pays/cancels). **Needs a full login session** — see the note under Known limitations.
- `get_wishlist(max_items)` — your saved / liked items via `mtop.ae.wishlist.allItems.render`, flagging items that **dropped in price** since you saved them (normalized to `<amount> <CUR> off`) and sold-out ones, with each item's saved-on date. Read-only. **Needs a full login session** (same as orders). Unlike the cart, the wishlist endpoint has no working pagination and locks its page size at ~16, so a larger wishlist is shown as "N of M".

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
- **Very large carts may still be shown partially.** AliExpress paginates the cart with an opaque, gzip-compressed append cursor (`linkage.common.queryParams`); `view_cart` walks it automatically via the Ultron/droplet endpoint (up to 10 pages), so most carts come back in full. If a page fetch fails or a cart exceeds that cap, the result is labelled "showing N of M items" so nothing is silently dropped. The computed subtotal is over the shown items; the server's own "estimated total" reflects only the checkbox-*selected* lines and is labelled as such.
- **`get_product_details` and `get_variants` prices can differ by a hair.** They're two separate point-in-time MTOP calls reading the same SKU price map, so a price refresh (or a coupon applied to one path) between them can leave the ranges ~0.5% apart. `get_variants` is the more granular, authoritative per-SKU view.

## Architecture

Follows the pattern of [dekudeals-mcp-server](../dekudeals-mcp-server/) — FastMCP + httpx + BeautifulSoup — with added MTOP client code in `aliexpress_mcp_server.py`. Session cookies come from the shared credential file written by the [MCP Auth Bridge](https://github.com/justinritchie/mcp-auth-bridge) Chrome extension.

The MTOP response field paths (`PRODUCT_TITLE.text`, `PRICE.targetSkuPriceInfo.salePriceString`, `PC_RATING.rating`, `SHIPPING.originalLayoutResultList[0].bizData.displayAmount`, etc.) were reverse-engineered live from a real response in April 2026 and are documented inline in `_extract_pdp_fields()`. If AliExpress reorganizes the component layout, that function is the place to update.
