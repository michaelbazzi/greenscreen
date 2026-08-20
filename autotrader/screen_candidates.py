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

import requests
from config import API_KEY, API_SECRET

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=15, help="how many per category")
    args = parser.parse_args()

    print("=== Most active (by volume) ===")
    actives = fetch("most-actives", top=args.top)
    for row in actives.get("most_actives", []):
        symbol = row["symbol"]
        if looks_like_junk(symbol):
            continue
        print(f"{symbol}: volume={row['volume']:,} trades={row['trade_count']:,}")

    print()
    print("=== Top gainers ===")
    movers = fetch("movers", top=args.top)
    for row in movers.get("gainers", []):
        symbol = row["symbol"]
        price = row.get("price") or 0
        if looks_like_junk(symbol) or price < MIN_PRICE:
            continue
        print(f"{symbol}: +{row['percent_change']:.1f}% price=${price:.2f}")

    print()
    print("=== Top losers (context only - not buy candidates) ===")
    for row in movers.get("losers", []):
        symbol = row["symbol"]
        price = row.get("price") or 0
        if looks_like_junk(symbol) or price < MIN_PRICE:
            continue
        print(f"{symbol}: {row['percent_change']:.1f}% price=${price:.2f}")

    print()
    print("Reminder: this is a raw screen, not a recommendation. Every name")
    print("here still has to clear market_data.py's technical score and")
    print("propose_trade.py's full guardrail chain before anything executes.")


if __name__ == "__main__":
    main()
