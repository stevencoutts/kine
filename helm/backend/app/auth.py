"""Single admin account, argon2 hashed, signed session cookie.

Traefik's forwardAuth middleware calls /api/auth/verify for every app
request, so one login covers the whole appliance. The apps keep their
own logins underneath as defence in depth.
"""
import os

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config

_ph = PasswordHasher()
MAX_AGE = 60 * 60 * 24 * 14


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("HELM_SESSION_SECRET") or config.read().get(
        "HELM_SESSION_SECRET", ""
    )
    if not secret:
        raise RuntimeError("HELM_SESSION_SECRET is unset")
    return URLSafeTimedSerializer(secret, salt="kine-session")


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def is_configured() -> bool:
    return bool(config.read().get("HELM_ADMIN_HASH"))


def set_password(pw: str) -> None:
    config.write({"HELM_ADMIN_HASH": hash_password(pw)})


def check(user: str, pw: str) -> bool:
    env = config.read()
    if user != env.get("HELM_ADMIN_USER", "admin"):
        return False
    stored = env.get("HELM_ADMIN_HASH", "")
    if not stored:
        return False
    try:
        return _ph.verify(stored, pw)
    except VerifyMismatchError:
        return False


def issue(user: str) -> str:
    return _serializer().dumps({"u": user})


def verify(token: str | None) -> str | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=MAX_AGE)
    except BadSignature:
        return None
    return data.get("u")
