#!/bin/bash
# Mirrors code from the live project (~/trading-project, what launchd
# actually runs) into the GitHub-tracked copy (~/Documents/trading-project).
# One-way only. Runtime state, secrets, and each copy's own .git are
# deliberately excluded - this only keeps the code in sync, not git history
# or anything that shouldn't leave this machine.
set -euo pipefail

SRC="/Users/MichaelBazzi/trading-project/"
DEST="/Users/MichaelBazzi/Documents/trading-project/"

rsync -a --delete \
    --exclude='.git/' \
    --exclude='autotrader/state/' \
    --exclude='__pycache__/' \
    --exclude='trades.db' \
    --exclude='.DS_Store' \
    "$SRC" "$DEST"
