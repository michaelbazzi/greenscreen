#!/bin/bash
# Mirrors code from the live project (~/trading-project, what launchd
# actually runs) into the GitHub-tracked copy (~/Documents/GreenScreen).
# One-way only. Runtime state, secrets, and each copy's own .git are
# deliberately excluded - this only keeps the code in sync, not git history
# or anything that shouldn't leave this machine.
#
# DEST was stale from 2026-09-09 until 2026-09-12: the Documents copy was
# renamed trading-project -> GreenScreen and only the DOCUMENTS copy of this
# script was updated - which is not the copy launchd runs. So for three days
# the every-30-minutes job faithfully rsynced into ~/Documents/trading-project/,
# a directory rsync itself recreated, with no .git and no GitHub remote, while
# ~/Documents/GreenScreen (the real repo) silently received nothing and fell
# 10 commits behind origin/main. If this ever points at a folder that doesn't
# exist, that is what happens - rsync creates it rather than failing, and the
# breakage is invisible until someone compares the two trees by hand.
set -euo pipefail

SRC="/Users/MichaelBazzi/trading-project/"
DEST="/Users/MichaelBazzi/Documents/GreenScreen/"

# Refuse to run if DEST isn't the real git-tracked repo. The failure above was
# silent for three days precisely because rsync will happily invent a
# destination; this makes that specific mistake loud instead.
if [ ! -d "${DEST}.git" ]; then
    echo "REFUSING TO SYNC: ${DEST} is not a git repository." >&2
    echo "The GitHub-tracked copy must exist with its .git intact - rsync would" >&2
    echo "otherwise create a detached mirror that can never be pushed." >&2
    exit 1
fi

rsync -a --delete \
    --exclude='.git/' \
    --exclude='autotrader/state/' \
    --exclude='__pycache__/' \
    --exclude='trades.db' \
    --exclude='.DS_Store' \
    --exclude='greenscreen_architecture.svg' \
    --exclude='webapp/data/' \
    --exclude='GreenScreen-backtest/' \
    "$SRC" "$DEST"
# greenscreen_architecture.svg: exists only in the Documents copy (this is
# a one-way live->Documents sync), excluding it from --delete so it stops
# getting wiped every cycle.
# webapp/data/: per-user encrypted credentials and databases - must never
# reach the copy that gets pushed to public GitHub, even accidentally.
# GreenScreen-backtest/: a SEPARATE project with its own git repo and its own
# GitHub remote (michaelbazzi/greenscreen-backtest), living inside the
# Documents copy but absent from ~/trading-project. Without this exclusion
# --delete wipes the entire tree - ~4,300 lines and 131 tests - on the first
# sync after DEST is corrected. It survived only because DEST was pointing
# somewhere else entirely.
