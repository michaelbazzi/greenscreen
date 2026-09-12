"""
Unit tests for propose_trade.py's guardrail logic (evaluate_buy / evaluate_sell) -
the code that decides whether the research agent's proposals are actually
allowed to execute. Alpaca and market-data calls are mocked; only the
decision logic itself is under test.

Run with: /Users/MichaelBazzi/trading-env/bin/python3 -m pytest -v
(from the autotrader/ directory, or anywhere - conftest handles the path)
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import risk_params as rp
import propose_trade as pt
from db import get_connection, init_decisions_table, log_decision, rotation_already_logged


# --- fixtures ---------------------------------------------------------

@pytest.fixture
def conn():
    """A fresh in-memory decisions DB per test."""
    c = get_connection(":memory:")
    init_decisions_table(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def isolated_breaker_state(tmp_path, monkeypatch):
    """Point circuit-breaker state at an empty tmp dir for EVERY test.
    evaluate_buy reads it now (for the post-trip size ramp), so without this
    a real tripped breaker in autotrader/state/ would silently halve the
    caps every sizing test asserts against."""
    monkeypatch.setattr(pt, "STATE_DIR", tmp_path)
    monkeypatch.setattr(pt, "CIRCUIT_BREAKER_PATH", tmp_path / "circuit_breaker.json")


def make_position(market_value, avg_entry_price=100.0, current_price=100.0, qty=1.0):
    return SimpleNamespace(
        market_value=market_value,
        avg_entry_price=avg_entry_price,
        current_price=current_price,
        qty=qty,
    )


def make_snapshot(cash, portfolio_value, positions=None):
    return {
        "account_id": "test-account",
        "cash": cash,
        "portfolio_value": portfolio_value,
        "positions": positions or {},
    }


def make_signals(current_price=110.0, sma20=100.0, return_5d=0.06,
                  avg_dollar_volume=10_000_000, trading_days_available=250,
                  sma50=None, sma200=None, rsi14=None, atr_pct=None, volume_ratio=None):
    """current_price > sma20 and positive return_5d => trend/momentum lean
    bullish by default, clearing TECH_SCORE_THRESHOLD_EXISTING (0.60) under
    the current _WEIGHTS with a small margin - callers that only care about
    downstream gate checks (sizing, caps, reserve) rely on this default
    scoring high enough that the score check itself never fires. The newer
    components (sma50/sma200/rsi14/atr_pct/volume_ratio) default to None,
    which technical_score() treats as neutral (0.5) - so a test that only
    cares about trend/momentum doesn't need to know about the other five
    components, and a fully-flat call (current_price == sma20, return_5d ==
    0) still scores exactly 0.5."""
    return {
        "ticker": "TEST",
        "current_price": current_price,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "return_5d": return_5d,
        "avg_dollar_volume": avg_dollar_volume,
        "rsi14": rsi14,
        "atr_pct": atr_pct,
        "volume_ratio": volume_ratio,
        "trading_days_available": trading_days_available,
        "first_bar_date": None,
    }


# --- evaluate_buy: basic gates ---------------------------------------

def test_buy_rejects_dust_order(conn):
    snapshot = make_snapshot(cash=500, portfolio_value=1000)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 1.0, None, conn)
    assert score is None
    assert "below floor" in reason


def test_buy_rejects_when_no_price_history(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: None)
    snapshot = make_snapshot(cash=500, portfolio_value=1000)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert score is None
    assert "insufficient price history" in reason


def test_buy_approved_when_all_checks_pass(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    # AAPL already held, plenty of cash and position headroom
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert reason is None
    assert score >= rp.TECH_SCORE_THRESHOLD_EXISTING


# --- evaluate_buy: technical score is informational only (demoted 2026-09-11) --

def test_buy_approved_regardless_of_low_technical_score(conn, monkeypatch):
    """Score is still computed and returned (for logging/analysis - see
    research_scorecard.py) but no longer gates admission: a decade of
    backtesting found it has no positive, and a mildly negative,
    correlation with actual forward returns for this universe. Only the
    risk limits below this point in evaluate_buy can still reject."""
    flat_signals = make_signals(current_price=100.0, sma20=100.0, return_5d=0.0)
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: flat_signals)
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert score == pytest.approx(0.5)  # still computed and returned
    assert reason is None  # but no longer rejected for it


# --- evaluate_buy: sizing / position caps ------------------------------

def test_buy_rejects_trade_exceeding_max_trade_pct(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    oversized = 1000 * rp.MAX_TRADE_PCT + 1
    score, reason = pt.evaluate_buy(snapshot, "AAPL", oversized, None, conn)
    assert "max trade size" in reason


def test_buy_rejects_exceeding_max_position_pct(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    # Already holding right up against the position cap
    already_held = 1000 * rp.MAX_POSITION_PCT - 5
    positions = {"AAPL": make_position(market_value=already_held)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 10.0, None, conn)
    assert "max position size" in reason


def test_buy_rejects_insufficient_cash_reserve(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    positions = {"AAPL": make_position(market_value=100.0)}
    # cash is basically zero, like the real live account
    snapshot = make_snapshot(cash=0.04, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert "reserve floor" in reason


# --- evaluate_buy: new-ticker admission ---------------------------------

def test_buy_rejects_new_ticker_with_insufficient_history(conn, monkeypatch):
    thin_signals = make_signals(trading_days_available=3)
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: thin_signals)
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions={})
    score, reason = pt.evaluate_buy(snapshot, "FRESHIPO", 40.0, None, conn)
    assert "trading days of history" in reason


def test_buy_rejects_new_ticker_when_at_max_tickers_held(conn, monkeypatch):
    # Needs to clear the new-ticker score threshold (0.75) so this
    # test actually exercises the max-tickers-held check, not an earlier
    # score-threshold rejection.
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: _signals_with_score(0.90))
    positions = {
        f"T{i}": make_position(market_value=10.0)
        for i in range(rp.MAX_TICKERS_HELD)
    }
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "NEWTICKER", 40.0, "technology", conn)
    assert "at max" in reason


def test_buy_uses_wider_criteria_and_smaller_cap_for_new_tickers(conn, monkeypatch):
    # Needs a score comfortably above the *new-ticker* threshold (0.75) so
    # the trade-size-cap rejection below is actually what's under test,
    # not an earlier score-threshold rejection.
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: _signals_with_score(0.90))
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions={})
    # new-ticker cap is smaller than the existing-holding cap
    just_over_new_cap = 1000 * rp.MAX_TRADE_PCT_NEW_TICKER + 1
    score, reason = pt.evaluate_buy(snapshot, "NEWTICKER", just_over_new_cap, "technology", conn)
    assert "max trade size" in reason


def test_buy_at_exact_max_trade_cap_is_not_rejected_by_rounding(conn, monkeypatch):
    """Regression: notional and portfolio_value * max_trade_pct can differ
    by sub-cent floating-point noise even when both represent the same
    rounded dollar amount (a caller typically computes notional as
    round(portfolio_value * some_pct, 2)) - comparing unrounded floats
    could reject a trade sized at exactly the cap with a self-contradictory
    message like "$41.06 exceeds max trade size $41.06". Found via
    GreenScreen-backtest regime testing: a post-liquidation account sizing
    every trade at exactly MAX_TRADE_PCT_NEW_TICKER hit this on nearly
    every attempt, real portfolio value below."""
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: _signals_with_score(0.90))
    portfolio_value = 821.1783741004233
    notional = round(portfolio_value * rp.MAX_TRADE_PCT_NEW_TICKER, 2)
    snapshot = make_snapshot(cash=portfolio_value, portfolio_value=portfolio_value, positions={})
    score, reason = pt.evaluate_buy(snapshot, "NEWTICKER", notional, "technology", conn)
    assert reason is None, f"a trade sized at exactly the cap must not be rejected: {reason}"


# --- evaluate_buy: daily caps -------------------------------------------

def test_buy_rejects_when_daily_buy_count_reached(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    today = pt.today_str()
    for i in range(rp.MAX_NEW_POSITIONS_PER_DAY):
        log_decision(conn, (
            f"{today}T00:00:0{i}Z", f"run-{i}", "SOMETICKER", "buy",
            "notional", 10.0, 0.9, "test fill", None, 1, None, f"order-{i}",
        ))
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert "buys today" in reason


def test_buy_rejects_when_daily_notional_cap_reached(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    today = pt.today_str()
    daily_cap = 1000 * rp.MAX_DAILY_NOTIONAL_DEPLOYED_PCT
    log_decision(conn, (
        f"{today}T00:00:00Z", "run-x", "SOMETICKER", "buy",
        "notional", daily_cap - 5, 0.9, "test fill", None, 1, None, "order-x",
    ))
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert "deploy $" in reason


# --- evaluate_buy: sector exposure --------------------------------------

def test_buy_rejects_exceeding_sector_exposure_cap(conn, monkeypatch):
    # MSFT isn't held yet here, so it needs the new-ticker threshold (0.75)
    # cleared for the sector-cap rejection below to be what's under test.
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: _signals_with_score(0.90))
    # AAPL and MSFT are both tagged "technology" in risk_params
    already_held = 1000 * rp.MAX_SECTOR_EXPOSURE_PCT - 5
    positions = {"AAPL": make_position(market_value=already_held)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "MSFT", 40.0, None, conn)
    assert "sector" in reason


# --- evaluate_sell -------------------------------------------------------

def test_sell_rejects_dust_order():
    snapshot = make_snapshot(cash=0, portfolio_value=1000,
                              positions={"AAPL": make_position(market_value=100.0)})
    reason = pt.evaluate_sell(snapshot, "AAPL", 1.0)
    assert "below floor" in reason


def test_sell_rejects_ticker_not_held():
    snapshot = make_snapshot(cash=0, portfolio_value=1000, positions={})
    reason = pt.evaluate_sell(snapshot, "AAPL", 40.0)
    assert "no open position" in reason


def test_sell_rejects_more_than_held_value():
    positions = {"AAPL": make_position(market_value=50.0)}
    snapshot = make_snapshot(cash=0, portfolio_value=1000, positions=positions)
    reason = pt.evaluate_sell(snapshot, "AAPL", 100.0)
    assert "exceeds held value" in reason


def test_sell_approved_within_held_value():
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=0, portfolio_value=1000, positions=positions)
    reason = pt.evaluate_sell(snapshot, "AAPL", 40.0)
    assert reason is None


def test_sell_rejects_manually_held_ticker(monkeypatch):
    monkeypatch.setattr(rp, "MANUALLY_HELD_TICKERS", {"BTCUSD"})
    positions = {"BTCUSD": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=0, portfolio_value=1000, positions=positions)
    reason = pt.evaluate_sell(snapshot, "BTCUSD", 40.0)
    assert "manually held" in reason


# --- evaluate_rotation ----------------------------------------------------

def _signals_with_score(target_score, sma20=100.0):
    """Reverse-engineer signals that produce exactly `target_score` from
    technical_score()'s formula. Drives all seven components to the same
    normalized value (`target_score` itself) - since the component weights
    sum to 1.0, a weighted average of seven identical values equals that
    value exactly, regardless of how the weight is split among them. Much
    simpler than solving the weighted-sum equation for one or two inputs,
    and immune to any future rebalancing of _WEIGHTS in market_data.py."""
    t = target_score

    trend_raw = t * 0.20 - 0.10  # inverse of _normalize(x, -0.10, 0.10)
    current_price = sma20 * (1 + trend_raw)

    return_5d = t * 0.20 - 0.10  # same +/-10% range as trend

    rsi14 = t * 100  # inverse of _normalize(x, 0, 100)

    trend50_raw = t * 0.30 - 0.15  # inverse of _normalize(x, -0.15, 0.15)
    sma50 = current_price / (1 + trend50_raw)

    trend200_raw = t * 0.40 - 0.20  # inverse of _normalize(x, -0.20, 0.20)
    sma200 = current_price / (1 + trend200_raw)

    # volatility is inverted in technical_score (lower ATR scores higher),
    # so the normalize() output needs to equal (1 - t), not t.
    atr_pct = 0.005 + (1 - t) * (0.08 - 0.005)

    volume_ratio = 0.5 + t * 1.0  # inverse of _normalize(x, 0.5, 1.5)

    return make_signals(
        current_price=current_price, sma20=sma20, return_5d=return_5d,
        sma50=sma50, sma200=sma200, rsi14=rsi14, atr_pct=atr_pct,
        volume_ratio=volume_ratio,
    )


def test_rotation_rejected_when_disabled(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", False)
    positions = {"JPM": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=100, portfolio_value=1000, positions=positions)
    from_score, to_score, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert "ROTATION_ENABLED" in reason


def test_rotation_rejected_when_from_ticker_not_held(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    snapshot = make_snapshot(cash=100, portfolio_value=1000, positions={})
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert "not currently held" in reason


def test_rotation_rejected_when_from_ticker_manually_held(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    monkeypatch.setattr(rp, "MANUALLY_HELD_TICKERS", {"BTCUSD"})
    positions = {"BTCUSD": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=100, portfolio_value=1000, positions=positions)
    _, _, reason = pt.evaluate_rotation(snapshot, "BTCUSD", "MRNA", 40.0, None, conn)
    assert "manually held" in reason


def test_rotation_rejected_when_from_is_not_weakest_holding(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    # JPM scores well (0.8), AAPL scores poorly (0.3) - AAPL is the real
    # weak link, so rotating JPM should be refused even if MRNA looks great.
    scores = {"JPM": 0.80, "AAPL": 0.30, "MRNA": 0.95}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {
        "JPM": make_position(market_value=100.0),
        "AAPL": make_position(market_value=100.0),
    }
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert "not the weakest-scoring holding" in reason
    assert "AAPL" in reason


def test_rotation_rejected_when_edge_too_small(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    # JPM is the only (and thus weakest) holding; MRNA barely beats it -
    # edge below ROTATION_MIN_SCORE_EDGE (0.15 default).
    scores = {"JPM": 0.70, "MRNA": 0.75}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {"JPM": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert "edge" in reason


def test_rotation_allowed_even_when_from_ticker_bought_moments_ago(conn, monkeypatch):
    """No calendar cooldown by design - see the comment in evaluate_rotation.
    A position bought seconds ago is still eligible for rotation if it's
    genuinely the weakest holding and the candidate clears the score edge."""
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    scores = {"JPM": 0.50, "MRNA": 0.90}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {"JPM": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    from db import log_trade
    log_trade(conn, (
        pt.now_iso(), "acct", "JPM", "buy", "market", None, 100.0,
        None, None, "filled", "order-1", 1,
    ))
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert reason is None


def test_rotation_approved_when_all_checks_pass(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    scores = {"JPM": 0.50, "MRNA": 0.90}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {"JPM": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    from_score, to_score, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert reason is None
    assert from_score == pytest.approx(0.50, abs=0.01)
    assert to_score == pytest.approx(0.90, abs=0.01)


def test_rotation_ignores_cash_reserve_but_plain_buy_still_respects_it(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    scores = {"JPM": 0.50, "MRNA": 0.90}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {"JPM": make_position(market_value=100.0)}
    # cash is already deep below the 10% reserve floor and a same-size
    # swap can never raise it - rotation should still be approved.
    snapshot = make_snapshot(cash=0.04, portfolio_value=1000, positions=positions)
    from_score, to_score, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert reason is None

    # But a plain (non-rotation) buy against the same starved-cash snapshot
    # must still be rejected on the reserve floor - this exemption is
    # rotation-specific, not a global loosening of the cash check.
    score, buy_rejection = pt.evaluate_buy(snapshot, "MRNA", 40.0, None, conn)
    assert "reserve floor" in buy_rejection


def test_rotation_already_logged_scoped_to_exact_pair(conn):
    log_decision(conn, (
        pt.now_iso(), "run-1", "MRNA", "buy", "notional", 40.0,
        0.9, "[ROTATION: JNJ -> MRNA] original attempt", None, 0,
        "JNJ is not the weakest holding", None,
    ), counterparty="JNJ")
    # Exact same pair, same run - blocked.
    assert rotation_already_logged(conn, "run-1", "JNJ", "MRNA") is True
    # Corrected retry with a different from_ticker for the same candidate -
    # NOT blocked, since this is the legitimate "the gate told me the real
    # weakest holding, retrying with the correction" case.
    assert rotation_already_logged(conn, "run-1", "JPM", "MRNA") is False
    # Different run entirely - not blocked either.
    assert rotation_already_logged(conn, "run-2", "JNJ", "MRNA") is False


def test_decision_already_logged_ignores_rotation_rows(conn):
    """A plain propose buy/sell check must not be confused by rotation
    legs for the same ticker+action - they're logged with a real
    counterparty, plain proposals always use ''."""
    log_decision(conn, (
        pt.now_iso(), "run-1", "MRNA", "buy", "notional", 40.0,
        0.9, "[ROTATION: JPM -> MRNA]", None, 1, None, "order-1",
    ), counterparty="JPM")
    from db import decision_already_logged
    assert decision_already_logged(conn, "run-1", "MRNA", "buy") is False


def test_rotation_rejected_when_buy_leg_fails_sector_cap(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    # AAPL scores higher than JPM so JPM stays the weakest holding (the
    # thing actually under test here is the sector-cap rejection, not the
    # weakest-holding check).
    scores = {"JPM": 0.50, "AAPL": 0.60, "MSFT": 0.90}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    # AAPL (technology) already sits right at the sector cap; rotating into
    # MSFT (also technology) should blow through it even though MSFT wins
    # on score and JPM is the weakest holding.
    already_held_tech = 1000 * rp.MAX_SECTOR_EXPOSURE_PCT - 5
    positions = {
        "JPM": make_position(market_value=100.0),
        "AAPL": make_position(market_value=already_held_tech),
    }
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MSFT", 40.0, None, conn)
    assert "buy leg would fail" in reason
    assert "sector" in reason


# --- circuit-breaker ramp-down -----------------------------------------
# The gap this closes: a tripped breaker used to resume at FULL size the
# instant it cleared (daily overnight, weekly via reset-circuit-breaker).

def _fake_client(daily_dd=0.0, weekly_dd=0.0):
    """Minimal stand-in for the Alpaca client, serving whatever drawdown a
    test needs from get_portfolio_history. Equity is synthesized as
    [100, 100*(1+dd)] so _drawdown_pct derives exactly `dd`."""
    def get_portfolio_history(request):
        dd = weekly_dd if request.period == "1W" else daily_dd
        return SimpleNamespace(equity=[100.0, 100.0 * (1 + dd)])
    return SimpleNamespace(get_portfolio_history=get_portfolio_history)


def _write_breaker_state(state):
    pt.CIRCUIT_BREAKER_PATH.write_text(json.dumps(state))


def _hours_ago_iso(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_ramp_multiplier_is_full_size_with_no_trip_history():
    assert pt.circuit_breaker_ramp_multiplier() == 1.0


def test_ramp_multiplier_reduced_inside_the_window_after_a_trip():
    _write_breaker_state({"last_trip_at": _hours_ago_iso(1)})
    assert pt.circuit_breaker_ramp_multiplier() == rp.CIRCUIT_BREAKER_RAMP_MULTIPLIER


def test_ramp_multiplier_returns_to_full_size_after_the_window():
    _write_breaker_state({"last_trip_at": _hours_ago_iso(rp.CIRCUIT_BREAKER_RAMP_HOURS + 1)})
    assert pt.circuit_breaker_ramp_multiplier() == 1.0


def test_ramp_multiplier_survives_a_corrupt_state_file():
    pt.CIRCUIT_BREAKER_PATH.write_text("{not json")
    assert pt.circuit_breaker_ramp_multiplier() == 1.0


def test_daily_trip_is_recorded_even_though_it_does_not_stick():
    """The daily breaker auto-clears and previously left no trace at all -
    so there was nothing for a post-clear ramp to measure from."""
    tripped, reason = pt.check_circuit_breaker(
        _fake_client(daily_dd=rp.DAILY_DRAWDOWN_CIRCUIT_BREAKER_PCT - 0.01))
    assert tripped is True
    assert "daily drawdown" in reason
    state = json.loads(pt.CIRCUIT_BREAKER_PATH.read_text())
    assert state["last_trip_at"]           # recorded for the ramp
    assert state.get("tripped") is None    # but still not sticky


def test_weekly_trip_is_both_sticky_and_recorded():
    tripped, _ = pt.check_circuit_breaker(
        _fake_client(weekly_dd=rp.WEEKLY_DRAWDOWN_CIRCUIT_BREAKER_PCT - 0.01))
    assert tripped is True
    state = json.loads(pt.CIRCUIT_BREAKER_PATH.read_text())
    assert state["tripped"] is True
    assert state["last_trip_at"]


def test_reset_clears_the_block_but_keeps_the_ramp():
    """The core of this change: clearing the breaker must not be a
    one-command jump back to full-size buys."""
    pt.check_circuit_breaker(_fake_client(weekly_dd=rp.WEEKLY_DRAWDOWN_CIRCUIT_BREAKER_PCT - 0.01))
    pt.cmd_reset_circuit_breaker(SimpleNamespace())

    state = json.loads(pt.CIRCUIT_BREAKER_PATH.read_text())
    assert state.get("tripped") is None                     # no longer blocking
    assert state["last_trip_at"]                             # ramp clock preserved
    assert pt.circuit_breaker_ramp_multiplier() == rp.CIRCUIT_BREAKER_RAMP_MULTIPLIER
    # and the breaker really is clear for a subsequent healthy check
    assert pt.check_circuit_breaker(_fake_client())[0] is False


def test_buy_at_normal_cap_is_rejected_while_ramped(conn, monkeypatch):
    """A trade that would be fine normally is too big during the ramp."""
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    at_normal_cap = round(1000 * rp.MAX_TRADE_PCT, 2)

    assert pt.evaluate_buy(snapshot, "AAPL", at_normal_cap, None, conn)[1] is None

    _write_breaker_state({"last_trip_at": _hours_ago_iso(1)})
    score, reason = pt.evaluate_buy(snapshot, "AAPL", at_normal_cap, None, conn)
    assert "max trade size" in reason
    assert "ramped to 50%" in reason


def test_halved_buy_still_allowed_while_ramped(conn, monkeypatch):
    """The ramp reduces size; it does not block buying outright."""
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    # Small existing position, so the ramped POSITION cap isn't what binds -
    # this test is about the per-trade cap specifically.
    positions = {"AAPL": make_position(market_value=50.0)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    _write_breaker_state({"last_trip_at": _hours_ago_iso(1)})

    ramped_cap = round(1000 * rp.MAX_TRADE_PCT * rp.CIRCUIT_BREAKER_RAMP_MULTIPLIER, 2)
    assert pt.evaluate_buy(snapshot, "AAPL", ramped_cap, None, conn)[1] is None


def test_position_cap_is_also_ramped(conn, monkeypatch):
    """Both size caps ramp, not just per-trade: otherwise repeated small
    buys would rebuild a full-size position during the cooldown."""
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    # Held value sits between the ramped and normal position caps.
    held = 1000 * rp.MAX_POSITION_PCT * rp.CIRCUIT_BREAKER_RAMP_MULTIPLIER + 10
    positions = {"AAPL": make_position(market_value=held)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)

    assert pt.evaluate_buy(snapshot, "AAPL", 20.0, None, conn)[1] is None

    _write_breaker_state({"last_trip_at": _hours_ago_iso(1)})
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 20.0, None, conn)
    assert "max position size" in reason


def test_ramp_expires_and_full_size_buys_resume(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    _write_breaker_state({"last_trip_at": _hours_ago_iso(rp.CIRCUIT_BREAKER_RAMP_HOURS + 1)})

    at_normal_cap = round(1000 * rp.MAX_TRADE_PCT, 2)
    assert pt.evaluate_buy(snapshot, "AAPL", at_normal_cap, None, conn)[1] is None


def test_rotation_buy_leg_inherits_the_ramp(conn, monkeypatch):
    """Rotation funds a new position through evaluate_buy, so it must not be
    a way around the reduced caps."""
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    scores = {"JPM": 0.50, "MRNA": 0.90}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {"JPM": make_position(market_value=300.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    at_normal_cap = round(1000 * rp.MAX_TRADE_PCT_NEW_TICKER, 2)

    assert pt.evaluate_rotation(snapshot, "JPM", "MRNA", at_normal_cap, None, conn)[2] is None

    _write_breaker_state({"last_trip_at": _hours_ago_iso(1)})
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", at_normal_cap, None, conn)
    assert "buy leg would fail" in reason
    assert "ramped to 50%" in reason


def test_stop_loss_sells_are_never_ramped(conn, monkeypatch):
    """Getting out must never be throttled - evaluate_sell has no size cap
    to ramp, and the ramp must not have introduced one."""
    _write_breaker_state({"last_trip_at": _hours_ago_iso(1)})
    positions = {"AAPL": make_position(market_value=500.0)}
    snapshot = make_snapshot(cash=100, portfolio_value=1000, positions=positions)
    assert pt.evaluate_sell(snapshot, "AAPL", 500.0) is None
