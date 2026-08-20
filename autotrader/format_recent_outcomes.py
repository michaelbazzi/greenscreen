#!/usr/bin/env python3
"""
format_recent_outcomes.py — prints a markdown block of the most recent rows
from the `outcomes` table (populated by review_outcomes.py), for injection
into research_prompt.md via run_cycle.sh's {{RECENT_OUTCOMES}} substitution.

Read-only. `looks_good` isn't a stored column - computed here the same way
review_outcomes.py does it: a buy looks good in hindsight if price rose
since, a sell looks good if price fell since.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_connection  # noqa: E402

RECENT_LIMIT = 10


def format_recent_outcomes(conn, limit=RECENT_LIMIT):
    rows = conn.execute(
        """SELECT ticker, action, pct_change
           FROM outcomes
           ORDER BY review_timestamp_utc DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    if not rows:
        return "No completed outcomes yet - the review job hasn't produced any results."

    lines = [f"Most recent {len(rows)} reviewed decision(s):"]
    for ticker, action, pct_change in rows:
        looks_good = (action == "buy" and pct_change > 0) or (action == "sell" and pct_change < 0)
        verdict = "looks good in hindsight" if looks_good else "looks questionable in hindsight"
        lines.append(f"- {ticker} ({action}): {pct_change:+.1%} — {verdict}")
    return "\n".join(lines)


def main():
    conn = get_connection()
    print(format_recent_outcomes(conn))


if __name__ == "__main__":
    main()
