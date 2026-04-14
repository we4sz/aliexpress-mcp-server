# AliExpress MCP Server

An MCP server that wraps AliExpress search and product detail scraping for a Canadian shopper — prices in CAD, shipping to Vancouver, BC.

Read-only by design: it searches, fetches product details, and checks shipping. It does **not** add to cart, check out, or pay.

## Tools

- `search_products(query, min_rating, max_price, sort_by)` — searches the public wholesale search URL and parses the embedded JSON.
- `get_product_details(item_id | url)` — title, price, discount, rating, sold count, seller, shipping cost & ETA.
- `get_shipping_estimate(item_id)` — shipping cost + ETA to the configured country.
- `view_cart()` — stub; see *Known Limitations* below.

## How it works

The initial product page is a client-side-rendered shell (`window._d_c_.isCSR = true`, `window.runParams = {}`) — real product data loads over AJAX from AliExpress's MTOP API after the JS runs. So HTML scraping doesn't work for PDP data.

Instead, this server replicates what `mtop.js` does in the browser: it makes signed calls to `mtop.aliexpress.pdp.pc.query` using the session's `_m_h5_tk` cookie as the HMAC token. The signing algorithm:

```
sign = md5(token + "&" + t_ms + "&" + "12574478" + "&" + json_payload)
```

where `token` is the prefix of `_m_h5_tk` before the underscore, and `12574478` is AliExpress's public web `appKey`. Token refresh on `FAIL_SYS_TOKEN_EXPIRED` is handled automatically.

Search still uses the simpler HTML path (embedded `window.runParams` JSON from the search SSR HTML) — no signed calls needed.

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

## Known limitations

- **Cart viewer is a stub.** The cart page is CSR too — a working version needs its own MTOP endpoint call (likely `mtop.aliexpress.trade.cart.queryCart`). Tracked as future work.
- **Low-volume / brand-new listings** sometimes return empty MTOP responses (endpoint returns `SUCCESS` but an empty data block). Likely a region/visibility gate. Search results still show the listing fine.
- **Cookies expire.** When tool calls start returning "session expired", re-open aliexpress.com in Chrome and click **Save AliExpress** again.
- **Rate limiting** is the user's responsibility. The server sends realistic Chrome headers but doesn't throttle; don't hammer the search.

## Architecture

Follows the pattern of [dekudeals-mcp-server](../dekudeals-mcp-server/) — FastMCP + httpx + BeautifulSoup — with added MTOP client code in `aliexpress_mcp_server.py`. Session cookies come from the shared credential file written by the [MCP Auth Bridge](https://github.com/justinritchie/mcp-auth-bridge) Chrome extension.

The MTOP response field paths (`PRODUCT_TITLE.text`, `PRICE.targetSkuPriceInfo.salePriceString`, `PC_RATING.rating`, `SHIPPING.originalLayoutResultList[0].bizData.displayAmount`, etc.) were reverse-engineered live from a real response in April 2026 and are documented inline in `_extract_pdp_fields()`. If AliExpress reorganizes the component layout, that function is the place to update.
