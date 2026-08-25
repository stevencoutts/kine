"""Ensure Dispatcharr admin API keys exist for Helm wiring."""
from __future__ import annotations

import asyncio

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


def ensure_token_sync(*, write_env: bool = True) -> str | None:
    """Blocking wrapper for provision paths and sync helpers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ensure_token(write_env=write_env))
    raise RuntimeError("ensure_token_sync cannot run inside an active event loop")
