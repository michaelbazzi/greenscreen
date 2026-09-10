"""
Unit tests for research_agent.py - the tool functions in isolation, the
loop's termination behavior, and the safety property that matters most:
there is no path from a tool name to anything beyond the four defined
tools, because _execute_tool simply doesn't implement anything else.
"""

from types import SimpleNamespace

import pytest

import research_agent as ra


def test_get_technical_score_returns_real_shape(monkeypatch):
    monkeypatch.setattr(
        ra.md, "compute_signals", lambda ticker: {"current_price": 123.45, "sma20": 120.0}
    )
    monkeypatch.setattr(ra.md, "technical_score", lambda signals: 0.71)

    result = ra._tool_get_technical_score({"ticker": "aapl"})
    assert result == {"ticker": "AAPL", "score": 0.71, "current_price": 123.45}


def test_get_technical_score_handles_insufficient_history(monkeypatch):
    monkeypatch.setattr(ra.md, "compute_signals", lambda ticker: None)
    result = ra._tool_get_technical_score({"ticker": "IPOXYZ"})
    assert "error" in result


def test_screen_candidates_passes_through(monkeypatch):
    fake_result = {"most_active": [{"symbol": "NVDA"}], "gainers": [], "losers": []}
    monkeypatch.setattr(ra.sc, "get_candidates", lambda top: fake_result)
    assert ra._tool_screen_candidates({"top": 5}) == fake_result


def test_web_search_without_key_configured_fails_gracefully():
    result = ra._tool_web_search({"query": "AAPL sentiment"}, search_api_key=None)
    assert "error" in result


def test_web_search_with_key_hits_tavily(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"title": "t", "url": "u", "content": "c"}]}

    monkeypatch.setattr(ra.requests if hasattr(ra, "requests") else __import__("requests"), "post", lambda *a, **k: FakeResponse())
    result = ra._tool_web_search({"query": "AAPL sentiment"}, search_api_key="fake-key")
    assert result["results"] == [{"title": "t", "url": "u", "content": "c"}]


def test_propose_builds_correct_subprocess_command(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(stdout="APPROVED and submitted: buy AAPL $50.00, order_id=abc", stderr="")

    monkeypatch.setattr(ra.subprocess, "run", fake_run)
    result = ra._tool_propose(
        {"ticker": "aapl", "side": "buy", "notional": 50.0, "rationale": "test rationale"},
        run_id="run-123",
    )

    cmd = captured["cmd"]
    assert "propose" in cmd
    assert "buy" in cmd
    assert "AAPL" in cmd  # uppercased
    assert "run-123" in cmd
    assert result["approved"] is True


def test_propose_reports_rejection_correctly(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(stdout="REJECTED: buy AAPL $50.00 — technical score 0.40 below threshold 0.60", stderr="")

    monkeypatch.setattr(ra.subprocess, "run", fake_run)
    result = ra._tool_propose(
        {"ticker": "AAPL", "side": "buy", "notional": 50.0, "rationale": "test"},
        run_id="run-123",
    )
    assert result["approved"] is False
    assert "REJECTED" in result["output"]


def test_execute_tool_unknown_name_is_a_dead_end():
    """The actual safety boundary: an unrecognized tool name (whatever a
    model might hallucinate or be tricked into requesting) returns an
    error, not an exception, and definitely not access to anything."""
    result = ra._execute_tool("read_file", {"path": "/etc/passwd"}, run_id="x", search_api_key=None)
    assert result == {"error": "unknown tool: read_file"}


def test_exactly_four_tools_are_exposed():
    names = {t["function"]["name"] for t in ra.TOOLS}
    assert names == {"screen_candidates", "get_technical_score", "web_search", "propose"}


class _FakeToolCall:
    def __init__(self, name, arguments, call_id="call_1"):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self):
        return {"role": "assistant", "content": self.content}


class _FakeResponse:
    def __init__(self, message):
        self.choices = [SimpleNamespace(message=message)]


def test_loop_terminates_when_model_stops_calling_tools(monkeypatch):
    call_count = {"n": 0}

    def fake_completion(model, api_key, messages, tools):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeResponse(_FakeMessage(tool_calls=[_FakeToolCall("screen_candidates", "{}")]))
        return _FakeResponse(_FakeMessage(content="Nothing looked strong enough today.", tool_calls=None))

    monkeypatch.setattr(ra.litellm, "completion", fake_completion)
    monkeypatch.setattr(ra.sc, "get_candidates", lambda top=15: {"most_active": [], "gainers": [], "losers": []})

    result = ra.run_research_cycle("anthropic/claude", "fake-key", "run-1", "research something")
    assert result["summary"] == "Nothing looked strong enough today."
    assert len(result["tool_calls"]) == 1
    assert call_count["n"] == 2


def test_loop_respects_max_tool_call_cap(monkeypatch):
    def fake_completion(model, api_key, messages, tools):
        return _FakeResponse(_FakeMessage(tool_calls=[_FakeToolCall("screen_candidates", "{}")]))

    monkeypatch.setattr(ra.litellm, "completion", fake_completion)
    monkeypatch.setattr(ra.sc, "get_candidates", lambda top=15: {"most_active": [], "gainers": [], "losers": []})

    result = ra.run_research_cycle("anthropic/claude", "fake-key", "run-1", "keep looking forever")
    assert len(result["tool_calls"]) == ra.MAX_TOOL_CALLS
    assert "cap" in result["summary"]
