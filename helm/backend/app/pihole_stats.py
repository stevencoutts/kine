"""Pi-hole v6 summary for the Helm Stats page.

Reads the API inside the stack. The web password stays in .env and is
sent only as the auth body; it is not returned or logged.
"""
from __future__ import annotations

import time

import httpx

from . import config

BASE = "http://pihole"
CACHE_TTL = 30.0
_cache: dict[str, object] = {"at": 0.0, "data": None}


def parse_summary(payload: dict) -> dict | None:
    """Map `/api/stats/summary` onto the four figures the Stats page shows."""
    if not isinstance(payload, dict):
        return None
    queries = payload.get("queries")
    if not isinstance(queries, dict):
        return None
    total = _count(queries.get("total"))
    blocked = _count(queries.get("blocked"))
    if total is None or blocked is None:
        return None
    percent = queries.get("percent_blocked")
    try:
        percent_blocked = round(float(percent), 1)
    except (TypeError, ValueError):
        percent_blocked = round(100 * blocked / total, 1) if total else 0.0
    out = {
        "queries": total,
        "blocked": blocked,
        "percent_blocked": percent_blocked,
    }
    gravity = payload.get("gravity")
    if isinstance(gravity, dict):
        domains = _count(gravity.get("domains_being_blocked"))
        if domains is not None:
            out["domains_blocked"] = domains
    return out


def _count(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def summary() -> dict | None:
    """Queries, blocked queries, percent, and blocklist size.

    None when the profile is off, the password is unset, or Pi-hole
    does not answer. Callers omit the panel in that case.
    """
    if "pihole" not in config.profiles():
        return None
    password = (config.read().get("PIHOLE_WEBPASSWORD") or "").strip()
    if not password:
        return None
    now = time.monotonic()
    if now - float(_cache["at"]) < CACHE_TTL:
        return _cache["data"]  # type: ignore[return-value]
    data = await _fetch(password)
    _cache["at"] = now
    _cache["data"] = data
    return data


async def _fetch(password: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            auth = await client.post(f"{BASE}/api/auth", json={"password": password})
            auth.raise_for_status()
            session = auth.json().get("session") or {}
            sid = str(session.get("sid") or "").strip()
            if not sid or session.get("valid") is False:
                return None
            headers = {"X-FTL-SID": sid}
            try:
                response = await client.get(f"{BASE}/api/stats/summary", headers=headers)
                response.raise_for_status()
                return parse_summary(response.json())
            finally:
                try:
                    await client.delete(f"{BASE}/api/auth", headers=headers)
                except httpx.HTTPError:
                    pass
    except (httpx.HTTPError, ValueError):
        return None
