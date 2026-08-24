"""Reachability of Settings-configured Plex and Emby for the Media overview."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from . import config
from .watching import _configured, _env_bool, _get_json, media_base


def parse_plex_identity(payload: dict) -> dict:
    container = payload.get("MediaContainer") or payload
    return {
        "name": (container.get("friendlyName") or "Plex").strip() or "Plex",
        "version": (container.get("version") or "").strip() or None,
        "platform": (container.get("platform") or "").strip() or None,
    }


def parse_emby_info(payload: dict) -> dict:
    return {
        "name": (payload.get("ServerName") or "Emby").strip() or "Emby",
        "version": (payload.get("Version") or "").strip() or None,
        "platform": (payload.get("OperatingSystem") or "").strip() or None,
    }


async def _plex_status(env: dict) -> dict | None:
    host = env.get("PLEX_HOST", "").strip()
    token = env.get("PLEX_TOKEN", "").strip()
    if not host or not token:
        return None
    port = env.get("PLEX_PORT", "").strip() or "32400"
    ssl = _env_bool(env.get("PLEX_USE_SSL", ""))
    base = media_base(host, port, ssl)
    row = {
        "id": "plex",
        "label": "Plex",
        "host": host,
        "url": f"{base}/web",
        "ok": False,
        "name": None,
        "version": None,
        "detail": None,
        "error": None,
    }
    try:
        data = await _get_json(
            f"{base}/identity",
            {"X-Plex-Token": token, "Accept": "application/json"},
        )
        info = parse_plex_identity(data if isinstance(data, dict) else {})
        row.update({
            "ok": True,
            "name": info["name"],
            "version": info["version"],
            "detail": _detail(info),
        })
    except httpx.HTTPError as exc:
        row["error"] = str(exc)
    return row


async def _emby_status(env: dict) -> dict | None:
    token = env.get("EMBY_API_KEY", "").strip()
    host = env.get("EMBY_HOST", "").strip()
    if not token:
        return None
    candidates: list[tuple[str, str]] = []
    if host:
        port = env.get("EMBY_PORT", "").strip() or (
            "443" if _env_bool(env.get("EMBY_USE_SSL", "")) else "8096"
        )
        ssl = _env_bool(env.get("EMBY_USE_SSL", ""))
        candidates.append((host, media_base(host, port, ssl)))
    if "emby" in config.profiles():
        internal = "http://emby:8096"
        if not any(base == internal for _, base in candidates):
            candidates.append(("emby (stack)", internal))
    if not candidates:
        return None

    display_host, base = candidates[0]
    row = {
        "id": "emby",
        "label": "Emby",
        "host": display_host,
        "url": f"{base}/web",
        "ok": False,
        "name": None,
        "version": None,
        "detail": None,
        "error": None,
    }
    last_error: str | None = None
    for display_host, base in candidates:
        try:
            data = await _get_json(
                f"{base}/System/Info",
                {"X-Emby-Token": token},
            )
            if not isinstance(data, dict):
                data = {}
            info = parse_emby_info(data)
            row.update({
                "ok": True,
                "host": display_host,
                "url": f"{base}/web",
                "name": info["name"],
                "version": info["version"],
                "detail": _detail(info),
                "error": None,
            })
            return row
        except httpx.HTTPError as exc:
            last_error = str(exc)
    row["error"] = last_error
    return row


def _detail(info: dict) -> str:
    bits = [b for b in (info.get("name"), info.get("version")) if b]
    return " · ".join(bits) if bits else "online"


async def snapshot() -> dict[str, Any]:
    env = config.read()
    plex_task = asyncio.create_task(_plex_status(env))
    emby_task = asyncio.create_task(_emby_status(env))
    plex, emby = await plex_task, await emby_task
    servers = [s for s in (plex, emby) if s]
    online = sum(1 for s in servers if s.get("ok"))
    return {
        "ok": True,
        "configured": {
            "plex": _configured(env, "PLEX_HOST", "PLEX_TOKEN"),
            "emby": bool(env.get("EMBY_API_KEY", "").strip()) and (
                _configured(env, "EMBY_HOST", "EMBY_API_KEY") or "emby" in config.profiles()
            ),
        },
        "count": len(servers),
        "online": online,
        "servers": servers,
    }
