# AliExpress MCP Server

An MCP server that wraps AliExpress search, product detail, cart, orders and wishlist. Destination country and currency are configurable via `ALIEXPRESS_COUNTRY` / `ALIEXPRESS_CURRENCY` (defaults `CA` / `CAD`); shipping, prices and delivery estimates all follow that setting.

Mostly read-only — it searches, fetches product details, checks shipping, and reads your cart/orders/wishlist — but nine tools write to your account (`add_to_cart`, `add_many_to_cart`, `set_cart_selection`, `set_cart_quantity`, `remove_from_cart`, `add_to_wishlist`, `remove_from_wishlist`, `create_wishlist`, `delete_wishlist`); none of them ever checks out or pays.

## Tools

- `search_products(query, min_rating, max_price, sort_by, ship_from, max_results)` — searches the public wholesale search URL and parses the embedded JSON. `max_price` is compared against the prices printed on the rows below — same number, same currency (your AliExpress site currency), no conversion. `ship_from` restricts results to a warehouse: a two-letter country code, a comma-separated string or list of codes, or `"EU"`/`"EEA"` for the whole customs union (expanded to its highest-stock members, probed via the first one). Empty = any warehouse. `max_results` caps how many rows print (default 25, capped at 60) — but when a thin keyword/warehouse combination makes AliExpress silently replace the query instead of returning few results, the row count collapses to 3 regardless of `max_results`, with a note saying so.
- `find_deals(query, min_discount, max_price, min_rating, sort_by, ship_from, max_results)` — same search, filtered to discounted listings and sorted by discount depth. `ship_from` and `max_results` behave exactly as in `search_products`.
- `get_product_details(item_id | url)` — title, price (a range for multi-config listings), discount, rating, sold count, seller, shipping cost & ETA.
- `get_variants(item_id | url)` — the full per-configuration (SKU) price table, e.g. "DDR4 32GB 1TB SSD · R7 5825U" → 916.63; the price→spec map a bare range can't give. Out-of-stock configs flagged.
- `get_shipping_estimate(item_id | url)` — shipping cost + ETA to the configured country, plus any paid express alternatives AliExpress offers alongside the default option (freight is computed against your saved delivery address; may report "unreachable" without one).
- `get_reviews(item_id | url, max_reviews, filter_by)` — rating breakdown (positive / neutral / negative) + individual reviews via the unsigned `feedback.aliexpress.com/pc/searchEvaluation.do` endpoint.
- `get_seller(item_id | url)` — positive-feedback rate, feedback volume, how long the store has been open, and where it ships from. Reads the PDP's `SHOP_CARD_PC` block **and** its EU trader-identification block, because on an "aggregation" listing the shop card names a shell store that never sees the order — see Known limitations. Deliberately omits AliExpress's own "seller level" and "seller score": it publishes no scale for either, so neither can be compared across stores.
- `compare_sellers(title | item_id | url, max_candidates)` — when several storefronts sell the same item (originals, relisters, dropshippers), searches the title, inspects the top hits' sellers, and ranks them **most-established first** (store age → feedback volume → positive rate) so you can prefer the long-running seller over a brand-new relister. Costs one search + one lookup per candidate.
- `add_to_cart(item_id | url, sku_id, quantity)` — **write.** Adds an item to your real cart via the signed `mtop.aliexpress.trade.cart.add` endpoint (which is signed with a *different* appKey than the read APIs — see `MTOP_CART_APP_KEY`). Buys nothing; the item waits in the cart until you check out on the site. `sku_id` defaults to the item's preselected variant, so pass one from `get_variants` when size/colour matters. To take something back out, use `remove_from_cart`.
- `add_many_to_cart(items)` — **write.** Adds a list of items in one call, paced between writes. Prefer it over looping `add_to_cart`: twenty-plus rapid single adds is precisely the pattern that trips the anti-bot check, and that block does not clear by waiting. If a challenge does land it **stops immediately** rather than spending the remaining items on a wall it cannot pass, and reports which items were added, which failed, and which were never attempted — so a retry neither duplicates nor drops anything. Each entry is `{"item_id" | "url", "sku_id"?, "quantity"?}`, or a bare item-id string.
- `set_cart_selection(selected, item_id | url | cart_id, sku_id, cart_ids, all_lines)` — **write.** Ticks or un-ticks cart lines for checkout — AliExpress orders ONLY the ticked lines. Un-ticking doesn't remove the line: it stays fully visible in `view_cart`, just excluded from what ships, and that is not recoverable after checkout. Targets one line (`item_id`/`url`/`cart_id`), several at once (`cart_ids=[...]`), or the whole cart (`all_lines=True`). **A single write is not reliable**: setting one line's checkbox has been observed flipping OTHER lines' checkboxes off server-side (reproduced against byte-identical browser payloads, so it isn't our request shape). The `cart_ids`/`all_lines` forms converge instead of firing once: they re-read the cart, re-write only what's still wrong, for a bounded number of rounds, then report what actually held against a fresh read rather than trusting AliExpress's ack. Prefer them over calling this once per line. Same ambiguous-`item_id` refusal as the other cart-line tools; `view_cart` flags un-ticked lines so this doesn't have to be run blind.
- `set_cart_quantity(quantity, item_id | url | cart_id, sku_id)` — **write.** Sets an *absolute* quantity on one cart line via the same droplet endpoint, using `operationType = "update_quantity"` and replacing `fields.quantityView` with a bare `{"current": N}` — sending the full rendered `quantityView` back returns `SUCCESS` and silently does nothing, so the result is always re-read to confirm. Use `remove_from_cart` to delete a line; quantity 0 is rejected.
- `remove_from_cart(item_id | url | cart_id, sku_id)` — **destructive write.** Removes one cart line via the Ultron/droplet endpoint `mtop.aliexpress.trade.cart.async`: POST the operated component with `fields.operationType = "delete"` plus the page root (sending the whole component tree is rejected with `AE-CART-PARSE-PARAM-ERROR`). One product can occupy several cart lines (one per variant), so an ambiguous `item_id` lists the candidate `cart_id`s and removes nothing. Re-reads the cart afterwards to confirm.
- `view_cart()` — current cart contents via the signed `mtop.aliexpress.trade.cart.render` endpoint, grouped by seller, with a computed subtotal over the shown items. Read-only. AliExpress paginates the cart behind an opaque cursor, but `view_cart` walks it automatically (up to 10 pages), so most carts come back in full; only if paging fails partway or a cart exceeds that cap is it shown as "N of M items" (see Known limitations).
- `list_orders(max_orders)` / `get_order(order_id)` — your recent orders and per-order status + line items via `mtop.aliexpress.trade.buyer.order.list`. AliExpress returns 10 orders per page; asking for `max_orders` above that walks back through additional pages of real history rather than capping at the most recent 10. Prices are normalized to `<amount> <ISO code>` (each order keeps the currency it was paid in). Read-only (never pays/cancels). **Needs a full login session** — see the note under Known limitations.
- `get_wishlist(max_items)` — your saved / liked items via `mtop.ae.wishlist.allItems.render`, flagging items that **dropped in price** since you saved them (normalized to `<amount> <CUR> off`) and sold-out ones, with each item's saved-on date. Read-only. **Needs a full login session** (same as orders). Unlike the cart, the wishlist endpoint has no working pagination and locks its page size at ~16, so a larger wishlist is shown as "N of M".
- `list_wishlists()` — the named lists ("collections") on the account, with ids and item counts, from `mtop.ae.wishlist.myList.render`. A *different* endpoint from the one above: `allItems.render` returns saved products and never names the lists; this returns the lists and never their products. Read-only.
- `add_to_wishlist(wishlist, item_id | url)` — **write.** Files a product under one of your lists. Two steps when the item isn't saved yet, because AliExpress models them separately: `wishitem.save` performs the ♡ (saving it ungrouped), then `myList.saveItem` moves it into the chosen list. Refuses to guess an ambiguous list name rather than filing the item somewhere you won't look.
- `remove_from_wishlist(item_id | url, wishlist, permanent)` — **destructive write.** Both removals are the same `DELETE_PRODUCT` droplet operation on `allItems.render`, distinguished only by `wishGroupId`, but they are very different in effect, so the tool refuses to pick one for you: `wishlist=` takes the item out of that list and it stays saved (ungrouped), `permanent=True` deletes it from the wishlist altogether. A call with neither removes nothing and reports which list the item is currently in. The irreversible action is the one you have to ask for, not the one you get by omission.
- `create_wishlist(name, public)` — **write.** Creates an empty list via `mtop.aliexpress.wishlist.group.update` at v2.0, whose `groupListString` is a nested JSON *string* rather than an array. Duplicate names are allowed by AliExpress, so the new list's id is reported back.
- `delete_wishlist(wishlist)` — **destructive write.** Deletes a list. Not the v2.0 API that creates them (it has no delete opType) but a `DELETE_GROUP` droplet operation on `myList.render`. Deletes the container only: items filed under it stay in the wishlist and become ungrouped.

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

1. Install the [MCP Auth Bridge](https://github.com/justinritchie/mcp-auth-bridge) Chrome extension. The `aliexpress` entry is already in its `sites.json`.

2. Open `https://www.aliexpress.com`, log in, then click **Save AliExpress** in the extension popup. This writes cookies to `~/.mcp-credentials/aliexpress.json`.

3. Install the server itself — pick one:

   **As a Claude Code plugin (recommended).** The repo ships `.claude-plugin/marketplace.json` and `.mcp.json`, which together register the MCP server and the `aliexpress-shopping` skill in one step, launched via `uv run` (no separate `pip install` — `uv` resolves `mcp<2`, `httpx[http2]` and `beautifulsoup4` itself from `.mcp.json`):

   ```
   /plugin marketplace add we4sz/aliexpress-mcp-server
   /plugin install aliexpress@aliexpress
   ```

   `aliexpress@aliexpress` is `<plugin>@<marketplace>`; both are named `aliexpress`
   in `.claude-plugin/marketplace.json`. Point `marketplace add` at whichever
   repo actually holds the code you want — this fork carries fixes (seller
   identity on aggregation listings, cart-selection convergence, the HTTP header
   profile) that upstream `AlexSabaka/aliexpress-mcp-server` does not.

   **Manual Claude Desktop config**, for setups that don't go through a Claude Code plugin marketplace:

   ```bash
   pip install -r requirements.txt
   ```

   Then add to `~/Library/Application Support/Claude/claude_desktop_config.json` (path is wherever you cloned this repo):

   ```json
   {
     "mcpServers": {
       "aliexpress": {
         "command": "python3",
         "args": ["/path/to/aliexpress-mcp-server/aliexpress_mcp_server.py"],
         "env": {
           "ALIEXPRESS_CREDENTIALS": "~/.mcp-credentials/aliexpress.json",
           "ALIEXPRESS_COUNTRY": "CA",
           "ALIEXPRESS_CURRENCY": "CAD"
         }
       }
     }
   }
   ```

   Restart Claude Desktop afterward.

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
- **Search sometimes renders without its product grid.** Same URL, same second — AliExpress occasionally serves the results page with a non-zero result count but no `itemList` grid inside it. `search_products`/`find_deals` retry internally; if it still fails they say so and suggest dropping `ship_from` or trying different keywords, rather than an identical retry — an identical resubmit was observed failing twice in a row for the same query, so the tools no longer imply retrying alone fixes it.
- **Cookies expire / lag the cart.** When tool calls start returning "session expired" — or `view_cart` reports an empty cart despite having items — re-open aliexpress.com in Chrome and click **Save AliExpress** again to capture fresh session cookies. The `_m_h5_tk` token and cart state rotate together; stale cookies show an empty server-side cart.
- **Rate limiting.** The server sends realistic Chrome headers and paces its own calls — a minimum gap between MTOP requests (`ALIEXPRESS_MIN_INTERVAL`, default 0.7s) and a much wider one between account writes (`ALIEXPRESS_CART_INTERVAL`, default 5s) — and it reuses the `_m_h5_tk` session token instead of re-fetching one per call, which roughly halves request volume. That is enough for ordinary use, not for bulk crawling: sustained hammering still trips AliExpress's anti-bot (`RGV587_ERROR` / `FAIL_SYS_USER_VALIDATE`). **That block does not clear on its own** — 45s and 90s waits were both tested and both failed; it lifted only once the challenge was completed in a logged-in browser tab. The tools now say so rather than inviting a retry, because retrying spends rate-limit budget prolonging a block that waiting cannot lift.
- **Orders and the wishlist need a full login session.** `list_orders` / `get_order` / `get_wishlist` call APIs that require the HttpOnly login cookies the quick `document.cookie` snippet can't capture — otherwise they return a "full login session" message. Add the HttpOnly rows (DevTools → **Application → Cookies → aliexpress.com**, the ✓ HttpOnly ones) to `~/.mcp-credentials/aliexpress.json`.
- **Very large carts may still be shown partially.** AliExpress paginates the cart with an opaque, gzip-compressed append cursor (`linkage.common.queryParams`); `view_cart` walks it automatically via the Ultron/droplet endpoint (up to 10 pages), so most carts come back in full. If a page fetch fails or a cart exceeds that cap, the result is labelled "showing N of M items" so nothing is silently dropped. The computed subtotal is over the shown items; the server's own "estimated total" reflects only the checkbox-*selected* lines and is labelled as such.
- **On an "aggregation" listing, the merchant's reputation is not available — only their identity.** AliExpress pools reviews and sales volume for one `item_id` across several overseas merchants; on those pages `SHOP_CARD_PC` names a shell store that never sees the order, and the real seller appears only in the EU trader-identification block. `get_seller` reports that merchant and deliberately omits the rating, feedback volume and store age, because those figures describe the pooled page rather than the merchant who fulfils the order — attributing them to a named store is precisely the bug this replaced, which once reported sixteen unrelated listings as all coming from one 10-feedback shop. Their real profile lives on their store page, which returns no payload this server can parse (probed Aug 2026: the store name is in the HTML, the structured profile is not), so the output hands over the store URL instead. That leaves "is this seller legit" unanswerable in-tool for exactly the listings where it matters most — the highest-volume, category-leading ones, since pooled volume is what makes them category-leading.

- **Wishlists are folders, not tags.** A saved item carries a single `productBaseDTO.groupId`, so it lives in at most one list; `add_to_wishlist` *moves* it rather than adding a second membership, and reports it that way. An item can also be saved with no list at all ("ungrouped"), which is where it lands after `remove_from_wishlist … wishlist=…` or after the list it was in is deleted.
- **`get_product_details` and `get_variants` prices can differ by a hair.** They're two separate point-in-time MTOP calls reading the same SKU price map, so a price refresh (or a coupon applied to one path) between them can leave the ranges ~0.5% apart. `get_variants` is the more granular, authoritative per-SKU view.

## Architecture

FastMCP + httpx (negotiates HTTP/2 automatically when the `h2` package is present — `httpx[http2]` in requirements.txt) + BeautifulSoup. Session cookies come from the shared credential file written by the [MCP Auth Bridge](https://github.com/justinritchie/mcp-auth-bridge) Chrome extension.

The code is a package, split by domain rather than by layer — `aliexpress_mcp_server.py` at the root is only a shim that imports and runs it, kept because `.mcp.json` invokes it by path:

| Module | Holds |
| --- | --- |
| `core.py` | Config, cookie/session-token cache, request pacing, MTOP signing and `mtop_call`, the block-map helpers, and every shared parser/formatter (money, sold counts, dates). |
| `scrape.py` | The search page: embedded-JSON extraction and card parsing. The only BeautifulSoup consumer. |
| `catalog.py` | Public catalogue — search, PDP, variants, seller, reviews, shipping, duty logic. |
| `cart.py` | Cart reads and writes, including its two response shapes and pagination. |
| `account.py` | Orders and wishlists. |
| `server.py` | The FastMCP singleton and all 22 tool definitions. The only module that imports `mcp`, and no other module in the package imports it back (avoids an import cycle) — the root `aliexpress_mcp_server.py` shim above does. |

The three account domains all speak Alibaba's Ultron/droplet protocol but encode it differently, which is the single most confusing thing in the codebase: the cart nests **gzip+base64** objects, orders nest plain-JSON **strings**, and the wishlist nests plain-JSON **objects**.

The MTOP response field paths (`PRODUCT_TITLE.text`, `PRICE.targetSkuPriceInfo.salePriceString`, `PC_RATING.rating`, `SHIPPING.originalLayoutResultList[0].bizData.displayAmount`, etc.) were reverse-engineered live from a real response in April 2026 and are documented inline in `_extract_pdp_fields()`. If AliExpress reorganizes the component layout, that function is the place to update.

### Testing

Two suites, because they answer different questions.

```bash
python3 -m unittest discover -s tests    # offline; no account needed
python3 tests/golden.py capture          # live; snapshots every read-only tool
python3 tests/golden.py capture --label after
python3 tests/golden.py diff
```

`tests/test_units.py` pins the pure parsing and formatting logic — localized money strings, abbreviated sold counts, duty expectations, sort ordering. It needs no cookies and no network, so it runs anywhere, and it is the suite to add to when fixing a data-quality bug.

`tests/golden.py` answers the other question, the one that has cost the most time on this project: *did AliExpress change, or did I break it?* It snapshots what every read-only tool actually returns, then diffs. Because it hits a live API where prices and delivery dates legitimately move, it does not fail on any difference — it separates lines whose **numbers** moved (volatile) from lines whose **wording** changed (structural), reports them apart, and leaves the judgement to a human. Snapshots are gitignored: they contain real account data. The write tools are never called.
