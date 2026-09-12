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

Direction-correct alone can't tell you whether a decision actually added
value: a buy that rose 1% while the market rose 8% over the same window
"looks correct" but would have been better left as an index purchase. So
every reviewed decision is also scored against a SPY buy-and-hold
baseline over the identical [decided_at, reviewed_at] window - see
benchmark_pct_change()/beats_baseline() below. That's the "do-nothing"
counterfactual this scorecard didn't previously capture: not "did the
ticker move the guessed direction" but "did this decision beat simply
holding the market instead."

Run on demand:
    /Users/MichaelBazzi/trading-env/bin/python3 autotrader/research_scorecard.py
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_loader import load_db_path
from db import get_connection
import review_outcomes as ro

REPO_DIR = Path(__file__).resolve().parent.parent
SESSIONS_LOG = REPO_DIR / "autotrader" / "state" / "logs" / "research_sessions.jsonl"
CLAUDE_PROJECT_DIR = Path.home() / ".claude" / "projects" / "-Users-MichaelBazzi-trading-project"

SCORE_BUCKETS = [(0.0, 0.5, "low (<0.5)"), (0.5, 0.7, "mid (0.5-0.7)"), (0.7, 1.01, "high (>=0.7)")]

# The "simply holding the market instead" counterfactual. SPY rather than a
# cash/0% baseline: a cash baseline collapses to the same direction_correct
# check already computed above (buy beats cash iff pct_change > 0, sell
# beats cash iff pct_change < 0), so it's not new information. SPY beating
# or losing to the ticker over the same window is.
BASELINE_TICKER = "SPY"

# run_cycle.sh names every real cycle with `date -u +%Y-%m-%dT%H%M%S`.
# Anything else ("cycle-test-2", "rotate-test-3", "collision-test-1", ...)
# came from a human running propose_trade.py by hand to exercise the
# guardrails. Those decisions are real rows with real outcomes, but they are
# NOT the research agent exercising judgment, and counting them as if they
# were inflates the only evidence base there is for whether it has any edge.
# As of 2026-09-12 that mattered a lot: 8 of the 13 outcome-reviewed
# decisions were test-harness rows, and the remaining 5 were 3 distinct
# ideas. Headline rates below are scheduled-only; manual rows are still
# reported, just separately, so nothing is hidden - only un-conflated.
SCHEDULED_RUN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}$")


def is_scheduled_run(run_id) -> bool:
    """True for a run_id run_cycle.sh generated, False for a hand-run test."""
    return bool(run_id) and bool(SCHEDULED_RUN_ID.match(str(run_id)))


def fetch_reviewed_decisions(conn):
    """Every decision that has been outcome-reviewed, joined back to its
    own rationale/technical_score/run_id - the raw material for
    everything below."""
    return conn.execute(
        """SELECT d.id, d.run_id, d.ticker, d.action, d.technical_score, d.rationale,
                  d.source_url, o.decision_timestamp_utc, o.review_timestamp_utc,
                  o.days_elapsed, o.pct_change, d.order_id
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


def benchmark_pct_change(decided_at: str, reviewed_at: str, cache: dict,
                          benchmark_ticker: str = BASELINE_TICKER):
    """What a passive buy-and-hold of `benchmark_ticker` returned over the
    decision's own [decided_at, reviewed_at] window. Reuses
    review_outcomes.price_near so the benchmark is priced by exactly the
    same nearest-bar-on-or-before rule the decision's own prices were.

    `cache` memoizes per (ticker, timestamp) within a single run: decisions
    from one cycle share a decided_at, and decisions reviewed in one weekly
    batch share a reviewed_at, so the same few lookups repeat many times."""
    def priced(target_iso):
        key = (benchmark_ticker, target_iso)
        if key not in cache:
            cache[key] = ro.price_near(benchmark_ticker, datetime.fromisoformat(target_iso))
        return cache[key]

    if not decided_at or not reviewed_at:
        return None
    price_then, price_now = priced(decided_at), priced(reviewed_at)
    if not price_then or not price_now:
        return None
    return (price_now - price_then) / price_then


def beats_baseline(action: str, pct_change, benchmark_pct):
    """Did this decision beat simply holding the market over the same window?

    A buy adds value when its ticker outran the benchmark. A sell adds value
    when the ticker UNDERperformed it - getting out of a laggard and into
    (effectively) the market was the better call. Note this can disagree
    with direction_correct in both directions, which is the whole point: a
    buy up 1% in an 8% market is direction-correct but value-destroying, and
    a sell whose ticker rose 2% in a 9% market looks wrong on direction yet
    still dodged relative underperformance."""
    if pct_change is None or benchmark_pct is None:
        return None
    excess = pct_change - benchmark_pct
    return excess > 0 if action == "buy" else excess < 0


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
    baseline_by_action = {"buy": [0, 0], "sell": [0, 0]}  # [n_beat_baseline, n_total]
    excess_returns = []
    distinct_ideas = set()   # (ticker, action) among scheduled runs only
    price_cache = {}
    decisions_out = []

    for row in rows:
        (dec_id, run_id, ticker, action, score, rationale, source_url,
         decided_at, reviewed_at, days_elapsed, pct_change, order_id) = row
        correct = direction_correct(action, pct_change)
        bucket = score_bucket(score)

        benchmark_pct = benchmark_pct_change(decided_at, reviewed_at, price_cache)
        beat_baseline = beats_baseline(action, pct_change, benchmark_pct)
        excess = (pct_change - benchmark_pct) if (pct_change is not None and benchmark_pct is not None) else None
        executed = order_id is not None
        scheduled = is_scheduled_run(run_id)
        # Both, not either: a hand-run verification can place a real order
        # (id 8, "verify-real-1") and a scheduled cycle can be a dry-run
        # (id 24, "2026-08-20T052129"). Only a decision the agent made in a
        # real cycle AND that actually executed is evidence about judgment.
        counts_as_evidence = scheduled and executed

        if counts_as_evidence:
            if action in by_action and correct is not None:
                by_action[action][1] += 1
                by_action[action][0] += int(correct)
            if correct is not None:
                by_bucket.setdefault(bucket, [0, 0])
                by_bucket[bucket][1] += 1
                by_bucket[bucket][0] += int(correct)
            if action in baseline_by_action and beat_baseline is not None:
                baseline_by_action[action][1] += 1
                baseline_by_action[action][0] += int(beat_baseline)
            if excess is not None:
                excess_returns.append(excess)
            distinct_ideas.add((ticker, action))

        session_id = session_index.get(run_id)
        research_excerpt = find_ticker_mentions(session_id, ticker) if session_id else []

        decisions_out.append({
            "decision_id": dec_id, "run_id": run_id, "ticker": ticker, "action": action,
            "technical_score": score, "rationale": rationale, "source_url": source_url,
            "decided_at": decided_at, "days_elapsed": round(days_elapsed, 1) if days_elapsed else None,
            "pct_change": pct_change, "direction_correct": correct,
            "benchmark_ticker": BASELINE_TICKER, "benchmark_pct_change": benchmark_pct,
            "excess_vs_benchmark": excess, "beats_baseline": beat_baseline,
            "scheduled_run": scheduled, "executed": executed,
            "counts_as_evidence": counts_as_evidence,
            "has_full_transcript": session_id is not None,
            "research_excerpt": research_excerpt[0] if research_excerpt else None,
        })

    def rate(pair):
        n_correct, n_total = pair
        return (n_correct / n_total) if n_total else None

    n_evidence = sum(1 for d in decisions_out if d["counts_as_evidence"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_reviewed_decisions": len(rows),
        "n_scheduled_decisions": n_evidence,
        "n_excluded_not_evidence": len(rows) - n_evidence,
        "n_excluded_dry_run": sum(1 for d in decisions_out if not d["executed"]),
        "n_excluded_hand_run": sum(1 for d in decisions_out if not d["scheduled_run"]),
        "n_distinct_scheduled_ideas": len(distinct_ideas),
        "n_with_full_transcript": sum(1 for d in decisions_out if d["has_full_transcript"]),
        "accuracy_by_action": {a: {"n": v[1], "rate": rate(v)} for a, v in by_action.items()},
        "accuracy_by_score_bucket": {b: {"n": v[1], "rate": rate(v)} for b, v in by_bucket.items()},
        "benchmark_ticker": BASELINE_TICKER,
        "beats_baseline_by_action": {a: {"n": v[1], "rate": rate(v)} for a, v in baseline_by_action.items()},
        "avg_excess_vs_benchmark": (sum(excess_returns) / len(excess_returns)) if excess_returns else None,
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

    print(f"  of which count as evidence: {scorecard['n_scheduled_decisions']}  "
          f"(excluded: {scorecard['n_excluded_not_evidence']} — "
          f"{scorecard['n_excluded_dry_run']} never executed (--dry-run), "
          f"{scorecard['n_excluded_hand_run']} from hand-run tests, overlapping)")
    print(f"  distinct (ticker, action) ideas among those: {scorecard['n_distinct_scheduled_ideas']}"
          f"  <- the honest sample size; the same idea re-proposed across "
          f"consecutive cycles is not independent evidence")

    if scorecard["n_scheduled_decisions"] == 0:
        print("\nNo reviewed decision was both from a real scheduled cycle and "
              "actually executed - every row so far was a hand-run test, a "
              "dry-run, or both. Nothing to conclude about the research "
              "agent's judgment either way.")
        return

    print("\nDirectional accuracy by action:")
    for action, stats in scorecard["accuracy_by_action"].items():
        rate_str = f"{stats['rate']:.0%}" if stats["rate"] is not None else "n/a"
        print(f"  {action:6s} n={stats['n']:3d}  correct={rate_str}")

    print("\nDirectional accuracy by technical-score bucket at decision time:")
    for bucket, stats in scorecard["accuracy_by_score_bucket"].items():
        rate_str = f"{stats['rate']:.0%}" if stats["rate"] is not None else "n/a"
        print(f"  {bucket:16s} n={stats['n']:3d}  correct={rate_str}")

    benchmark = scorecard["benchmark_ticker"]
    print(f"\nBeat a {benchmark} buy-and-hold baseline over the same window "
          f"(the 'did this add value over simply holding' test):")
    for action, stats in scorecard["beats_baseline_by_action"].items():
        rate_str = f"{stats['rate']:.0%}" if stats["rate"] is not None else "n/a"
        print(f"  {action:6s} n={stats['n']:3d}  beat baseline={rate_str}")
    avg_excess = scorecard["avg_excess_vs_benchmark"]
    if avg_excess is not None:
        print(f"  average excess return vs {benchmark}: {avg_excess:+.1%} "
              f"(raw ticker move minus {benchmark}'s over the identical window; "
              f"not action-adjusted, so a sell's negative excess is a good sign)")
    else:
        print(f"  (no decision could be priced against {benchmark} yet)")

    def pct(value):
        return f"{value:.0%}" if value is not None else "n/a"

    direction = scorecard["accuracy_by_action"]
    baseline = scorecard["beats_baseline_by_action"]
    print(f"\nSUMMARY: {scorecard['n_scheduled_decisions']} executed scheduled-cycle decisions reviewed "
          f"({scorecard['n_distinct_scheduled_ideas']} distinct ideas; "
          f"{scorecard['n_excluded_not_evidence']} non-evidence rows excluded), "
          f"{scorecard['n_with_full_transcript']} with full research transcripts linked; "
          f"direction-correct buy {pct(direction['buy']['rate'])} / sell {pct(direction['sell']['rate'])}; "
          f"beat {benchmark} buy-and-hold buy {pct(baseline['buy']['rate'])} / "
          f"sell {pct(baseline['sell']['rate'])}.")


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
