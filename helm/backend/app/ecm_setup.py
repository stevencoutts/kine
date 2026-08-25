"""Complete ECM first-run admin setup using Helm credentials."""
from __future__ import annotations

import httpx

from . import config

ECM_BASE = "http://gluetun:6100"


def enabled() -> bool:
    return "ecm" in config.profiles()


def setup_required(*, timeout: float = 8.0) -> bool:
    """True when ECM is up and still needs an initial admin."""
    if not enabled():
        return False
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{ECM_BASE}/api/auth/setup-required")
            if resp.status_code != 200:
                return False
            data = resp.json() if resp.content else {}
            return bool(data.get("required"))
    except (httpx.HTTPError, ValueError):
        return False


def ensure_admin(
    username: str,
    password: str,
    *,
    email: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Create ECM's first admin if setup is still required.

    Returns a small status dict: skipped / created / failed.
    """
    if not enabled():
        return {"ok": True, "status": "skipped", "reason": "ecm disabled"}
    user = (username or "").strip() or "admin"
    if not password or len(password) < 8:
        return {"ok": False, "status": "failed", "reason": "password too short for ECM"}
    if user.lower() in password.lower():
        return {
            "ok": False,
            "status": "failed",
            "reason": "ECM rejects passwords that contain the username",
        }

    domain = (config.read().get("KINE_DOMAIN") or "").strip()
    mail = (email or "").strip() or (
        f"{user}@{domain}" if domain else f"{user}@localhost"
    )

    try:
        with httpx.Client(timeout=timeout) as client:
            check = client.get(f"{ECM_BASE}/api/auth/setup-required")
            if check.status_code != 200:
                return {
                    "ok": False,
                    "status": "failed",
                    "reason": f"setup-required HTTP {check.status_code}",
                }
            if not (check.json() or {}).get("required"):
                return {"ok": True, "status": "skipped", "reason": "already configured"}

            resp = client.post(
                f"{ECM_BASE}/api/auth/setup",
                json={"username": user, "email": mail, "password": password},
            )
            if resp.status_code in {200, 201}:
                return {"ok": True, "status": "created", "username": user}
            if resp.status_code == 403:
                return {"ok": True, "status": "skipped", "reason": "already configured"}
            detail = resp.text[:300]
            try:
                detail = str((resp.json() or {}).get("detail") or detail)
            except ValueError:
                pass
            return {"ok": False, "status": "failed", "reason": detail}
    except httpx.HTTPError as exc:
        return {"ok": False, "status": "failed", "reason": str(exc)}
