"""
Per-user storage: account directories, encrypted credentials, risk-param
overrides. Each user gets webapp/data/users/<user_id>/ containing:
  - credentials.enc   Alpaca + LLM-provider keys, Fernet-encrypted at rest
  - risk_params.json  plaintext (not secret - it's the same shape as the
                       live risk_params.py's constants, just per-user)
  - trades.db         that user's own decisions/trades/outcomes tables,
                       schema reused directly from autotrader/db.py and
                       autotrader/review_outcomes.py

The master encryption key lives at webapp/data/master.key, generated on
first use, gitignored - same trust model config.py already uses, just
guarding N users' credentials instead of one.

Nothing here touches autotrader/'s decision logic. This module only ever
produces two things another process reads: a decrypted, short-lived
GREENSCREEN_CONFIG_DIR (see run_user_cycle in scheduler.py) and the
risk_params defaults new users see at signup - both read-only borrowings
from the real system, never a fork of its logic.
"""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

DATA_DIR = Path(__file__).resolve().parent / "data"
USERS_DIR = DATA_DIR / "users"
MASTER_KEY_PATH = DATA_DIR / "master.key"

_AUTOTRADER_DIR = Path(__file__).resolve().parent.parent / "autotrader"


def _get_or_create_master_key() -> bytes:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if MASTER_KEY_PATH.exists():
        return MASTER_KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    MASTER_KEY_PATH.write_bytes(key)
    MASTER_KEY_PATH.chmod(0o600)
    return key


def _fernet() -> Fernet:
    return Fernet(_get_or_create_master_key())


def live_risk_param_defaults() -> dict:
    """The live risk_params.py's current constants, read directly - never
    hand-copied, so this can't silently drift from what's actually running.
    Onboarding pre-fills a new user's form with these; nothing is inherited
    until they explicitly review and save their own copy."""
    import sys

    sys.path.insert(0, str(_AUTOTRADER_DIR))
    import risk_params as live_rp

    return {k: v for k, v in vars(live_rp).items() if k.isupper()}


def user_dir(user_id: str) -> Path:
    return USERS_DIR / user_id


def create_user_dir(user_id: str) -> Path:
    d = user_dir(user_id)
    d.mkdir(parents=True, exist_ok=False)
    return d


def save_credentials(user_id: str, api_key: str, api_secret: str, llm_provider: str, llm_api_key: str) -> None:
    payload = json.dumps(
        {
            "api_key": api_key,
            "api_secret": api_secret,
            "llm_provider": llm_provider,
            "llm_api_key": llm_api_key,
        }
    ).encode()
    encrypted = _fernet().encrypt(payload)
    (user_dir(user_id) / "credentials.enc").write_bytes(encrypted)


def load_credentials(user_id: str) -> dict:
    encrypted = (user_dir(user_id) / "credentials.enc").read_bytes()
    return json.loads(_fernet().decrypt(encrypted))


def save_risk_params(user_id: str, params: dict) -> None:
    (user_dir(user_id) / "risk_params.json").write_text(json.dumps(params, indent=2))


def load_risk_params(user_id: str) -> dict:
    return json.loads((user_dir(user_id) / "risk_params.json").read_text())


class ephemeral_config_dir:
    """Context manager: decrypts a user's credentials + risk_params into a
    short-lived plaintext directory (GREENSCREEN_CONFIG_DIR points here for
    the duration of one subprocess cycle), then deletes it - so decrypted
    keys never sit on disk longer than one cycle actually needs them.

        with ephemeral_config_dir(user_id) as config_dir:
            subprocess.run([..., "propose", ...], env={**os.environ, "GREENSCREEN_CONFIG_DIR": str(config_dir)})
    """

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix=f"gs_{self.user_id}_"))
        creds = load_credentials(self.user_id)
        (self.path / "credentials.json").write_text(
            json.dumps({"api_key": creds["api_key"], "api_secret": creds["api_secret"]})
        )
        (self.path / "risk_params.json").write_text(
            json.dumps(load_risk_params(self.user_id))
        )
        return self.path

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)


def new_user_id() -> str:
    return uuid.uuid4().hex
