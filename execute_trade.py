#!/usr/bin/env python3
"""
execute_trade.py — general-purpose execution layer for the paper account
configured in config.py (currently PA3D3WAKC861, "New Paper Account")

Separate from deploy.py intentionally: deploy.py is the validated buy-and-hold
allocator and stays untouched. This script executes individual trades from any
signal source (manual, future backtested strategy, etc.) and logs every fill
to a queryable SQLite database.

Usage:
    python execute_trade.py AAPL buy 5
    python execute_trade.py AAPL sell 5 --order-type limit --limit-price 210.50
    python execute_trade.py MSFT buy --notional 100

Sizing: share count is the primary positional argument. --notional is an
optional alternative for dollar-based sizing. Exactly one must be provided.

Safety rail: this script will refuse to submit any order against a non-paper
account unless PAPER_ONLY is explicitly overridden AND the operator types the
exact confirmation phrase. This is not a formality — it is the checkpoint that
stops a script built for paper-money iteration from silently touching real
capital later.
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus

from config import API_KEY, API_SECRET
from autotrader.db import get_connection, init_decisions_table, log_trade, export_csv

# --- Safety rail ---------------------------------------------------------
PAPER_ONLY = True
LIVE_CONFIRMATION_PHRASE = "I CONFIRM LIVE TRADING WITH REAL MONEY"

REPO_DIR = Path(__file__).resolve().parent
DB_PATH = REPO_DIR / "trades.db"
CSV_PATH = REPO_DIR / "trades.csv"


def init_db():
    conn = get_connection(DB_PATH)
    init_decisions_table(conn)  # also creates the trades table; WAL-enabled
    return conn


def confirm_live_or_exit(paper_flag):
    if paper_flag:
        return
    if not PAPER_ONLY:
        print("PAPER_ONLY override detected in source. Proceeding to live confirmation gate.")
    print("\n*** THIS WILL SUBMIT AN ORDER AGAINST A LIVE, REAL-MONEY ACCOUNT. ***")
    typed = input(f'Type exactly: "{LIVE_CONFIRMATION_PHRASE}" to proceed, anything else cancels: ')
    if typed.strip() != LIVE_CONFIRMATION_PHRASE:
        print("Confirmation phrase did not match. Aborting. No order submitted.")
        sys.exit(1)


def poll_for_fill(client, order_id, timeout_sec=8, interval_sec=1.0):
    """Poll briefly for a fill price. Market may be closed / order may stay
    'accepted' rather than 'filled' — that's expected and logged as-is."""
    deadline = time.time() + timeout_sec
    order = client.get_order_by_id(order_id)
    while time.time() < deadline:
        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            return order
        if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            return order
        time.sleep(interval_sec)
        order = client.get_order_by_id(order_id)
    return order


def main():
    parser = argparse.ArgumentParser(description="Execute a single trade against Alpaca and log it.")
    parser.add_argument("ticker", type=str, help="e.g. AAPL")
    parser.add_argument("side", choices=["buy", "sell"])
    parser.add_argument("shares", nargs="?", type=float, default=None,
                         help="Primary sizing method: number of shares (fractional allowed)")
    parser.add_argument("--notional", type=float, default=None,
                         help="Alternative sizing: dollar amount instead of share count")
    parser.add_argument("--order-type", choices=["market", "limit"], default="market")
    parser.add_argument("--limit-price", type=float, default=None,
                         help="Required if --order-type limit")
    parser.add_argument("--paper", dest="paper", action="store_true", default=True,
                         help="Default. Explicit for clarity.")
    parser.add_argument("--live", dest="paper", action="store_false",
                         help="DANGER: routes to live account. Requires typed confirmation.")

    args = parser.parse_args()

    if args.shares is None and args.notional is None:
        parser.error("Provide either a share count or --notional, exactly one.")
    if args.shares is not None and args.notional is not None:
        parser.error("Provide either shares or --notional, not both.")
    if args.order_type == "limit" and args.limit_price is None:
        parser.error("--order-type limit requires --limit-price")

    confirm_live_or_exit(args.paper)

    client = TradingClient(API_KEY, API_SECRET, paper=args.paper)
    account = client.get_account()
    print(f"Account: {account.id} | Paper: {args.paper} | Buying power: ${account.buying_power}")

    side_enum = OrderSide.BUY if args.side == "buy" else OrderSide.SELL

    order_kwargs = dict(
        symbol=args.ticker.upper(),
        side=side_enum,
        time_in_force=TimeInForce.DAY,
    )
    if args.shares is not None:
        order_kwargs["qty"] = args.shares
    else:
        order_kwargs["notional"] = round(args.notional, 2)

    if args.order_type == "market":
        order_request = MarketOrderRequest(**order_kwargs)
    else:
        order_request = LimitOrderRequest(limit_price=args.limit_price, **order_kwargs)

    print(f"Submitting {args.side} {args.ticker.upper()} "
          f"({'qty='+str(args.shares) if args.shares is not None else 'notional=$'+str(args.notional)}) "
          f"as {args.order_type}...")

    submitted = client.submit_order(order_data=order_request)
    print(f"Order submitted: id={submitted.id} status={submitted.status}")

    final = poll_for_fill(client, submitted.id)
    fill_price = float(final.filled_avg_price) if final.filled_avg_price else None

    conn = init_db()
    log_trade(conn, (
        datetime.now(timezone.utc).isoformat(),
        str(account.id),
        args.ticker.upper(),
        args.side,
        args.order_type,
        args.shares,
        args.notional,
        args.limit_price,
        fill_price,
        str(final.status),
        str(final.id),
        1 if args.paper else 0,
    ))
    export_csv(conn)
    conn.close()

    print(f"Logged to {DB_PATH.name} (and mirrored to {CSV_PATH.name}). "
          f"Status: {final.status} | Fill price: {fill_price}")


if __name__ == "__main__":
    main()
