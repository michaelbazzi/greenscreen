"""
Market-hours-aware dispatcher. On each tick, checks whether the market is
actually open via Alpaca's own clock endpoint - not a static calendar, so
holidays are handled correctly without hardcoded dates - and for each
ready (fully onboarded, not paused) user, runs their cycle: sweep
stop-losses first always, then one research cycle, each as a genuinely
fresh subprocess per research_agent.py's module docstring.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.client import TradingClient
from apscheduler.schedulers.background import BackgroundScheduler

import auth
import user_store as us

_WEBAPP_DIR = Path(__file__).resolve().parent
_RESEARCH_AGENT = _WEBAPP_DIR / "research_agent.py"
_AUTOTRADER_DIR = _WEBAPP_DIR.parent / "autotrader"
_PROPOSE_TRADE = _AUTOTRADER_DIR / "propose_trade.py"

CYCLE_INTERVAL_MINUTES = 90  # roughly matches the live account's 4x/day within market hours


def is_market_open() -> bool:
    """Market hours are a universal fact, not per-user - uses the app's
    own default (live) credentials for this one shared, read-only check."""
    sys.path.insert(0, str(_AUTOTRADER_DIR))
    from config_loader import load_credentials

    key, secret = load_credentials()
    client = TradingClient(key, secret, paper=True)
    return client.get_clock().is_open


def list_ready_users() -> list:
    """User IDs that have completed onboarding - credentials saved, risk
    params saved, disclaimer acknowledged. Anything short of that never
    runs a cycle."""
    ready = []
    if not us.USERS_DIR.exists():
        return ready
    for user_dir in us.USERS_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        user_id = user_dir.name
        if not (user_dir / "credentials.enc").exists():
            continue
        if not (user_dir / "risk_params.json").exists():
            continue
        if not (user_dir / "account.json").exists():
            continue
        if not auth.has_acknowledged_disclaimer(user_id):
            continue
        ready.append(user_id)
    return ready


def _run_id_for_now(user_id: str) -> str:
    return f"{user_id[:8]}-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%S')}"


def run_user_cycle(user_id: str) -> dict:
    """Sweep stop-losses, then one research cycle - both subprocess
    isolated so each user's cycle only ever sees its own credentials and
    risk params. The authoritative record of what happened lives in that
    user's own trades.db, written by propose_trade.py itself exactly as
    it is for the live account - this function's return value is a
    logging convenience, not the source of truth."""
    run_id = _run_id_for_now(user_id)
    creds = us.load_credentials(user_id)

    with us.ephemeral_config_dir(user_id) as config_dir:
        env = {**os.environ, "GREENSCREEN_CONFIG_DIR": str(config_dir)}

        sweep = subprocess.run(
            [sys.executable, str(_PROPOSE_TRADE), "sweep-stop-loss", "--run-id", run_id],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

        research_env = {
            **env,
            "GREENSCREEN_LLM_PROVIDER": creds["llm_provider"],
            "GREENSCREEN_LLM_API_KEY": creds["llm_api_key"],
        }
        research = subprocess.run(
            [sys.executable, str(_RESEARCH_AGENT), "--run-id", run_id],
            capture_output=True,
            text=True,
            timeout=600,
            env=research_env,
        )

    return {
        "user_id": user_id,
        "run_id": run_id,
        "sweep_output": sweep.stdout,
        "sweep_errors": sweep.stderr,
        "research_output": research.stdout,
        "research_errors": research.stderr,
    }


def run_all_ready_users() -> list:
    """The scheduled job. Market closed -> does nothing at all, every
    tick, until it reopens - no wasted subprocess spawns outside hours."""
    if not is_market_open():
        return []
    results = []
    for user_id in list_ready_users():
        try:
            results.append(run_user_cycle(user_id))
        except Exception as e:
            results.append({"user_id": user_id, "error": str(e)})
    return results


def start_background_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_all_ready_users,
        "interval",
        minutes=CYCLE_INTERVAL_MINUTES,
        id="greenscreen-cycles",
    )
    scheduler.start()
    return scheduler
