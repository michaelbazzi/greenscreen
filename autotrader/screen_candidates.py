#!/usr/bin/env python3
"""
screen_candidates.py — pulls real, structured "what's actually moving in
the market right now" data from Alpaca's own screener API, instead of
relying on the research agent stumbling onto something in a news search.

Deliberately dumb: does a bare minimum sanity filter (price floor, symbol
shape) and nothing else. All real admission decisions (liquidity, trading
history, technical score) stay in propose_trade.py / market_data.py — this
script's only job is to surface candidates worth *looking at*, not to
approve anything.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from config_loader import load_credentials

API_KEY, API_SECRET = load_credentials()
HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}
BASE = "https://data.alpaca.markets/v1beta1/screener/stocks"

MIN_PRICE = 5.00  # sanity floor - excludes most penny-stock/warrant noise
# Warrants, units, and rights typically carry a suffix letter after the
# base ticker (e.g. FOOW, FOOU, FOOR) or a literal "W"/"WS" tail. This is a
# heuristic, not authoritative - propose_trade.py's real checks are what
# actually protect against anything that slips past this.
JUNK_SUFFIX_RE = re.compile(r"(W|WS|WW|U|R|RT)$")


def fetch(endpoint, top=20):
    r = requests.get(f"{BASE}/{endpoint}", headers=HEADERS, params={"top": top}, timeout=10)
    r.raise_for_status()
    return r.json()


def looks_like_junk(symbol):
    return bool(JUNK_SUFFIX_RE.search(symbol)) and len(symbol) > 3


def get_candidates(top=15):
    """Structured version of the screen - same filtering, no printing.
    Returns {"most_active": [...], "gainers": [...], "losers": [...]},
    each a list of {"symbol", ...raw Alpaca fields}. Used by both main()
    below (CLI/print) and webapp/research_agent.py's screen_candidates
    tool - one filtering implementation, two presentations."""
    actives = fetch("most-actives", top=top)
    most_active = [
        row for row in actives.get("most_actives", [])
        if not looks_like_junk(row["symbol"])
    ]

    movers = fetch("movers", top=top)
    gainers = [
        row for row in movers.get("gainers", [])
        if not looks_like_junk(row["symbol"]) and (row.get("price") or 0) >= MIN_PRICE
    ]
    losers = [
        row for row in movers.get("losers", [])
        if not looks_like_junk(row["symbol"]) and (row.get("price") or 0) >= MIN_PRICE
    ]
    return {"most_active": most_active, "gainers": gainers, "losers": losers}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=15, help="how many per category")
    args = parser.parse_args()

    result = get_candidates(top=args.top)

    print("=== Most active (by volume) ===")
    for row in result["most_active"]:
        print(f"{row['symbol']}: volume={row['volume']:,} trades={row['trade_count']:,}")

    print()
    print("=== Top gainers ===")
    for row in result["gainers"]:
        print(f"{row['symbol']}: +{row['percent_change']:.1f}% price=${row['price']:.2f}")

    print()
    print("=== Top losers (context only - not buy candidates) ===")
    for row in result["losers"]:
        print(f"{row['symbol']}: {row['percent_change']:.1f}% price=${row['price']:.2f}")

    print()
    print("Reminder: this is a raw screen, not a recommendation. Every name")
    print("here still has to clear market_data.py's technical score and")
    print("propose_trade.py's full guardrail chain before anything executes.")


if __name__ == "__main__":
    main()
