"""
Unit tests for log_research_session.py - the run_cycle.sh step that links
a trading cycle's run_id to the session_id of the full research
transcript Claude Code already persists automatically, and preserves the
same human-readable result text the log/notification/SUMMARY-grep logic
has always relied on.
"""

import json

import log_research_session as lrs


def test_parse_result_valid_json():
    raw = json.dumps({
        "result": "did nothing this cycle\n\nSUMMARY: no trades",
        "session_id": "abc-123", "is_error": False, "num_turns": 12, "total_cost_usd": 0.42,
    })
    parsed = lrs.parse_result(raw)
    assert parsed["result_text"] == "did nothing this cycle\n\nSUMMARY: no trades"
    assert parsed["session_id"] == "abc-123"
    assert parsed["is_error"] is False
    assert parsed["num_turns"] == 12
    assert parsed["total_cost_usd"] == 0.42


def test_parse_result_falls_back_on_malformed_json():
    """A malformed/empty response must never crash the cycle - fall back
    to the raw text verbatim so the log still shows SOMETHING, and mark
    the session unknown rather than guessing."""
    raw = "Error: something went wrong, not JSON at all"
    parsed = lrs.parse_result(raw)
    assert parsed["result_text"] == raw
    assert parsed["session_id"] == "unknown"
    assert parsed["is_error"] is None


def test_parse_result_missing_fields_default_gracefully():
    raw = json.dumps({"result": "ok"})  # no session_id/is_error/etc.
    parsed = lrs.parse_result(raw)
    assert parsed["result_text"] == "ok"
    assert parsed["session_id"] == "unknown"


def test_record_session_appends_one_jsonl_line_per_call(tmp_path):
    sessions_log = tmp_path / "research_sessions.jsonl"
    parsed_a = lrs.parse_result(json.dumps({"result": "a", "session_id": "sess-a"}))
    parsed_b = lrs.parse_result(json.dumps({"result": "b", "session_id": "sess-b"}))

    lrs.record_session("run-1", parsed_a, sessions_log=sessions_log)
    lrs.record_session("run-2", parsed_b, sessions_log=sessions_log)

    lines = sessions_log.read_text().strip().split("\n")
    assert len(lines) == 2
    row1, row2 = json.loads(lines[0]), json.loads(lines[1])
    assert row1["run_id"] == "run-1" and row1["session_id"] == "sess-a"
    assert row2["run_id"] == "run-2" and row2["session_id"] == "sess-b"
