"""Ensure Dispatcharr admin API keys exist for Helm wiring."""
from __future__ import annotations

import asyncio
import secrets

from . import compose, config

ENSURE_SCRIPT = """
import secrets, os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
import sys
sys.path.insert(0, "/app")
django.setup()
from apps.accounts.models import User
user = User.objects.filter(is_superuser=True).order_by("id").first()
if not user:
    raise SystemExit(2)
if user.api_key:
    print(user.api_key)
else:
    user.api_key = secrets.token_urlsafe(40)
    user.save(update_fields=["api_key"])
    print(user.api_key)
"""

# Teamarr (and similar) authenticate to Dispatcharr with username/password
# JWT login — they cannot use X-API-Key. Keep a dedicated service account
# so we never overwrite the interactive admin password.
LOGIN_USER_DEFAULT = "kine"

ENSURE_LOGIN_SCRIPT = """
import os, secrets, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
import sys
sys.path.insert(0, "/app")
django.setup()
from apps.accounts.models import User
username = {username!r}
password = {password!r}
user, _created = User.objects.get_or_create(
    username=username,
    defaults={{
        "email": "",
        "is_staff": True,
        "is_superuser": True,
        "user_level": 10,
    }},
)
user.set_password(password)
user.is_active = True
user.is_staff = True
user.is_superuser = True
if getattr(user, "user_level", None) in (None, 0):
    user.user_level = 10
if not user.api_key:
    user.api_key = secrets.token_urlsafe(40)
user.save()
print("OK")
"""


def enabled() -> bool:
    return "dispatcharr" in config.profiles()


def configured() -> bool:
    return bool((config.read().get("DISPATCHARR_TOKEN") or "").strip())


async def ensure_token(*, write_env: bool = True) -> str | None:
    """Return a Dispatcharr API key, generating one for the admin if needed."""
    if not enabled():
        return None
    existing = (config.read().get("DISPATCHARR_TOKEN") or "").strip()
    if existing:
        return existing

    code, out = await compose.run(
        "exec", "-T", "dispatcharr", "python3", "-c", ENSURE_SCRIPT,
        timeout=45,
    )
    if code == 2:
        return None
    token = ""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line and not line.startswith("INFO ") and "RuntimeWarning" not in line:
            token = line
            break
    if code != 0 or not token:
        return None
    if write_env:
        config.write({"DISPATCHARR_TOKEN": token})
    return token


async def ensure_login(*, write_env: bool = True) -> tuple[str | None, str | None]:
    """Ensure a Dispatcharr service user exists for password-based clients.

    Returns ``(username, password)``. Password is generated once and stored in
    ``.env`` as ``DISPATCHARR_LOGIN_PASSWORD`` so Teamarr can JWT-login without
    knowing the interactive admin password.
    """
    if not enabled():
        return None, None
    env = config.read()
    username = (env.get("DISPATCHARR_LOGIN_USER") or LOGIN_USER_DEFAULT).strip() or LOGIN_USER_DEFAULT
    password = (env.get("DISPATCHARR_LOGIN_PASSWORD") or "").strip()
    if not password:
        password = secrets.token_urlsafe(24)
        if write_env:
            config.write({
                "DISPATCHARR_LOGIN_USER": username,
                "DISPATCHARR_LOGIN_PASSWORD": password,
            })
    elif write_env and not (env.get("DISPATCHARR_LOGIN_USER") or "").strip():
        config.write({"DISPATCHARR_LOGIN_USER": username})

    script = ENSURE_LOGIN_SCRIPT.format(username=username, password=password)
    code, out = await compose.run(
        "exec", "-T", "dispatcharr", "python3", "-c", script,
        timeout=45,
    )
    if code != 0 or "OK" not in out:
        return None, None
    return username, password


def ensure_token_sync(*, write_env: bool = True) -> str | None:
    """Blocking wrapper for provision paths and sync helpers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ensure_token(write_env=write_env))
    raise RuntimeError("ensure_token_sync cannot run inside an active event loop")


def ensure_login_sync(*, write_env: bool = True) -> tuple[str | None, str | None]:
    """Blocking wrapper for Teamarr enable / provision paths."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ensure_login(write_env=write_env))
    raise RuntimeError("ensure_login_sync cannot run inside an active event loop")
