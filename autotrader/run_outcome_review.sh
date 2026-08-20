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

REVIEWED_COUNT="$(grep -c '^  ' "$LOG_FILE" 2>/dev/null || echo 0)"
osascript -e "display notification \"Reviewed $REVIEWED_COUNT decision(s) from the past week. Log: $(basename "$LOG_FILE")\" with title \"GreenScreen: weekly outcome review\"" >/dev/null 2>&1
