"""
Unit tests for format_recent_outcomes.py - the outcomes -> prompt-text
formatter injected into research_prompt.md via run_cycle.sh.
"""

import pytest

from db import get_connection
import review_outcomes as ro
import format_recent_outcomes as fro


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    ro.init_outcomes_table(c)
    yield c
    c.close()


def _insert_outcome(conn, decision_id, ticker, action, pct_change, review_ts):
    conn.execute(
        """INSERT INTO outcomes
           (decision_id, ticker, action, decision_timestamp_utc, review_timestamp_utc,
            days_elapsed, price_at_decision, price_at_review, pct_change, technical_score_at_decision)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (decision_id, ticker, action, "2026-08-01T00:00:00+00:00", review_ts,
         7.0, 100.0, 100.0 * (1 + pct_change), pct_change, 0.75),
    )
    conn.commit()


def test_no_rows_returns_graceful_message(conn):
    assert fro.format_recent_outcomes(conn) == (
        "No completed outcomes yet - the review job hasn't produced any results."
    )


def test_formats_rows_with_correct_verdicts(conn):
    _insert_outcome(conn, 1, "AAPL", "buy", 0.05, "2026-08-10T00:00:00+00:00")   # good
    _insert_outcome(conn, 2, "JPM", "sell", 0.03, "2026-08-11T00:00:00+00:00")   # questionable
    _insert_outcome(conn, 3, "XOM", "sell", -0.02, "2026-08-12T00:00:00+00:00")  # good

    text = fro.format_recent_outcomes(conn)

    assert "3 reviewed decision" in text
    assert "AAPL (buy): +5.0% — looks good in hindsight" in text
    assert "JPM (sell): +3.0% — looks questionable in hindsight" in text
    assert "XOM (sell): -2.0% — looks good in hindsight" in text


def test_orders_most_recent_first_and_respects_limit(conn):
    for i in range(3):
        _insert_outcome(conn, i, f"T{i}", "buy", 0.01, f"2026-08-{10 + i:02d}T00:00:00+00:00")

    text = fro.format_recent_outcomes(conn, limit=2)

    assert "2 reviewed decision" in text
    assert "T2" in text and "T1" in text
    assert "T0" not in text
