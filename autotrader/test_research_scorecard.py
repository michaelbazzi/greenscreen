"""
Unit tests for research_scorecard.py - the report-only research-quality
scorecard. All offline: an in-memory decisions+outcomes db and a tmp_path
stand-in for the Claude Code session-transcript directory.
"""

import json

import pytest

from db import get_connection, init_decisions_table, log_decision
import review_outcomes as ro
import research_scorecard as rs


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    init_decisions_table(c)
    ro.init_outcomes_table(c)
    yield c
    c.close()


def _insert_decision(conn, run_id, ticker, action, score, rationale="test rationale"):
    log_decision(conn, (
        "2026-08-01T00:00:00+00:00", run_id, ticker, action, "notional", 40.0,
        score, rationale, None, 1, None, "order-1",
    ))
    conn.commit()
    return conn.execute("SELECT id FROM decisions WHERE run_id = ? AND ticker = ?",
                         (run_id, ticker)).fetchone()[0]


def _insert_outcome(conn, decision_id, ticker, action, pct_change):
    conn.execute(
        """INSERT INTO outcomes
           (decision_id, ticker, action, decision_timestamp_utc, review_timestamp_utc,
            days_elapsed, price_at_decision, price_at_review, pct_change, technical_score_at_decision)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (decision_id, ticker, action, "2026-08-01T00:00:00+00:00", "2026-08-08T00:00:00+00:00",
         7.0, 100.0, 100.0 * (1 + pct_change), pct_change, 0.75),
    )
    conn.commit()


# --- direction_correct / score_bucket ---------------------------------------

def test_direction_correct_buy_up_is_correct():
    assert rs.direction_correct("buy", 0.05) is True


def test_direction_correct_buy_down_is_incorrect():
    assert rs.direction_correct("buy", -0.05) is False


def test_direction_correct_sell_down_is_correct():
    assert rs.direction_correct("sell", -0.05) is True


def test_direction_correct_none_pct_change_is_none():
    assert rs.direction_correct("buy", None) is None


def test_score_bucket_boundaries():
    assert rs.score_bucket(0.3) == "low (<0.5)"
    assert rs.score_bucket(0.6) == "mid (0.5-0.7)"
    assert rs.score_bucket(0.9) == "high (>=0.7)"
    assert rs.score_bucket(None) == "unknown"


# --- build_scorecard ---------------------------------------------------------

def test_build_scorecard_empty(conn):
    scorecard = rs.build_scorecard(conn, session_index={})
    assert scorecard["n_reviewed_decisions"] == 0
    assert scorecard["accuracy_by_action"]["buy"]["rate"] is None


def test_build_scorecard_aggregates_by_action_and_bucket(conn):
    d1 = _insert_decision(conn, "run-1", "AAPL", "buy", 0.80)
    d2 = _insert_decision(conn, "run-2", "JPM", "buy", 0.80)
    d3 = _insert_decision(conn, "run-3", "XOM", "sell", 0.60)
    _insert_outcome(conn, d1, "AAPL", "buy", 0.05)   # buy, price up -> correct
    _insert_outcome(conn, d2, "JPM", "buy", -0.03)   # buy, price down -> incorrect
    _insert_outcome(conn, d3, "XOM", "sell", 0.02)   # sell, price up -> incorrect

    scorecard = rs.build_scorecard(conn, session_index={})

    assert scorecard["n_reviewed_decisions"] == 3
    assert scorecard["accuracy_by_action"]["buy"]["n"] == 2
    assert scorecard["accuracy_by_action"]["buy"]["rate"] == pytest.approx(0.5)
    assert scorecard["accuracy_by_action"]["sell"]["n"] == 1
    assert scorecard["accuracy_by_action"]["sell"]["rate"] == pytest.approx(0.0)
    assert scorecard["accuracy_by_score_bucket"]["high (>=0.7)"]["n"] == 2
    assert scorecard["accuracy_by_score_bucket"]["mid (0.5-0.7)"]["n"] == 1


def test_build_scorecard_links_full_transcript_when_session_indexed(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "CLAUDE_PROJECT_DIR", tmp_path)
    session_id = "sess-abc"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "AAPL looks strong on ETF inflows."}]},
    }) + "\n")

    d1 = _insert_decision(conn, "run-1", "AAPL", "buy", 0.80)
    _insert_outcome(conn, d1, "AAPL", "buy", 0.05)

    scorecard = rs.build_scorecard(conn, session_index={"run-1": session_id})

    assert scorecard["n_with_full_transcript"] == 1
    row = scorecard["decisions"][0]
    assert row["has_full_transcript"] is True
    assert "ETF inflows" in row["research_excerpt"]


def test_build_scorecard_no_transcript_when_run_id_not_indexed(conn):
    d1 = _insert_decision(conn, "run-1", "AAPL", "buy", 0.80)
    _insert_outcome(conn, d1, "AAPL", "buy", 0.05)
    scorecard = rs.build_scorecard(conn, session_index={})
    assert scorecard["decisions"][0]["has_full_transcript"] is False
    assert scorecard["decisions"][0]["research_excerpt"] is None


# --- load_session_index -------------------------------------------------------

def test_load_session_index_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "SESSIONS_LOG", tmp_path / "does_not_exist.jsonl")
    assert rs.load_session_index() == {}


def test_load_session_index_parses_jsonl(tmp_path, monkeypatch):
    sessions_log = tmp_path / "research_sessions.jsonl"
    sessions_log.write_text(
        json.dumps({"run_id": "run-1", "session_id": "sess-a"}) + "\n"
        + json.dumps({"run_id": "run-2", "session_id": "sess-b"}) + "\n"
    )
    monkeypatch.setattr(rs, "SESSIONS_LOG", sessions_log)
    assert rs.load_session_index() == {"run-1": "sess-a", "run-2": "sess-b"}


def test_load_session_index_skips_malformed_lines(tmp_path, monkeypatch):
    sessions_log = tmp_path / "research_sessions.jsonl"
    sessions_log.write_text("not json\n" + json.dumps({"run_id": "run-1", "session_id": "sess-a"}) + "\n")
    monkeypatch.setattr(rs, "SESSIONS_LOG", sessions_log)
    assert rs.load_session_index() == {"run-1": "sess-a"}
