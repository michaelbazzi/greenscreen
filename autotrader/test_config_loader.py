"""
Unit tests for config_loader.py - the per-user override mechanism the
webapp's subprocess cycles rely on. The important property: unset
GREENSCREEN_CONFIG_DIR must be indistinguishable from the pre-webapp
behavior (real config.py / risk_params.py / repo-root trades.db).
"""

import json
from pathlib import Path

import pytest

import config_loader as cl


def test_unset_env_uses_real_config(monkeypatch):
    monkeypatch.delenv("GREENSCREEN_CONFIG_DIR", raising=False)
    key, secret = cl.load_credentials()
    assert key and secret  # real config.py values, whatever they are

    rp = cl.load_risk_params()
    assert rp.MAX_POSITION_PCT == 0.25  # the real, live constant

    assert cl.load_db_path() == cl._REPO_ROOT / "trades.db"


def test_set_env_uses_override_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GREENSCREEN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "credentials.json").write_text(
        json.dumps({"api_key": "OVERRIDE_KEY", "api_secret": "OVERRIDE_SECRET"})
    )
    (tmp_path / "risk_params.json").write_text(
        json.dumps({"MAX_POSITION_PCT": 0.1, "ENABLED": False})
    )

    key, secret = cl.load_credentials()
    assert (key, secret) == ("OVERRIDE_KEY", "OVERRIDE_SECRET")

    rp = cl.load_risk_params()
    assert rp.MAX_POSITION_PCT == 0.1
    assert rp.ENABLED is False

    assert cl.load_db_path() == tmp_path / "trades.db"
