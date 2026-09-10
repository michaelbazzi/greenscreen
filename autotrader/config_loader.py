"""
Resolves credentials and risk parameters for the current run: the live
account's own config.py/risk_params.py by default, or a per-user override
directory when GREENSCREEN_CONFIG_DIR is set (used by the webapp's
per-user subprocess invocations - see webapp/user_store.py and
webapp/scheduler.py, which decrypt a user's stored credentials into a
short-lived plaintext directory before spawning a cycle, then clean it up).

Unset GREENSCREEN_CONFIG_DIR -> byte-for-byte identical to importing
config/risk_params directly, which is exactly what the live launchd-driven
cycles do today - they never set this variable, so this file changes
nothing about their behavior.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent


def load_credentials():
    """Returns (api_key, api_secret)."""
    config_dir = os.environ.get("GREENSCREEN_CONFIG_DIR")
    if not config_dir:
        sys.path.insert(0, str(_REPO_ROOT))
        from config import API_KEY, API_SECRET
        return API_KEY, API_SECRET
    data = json.loads((Path(config_dir) / "credentials.json").read_text())
    return data["api_key"], data["api_secret"]


def load_db_path():
    """Returns the trades.db path to use: the live account's own path by
    default, or a per-user database when GREENSCREEN_CONFIG_DIR is set -
    so a signed-up user's decisions, trades, and outcomes live in their
    own file, never the shared live database."""
    config_dir = os.environ.get("GREENSCREEN_CONFIG_DIR")
    if not config_dir:
        return _REPO_ROOT / "trades.db"
    return Path(config_dir) / "trades.db"


def load_state_dir() -> Path:
    """Returns the directory for this run's mutable state (run.lock,
    circuit_breaker.json): the live account's own autotrader/state/ by
    default, or a per-user directory when GREENSCREEN_CONFIG_DIR is set -
    so one account's overlapping-cycle lock or drawdown circuit breaker
    can never block or halt a different account."""
    config_dir = os.environ.get("GREENSCREEN_CONFIG_DIR")
    if not config_dir:
        return _REPO_ROOT / "autotrader" / "state"
    return Path(config_dir) / "state"


def load_risk_params():
    """Returns a risk_params-like namespace: the real module by default, or
    a per-user override exposing the identical attribute names, built from
    that user's risk_params.json (pre-filled from the live defaults at
    onboarding, but never silently inherited without explicit review)."""
    config_dir = os.environ.get("GREENSCREEN_CONFIG_DIR")
    if not config_dir:
        import risk_params as rp
        return rp
    data = json.loads((Path(config_dir) / "risk_params.json").read_text())
    return SimpleNamespace(**data)
