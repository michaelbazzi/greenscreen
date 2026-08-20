#!/usr/bin/env python3
"""
review_outcomes.py — the outcome-tracking half of GreenScreen's learning
loop. Looks back at approved decisions from >= REVIEW_AFTER_DAYS ago that
haven't been reviewed yet, checks what actually happened to the price
since, and logs it to the `outcomes` table.

This is the prerequisite for any real recalibration, not the
recalibration itself: you can't learn from a decision whose outcome was
never measured. Nothing here adjusts risk_params.py or anything else -
it's read-only with respect to trading, purely an accumulating record of
"here's what actually happened." A separate, later, explicitly-reviewed
step is what would ever act on this data.

Run weekly via launchd (see run_outcome_review.sh / the
com.michaelbazzi.greenscreen-outcomes job).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_connection, init_decisions_table, execute_with_retry
import market_data as md

REVIEW_AFTER_DAYS = 7

OUTCOMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    decision_timestamp_utc TEXT NOT NULL,
    review_timestamp_utc TEXT NOT NULL,
    days_elapsed REAL NOT NULL,
    price_at_decision REAL,
    price_at_review REAL,
    pct_change REAL,
    technical_score_at_decision REAL,
    UNIQUE(decision_id)
);
"""


def init_outcomes_table(conn):
    conn.execute(OUTCOMES_SCHEMA)
    conn.commit()


def price_near(ticker, target_dt):
    """Closing price from the daily bar nearest on-or-before target_dt."""
    lookback = (datetime.now(timezone.utc) - target_dt).days + 10
    bars = md.get_bars(ticker, lookback_days=lookback)
    candidates = [b for b in bars if b.timestamp <= target_dt]
    if not candidates:
        return None
    return candidates[-1].close


def find_reviewable_decisions(conn, cutoff_str):
    return conn.execute(
        """SELECT id, ticker, action, timestamp_utc, technical_score
           FROM decisions
           WHERE risk_checks_passed = 1
             AND action IN ('buy', 'sell')
             AND timestamp_utc <= ?
             AND id NOT IN (SELECT decision_id FROM outcomes)
           ORDER BY timestamp_utc""",
        (cutoff_str,),
    ).fetchall()


def review_one(conn, decision_id, ticker, action, decision_ts, score):
    decision_dt = datetime.fromisoformat(decision_ts)
    price_then = price_near(ticker, decision_dt)
    signals = md.compute_signals(ticker)
    price_now = signals["current_price"] if signals else None

    if price_then is None or price_now is None:
        print(f"  {ticker} ({action}, decision {decision_id}): couldn't price this - skipped")
        return

    pct_change = (price_now - price_then) / price_then
    days_elapsed = (datetime.now(timezone.utc) - decision_dt).total_seconds() / 86400

    execute_with_retry(
        conn,
        """INSERT INTO outcomes
           (decision_id, ticker, action, decision_timestamp_utc, review_timestamp_utc,
            days_elapsed, price_at_decision, price_at_review, pct_change, technical_score_at_decision)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (decision_id, ticker, action, decision_ts, datetime.now(timezone.utc).isoformat(),
         days_elapsed, price_then, price_now, pct_change, score),
    )

    # A buy looks good in hindsight if price rose since; a sell looks good
    # if price fell since (we correctly got out ahead of a decline).
    looks_good = (action == "buy" and pct_change > 0) or (action == "sell" and pct_change < 0)
    verdict = "looks good in hindsight" if looks_good else "looks questionable in hindsight"
    print(f"  {ticker} ({action}, {days_elapsed:.0f}d ago, score={score}): "
          f"${price_then:.2f} -> ${price_now:.2f} ({pct_change:+.1%}) — {verdict}")

    return {"ticker": ticker, "action": action, "pct_change": pct_change, "looks_good": looks_good}


def main():
    conn = get_connection()
    init_decisions_table(conn)
    init_outcomes_table(conn)

    cutoff = datetime.now(timezone.utc) - timedelta(days=REVIEW_AFTER_DAYS)
    rows = find_reviewable_decisions(conn, cutoff.isoformat())

    if not rows:
        print("No decisions newly eligible for outcome review.")
        print("SUMMARY: No decisions were newly eligible for review this week.")
        conn.close()
        return

    print(f"Reviewing {len(rows)} decision(s) that are >= {REVIEW_AFTER_DAYS} days old:")
    results = []
    for decision_id, ticker, action, ts, score in rows:
        result = review_one(conn, decision_id, ticker, action, ts, score)
        if result:
            results.append(result)
    conn.close()

    if not results:
        print("SUMMARY: Reviewed decisions but couldn't price any of them - see log.")
        return

    good_count = sum(1 for r in results if r["looks_good"])
    best = max(results, key=lambda r: r["pct_change"])
    worst = min(results, key=lambda r: r["pct_change"])
    avg_pct = sum(r["pct_change"] for r in results) / len(results)

    print(
        f"SUMMARY: Reviewed {len(results)} decision(s) - {good_count}/{len(results)} "
        f"look good in hindsight. Best: {best['ticker']} {best['pct_change']:+.1%}. "
        f"Worst: {worst['ticker']} {worst['pct_change']:+.1%}. Avg: {avg_pct:+.1%}."
    )


if __name__ == "__main__":
    main()
