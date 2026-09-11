#!/usr/bin/env python3
"""
Research-quality scorecard: a human-reviewed report, not an automatic
recalibration. Reads the outcomes table (review_outcomes.py's weekly
measurement of what actually happened after each decision) joined with
the decisions that produced them, and reports whether GreenScreen's
decisions have actually been directionally right - broken down by
action, technical-score bucket, and time - plus, for any decision made
after research-session linking went live (see log_research_session.py),
the actual research reasoning behind it, not just the terse rationale
string.

This changes NOTHING automatically. It is the "separate, later,
explicitly-reviewed step" review_outcomes.py's own docstring says would
ever act on outcome data - and even this script doesn't act, it reports,
so a human can decide whether to. Never wire this into anything that
edits risk_params.py or market_data.py without a human reading the
output first: this whole project's own backtest work found that tuning
against recent outcomes without a held-out check produces configurations
that look better and generalize worse (a stop-loss/rotation-edge
combination that won 58% of development windows won only 14% out of
sample) - the same trap applies here, more so, since there's no
held-out split on live data at all yet.

Run on demand:
    /Users/MichaelBazzi/trading-env/bin/python3 autotrader/research_scorecard.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_loader import load_db_path
from db import get_connection

REPO_DIR = Path(__file__).resolve().parent.parent
SESSIONS_LOG = REPO_DIR / "autotrader" / "state" / "logs" / "research_sessions.jsonl"
CLAUDE_PROJECT_DIR = Path.home() / ".claude" / "projects" / "-Users-MichaelBazzi-trading-project"

SCORE_BUCKETS = [(0.0, 0.5, "low (<0.5)"), (0.5, 0.7, "mid (0.5-0.7)"), (0.7, 1.01, "high (>=0.7)")]


def fetch_reviewed_decisions(conn):
    """Every decision that has been outcome-reviewed, joined back to its
    own rationale/technical_score/run_id - the raw material for
    everything below."""
    return conn.execute(
        """SELECT d.id, d.run_id, d.ticker, d.action, d.technical_score, d.rationale,
                  d.source_url, o.decision_timestamp_utc, o.review_timestamp_utc,
                  o.days_elapsed, o.pct_change
           FROM outcomes o
           JOIN decisions d ON o.decision_id = d.id
           ORDER BY o.decision_timestamp_utc"""
    ).fetchall()


def direction_correct(action: str, pct_change: float) -> bool:
    """A buy looks right in hindsight if price rose since; a sell looks
    right if price fell since (correctly got out ahead of a decline).
    Same convention review_outcomes.py itself already uses."""
    if pct_change is None:
        return None
    return pct_change > 0 if action == "buy" else pct_change < 0


def score_bucket(score):
    if score is None:
        return "unknown"
    for lo, hi, label in SCORE_BUCKETS:
        if lo <= score < hi:
            return label
    return "unknown"


def load_session_index():
    """run_id -> session_id, from every cycle logged since
    log_research_session.py went live. Empty (not an error) for a fresh
    install or before that fix landed - most of history has no linked
    transcript yet, which is expected and reported as such, not hidden."""
    if not SESSIONS_LOG.exists():
        return {}
    index = {}
    for line in SESSIONS_LOG.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        index[row["run_id"]] = row.get("session_id")
    return index


def find_ticker_mentions(session_id: str, ticker: str, max_chars: int = 400) -> list:
    """Best-effort excerpt of what the research agent actually said about
    `ticker` in the linked full transcript - not a citation-perfect
    quote, just enough for a human skimming the scorecard to see the
    stated thesis without opening the raw 100+-line JSONL by hand."""
    path = CLAUDE_PROJECT_DIR / f"{session_id}.jsonl"
    if not path.is_file():
        return []
    excerpts = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        message = obj.get("message", {})
        for block in message.get("content", []) if isinstance(message, dict) else []:
            text = block.get("text") if isinstance(block, dict) else None
            if text and ticker in text:
                excerpts.append(text.strip()[:max_chars])
    return excerpts


def build_scorecard(conn, session_index: dict) -> dict:
    rows = fetch_reviewed_decisions(conn)

    by_action = {"buy": [0, 0], "sell": [0, 0]}       # [n_correct, n_total]
    by_bucket = {}                                     # label -> [n_correct, n_total]
    decisions_out = []

    for row in rows:
        (dec_id, run_id, ticker, action, score, rationale, source_url,
         decided_at, reviewed_at, days_elapsed, pct_change) = row
        correct = direction_correct(action, pct_change)
        bucket = score_bucket(score)

        if action in by_action and correct is not None:
            by_action[action][1] += 1
            by_action[action][0] += int(correct)
        if correct is not None:
            by_bucket.setdefault(bucket, [0, 0])
            by_bucket[bucket][1] += 1
            by_bucket[bucket][0] += int(correct)

        session_id = session_index.get(run_id)
        research_excerpt = find_ticker_mentions(session_id, ticker) if session_id else []

        decisions_out.append({
            "decision_id": dec_id, "run_id": run_id, "ticker": ticker, "action": action,
            "technical_score": score, "rationale": rationale, "source_url": source_url,
            "decided_at": decided_at, "days_elapsed": round(days_elapsed, 1) if days_elapsed else None,
            "pct_change": pct_change, "direction_correct": correct,
            "has_full_transcript": session_id is not None,
            "research_excerpt": research_excerpt[0] if research_excerpt else None,
        })

    def rate(pair):
        n_correct, n_total = pair
        return (n_correct / n_total) if n_total else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_reviewed_decisions": len(rows),
        "n_with_full_transcript": sum(1 for d in decisions_out if d["has_full_transcript"]),
        "accuracy_by_action": {a: {"n": v[1], "rate": rate(v)} for a, v in by_action.items()},
        "accuracy_by_score_bucket": {b: {"n": v[1], "rate": rate(v)} for b, v in by_bucket.items()},
        "decisions": decisions_out,
    }


def print_report(scorecard: dict):
    print("=" * 78)
    print("GreenScreen research-quality scorecard (report only - changes nothing)")
    print("=" * 78)
    print(f"Reviewed decisions: {scorecard['n_reviewed_decisions']}  "
          f"(with a full linked research transcript: {scorecard['n_with_full_transcript']})")
    if scorecard["n_reviewed_decisions"] == 0:
        print("\nNo outcome-reviewed decisions yet - review_outcomes.py only reviews "
              "decisions >= 7 days old. Nothing to report until some have aged past that.")
        return

    print("\nDirectional accuracy by action:")
    for action, stats in scorecard["accuracy_by_action"].items():
        rate_str = f"{stats['rate']:.0%}" if stats["rate"] is not None else "n/a"
        print(f"  {action:6s} n={stats['n']:3d}  correct={rate_str}")

    print("\nDirectional accuracy by technical-score bucket at decision time:")
    for bucket, stats in scorecard["accuracy_by_score_bucket"].items():
        rate_str = f"{stats['rate']:.0%}" if stats["rate"] is not None else "n/a"
        print(f"  {bucket:16s} n={stats['n']:3d}  correct={rate_str}")

    print(f"\nSUMMARY: {scorecard['n_reviewed_decisions']} decisions reviewed, "
          f"{scorecard['n_with_full_transcript']} with full research transcripts linked; "
          f"buy accuracy {scorecard['accuracy_by_action']['buy']['rate']}, "
          f"sell accuracy {scorecard['accuracy_by_action']['sell']['rate']}."
          if scorecard["accuracy_by_action"]["buy"]["rate"] is not None
          and scorecard["accuracy_by_action"]["sell"]["rate"] is not None
          else f"\nSUMMARY: {scorecard['n_reviewed_decisions']} decisions reviewed so far.")


def main():
    conn = get_connection(load_db_path())
    session_index = load_session_index()
    scorecard = build_scorecard(conn, session_index)
    conn.close()

    print_report(scorecard)

    out_dir = Path(__file__).resolve().parent / "state" / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"research_scorecard_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(scorecard, indent=2, default=str))
    print(f"\nFull report written to {out_path}")


if __name__ == "__main__":
    main()
