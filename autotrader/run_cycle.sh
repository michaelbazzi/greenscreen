#!/bin/bash
# One research+trade cycle. Invoked by launchd on a timer, or manually for
# testing (pass --dry-run to force every propose/sweep call in the cycle to
# use --dry-run, regardless of market hours or guardrail outcomes).
set -uo pipefail

REPO_DIR="/Users/MichaelBazzi/trading-project"
CLAUDE_BIN="/usr/local/bin/claude"
TOKEN_FILE="$REPO_DIR/autotrader/state/claude_oauth_token"
LOG_DIR="$REPO_DIR/autotrader/state/logs"
PROMPT_FILE="$REPO_DIR/autotrader/research_prompt.md"

cd "$REPO_DIR" || exit 1
mkdir -p "$LOG_DIR"

RUN_ID="$(date -u +%Y-%m-%dT%H%M%S)"
LOG_FILE="$LOG_DIR/$RUN_ID.log"

PROMPT="$(cat "$PROMPT_FILE")"
PROMPT="${PROMPT//\{\{RUN_ID\}\}/$RUN_ID}"

if [ "${1:-}" = "--dry-run" ]; then
    PROMPT="$PROMPT

TEST MODE: append --dry-run to every propose_trade.py call in this cycle
(sweep-stop-loss and propose both). Do not submit any real orders."
fi

export CLAUDE_CODE_OAUTH_TOKEN
CLAUDE_CODE_OAUTH_TOKEN="$(cat "$TOKEN_FILE")"

{
    echo "=== cycle $RUN_ID starting $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    "$CLAUDE_BIN" --print --permission-mode dontAsk \
        --allowedTools "Bash(/Users/MichaelBazzi/trading-env/bin/python3 autotrader/propose_trade.py *) Bash(/Users/MichaelBazzi/trading-env/bin/python3 autotrader/market_data.py *) WebSearch WebFetch" \
        --settings .claude/settings.json \
        "$PROMPT"
    echo "=== cycle $RUN_ID finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} >> "$LOG_FILE" 2>&1

unset CLAUDE_CODE_OAUTH_TOKEN

# Release the run-lock now that this cycle is fully done, so the next
# scheduled cycle doesn't have to wait out the 30-min staleness window.
# Only remove it if it's still ours (matches this run_id) - a concurrent
# cycle's lock (which we'd have been refused entry to) must not be touched.
LOCK_FILE="$REPO_DIR/autotrader/state/run.lock"
if [ -f "$LOCK_FILE" ] && grep -q "\"run_id\": \"$RUN_ID\"" "$LOCK_FILE" 2>/dev/null; then
    rm -f "$LOCK_FILE"
fi

SUMMARY_LINE="$(grep '^SUMMARY:' "$LOG_FILE" | tail -n1)"
if [ -n "$SUMMARY_LINE" ]; then
    osascript -e "display notification \"${SUMMARY_LINE#SUMMARY:}\" with title \"Autotrader: $RUN_ID\"" >/dev/null 2>&1
else
    osascript -e "display notification \"Cycle $RUN_ID finished with no SUMMARY line - check the log\" with title \"Autotrader: $RUN_ID\"" >/dev/null 2>&1
fi
