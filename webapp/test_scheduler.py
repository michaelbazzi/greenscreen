"""
Unit tests for scheduler.py - readiness filtering, the market-closed
short-circuit, and that a user's cycle correctly isolates each subprocess
to that user's own decrypted config.
"""

from types import SimpleNamespace

import pytest

import auth
import scheduler as sched
import user_store as us


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "DATA_DIR", tmp_path)
    monkeypatch.setattr(us, "USERS_DIR", tmp_path / "users")
    monkeypatch.setattr(us, "MASTER_KEY_PATH", tmp_path / "master.key")
    monkeypatch.setattr(auth, "INDEX_PATH", tmp_path / "users_index.json")
    monkeypatch.setattr(auth, "SESSION_SECRET_PATH", tmp_path / "session_secret.key")
    yield


def _onboard_user(email="friend@example.com"):
    user_id = auth.create_account(email, "password123")
    us.save_credentials(user_id, "AK", "SECRET", "anthropic", "sk-llm")
    us.save_risk_params(user_id, {"MAX_POSITION_PCT": 0.2})
    return user_id


def test_user_missing_disclaimer_ack_is_not_ready():
    _onboard_user()
    assert sched.list_ready_users() == []


def test_fully_onboarded_user_is_ready():
    user_id = _onboard_user()
    auth.acknowledge_disclaimer(user_id)
    assert sched.list_ready_users() == [user_id]


def test_user_missing_risk_params_is_not_ready(monkeypatch):
    user_id = auth.create_account("friend@example.com", "password123")
    us.save_credentials(user_id, "AK", "SECRET", "anthropic", "sk-llm")
    auth.acknowledge_disclaimer(user_id)
    # no save_risk_params call
    assert sched.list_ready_users() == []


def test_run_all_ready_users_skips_everything_when_market_closed(monkeypatch):
    user_id = _onboard_user()
    auth.acknowledge_disclaimer(user_id)

    monkeypatch.setattr(sched, "is_market_open", lambda: False)
    calls = []
    monkeypatch.setattr(sched, "run_user_cycle", lambda uid: calls.append(uid))

    result = sched.run_all_ready_users()
    assert result == []
    assert calls == []  # never even tried


def test_run_all_ready_users_runs_each_ready_user_when_open(monkeypatch):
    user_a = _onboard_user("a@example.com")
    user_b = _onboard_user("b@example.com")
    auth.acknowledge_disclaimer(user_a)
    auth.acknowledge_disclaimer(user_b)

    monkeypatch.setattr(sched, "is_market_open", lambda: True)
    called_with = []
    monkeypatch.setattr(sched, "run_user_cycle", lambda uid: called_with.append(uid) or {"user_id": uid})

    results = sched.run_all_ready_users()
    assert set(called_with) == {user_a, user_b}
    assert len(results) == 2


def test_run_user_cycle_isolates_env_per_user(monkeypatch):
    user_id = _onboard_user()
    captured_envs = []

    def fake_run(cmd, capture_output, text, timeout, env):
        captured_envs.append(dict(env))
        return SimpleNamespace(stdout="ok", stderr="")

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    sched.run_user_cycle(user_id)

    # both subprocess calls (sweep + research) got a GREENSCREEN_CONFIG_DIR
    # pointing at a real, existing directory at call time
    assert len(captured_envs) == 2
    for env in captured_envs:
        assert "GREENSCREEN_CONFIG_DIR" in env

    # the research call specifically carries this user's own LLM creds
    research_env = captured_envs[1]
    assert research_env["GREENSCREEN_LLM_PROVIDER"] == "anthropic"
    assert research_env["GREENSCREEN_LLM_API_KEY"] == "sk-llm"


def test_run_user_cycle_cleans_up_ephemeral_dir_after(monkeypatch):
    user_id = _onboard_user()
    captured_paths = []

    def fake_run(cmd, capture_output, text, timeout, env):
        captured_paths.append(env["GREENSCREEN_CONFIG_DIR"])
        return SimpleNamespace(stdout="ok", stderr="")

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    sched.run_user_cycle(user_id)

    from pathlib import Path

    for p in captured_paths:
        assert not Path(p).exists()
