---
name: aliexpress-shopping
description: >-
  AliExpress research and account access — mostly read-only, but can also add/update/remove
  cart items and manage wishlists (never checks out or pays). Use this whenever the user wants to
  search AliExpress, find deals, pull product details, compare per-configuration (SKU)
  prices, read buyer reviews, judge whether a seller is trustworthy, compare which store
  selling the same item is most established, estimate shipping, or reach their own
  AliExpress account — cart, orders (status/tracking), and saved / wishlist items
  including price drops. Trigger on AliExpress or "Ali", an aliexpress.com or
  a.aliexpress.com link or item ID, "how much on AliExpress", "is this seller legit",
  "which store should I buy from", "where's my order", "what's in my cart", "did my
  wishlist get cheaper", or wanting a cheaper equivalent from Chinese sellers even when
  AliExpress isn't named. Do NOT use it for other marketplaces (Amazon, eBay, Temu,
  Alibaba B2B), for raw carrier tracking with no AliExpress order, or for coding tasks
  that merely mention AliExpress. It never checks out, pays, or cancels.
---

# AliExpress (read-only research + cart/wishlist writes)

These tools wrap AliExpress's own signed mobile API (MTOP) plus its search page, giving
clean structured data instead of scraped HTML.

Most tools only read. Eight write to the user's real account — `add_to_cart`,
`set_cart_selection`, `set_cart_quantity`, `remove_from_cart`, `add_to_wishlist`,
`remove_from_wishlist`, `create_wishlist`, `delete_wishlist` — and three of those destroy
something. **There is no path to checkout, pay, or cancel an order**, which is the
reassurance a worried user actually wants; don't overstate it into "everything is
read-only".

Say what a write will do before doing it, and prefer the reversible form: taking an item
out of one list (`remove_from_wishlist` with `wishlist=`) keeps it saved, while
`permanent=True` deletes it outright. A call with neither removes nothing and asks which
you meant — that refusal is the tool working correctly, not an error to route around.
Nothing here can be undone by the server.

## Pick the right tool

Match the user's intent to the narrowest tool that answers it — don't fetch full details
when they only asked "how much", and don't guess a config price when `get_variants` gives
the exact table.

| The user wants… | Use |
| --- | --- |
| To find products by keyword | `search_products(query, min_rating, max_price, sort_by, ship_from)` |
| Specifically *discounted* items, sorted by how deep the discount is | `find_deals(query, min_discount, max_price, min_rating, sort_by, ship_from)` |
| The full picture of one item (price, rating, sold count, seller, shipping) | `get_product_details(item_id \| url)` |
| Which exact config a price buys — RAM / storage / CPU / bundle / size → price | `get_variants(item_id \| url)` |
| Just the shipping cost and ETA to the configured country | `get_shipping_estimate(item_id \| url)` |
| To read buyer reviews and the positive/neutral/negative breakdown | `get_reviews(item_id \| url, max_reviews, filter_by)` |
| To judge whether a store is trustworthy | `get_seller(item_id \| url)` |
| Which store to buy from when several sell the same item | `compare_sellers(title \| item_id \| url)` |
| What's currently in *their* cart | `view_cart()` |
| Their recent orders / to track one | `list_orders(max_orders)`, `get_order(order_id)` |
| Their saved / liked items, and which got cheaper | `get_wishlist(max_items)` |
| Their named wishlists / collections, with ids and counts | `list_wishlists()` |
| To put something in their cart — **write** | `add_to_cart(item_id \| url, sku_id, quantity)` |
| To make sure a cart line will actually be included at checkout, or to leave it in the cart without ordering it — **write** | `set_cart_selection(selected, item_id \| url \| cart_id, sku_id)` |
| To change how many of a cart line — **write** | `set_cart_quantity(quantity, item_id \| url \| cart_id, sku_id)` |
| To take a line out of the cart — **destructive** | `remove_from_cart(item_id \| url \| cart_id, sku_id)` |
| To save an item into one of their lists — **write** | `add_to_wishlist(wishlist, item_id \| url)` |
| To take an item out of one list, keeping it saved — **write** | `remove_from_wishlist(item_id \| url, wishlist)` |
| To delete a saved item outright — **destructive, permanent** | `remove_from_wishlist(item_id \| url, permanent=True)` |
| A new list to organise saves into — **write** | `create_wishlist(name, public)` |
| To get rid of a list (its items survive, ungrouped) — **destructive** | `delete_wishlist(wishlist)` |

When an item shows a wide price range in `get_product_details`, follow up with
`get_variants` — the range alone can't tell the user *which* configuration costs what,
and that's usually the actual question behind "how much is it".

## Conventions worth knowing

**Item references are flexible.** Every `item_id | url` tool accepts a bare numeric id, a
full `/item/<id>.html` URL, or a short **`a.aliexpress.com/_xxx`** share link (the mobile
share-sheet format) — the short link is resolved by following its redirect, so you can pass
whatever the user pasted.

**Prices are self-describing, not uniform.** AliExpress renders each surface in a different
currency and the server can't force them to agree: search and product details come back in
the account's site currency, the cart in each listing's currency (often USD), orders in
whatever currency each order was actually paid in (so one response can legitimately mix,
say, UAH and USD), and the wishlist in the configured currency. Because of that, **every
amount is printed with its ISO currency code** (`212.90 USD`, `808.96 UAH`, `82.44 CAD`).
Read the code off each number rather than assuming one currency; if the user needs them
reconciled, convert explicitly and say you did.

**A steep discount may be fake.** A strike-through "was" price that implies ≥ 60% off is
tagged **⚠ MSRP?** because AliExpress's reference prices are frequently inflated. Pass that
skepticism on — don't present the "savings" as real without the flag.

**The cart usually comes back in full.** AliExpress paginates it behind an opaque cursor,
but `view_cart` walks that automatically (up to 10 pages), so most carts are complete. Only
when a page fetch fails or a cart exceeds the cap is it labelled "showing N of M items" —
if you see that, say so rather than implying it's everything.

**AliExpress orders only the TICKED cart lines.** An un-ticked line stays fully visible in
the cart and simply never arrives, and that is not recoverable after checkout — so it is
worth checking before the user orders, not after. `view_cart` flags un-ticked lines
individually and in a summary, and reconciles that against AliExpress's own count of
selected lines; when the two disagree it says so instead of guessing. `set_cart_selection`
ticks or un-ticks a line. The subtotal `view_cart` prints covers the shown items; the
server's own "checkout estimate" covers only the ticked ones and is labelled that way.

**Orders and the wishlist need a full login session.** `list_orders`, `get_order`, and
`get_wishlist` require the HttpOnly login cookies. If they return a "full login session"
message, the fix is to re-save cookies including the HttpOnly rows (see the project README)
— it's not something a different tool call will solve.

**Sessions expire.** If any tool reports an expired/invalid session, the user needs to
re-save their AliExpress cookies; the signing token rotates and stale cookies can also show
an out-of-date (or empty) cart. Surface that clearly rather than retrying blindly.

## Presenting results

Lead with what the user asked for, keep the item id or link handy so they can act on it,
and preserve the ⚠ flags (MSRP, price drop, sold out, cart truncation) — those are the
signal, not decoration. For price comparisons across configs or sellers, a short list or
small table reads better than prose.
