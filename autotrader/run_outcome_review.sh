#!/bin/bash
# Weekly outcome review. Read-only with respect to trading - doesn't need
# the Claude CLI or any AI reasoning, just deterministic price lookups, so
# unlike run_cycle.sh this runs the Python script directly.
set -uo pipefail

REPO_DIR="/Users/MichaelBazzi/trading-project"
LOG_DIR="$REPO_DIR/autotrader/state/logs"

cd "$REPO_DIR" || exit 1
mkdir -p "$LOG_DIR"

TS="$(date -u +%Y-%m-%dT%H%M%S)"
LOG_FILE="$LOG_DIR/outcomes-$TS.log"

{
    echo "=== outcome review starting $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    /Users/MichaelBazzi/trading-env/bin/python3 autotrader/review_outcomes.py
    echo "=== outcome review finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} >> "$LOG_FILE" 2>&1

SUMMARY_LINE="$(grep '^SUMMARY:' "$LOG_FILE" | tail -n1)"
if [ -n "$SUMMARY_LINE" ]; then
    NOTIFY_BODY="${SUMMARY_LINE#SUMMARY:}"
else
    NOTIFY_BODY="Weekly review ran but produced no summary - check the log"
fi

osascript -e "display notification \"$NOTIFY_BODY\" with title \"GreenScreen: weekly outcome review\"" >/dev/null 2>&1
/Users/MichaelBazzi/trading-env/bin/python3 autotrader/notify_email.py \
    "GreenScreen weekly outcome review" "$NOTIFY_BODY" >/dev/null 2>&1 || true
