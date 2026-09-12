"""
Unit tests for research_scorecard.py - the report-only research-quality
scorecard. All offline: an in-memory decisions+outcomes db and a tmp_path
stand-in for the Claude Code session-transcript directory. The SPY
buy-and-hold baseline is fed by a stubbed price_near, never the network.
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


@pytest.fixture(autouse=True)
def no_network_benchmark(monkeypatch):
    """Default every test to an unpriceable benchmark, so only the tests that
    explicitly stub SPY prices exercise the baseline comparison - and none of
    them can silently reach Alpaca."""
    monkeypatch.setattr(rs.ro, "price_near", lambda ticker, target_dt: None)


def _stub_benchmark(monkeypatch, price_by_iso):
    """Price SPY from a {timestamp_iso: close} map, so a test states the
    benchmark's move over the decision window directly."""
    monkeypatch.setattr(
        rs.ro, "price_near",
        lambda ticker, target_dt: price_by_iso.get(target_dt.isoformat()),
    )


def _insert_decision(conn, run_id, ticker, action, score, rationale="test rationale",
                      order_id="order-1"):
    """order_id=None models a --dry-run: approved and logged, but no order
    was ever placed."""
    log_decision(conn, (
        "2026-08-01T00:00:00+00:00", run_id, ticker, action, "notional", 40.0,
        score, rationale, None, 1, None, order_id,
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


# --- is_scheduled_run --------------------------------------------------------

def test_scheduled_run_ids_are_the_ones_run_cycle_generates():
    assert rs.is_scheduled_run("2026-08-20T134505") is True
    assert rs.is_scheduled_run("2026-09-11T194501") is True


def test_hand_run_test_ids_are_not_scheduled():
    for run_id in ("cycle-test-2", "verify-real-1", "rotate-test-3",
                    "collision-test-1", "cooldown-removed-test", "", None):
        assert rs.is_scheduled_run(run_id) is False


def test_scheduled_run_id_must_match_the_whole_string():
    """A test run_id that merely embeds a timestamp isn't a real cycle."""
    assert rs.is_scheduled_run("test-2026-08-20T134505") is False
    assert rs.is_scheduled_run("2026-08-20T134505-retry") is False


# --- beats_baseline ----------------------------------------------------------

def test_buy_that_outran_the_benchmark_beats_baseline():
    assert rs.beats_baseline("buy", 0.08, 0.03) is True


def test_buy_that_rose_less_than_the_benchmark_fails_baseline():
    """The case direction_correct alone gets wrong: up 1% is 'correct', but
    the same money in SPY would have made 8%."""
    assert rs.direction_correct("buy", 0.01) is True
    assert rs.beats_baseline("buy", 0.01, 0.08) is False


def test_sell_of_an_underperformer_beats_baseline():
    assert rs.beats_baseline("sell", -0.05, 0.02) is True


def test_sell_of_a_ticker_that_outran_the_benchmark_fails_baseline():
    assert rs.beats_baseline("sell", 0.09, 0.02) is False


def test_sell_that_rose_less_than_the_benchmark_beats_baseline_despite_wrong_direction():
    """The mirror case: the ticker rose, so direction_correct calls the sell
    wrong, but it still dodged relative underperformance vs the market."""
    assert rs.direction_correct("sell", 0.02) is False
    assert rs.beats_baseline("sell", 0.02, 0.09) is True


def test_beats_baseline_none_when_either_side_unpriceable():
    assert rs.beats_baseline("buy", None, 0.03) is None
    assert rs.beats_baseline("buy", 0.03, None) is None


# --- benchmark_pct_change -----------------------------------------------------

def test_benchmark_pct_change_computes_return_over_the_decision_window(monkeypatch):
    _stub_benchmark(monkeypatch, {
        "2026-08-01T00:00:00+00:00": 100.0,
        "2026-08-08T00:00:00+00:00": 104.0,
    })
    result = rs.benchmark_pct_change("2026-08-01T00:00:00+00:00", "2026-08-08T00:00:00+00:00", {})
    assert result == pytest.approx(0.04)


def test_benchmark_pct_change_none_when_unpriceable(monkeypatch):
    _stub_benchmark(monkeypatch, {})
    assert rs.benchmark_pct_change("2026-08-01T00:00:00+00:00", "2026-08-08T00:00:00+00:00", {}) is None


def test_benchmark_pct_change_caches_repeated_lookups(monkeypatch):
    """Decisions from one cycle share a decided_at and decisions reviewed in
    one weekly batch share a reviewed_at - those repeats must not each hit
    the price feed again."""
    calls = []

    def counting_price_near(ticker, target_dt):
        calls.append((ticker, target_dt.isoformat()))
        return 100.0 if target_dt.isoformat().startswith("2026-08-01") else 105.0

    monkeypatch.setattr(rs.ro, "price_near", counting_price_near)
    cache = {}
    for _ in range(3):
        rs.benchmark_pct_change("2026-08-01T00:00:00+00:00", "2026-08-08T00:00:00+00:00", cache)
    assert len(calls) == 2  # one per distinct timestamp, not per call


# --- build_scorecard ---------------------------------------------------------

def test_build_scorecard_empty(conn):
    scorecard = rs.build_scorecard(conn, session_index={})
    assert scorecard["n_reviewed_decisions"] == 0
    assert scorecard["accuracy_by_action"]["buy"]["rate"] is None


def test_build_scorecard_aggregates_by_action_and_bucket(conn):
    d1 = _insert_decision(conn, "2026-08-20T134505", "AAPL", "buy", 0.80)
    d2 = _insert_decision(conn, "2026-08-21T134505", "JPM", "buy", 0.80)
    d3 = _insert_decision(conn, "2026-08-22T134505", "XOM", "sell", 0.60)
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


def test_build_scorecard_scores_decisions_against_the_benchmark(conn, monkeypatch):
    """Both buys rose, so both are direction-correct - but only one beat the
    +6% market over the same window. That gap is the whole point of the
    baseline comparison."""
    _stub_benchmark(monkeypatch, {
        "2026-08-01T00:00:00+00:00": 100.0,
        "2026-08-08T00:00:00+00:00": 106.0,
    })
    d1 = _insert_decision(conn, "2026-08-20T134505", "AAPL", "buy", 0.80)
    d2 = _insert_decision(conn, "2026-08-21T134505", "JPM", "buy", 0.80)
    _insert_outcome(conn, d1, "AAPL", "buy", 0.10)   # +10% vs +6% market -> added value
    _insert_outcome(conn, d2, "JPM", "buy", 0.01)    # +1% vs +6% market -> lagged it

    scorecard = rs.build_scorecard(conn, session_index={})

    assert scorecard["accuracy_by_action"]["buy"]["rate"] == pytest.approx(1.0)
    assert scorecard["beats_baseline_by_action"]["buy"]["n"] == 2
    assert scorecard["beats_baseline_by_action"]["buy"]["rate"] == pytest.approx(0.5)
    assert scorecard["avg_excess_vs_benchmark"] == pytest.approx((0.04 + -0.05) / 2)

    by_ticker = {d["ticker"]: d for d in scorecard["decisions"]}
    assert by_ticker["AAPL"]["beats_baseline"] is True
    assert by_ticker["AAPL"]["excess_vs_benchmark"] == pytest.approx(0.04)
    assert by_ticker["JPM"]["beats_baseline"] is False
    assert by_ticker["JPM"]["benchmark_pct_change"] == pytest.approx(0.06)


def test_build_scorecard_leaves_baseline_unscored_when_benchmark_unpriceable(conn):
    """A benchmark the price feed can't serve must not silently count as a
    loss - it drops out of the rate entirely, same as pct_change does."""
    d1 = _insert_decision(conn, "2026-08-20T134505", "AAPL", "buy", 0.80)
    _insert_outcome(conn, d1, "AAPL", "buy", 0.05)

    scorecard = rs.build_scorecard(conn, session_index={})

    assert scorecard["accuracy_by_action"]["buy"]["n"] == 1       # direction still scored
    assert scorecard["beats_baseline_by_action"]["buy"]["n"] == 0  # baseline not
    assert scorecard["beats_baseline_by_action"]["buy"]["rate"] is None
    assert scorecard["avg_excess_vs_benchmark"] is None
    assert scorecard["decisions"][0]["beats_baseline"] is None


def test_build_scorecard_excludes_hand_run_test_decisions_from_rates(conn):
    """The contamination this guard exists for: a test row whose outcome was
    a loss must not drag down the rate that's read as evidence about the
    research agent's judgment."""
    real = _insert_decision(conn, "2026-08-20T134505", "IBIT", "buy", 0.80)
    test = _insert_decision(conn, "collision-test-1", "MRNA", "buy", 0.80)
    _insert_outcome(conn, real, "IBIT", "buy", 0.10)    # scheduled: correct
    _insert_outcome(conn, test, "MRNA", "buy", -0.10)   # hand-run: incorrect

    scorecard = rs.build_scorecard(conn, session_index={})

    assert scorecard["n_reviewed_decisions"] == 2
    assert scorecard["n_scheduled_decisions"] == 1
    assert scorecard["n_excluded_hand_run"] == 1
    # 100%, not 50% - the test row is reported but never scored
    assert scorecard["accuracy_by_action"]["buy"]["n"] == 1
    assert scorecard["accuracy_by_action"]["buy"]["rate"] == pytest.approx(1.0)
    by_ticker = {d["ticker"]: d for d in scorecard["decisions"]}
    assert by_ticker["MRNA"]["scheduled_run"] is False
    assert by_ticker["IBIT"]["scheduled_run"] is True


def test_build_scorecard_excludes_dry_runs_even_from_scheduled_cycles(conn):
    """A real cycle run with --dry-run logs an approved decision that never
    executed - a price move after a trade nobody made is not evidence."""
    executed = _insert_decision(conn, "2026-08-20T134505", "IBIT", "buy", 0.80)
    dry_run = _insert_decision(conn, "2026-08-21T134505", "MRNA", "buy", 0.80, order_id=None)
    _insert_outcome(conn, executed, "IBIT", "buy", 0.10)
    _insert_outcome(conn, dry_run, "MRNA", "buy", 0.10)

    scorecard = rs.build_scorecard(conn, session_index={})

    assert scorecard["n_scheduled_decisions"] == 1
    assert scorecard["n_excluded_dry_run"] == 1
    assert scorecard["accuracy_by_action"]["buy"]["n"] == 1
    by_ticker = {d["ticker"]: d for d in scorecard["decisions"]}
    assert by_ticker["MRNA"]["executed"] is False
    assert by_ticker["MRNA"]["counts_as_evidence"] is False
    assert by_ticker["IBIT"]["counts_as_evidence"] is True


def test_build_scorecard_excludes_hand_run_orders_that_really_executed(conn):
    """The mirror of the dry-run case: 'verify-real-1' placed a genuine
    order, but a human chose it, so it says nothing about agent judgment."""
    hand_run = _insert_decision(conn, "verify-real-1", "JPM", "sell", 0.40)
    _insert_outcome(conn, hand_run, "JPM", "sell", 0.02)

    scorecard = rs.build_scorecard(conn, session_index={})

    assert scorecard["n_scheduled_decisions"] == 0
    row = scorecard["decisions"][0]
    assert row["executed"] is True          # the order was real
    assert row["counts_as_evidence"] is False  # but the decision wasn't the agent's


def test_build_scorecard_counts_distinct_ideas_not_repeated_proposals(conn):
    """The same sell re-proposed across three consecutive cycles is one idea,
    not three independent observations."""
    for run_id in ("2026-08-20T134505", "2026-08-21T134505", "2026-08-22T134505"):
        decision_id = _insert_decision(conn, run_id, "JPM", "sell", 0.40)
        _insert_outcome(conn, decision_id, "JPM", "sell", 0.02)

    scorecard = rs.build_scorecard(conn, session_index={})

    assert scorecard["n_scheduled_decisions"] == 3
    assert scorecard["n_distinct_scheduled_ideas"] == 1


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
