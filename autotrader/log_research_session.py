#!/usr/bin/env python3
"""
Parses one `claude --print --output-format json` result (read from
stdin), prints the same human-readable text run_cycle.sh's log file /
notification / SUMMARY-grep logic has always expected, and durably
records this cycle's session_id - keyed by run_id - in
state/logs/research_sessions.jsonl.

Why this exists: Claude Code already persists the FULL research
transcript for every cycle automatically - every WebSearch/WebFetch/Bash
call and its result - at
~/.claude/projects/-Users-MichaelBazzi-trading-project/<session_id>.jsonl.
That was never actually missing. What was missing is any durable link
from a trading cycle's run_id (what trades.db's decisions are keyed by)
to the session_id of the transcript that produced it - without it,
finding "what did the research agent actually see and search before this
decision" means guessing from file timestamps. This closes that gap with
one line of index, not by trying to recapture data Claude Code was
already recording.

Usage: <claude output> | log_research_session.py RUN_ID

Falls back to printing stdin verbatim (and recording session_id as
"unknown") if the input isn't valid JSON - a malformed response must
never break the cycle's existing logging/notification behavior, which is
why this is a separate, narrowly-scoped step rather than inline shell
JSON parsing.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_LOG = Path(__file__).resolve().parent / "state" / "logs" / "research_sessions.jsonl"


def parse_result(raw: str) -> dict:
    """Pure parsing: given raw stdin text, returns
    {result_text, session_id, is_error, num_turns, total_cost_usd}.
    Falls back to the raw text verbatim and session_id="unknown" if it
    isn't valid JSON - never raises."""
    parsed = {
        "result_text": raw, "session_id": "unknown",
        "is_error": None, "num_turns": None, "total_cost_usd": None,
    }
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return parsed
    parsed["result_text"] = data.get("result", raw)
    parsed["session_id"] = data.get("session_id", "unknown")
    parsed["is_error"] = data.get("is_error")
    parsed["num_turns"] = data.get("num_turns")
    parsed["total_cost_usd"] = data.get("total_cost_usd")
    return parsed


def record_session(run_id: str, parsed: dict, sessions_log: Path = SESSIONS_LOG) -> None:
    sessions_log.parent.mkdir(parents=True, exist_ok=True)
    with sessions_log.open("a") as f:
        f.write(json.dumps({
            "run_id": run_id,
            "session_id": parsed["session_id"],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "is_error": parsed["is_error"],
            "num_turns": parsed["num_turns"],
            "total_cost_usd": parsed["total_cost_usd"],
        }) + "\n")


def main():
    run_id = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raw = sys.stdin.read()
    parsed = parse_result(raw)
    print(parsed["result_text"])
    print(f"[session_id: {parsed['session_id']}]")
    record_session(run_id, parsed)


if __name__ == "__main__":
    main()
