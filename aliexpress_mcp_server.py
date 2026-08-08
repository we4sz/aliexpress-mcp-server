#!/usr/bin/env python3
"""
AliExpress MCP Server — entry point.

Search AliExpress, pull clean product details, check shipping to the
configured country (ALIEXPRESS_COUNTRY, default CA), and manage your cart, orders, and wishlist.

Auth: Session cookies from MCP Auth Bridge extension at
~/.mcp-credentials/aliexpress.json

Thin shim: all real code lives in the aliexpress_mcp package (core, scrape,
catalog, cart, account, server). .mcp.json invokes this file by path, so it
must keep working when run directly.
"""

from aliexpress_mcp.server import mcp

if __name__ == "__main__":
    mcp.run()
