"""
Account creation, login, and session handling. Argon2 for password hashing
(the current recommended default, not a legacy scheme), itsdangerous for
signed session cookies. No third-party auth service - the scale here (a
handful of accounts) doesn't need one, and a well-implemented local system
avoids an external dependency for something this small.

users_index.json maps email -> user_id; each user's own account.json
(inside their user_store directory) holds the password hash and signup
metadata. Neither file holds anything secret enough to need encryption
the way credentials.enc does - a password hash isn't the password.
"""

import json
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer

import user_store as us

INDEX_PATH = us.DATA_DIR / "users_index.json"
SESSION_SECRET_PATH = us.DATA_DIR / "session_secret.key"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 14  # 14 days

_hasher = PasswordHasher()


class EmailAlreadyRegistered(Exception):
    pass


class InvalidLogin(Exception):
    pass


def _load_index() -> dict:
    if not INDEX_PATH.exists():
        return {}
    return json.loads(INDEX_PATH.read_text())


def _save_index(index: dict) -> None:
    us.DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, indent=2))


def create_account(email: str, password: str) -> str:
    email = email.strip().lower()
    index = _load_index()
    if email in index:
        raise EmailAlreadyRegistered(email)

    user_id = us.new_user_id()
    us.create_user_dir(user_id)
    account = {
        "email": email,
        "password_hash": _hasher.hash(password),
        "created_at": time.time(),
        "acknowledged_disclaimer": False,
    }
    (us.user_dir(user_id) / "account.json").write_text(json.dumps(account, indent=2))

    index[email] = user_id
    _save_index(index)
    return user_id


def verify_login(email: str, password: str) -> str:
    """Returns user_id on success, raises InvalidLogin otherwise. Doesn't
    distinguish "no such email" from "wrong password" in the error - that
    distinction is exactly the kind of thing that helps an attacker
    enumerate registered emails."""
    email = email.strip().lower()
    index = _load_index()
    user_id = index.get(email)
    if user_id is None:
        raise InvalidLogin()

    account = json.loads((us.user_dir(user_id) / "account.json").read_text())
    try:
        _hasher.verify(account["password_hash"], password)
    except VerifyMismatchError:
        raise InvalidLogin()
    return user_id


def _get_or_create_session_secret() -> str:
    us.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SESSION_SECRET_PATH.exists():
        return SESSION_SECRET_PATH.read_text()
    import secrets

    secret = secrets.token_hex(32)
    SESSION_SECRET_PATH.write_text(secret)
    SESSION_SECRET_PATH.chmod(0o600)
    return secret


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_get_or_create_session_secret(), salt="greenscreen-session")


def create_session_token(user_id: str) -> str:
    return _serializer().dumps({"user_id": user_id})


def verify_session_token(token: str) -> str | None:
    """Returns user_id if the token is valid and unexpired, else None -
    never raises, since every call site just wants "logged in or not"."""
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except BadSignature:
        return None
    return data.get("user_id")


def acknowledge_disclaimer(user_id: str) -> None:
    """Marks that this user has explicitly confirmed the experimental /
    paper-only / not-financial-advice disclaimer - required before their
    first cycle can run (enforced by the caller, not this module)."""
    account_path = us.user_dir(user_id) / "account.json"
    account = json.loads(account_path.read_text())
    account["acknowledged_disclaimer"] = True
    account_path.write_text(json.dumps(account, indent=2))


def has_acknowledged_disclaimer(user_id: str) -> bool:
    account = json.loads((us.user_dir(user_id) / "account.json").read_text())
    return account.get("acknowledged_disclaimer", False)
