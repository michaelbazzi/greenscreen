#!/usr/bin/env python3
"""
propose_trade.py — the ONLY path through which the autonomous research loop
is allowed to place an order. Every other script (or an agent's own Bash/API
calls) bypassing this file is out of scope for the guardrails below.

Design principle: the research step (a scheduled agent doing web search) picks
candidate tickers and writes the human-readable rationale - selection is its
judgment call, not a --conviction flag it hands this script. What it CANNOT
do is talk its way past the risk limits: cash reserve, position/trade size
caps, sector exposure, ticker-count and daily caps, the stop-loss sweep, and
the circuit breakers all still apply to every proposal regardless of how
compelling the rationale is. (evaluate_buy also still recomputes a technical
score from live market data on every proposal and logs it - visible for
analysis - but as of 2026-09-11 that score no longer gates admission; see
risk_params.py's TECH_SCORE_THRESHOLD_* comment for why.) Every proposal is
logged to the `decisions` table whether it's approved or rejected, so
rejections are as visible as trades.

This script is paper-trading only. There is no --live flag here at all —
unlike execute_trade.py, going live is deliberately not something this
script can ever be asked to do.

Subcommands:
    python propose_trade.py status
    python propose_trade.py sweep-stop-loss --run-id 2026-08-19T14
    python propose_trade.py propose buy AAPL --notional 50 \\
        --rationale "..." --run-id 2026-08-19T14 [--source-url ...] [--sector technology] [--dry-run]
    python propose_trade.py propose sell AAPL --notional 50 \\
        --rationale "..." --run-id 2026-08-19T14
    python propose_trade.py rotate JPM MRNA --notional 40 \\
        --rationale "..." --run-id 2026-08-19T14 [--dry-run]
        (sells $40 of JPM to buy $40 of MRNA - only if JPM is the weakest-
        scoring current holding, MRNA's score beats it by ROTATION_MIN_SCORE_EDGE,
        and MRNA passes the same buy gate as any other proposal. No calendar
        cooldown - the technical score is daily-bar-based and can't
        meaningfully shift within a trading day, so the score-edge
        requirement is the real protection against chasing noise.)
    python propose_trade.py reset-circuit-breaker
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetPortfolioHistoryRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from config_loader import load_credentials, load_risk_params, load_db_path, load_state_dir

API_KEY, API_SECRET = load_credentials()
rp = load_risk_params()
DB_PATH = load_db_path()
from db import (
    get_connection,
    init_decisions_table,
    log_decision,
    log_trade,
    export_csv,
    count_new_positions_today,
    sum_notional_deployed_today,
    decision_already_logged,
    rotation_already_logged,
)
import market_data as md

REPO_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = load_state_dir()
LOCK_PATH = STATE_DIR / "run.lock"
CIRCUIT_BREAKER_PATH = STATE_DIR / "circuit_breaker.json"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_client():
    return TradingClient(API_KEY, API_SECRET, paper=True)


def fail(reason):
    print(f"REFUSED: {reason}")
    sys.exit(1)


# --- Kill switch -----------------------------------------------------------

def check_kill_switch():
    if not rp.ENABLED:
        fail("ENABLED = False in risk_params.py — autonomous trading is switched off.")


# --- Run lock ---------------------------------------------------------------

def check_and_acquire_lock(run_id):
    STATE_DIR.mkdir(exist_ok=True)
    if LOCK_PATH.exists():
        try:
            held = json.loads(LOCK_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            held = {}
        age_minutes = (time.time() - LOCK_PATH.stat().st_mtime) / 60
        if held.get("run_id") != run_id and age_minutes < rp.RUN_LOCK_STALE_MINUTES:
            fail(
                f"run.lock held by run_id={held.get('run_id')!r} "
                f"({age_minutes:.1f} min old) — refusing to run overlapping cycle "
                f"{run_id!r}. Wait for it to go stale (> {rp.RUN_LOCK_STALE_MINUTES} min) "
                f"or investigate a stalled run."
            )
        if age_minutes >= rp.RUN_LOCK_STALE_MINUTES:
            print(f"(stale lock from run_id={held.get('run_id')!r} discarded)")
    LOCK_PATH.write_text(json.dumps({"run_id": run_id, "pid": os.getpid(), "updated_at": now_iso()}))


# --- Circuit breaker ---------------------------------------------------------

def _drawdown_pct(client, period):
    req = GetPortfolioHistoryRequest(period=period, timeframe="15Min")
    history = client.get_portfolio_history(req)
    equity_values = [e for e in (history.equity or []) if e is not None]
    if len(equity_values) < 2:
        return None
    start, latest = equity_values[0], equity_values[-1]
    if start == 0:
        return None
    return (latest - start) / start


def _read_breaker_state():
    """Whatever's on disk, or {} if the file is missing or unreadable. A
    corrupt state file must not be able to block trading or silently grant
    full size - both callers treat {} as 'no breaker history'."""
    if not CIRCUIT_BREAKER_PATH.exists():
        return {}
    try:
        return json.loads(CIRCUIT_BREAKER_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _record_trip(reason, sticky):
    """Persist a trip. `last_trip_at` is written for EVERY trip, daily ones
    included, because it's what the post-clear ramp measures from - the
    daily breaker otherwise leaves no trace at all once the day rolls over.
    `sticky` (weekly only) additionally sets the tripped flag that blocks
    buys until reset-circuit-breaker runs."""
    state = _read_breaker_state()
    now = now_iso()
    state.update({"last_trip_at": now, "last_trip_reason": reason})
    if sticky:
        state.update({"tripped": True, "reason": reason, "tripped_at": now})
    STATE_DIR.mkdir(exist_ok=True)
    CIRCUIT_BREAKER_PATH.write_text(json.dumps(state))


def circuit_breaker_ramp_multiplier():
    """Scale factor on new-buy size caps: 1.0 normally,
    CIRCUIT_BREAKER_RAMP_MULTIPLIER for CIRCUIT_BREAKER_RAMP_HOURS after the
    most recent trip of either kind - whether it's still tripped or was
    cleared minutes ago. See risk_params.py's CIRCUIT_BREAKER_RAMP_* block."""
    last_trip_at = _read_breaker_state().get("last_trip_at")
    if not last_trip_at:
        return 1.0
    try:
        tripped_dt = datetime.fromisoformat(last_trip_at)
    except (TypeError, ValueError):
        return 1.0
    hours_since = (datetime.now(timezone.utc) - tripped_dt).total_seconds() / 3600
    if hours_since < rp.CIRCUIT_BREAKER_RAMP_HOURS:
        return rp.CIRCUIT_BREAKER_RAMP_MULTIPLIER
    return 1.0


def check_circuit_breaker(client):
    """Returns (tripped: bool, reason: str | None). Weekly trips persist
    until reset-circuit-breaker is run; daily trips are re-evaluated fresh
    every call (a new day naturally clears them). Either kind also stamps
    last_trip_at, which outlives the clear and drives the size ramp."""
    state = _read_breaker_state()
    if state.get("tripped"):
        return True, f"circuit breaker already tripped: {state.get('reason')} " \
                      f"(at {state.get('tripped_at')}) — run reset-circuit-breaker to clear"

    weekly_dd = _drawdown_pct(client, "1W")
    if weekly_dd is not None and weekly_dd <= rp.WEEKLY_DRAWDOWN_CIRCUIT_BREAKER_PCT:
        reason = f"weekly drawdown {weekly_dd:.1%} <= {rp.WEEKLY_DRAWDOWN_CIRCUIT_BREAKER_PCT:.1%}"
        _record_trip(reason, sticky=True)
        return True, reason

    daily_dd = _drawdown_pct(client, "1D")
    if daily_dd is not None and daily_dd <= rp.DAILY_DRAWDOWN_CIRCUIT_BREAKER_PCT:
        reason = f"daily drawdown {daily_dd:.1%} <= {rp.DAILY_DRAWDOWN_CIRCUIT_BREAKER_PCT:.1%} (auto-clears tomorrow)"
        _record_trip(reason, sticky=False)
        return True, reason

    return False, None


def cmd_reset_circuit_breaker(args):
    """Clears the block. Deliberately does NOT clear last_trip_at: sizing
    stays ramped down for the rest of the ramp window, so clearing the
    breaker is no longer a one-command jump back to full size."""
    state = _read_breaker_state()
    if not state.get("tripped"):
        print("Circuit breaker was not tripped.")
        return

    for key in ("tripped", "reason", "tripped_at"):
        state.pop(key, None)
    CIRCUIT_BREAKER_PATH.write_text(json.dumps(state))

    multiplier = circuit_breaker_ramp_multiplier()
    if multiplier < 1.0:
        print(f"Circuit breaker cleared. New-buy sizing stays at {multiplier:.0%} of "
              f"normal caps until {rp.CIRCUIT_BREAKER_RAMP_HOURS}h after the last trip "
              f"({state.get('last_trip_at')}).")
    else:
        print("Circuit breaker cleared.")


# --- Account snapshot --------------------------------------------------------

def get_snapshot(client):
    account = client.get_account()
    positions = {p.symbol: p for p in client.get_all_positions()}
    return {
        "account_id": str(account.id),
        "cash": float(account.cash),
        "portfolio_value": float(account.portfolio_value),
        "positions": positions,
    }


def sector_exposure(snapshot, sector):
    total = 0.0
    for symbol, pos in snapshot["positions"].items():
        tagged = rp.TICKER_SECTORS.get(symbol, "unknown")
        if tagged == sector:
            total += float(pos.market_value)
    return total


# --- status ------------------------------------------------------------------

def cmd_status(args):
    client = get_client()
    snapshot = get_snapshot(client)
    conn = get_connection(DB_PATH)
    init_decisions_table(conn)

    tripped, reason = check_circuit_breaker(client)

    print(f"Account: {snapshot['account_id']}")
    print(f"Portfolio value: ${snapshot['portfolio_value']:,.2f}")
    print(f"Cash: ${snapshot['cash']:,.2f}")
    print(f"Positions held: {len(snapshot['positions'])} ({', '.join(sorted(snapshot['positions'])) or 'none'})")
    print(f"New buys today: {count_new_positions_today(conn, today_str())} / {rp.MAX_NEW_POSITIONS_PER_DAY}")
    print(f"Notional deployed today: ${sum_notional_deployed_today(conn, today_str()):,.2f} "
          f"/ ${snapshot['portfolio_value'] * rp.MAX_DAILY_NOTIONAL_DEPLOYED_PCT:,.2f}")
    print(f"Circuit breaker: {'TRIPPED — ' + reason if tripped else 'clear'}")
    ramp = circuit_breaker_ramp_multiplier()
    if ramp < 1.0:
        last_trip = _read_breaker_state().get("last_trip_at")
        print(f"Buy-size ramp: {ramp:.0%} of normal caps "
              f"(max trade {rp.MAX_TRADE_PCT * ramp:.1%}, max position {rp.MAX_POSITION_PCT * ramp:.1%}) "
              f"— within {rp.CIRCUIT_BREAKER_RAMP_HOURS}h of last trip at {last_trip}")
    else:
        print("Buy-size ramp: none (full size)")
    print(f"Kill switch (ENABLED): {rp.ENABLED}")
    conn.close()


# --- sweep-stop-loss -----------------------------------------------------

def cmd_sweep_stop_loss(args):
    check_kill_switch()
    check_and_acquire_lock(args.run_id)
    client = get_client()
    snapshot = get_snapshot(client)
    conn = get_connection(DB_PATH)
    init_decisions_table(conn)

    if not snapshot["positions"]:
        print("No open positions to sweep.")
        conn.close()
        return

    for symbol, pos in snapshot["positions"].items():
        if symbol in rp.MANUALLY_HELD_TICKERS:
            continue
        entry = float(pos.avg_entry_price)
        current = float(pos.current_price)
        plpc = (current - entry) / entry
        is_new = symbol not in rp.TICKER_SECTORS  # core 5 are pre-tagged; anything else is "new"
        stop = rp.STOP_LOSS_PCT_NEW_TICKER if is_new else rp.STOP_LOSS_PCT

        if plpc > stop:
            continue

        reason = f"stop-loss triggered: {plpc:.1%} <= {stop:.1%}"
        print(f"{symbol}: {reason}")
        if args.dry_run:
            print(f"  (dry-run, not selling)")
            continue

        order = MarketOrderRequest(
            symbol=symbol,
            qty=str(pos.qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        submitted = client.submit_order(order_data=order)
        print(f"  SOLD {pos.qty} shares, order_id={submitted.id}")

        log_trade(conn, (
            now_iso(), snapshot["account_id"], symbol, "sell", "market",
            float(pos.qty), None, None, None, str(submitted.status),
            str(submitted.id), 1,
        ))
        log_decision(conn, (
            now_iso(), args.run_id, symbol, "sell", "shares", float(pos.qty),
            None, reason, None, 1, None, str(submitted.id),
        ))

    export_csv(conn)
    conn.close()


# --- propose (buy/sell) -------------------------------------------------

def evaluate_buy(snapshot, ticker, notional, sector_arg, conn, skip_cash_reserve=False):
    if notional < rp.MIN_ORDER_NOTIONAL:
        return None, f"notional ${notional:.2f} below floor ${rp.MIN_ORDER_NOTIONAL:.2f}"

    is_new = ticker not in snapshot["positions"]
    portfolio_value = snapshot["portfolio_value"]

    signals = md.compute_signals(ticker)
    if signals is None:
        return None, "insufficient price history to compute signals"

    if is_new:
        ok, reason = md.meets_new_ticker_criteria(
            signals, rp.NEW_TICKER_MIN_TRADING_DAYS, rp.NEW_TICKER_MIN_AVG_DOLLAR_VOLUME
        )
        if not ok:
            return md.technical_score(signals), reason

    # Still computed and logged on every decision (so it stays visible for
    # analysis, e.g. research_scorecard.py's score-bucket breakdown), but no
    # longer a rejection gate - see risk_params.py's TECH_SCORE_THRESHOLD_*
    # comment for why (demoted 2026-09-11: shown to have no positive, and a
    # mildly negative, correlation with actual forward returns). Admission
    # is now the research agent's own judgment call, gated only by the risk
    # limits below.
    score = md.technical_score(signals)

    # Halved (or whatever the multiplier says) for a window after any
    # circuit-breaker trip, so a cleared breaker ramps back to full size
    # instead of snapping to it - see risk_params.py CIRCUIT_BREAKER_RAMP_*.
    ramp = circuit_breaker_ramp_multiplier()
    ramp_note = (f" [ramped to {ramp:.0%} of normal caps after a recent "
                 f"circuit-breaker trip]") if ramp < 1.0 else ""

    max_trade_pct = (rp.MAX_TRADE_PCT_NEW_TICKER if is_new else rp.MAX_TRADE_PCT) * ramp
    # Round both sides to the cent before comparing - notional arrives
    # already rounded to the cent from callers, but portfolio_value *
    # max_trade_pct is an unrounded float, so a trade sized at exactly the
    # cap could be rejected by sub-cent floating-point noise alone (e.g.
    # "$41.06 exceeds max trade size $41.06"). Money is never actually
    # more precise than the cent, so comparing at cent precision is the
    # correct comparison, not a loosened one.
    max_trade_notional = round(portfolio_value * max_trade_pct, 2)
    if round(notional, 2) > max_trade_notional:
        return score, (
            f"notional ${notional:.2f} exceeds max trade size "
            f"${max_trade_notional:,.2f} ({max_trade_pct:.1%} of portfolio){ramp_note}"
        )

    max_position_pct = rp.MAX_POSITION_PCT * ramp
    existing_value = float(snapshot["positions"][ticker].market_value) if not is_new else 0.0
    if existing_value + notional > portfolio_value * max_position_pct:
        return score, (
            f"projected position ${existing_value + notional:,.2f} exceeds "
            f"max position size ${portfolio_value * max_position_pct:,.2f}{ramp_note}"
        )

    if not skip_cash_reserve and snapshot["cash"] - notional < portfolio_value * rp.MIN_CASH_RESERVE_PCT:
        return score, (
            f"would leave cash ${snapshot['cash'] - notional:,.2f} below reserve floor "
            f"${portfolio_value * rp.MIN_CASH_RESERVE_PCT:,.2f}"
        )

    if is_new and len(snapshot["positions"]) >= rp.MAX_TICKERS_HELD:
        return score, f"already holding {len(snapshot['positions'])} tickers, at max {rp.MAX_TICKERS_HELD}"

    buys_today = count_new_positions_today(conn, today_str())
    if buys_today >= rp.MAX_NEW_POSITIONS_PER_DAY:
        return score, f"already {buys_today} buys today, at max {rp.MAX_NEW_POSITIONS_PER_DAY}"

    deployed_today = sum_notional_deployed_today(conn, today_str())
    daily_cap = portfolio_value * rp.MAX_DAILY_NOTIONAL_DEPLOYED_PCT
    if deployed_today + notional > daily_cap:
        return score, (
            f"would deploy ${deployed_today + notional:,.2f} today, "
            f"exceeding daily cap ${daily_cap:,.2f}"
        )

    sector = sector_arg or rp.TICKER_SECTORS.get(ticker, "unknown")
    sector_cap_pct = rp.MAX_SECTOR_EXPOSURE_PCT if sector != "unknown" else rp.MAX_SECTOR_EXPOSURE_PCT_UNKNOWN
    projected_sector_exposure = sector_exposure(snapshot, sector) + notional
    if projected_sector_exposure > portfolio_value * sector_cap_pct:
        return score, (
            f"sector '{sector}' exposure ${projected_sector_exposure:,.2f} would exceed "
            f"cap ${portfolio_value * sector_cap_pct:,.2f} ({sector_cap_pct:.0%})"
        )

    return score, None


def evaluate_sell(snapshot, ticker, notional):
    if ticker in rp.MANUALLY_HELD_TICKERS:
        return f"{ticker} is manually held - excluded from all automated sells"
    if notional < rp.MIN_ORDER_NOTIONAL:
        return f"notional ${notional:.2f} below floor ${rp.MIN_ORDER_NOTIONAL:.2f}"
    pos = snapshot["positions"].get(ticker)
    if pos is None:
        return f"no open position in {ticker} to sell"
    if notional > float(pos.market_value):
        return f"requested ${notional:.2f} exceeds held value ${float(pos.market_value):.2f}"
    return None


# --- rotate (sell a held position to fund a better-looking candidate) ------

def _score_ticker(ticker):
    """Returns (score, signals) or (None, None) if signals can't be computed."""
    signals = md.compute_signals(ticker)
    if signals is None:
        return None, None
    return md.technical_score(signals), signals


def evaluate_rotation(snapshot, from_ticker, to_ticker, notional, sector_arg, conn):
    """Returns (from_score, to_score, rejection_reason | None). Reused by
    cmd_rotate; kept separate so the decision logic is unit-testable without
    hitting the network."""
    if not rp.ROTATION_ENABLED:
        return None, None, "ROTATION_ENABLED = False in risk_params.py — rotation is off"

    if from_ticker == to_ticker:
        return None, None, "from and to tickers are the same"

    if from_ticker in rp.MANUALLY_HELD_TICKERS:
        return None, None, f"{from_ticker} is manually held - excluded from all automated sells"

    from_pos = snapshot["positions"].get(from_ticker)
    if from_pos is None:
        return None, None, f"{from_ticker} is not currently held, nothing to rotate out of"

    if notional < rp.MIN_ORDER_NOTIONAL:
        return None, None, f"notional ${notional:.2f} below floor ${rp.MIN_ORDER_NOTIONAL:.2f}"

    from_value = float(from_pos.market_value)
    if notional > from_value:
        return None, None, f"requested ${notional:.2f} exceeds {from_ticker}'s held value ${from_value:.2f}"

    # Score every current holding - from_ticker has to be the weakest one,
    # not just weaker than to_ticker. If a worse holding exists, that's the
    # one that should be rotated, not this one.
    held_scores = {}
    for symbol in snapshot["positions"]:
        score, _ = _score_ticker(symbol)
        held_scores[symbol] = score  # may be None if signals unavailable

    scored = {k: v for k, v in held_scores.items() if v is not None}
    if scored:
        weakest_ticker = min(scored, key=scored.get)
        if weakest_ticker != from_ticker and scored[weakest_ticker] < scored.get(from_ticker, 1.0):
            return None, held_scores.get(from_ticker), (
                f"{from_ticker} is not the weakest-scoring holding — {weakest_ticker} "
                f"scores lower ({scored[weakest_ticker]:.2f} vs {scored.get(from_ticker, float('nan')):.2f}); "
                f"rotate that one instead if this candidate is worth funding"
            )

    from_score, _ = _score_ticker(from_ticker)
    if from_score is None:
        return None, None, f"couldn't compute a current technical score for {from_ticker}"

    to_score, to_signals = _score_ticker(to_ticker)
    if to_score is None:
        return from_score, None, f"couldn't compute a technical score for {to_ticker}"

    edge = to_score - from_score
    if edge < rp.ROTATION_MIN_SCORE_EDGE:
        return from_score, to_score, (
            f"{to_ticker}'s edge over {from_ticker} ({edge:+.2f}) is below the "
            f"minimum required to justify a rotation ({rp.ROTATION_MIN_SCORE_EDGE:.2f})"
        )

    # No calendar-based cooldown here on purpose: the technical score is
    # built from daily bars (5-day return, price vs. 20-day SMA), which
    # can't materially shift within a single trading day across this
    # system's 4 intraday cycles - a same-day reversal isn't just unlikely,
    # it's mechanically close to impossible given how the score is
    # computed. The score-edge requirement above is the real, live-data-
    # grounded protection against chasing noise; an arbitrary time limit
    # on top of it would block a genuinely correct decision for no reason
    # tied to actual portfolio conditions.

    # Simulate the post-sell snapshot and run the *exact same* buy gate
    # everything else goes through - no separate/looser path for rotation.
    simulated = {
        "account_id": snapshot["account_id"],
        "cash": snapshot["cash"] + notional,
        "portfolio_value": snapshot["portfolio_value"],
        "positions": dict(snapshot["positions"]),
    }
    remaining = from_value - notional
    if remaining <= rp.MIN_ORDER_NOTIONAL:
        simulated["positions"].pop(from_ticker, None)
    else:
        reduced = SimpleNamespace(**{
            k: getattr(from_pos, k) for k in ("market_value", "avg_entry_price", "current_price", "qty")
        })
        reduced.market_value = remaining
        simulated["positions"][from_ticker] = reduced

    # skip_cash_reserve: a same-size rotation is cash-neutral by
    # construction (sell $N, buy $N) - it never touches uninvested cash,
    # which is what the reserve floor exists to protect. Gating it on that
    # check would mean rotation can never fire once cash is already below
    # reserve (true for this account permanently, since it never receives
    # fresh deposits) - regardless of how good the candidate is. Every
    # other check (position/sector caps, liquidity, technical score) still
    # applies in full.
    buy_score, buy_rejection = evaluate_buy(
        simulated, to_ticker, notional, sector_arg, conn, skip_cash_reserve=True
    )
    if buy_rejection is not None:
        return from_score, to_score, f"{to_ticker} buy leg would fail: {buy_rejection}"

    return from_score, to_score, None


def cmd_rotate(args):
    check_kill_switch()
    check_and_acquire_lock(args.run_id)
    client = get_client()

    tripped, cb_reason = check_circuit_breaker(client)
    if tripped:
        fail(f"circuit breaker blocks rotation (it's a new-buy-shaped action): {cb_reason}")

    snapshot = get_snapshot(client)
    conn = get_connection(DB_PATH)
    init_decisions_table(conn)

    if rotation_already_logged(conn, args.run_id, args.from_ticker, args.to_ticker):
        conn.close()
        fail(f"a rotation from {args.from_ticker} to {args.to_ticker} was already "
             f"logged under run_id={args.run_id!r} — refusing to duplicate this exact pair. "
             f"(A different from_ticker for the same candidate is not blocked by this check.)")

    from_score, to_score, rejection = evaluate_rotation(
        snapshot, args.from_ticker, args.to_ticker, args.notional, args.sector, conn
    )
    approved = rejection is None
    sell_order_id = buy_order_id = None
    rationale = f"[ROTATION: {args.from_ticker} -> {args.to_ticker}] {args.rationale}"

    if approved and not args.dry_run:
        sell_order = MarketOrderRequest(
            symbol=args.from_ticker, notional=round(args.notional, 2),
            side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        )
        sell_submitted = client.submit_order(order_data=sell_order)
        sell_order_id = str(sell_submitted.id)
        log_trade(conn, (
            now_iso(), snapshot["account_id"], args.from_ticker, "sell", "market",
            None, args.notional, None, None, str(sell_submitted.status), sell_order_id, 1,
        ))

        buy_order = MarketOrderRequest(
            symbol=args.to_ticker, notional=round(args.notional, 2),
            side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        )
        buy_submitted = client.submit_order(order_data=buy_order)
        buy_order_id = str(buy_submitted.id)
        log_trade(conn, (
            now_iso(), snapshot["account_id"], args.to_ticker, "buy", "market",
            None, args.notional, None, None, str(buy_submitted.status), buy_order_id, 1,
        ))
        export_csv(conn)
        print(f"APPROVED and submitted rotation: sold {args.from_ticker} "
              f"(order={sell_order_id}), bought {args.to_ticker} (order={buy_order_id})")
    elif approved and args.dry_run:
        print(f"APPROVED (dry-run, not submitted): rotate {args.from_ticker} -> {args.to_ticker} "
              f"${args.notional:.2f}")
    else:
        print(f"REJECTED: rotate {args.from_ticker} -> {args.to_ticker} ${args.notional:.2f} — {rejection}")

    log_decision(conn, (
        now_iso(), args.run_id, args.from_ticker, "sell", "notional", args.notional,
        from_score, rationale, args.source_url, 1 if approved else 0, rejection, sell_order_id,
    ), counterparty=args.to_ticker)
    log_decision(conn, (
        now_iso(), args.run_id, args.to_ticker, "buy", "notional", args.notional,
        to_score, rationale, args.source_url, 1 if approved else 0, rejection, buy_order_id,
    ), counterparty=args.from_ticker)
    conn.close()

    if not approved:
        sys.exit(1)


def cmd_propose(args):
    check_kill_switch()
    check_and_acquire_lock(args.run_id)
    client = get_client()

    tripped, cb_reason = check_circuit_breaker(client)
    if tripped and args.side == "buy":
        fail(f"circuit breaker blocks new buys: {cb_reason}")

    snapshot = get_snapshot(client)
    conn = get_connection(DB_PATH)
    init_decisions_table(conn)

    if decision_already_logged(conn, args.run_id, args.ticker, args.side):
        conn.close()
        fail(f"a '{args.side}' decision for {args.ticker} was already logged under run_id={args.run_id!r} "
             f"— refusing to duplicate (use a distinct --run-id per cycle).")

    if args.side == "buy":
        score, rejection = evaluate_buy(snapshot, args.ticker, args.notional, args.sector, conn)
    else:
        score, rejection = None, evaluate_sell(snapshot, args.ticker, args.notional)

    approved = rejection is None
    order_id = None

    if approved and not args.dry_run:
        order = MarketOrderRequest(
            symbol=args.ticker,
            notional=round(args.notional, 2),
            side=OrderSide.BUY if args.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        submitted = client.submit_order(order_data=order)
        order_id = str(submitted.id)
        print(f"APPROVED and submitted: {args.side} {args.ticker} ${args.notional:.2f}, order_id={order_id}")

        log_trade(conn, (
            now_iso(), snapshot["account_id"], args.ticker, args.side, "market",
            None, args.notional, None, None, str(submitted.status), order_id, 1,
        ))
        export_csv(conn)
    elif approved and args.dry_run:
        print(f"APPROVED (dry-run, not submitted): {args.side} {args.ticker} ${args.notional:.2f}")
    else:
        print(f"REJECTED: {args.side} {args.ticker} ${args.notional:.2f} — {rejection}")

    log_decision(conn, (
        now_iso(), args.run_id, args.ticker, args.side, "notional", args.notional,
        score, args.rationale, args.source_url, 1 if approved else 0, rejection, order_id,
    ))
    conn.close()

    if not approved:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)

    p_sweep = sub.add_parser("sweep-stop-loss")
    p_sweep.add_argument("--run-id", required=True)
    p_sweep.add_argument("--dry-run", action="store_true")
    p_sweep.set_defaults(func=cmd_sweep_stop_loss)

    p_reset = sub.add_parser("reset-circuit-breaker")
    p_reset.set_defaults(func=cmd_reset_circuit_breaker)

    p_propose = sub.add_parser("propose")
    p_propose.add_argument("side", choices=["buy", "sell"])
    p_propose.add_argument("ticker", type=str)
    p_propose.add_argument("--notional", type=float, required=True)
    p_propose.add_argument("--rationale", type=str, required=True)
    p_propose.add_argument("--source-url", type=str, default=None)
    p_propose.add_argument("--sector", type=str, default=None,
                            help="Required context for new tickers not in TICKER_SECTORS; "
                                 "defaults to 'unknown' (tighter exposure cap) if omitted.")
    p_propose.add_argument("--run-id", required=True)
    p_propose.add_argument("--dry-run", action="store_true")
    p_propose.set_defaults(func=cmd_propose)

    p_rotate = sub.add_parser(
        "rotate",
        description="Sell a held position to fund a better-looking candidate. "
                     "Off by default - see ROTATION_ENABLED in risk_params.py.",
    )
    p_rotate.add_argument("from_ticker", type=str, help="currently-held ticker to sell")
    p_rotate.add_argument("to_ticker", type=str, help="candidate ticker to buy")
    p_rotate.add_argument("--notional", type=float, required=True,
                           help="dollar amount to swap (sold from from_ticker, bought into to_ticker)")
    p_rotate.add_argument("--rationale", type=str, required=True)
    p_rotate.add_argument("--source-url", type=str, default=None)
    p_rotate.add_argument("--sector", type=str, default=None)
    p_rotate.add_argument("--run-id", required=True)
    p_rotate.add_argument("--dry-run", action="store_true")
    p_rotate.set_defaults(func=cmd_rotate)

    args = parser.parse_args()
    if hasattr(args, "ticker"):
        args.ticker = args.ticker.upper()
    if hasattr(args, "from_ticker"):
        args.from_ticker = args.from_ticker.upper()
        args.to_ticker = args.to_ticker.upper()
    args.func(args)


if __name__ == "__main__":
    main()
