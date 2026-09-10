"""
Unit tests for auth.py - signup, login, session tokens, and the
disclaimer-acknowledgment gate.
"""

import pytest

import auth
import user_store as us


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "DATA_DIR", tmp_path)
    monkeypatch.setattr(us, "USERS_DIR", tmp_path / "users")
    monkeypatch.setattr(us, "MASTER_KEY_PATH", tmp_path / "master.key")
    monkeypatch.setattr(auth, "INDEX_PATH", tmp_path / "users_index.json")
    monkeypatch.setattr(auth, "SESSION_SECRET_PATH", tmp_path / "session_secret.key")
    yield


def test_create_account_then_login():
    user_id = auth.create_account("friend@example.com", "correct horse battery staple")
    logged_in_id = auth.verify_login("friend@example.com", "correct horse battery staple")
    assert logged_in_id == user_id


def test_login_wrong_password_rejected():
    auth.create_account("friend@example.com", "the-real-password")
    with pytest.raises(auth.InvalidLogin):
        auth.verify_login("friend@example.com", "a-guess")


def test_login_unknown_email_rejected():
    with pytest.raises(auth.InvalidLogin):
        auth.verify_login("nobody@example.com", "whatever")


def test_duplicate_email_rejected():
    auth.create_account("friend@example.com", "password-one")
    with pytest.raises(auth.EmailAlreadyRegistered):
        auth.create_account("friend@example.com", "password-two")


def test_email_is_case_and_whitespace_insensitive():
    auth.create_account("Friend@Example.com ", "password123")
    user_id = auth.verify_login(" friend@example.com", "password123")
    assert user_id is not None


def test_password_is_not_stored_in_plaintext():
    auth.create_account("friend@example.com", "super-secret-password")
    index = auth._load_index()
    user_id = index["friend@example.com"]
    import json
    account = json.loads((us.user_dir(user_id) / "account.json").read_text())
    assert "super-secret-password" not in account["password_hash"]


def test_session_token_round_trip():
    user_id = auth.create_account("friend@example.com", "password123")
    token = auth.create_session_token(user_id)
    assert auth.verify_session_token(token) == user_id


def test_tampered_session_token_rejected():
    user_id = auth.create_account("friend@example.com", "password123")
    token = auth.create_session_token(user_id)
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert auth.verify_session_token(tampered) is None


def test_disclaimer_not_acknowledged_by_default():
    user_id = auth.create_account("friend@example.com", "password123")
    assert auth.has_acknowledged_disclaimer(user_id) is False


def test_disclaimer_acknowledgment_persists():
    user_id = auth.create_account("friend@example.com", "password123")
    auth.acknowledge_disclaimer(user_id)
    assert auth.has_acknowledged_disclaimer(user_id) is True
