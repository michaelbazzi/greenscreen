"""
Unit tests for review_outcomes.py - the outcome-tracking prerequisite for
any future recalibration. Market data is mocked; only the review logic
itself (eligibility filtering, outcome computation, verdicts) is tested.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db import get_connection, init_decisions_table, log_decision
import review_outcomes as ro


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    init_decisions_table(c)
    ro.init_outcomes_table(c)
    yield c
    c.close()


def _log(conn, ticker, action, days_ago, passed=1, score=0.75, order_id="order-1"):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    log_decision(conn, (
        ts, f"run-{ticker}-{days_ago}", ticker, action, "notional", 40.0,
        score, "test", None, passed, None if passed else "rejected",
        order_id if passed else None,
    ))
    return ts


def test_reviewable_decisions_excludes_dry_runs(conn):
    """A --dry-run logs an approved decision but places no order, so there
    was never a position and there is no outcome to measure."""
    _log(conn, "AAPL", "buy", days_ago=10, order_id=None)  # dry-run
    _log(conn, "MSFT", "buy", days_ago=10)                  # really executed
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = ro.find_reviewable_decisions(conn, cutoff)
    assert [r[1] for r in rows] == ["MSFT"]


def test_reviewable_decisions_excludes_too_recent(conn):
    _log(conn, "AAPL", "buy", days_ago=2)  # too recent
    _log(conn, "MSFT", "buy", days_ago=10)  # eligible
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = ro.find_reviewable_decisions(conn, cutoff)
    tickers = [r[1] for r in rows]
    assert tickers == ["MSFT"]


def test_reviewable_decisions_excludes_rejected(conn):
    _log(conn, "AAPL", "buy", days_ago=10, passed=0)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = ro.find_reviewable_decisions(conn, cutoff)
    assert rows == []


def test_reviewable_decisions_excludes_already_reviewed(conn, monkeypatch):
    ts = _log(conn, "AAPL", "buy", days_ago=10)
    monkeypatch.setattr(ro.md, "get_bars", lambda ticker, lookback_days=None: [
        SimpleNamespace(timestamp=datetime.now(timezone.utc) - timedelta(days=11), close=100.0),
    ])
    monkeypatch.setattr(ro.md, "compute_signals", lambda ticker: {"current_price": 110.0})
    rows = ro.find_reviewable_decisions(conn, (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    decision_id, ticker, action, ts, score = rows[0]
    ro.review_one(conn, decision_id, ticker, action, ts, score)

    rows_again = ro.find_reviewable_decisions(conn, (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    assert rows_again == []


def test_review_one_computes_correct_pct_change_and_logs_outcome(conn, monkeypatch):
    ts = _log(conn, "AAPL", "buy", days_ago=10, score=0.80)
    monkeypatch.setattr(ro.md, "get_bars", lambda ticker, lookback_days=None: [
        SimpleNamespace(timestamp=datetime.now(timezone.utc) - timedelta(days=11), close=100.0),
    ])
    monkeypatch.setattr(ro.md, "compute_signals", lambda ticker: {"current_price": 110.0})

    ro.review_one(conn, 1, "AAPL", "buy", ts, 0.80)

    row = conn.execute(
        "SELECT ticker, action, price_at_decision, price_at_review, pct_change, technical_score_at_decision "
        "FROM outcomes WHERE decision_id = 1"
    ).fetchone()
    assert row == ("AAPL", "buy", 100.0, 110.0, pytest.approx(0.10), 0.80)


def test_action_breakdown_splits_by_type():
    results = [
        {"ticker": "AAPL", "action": "buy", "pct_change": 0.05, "looks_good": True},
        {"ticker": "MSFT", "action": "buy", "pct_change": -0.02, "looks_good": False},
        {"ticker": "JPM", "action": "sell", "pct_change": 0.03, "looks_good": False},
        {"ticker": "XOM", "action": "sell", "pct_change": -0.01, "looks_good": True},
        {"ticker": "JNJ", "action": "sell", "pct_change": 0.02, "looks_good": False},
    ]
    assert ro._action_breakdown(results) == {"buy": (1, 2), "sell": (1, 3)}


def test_action_breakdown_omits_absent_action_type():
    results = [{"ticker": "AAPL", "action": "buy", "pct_change": 0.05, "looks_good": True}]
    breakdown = ro._action_breakdown(results)
    assert breakdown == {"buy": (1, 1)}
    assert "sell" not in breakdown


def test_format_breakdown_renders_readable_fragment():
    assert ro._format_breakdown({"buy": (3, 3), "sell": (0, 10)}) == "buys: 3/3, sells: 0/10"
    assert ro._format_breakdown({"buy": (1, 1)}) == "buys: 1/1"
    assert ro._format_breakdown({}) == ""


def test_review_one_skips_when_price_unavailable(conn, monkeypatch):
    ts = _log(conn, "AAPL", "buy", days_ago=10)
    monkeypatch.setattr(ro.md, "get_bars", lambda ticker, lookback_days=None: [])
    monkeypatch.setattr(ro.md, "compute_signals", lambda ticker: None)

    ro.review_one(conn, 1, "AAPL", "buy", ts, 0.75)

    count = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    assert count == 0
