"""
Unit tests for user_store.py - encryption round-trip, per-user isolation,
and that the ephemeral config dir actually gets cleaned up. Uses a
temporary DATA_DIR per test so nothing here touches real user data.
"""

import json

import pytest

import user_store as us


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "DATA_DIR", tmp_path)
    monkeypatch.setattr(us, "USERS_DIR", tmp_path / "users")
    monkeypatch.setattr(us, "MASTER_KEY_PATH", tmp_path / "master.key")
    yield


def test_credentials_round_trip():
    uid = us.new_user_id()
    us.create_user_dir(uid)
    us.save_credentials(uid, "AKFAKE123", "secretfake456", "anthropic", "sk-fake-llm-key")

    loaded = us.load_credentials(uid)
    assert loaded == {
        "api_key": "AKFAKE123",
        "api_secret": "secretfake456",
        "llm_provider": "anthropic",
        "llm_api_key": "sk-fake-llm-key",
    }


def test_credentials_are_actually_encrypted_on_disk():
    uid = us.new_user_id()
    us.create_user_dir(uid)
    us.save_credentials(uid, "AKFAKE123", "secretfake456", "anthropic", "sk-fake-llm-key")

    raw = (us.user_dir(uid) / "credentials.enc").read_bytes()
    assert b"AKFAKE123" not in raw
    assert b"secretfake456" not in raw
    assert b"sk-fake-llm-key" not in raw


def test_two_users_credentials_dont_cross():
    uid_a = us.new_user_id()
    uid_b = us.new_user_id()
    us.create_user_dir(uid_a)
    us.create_user_dir(uid_b)
    us.save_credentials(uid_a, "KEY_A", "SECRET_A", "anthropic", "llm_a")
    us.save_credentials(uid_b, "KEY_B", "SECRET_B", "openai", "llm_b")

    assert us.load_credentials(uid_a)["api_key"] == "KEY_A"
    assert us.load_credentials(uid_b)["api_key"] == "KEY_B"


def test_risk_params_round_trip():
    uid = us.new_user_id()
    us.create_user_dir(uid)
    params = {"MAX_POSITION_PCT": 0.2, "ENABLED": True}
    us.save_risk_params(uid, params)
    assert us.load_risk_params(uid) == params


def test_live_risk_param_defaults_includes_known_fields():
    defaults = us.live_risk_param_defaults()
    assert defaults["MAX_POSITION_PCT"] == 0.25
    assert defaults["TECH_SCORE_THRESHOLD_EXISTING"] == 0.60
    assert defaults["ROTATION_ENABLED"] is True
    assert "TICKER_SECTORS" in defaults


def test_ephemeral_config_dir_is_readable_during_and_gone_after():
    uid = us.new_user_id()
    us.create_user_dir(uid)
    us.save_credentials(uid, "AKFAKE123", "secretfake456", "anthropic", "sk-fake")
    us.save_risk_params(uid, {"MAX_POSITION_PCT": 0.2})

    captured_path = None
    with us.ephemeral_config_dir(uid) as config_dir:
        captured_path = config_dir
        creds = json.loads((config_dir / "credentials.json").read_text())
        assert creds == {"api_key": "AKFAKE123", "api_secret": "secretfake456"}
        rp = json.loads((config_dir / "risk_params.json").read_text())
        assert rp == {"MAX_POSITION_PCT": 0.2}

    assert not captured_path.exists()
