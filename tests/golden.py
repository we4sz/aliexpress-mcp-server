#!/usr/bin/env python3
"""
Golden-output harness: the safety net for refactors.

The problem this solves is the one that has cost the most time on this project —
"did AliExpress change, or did I break it?". Before a refactor, snapshot exactly
what every read-only tool returns. After, run again and diff. A structural change
that alters no output is safe; anything else is a bug caught before it ships.

    python3 tests/golden.py capture            # write tests/golden/<tool>.txt
    python3 tests/golden.py capture --label after
    python3 tests/golden.py diff               # golden/ vs golden-after/

Deliberately NOT byte-exact-or-fail: this hits a live API, so prices, delivery
dates and stock legitimately move between runs. Volatile-looking differences are
reported separately from structural ones, and a human decides. An automated pass
that ignored value changes would defeat the entire purpose.

Read-only tools only. The write tools mutate a real account and are never called.
"""
import difflib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ALIEXPRESS_COUNTRY", "SE")
os.environ.setdefault("ALIEXPRESS_CURRENCY", "SEK")

import aliexpress_mcp_server as srv  # noqa: E402  (exercises the shim import path)
import aliexpress_mcp.server as _server_module  # noqa: E402  (where the @mcp.tool defs actually live)

ROOT = Path(__file__).resolve().parent

# Fixed inputs so runs are comparable. Item ids are real and long-lived.
ITEM_MULTI = "1005007235591794"   # multi-variant (the "grab bag" case)
ITEM_SIMPLE = "1005008819293735"  # simple listing with a real shipping quote

# `best_match` ranking is server-side non-deterministic — running the ORIGINAL
# implementation against itself produced a larger diff than a refactor did — so
# the search cases pin an explicit sort. Otherwise every run reports structural
# noise and the harness trains you to ignore it, which is worse than no harness.
CASES = [
    ("search_products", dict(query="usb c cable", sort_by="price_asc")),
    ("search_products_shipfrom", dict(query="usb c cable", ship_from="ES",
                                      sort_by="price_asc"), "search_products"),
    ("find_deals", dict(query="usb c cable", sort_by="price_asc")),
    ("get_product_details", dict(item_id=ITEM_SIMPLE)),
    ("get_variants", dict(item_id=ITEM_MULTI)),
    ("get_shipping_estimate", dict(item_id=ITEM_SIMPLE)),
    ("get_reviews", dict(item_id=ITEM_MULTI)),
    ("get_seller", dict(item_id=ITEM_SIMPLE)),
    ("view_cart", {}),
    ("list_orders", dict(max_orders=30)),
    ("get_wishlist", {}),
    ("list_wishlists", {}),
]

# A line is "volatile" only when its NUMBERS moved and its WORDS did not.
#
# The first version of this asked "does the line contain a price?" and called any
# such change volatile. That dismissed a real regression: a subtotal whose label
# changed from "all items shown" to "23 of 24 items; 1 unpriced" while the number
# stayed identical was reported as price drift, because the line happened to
# contain a price. Masking the digits and comparing what's left catches that —
# same skeleton means only values moved; different skeleton is structural.
_NUM = re.compile(r"\d[\d\s.,]*")
_DATE = re.compile(r"[A-Z][a-z]{2}\.?\s+#")   # applied after digit masking


def skeleton(line):
    """The line with every numeric run replaced, so only its wording remains."""
    return _DATE.sub("<date>", _NUM.sub("#", line))


def unwrap(name):
    # aliexpress_mcp_server.py is now a thin shim (imports `mcp` only) — the
    # actual @mcp.tool functions live in aliexpress_mcp.server. Check the shim
    # first (harmless no-op today) so this keeps working if that ever changes.
    fn = getattr(srv, name, None) or getattr(_server_module, name, None)
    return getattr(fn, "fn", fn) if fn else None


def capture(label):
    out = ROOT / (f"golden-{label}" if label else "golden")
    out.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        name, kwargs = case[0], case[1]
        tool = case[2] if len(case) > 2 else name
        fn = unwrap(tool)
        if fn is None:
            print(f"  SKIP {name}: tool not found")
            continue
        try:
            text = str(fn(**kwargs))
        except Exception as e:                      # noqa: BLE001 - record the failure too
            text = f"<<EXCEPTION>> {type(e).__name__}: {e}"
        (out / f"{name}.txt").write_text(text)
        first = text.splitlines()[0] if text.splitlines() else ""
        print(f"  {name:<26} {len(text):>6} chars  {first[:60]}")
    print(f"\nwrote {out}")


def only_numbers_moved(old, new):
    """True when the two files differ only in numeric values, not in wording."""
    return [skeleton(l) for l in old] == [skeleton(l) for l in new]


def diff(label):
    a, b = ROOT / "golden", ROOT / f"golden-{label}"
    if not a.exists() or not b.exists():
        sys.exit(f"need both {a} and {b} — run capture twice")
    structural = volatile = identical = 0
    for f in sorted(a.glob("*.txt")):
        other = b / f.name
        if not other.exists():
            print(f"MISSING in {label}: {f.name}")
            structural += 1
            continue
        old, new = f.read_text().splitlines(), other.read_text().splitlines()
        if old == new:
            identical += 1
            continue
        changed = [l for l in difflib.unified_diff(old, new, lineterm="", n=0)
                   if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        if only_numbers_moved(old, new):
            volatile += 1
            print(f"~ {f.name}: {len(changed)} line(s), values moved but wording identical")
            continue
        structural += 1
        print(f"\n✗ {f.name}: STRUCTURAL DIFF")
        for l in changed[:12]:
            print("   ", l[:150])
        if len(changed) > 12:
            print(f"    ... {len(changed) - 12} more")
    print(f"\nidentical={identical}  volatile-only={volatile}  STRUCTURAL={structural}")
    sys.exit(1 if structural else 0)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"
    lbl = sys.argv[sys.argv.index("--label") + 1] if "--label" in sys.argv else ""
    if cmd == "capture":
        capture(lbl)
    elif cmd == "diff":
        diff(lbl or "after")
    else:
        sys.exit(__doc__)
