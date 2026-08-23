"""Now-playing sessions from Plex and Emby, using Settings credentials."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from . import config


def media_base(host: str, port: str | int, use_ssl: bool) -> str:
    scheme = "https" if use_ssl else "http"
    return f"{scheme}://{host}:{port}"


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def progress_pct(position: int | float | None, duration: int | float | None) -> int | None:
    if not duration or duration <= 0 or position is None:
        return None
    return max(0, min(100, round(100 * float(position) / float(duration))))


def _season_episode(season: Any, episode: Any, title: str) -> str:
    try:
        s, e = int(season), int(episode)
    except (TypeError, ValueError):
        return title
    return f"S{s:02d}E{e:02d} {title}".strip()


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def parse_plex_sessions(payload: dict) -> list[dict]:
    container = payload.get("MediaContainer") or {}
    rows: list[dict] = []
    for item in _as_list(container.get("Metadata")):
        if not isinstance(item, dict):
            continue
        kind = (item.get("type") or "").lower()
        title = (item.get("title") or "Untitled").strip()
        year = item.get("year")
        if kind == "episode":
            show = (item.get("grandparentTitle") or "").strip()
            ep = _season_episode(item.get("parentIndex"), item.get("index"), title)
            display = f"{show} — {ep}" if show else ep
        elif year:
            display = f"{title} ({year})"
        else:
            display = title
        user = ((item.get("User") or {}).get("title") or "").strip()
        player = item.get("Player") or {}
        rows.append({
            "server": "plex",
            "title": display,
            "user": user or "unknown",
            "player": (player.get("title") or player.get("product") or "").strip() or "Plex",
            "state": (player.get("state") or "playing").lower(),
            "progress": progress_pct(item.get("viewOffset"), item.get("duration")),
        })
    return rows


def parse_emby_sessions(payload: list | dict) -> list[dict]:
    sessions = payload if isinstance(payload, list) else payload.get("Items") or []
    rows: list[dict] = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        now = item.get("NowPlayingItem")
        if not isinstance(now, dict):
            continue
        kind = (now.get("Type") or "").lower()
        title = (now.get("Name") or "Untitled").strip()
        year = now.get("ProductionYear")
        if kind == "episode":
            show = (now.get("SeriesName") or "").strip()
            ep = _season_episode(now.get("ParentIndexNumber"), now.get("IndexNumber"), title)
            display = f"{show} — {ep}" if show else ep
        elif year:
            display = f"{title} ({year})"
        else:
            display = title
        play = item.get("PlayState") or {}
        paused = bool(play.get("IsPaused"))
        runtime = now.get("RunTimeTicks")
        position = play.get("PositionTicks")
        rows.append({
            "server": "emby",
            "title": display,
            "user": (item.get("UserName") or "unknown").strip(),
            "player": (item.get("DeviceName") or item.get("Client") or "Emby").strip(),
            "state": "paused" if paused else "playing",
            "progress": progress_pct(position, runtime),
        })
    return rows


def _configured(env: dict, host_key: str, secret_key: str) -> bool:
    return bool(env.get(host_key, "").strip() and env.get(secret_key, "").strip())


async def _get_json(url: str, headers: dict, params: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=6.0, verify=False, follow_redirects=True) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()


async def _plex_sessions(env: dict) -> tuple[list[dict], str | None]:
    host = env.get("PLEX_HOST", "").strip()
    token = env.get("PLEX_TOKEN", "").strip()
    if not host or not token:
        return [], None
    port = env.get("PLEX_PORT", "").strip() or "32400"
    ssl = _env_bool(env.get("PLEX_USE_SSL", ""))
    url = f"{media_base(host, port, ssl)}/status/sessions"
    try:
        data = await _get_json(
            url,
            {"X-Plex-Token": token, "Accept": "application/json"},
        )
        return parse_plex_sessions(data if isinstance(data, dict) else {}), None
    except httpx.HTTPError as exc:
        return [], str(exc)


async def _emby_sessions(env: dict) -> tuple[list[dict], str | None]:
    token = env.get("EMBY_API_KEY", "").strip()
    host = env.get("EMBY_HOST", "").strip()
    candidates: list[str] = []
    if host and token:
        port = env.get("EMBY_PORT", "").strip() or (
            "443" if _env_bool(env.get("EMBY_USE_SSL", "")) else "8096"
        )
        ssl = _env_bool(env.get("EMBY_USE_SSL", ""))
        candidates.append(media_base(host, port, ssl))
    if "emby" in config.profiles() and token:
        internal = "http://emby:8096"
        if internal not in candidates:
            candidates.append(internal)
    if not token or not candidates:
        return [], None
    last_error: str | None = None
    for base in candidates:
        try:
            data = await _get_json(
                f"{base}/Sessions",
                {"X-Emby-Token": token},
                {"ActiveWithinSeconds": 300},
            )
            return parse_emby_sessions(data), None
        except httpx.HTTPError as exc:
            last_error = str(exc)
    return [], last_error


async def snapshot() -> dict:
    env = config.read()
    plex_task = asyncio.create_task(_plex_sessions(env))
    emby_task = asyncio.create_task(_emby_sessions(env))
    plex_rows, plex_error = await plex_task
    emby_rows, emby_error = await emby_task
    sessions = [*plex_rows, *emby_rows]
    return {
        "ok": True,
        "configured": {
            "plex": _configured(env, "PLEX_HOST", "PLEX_TOKEN"),
            "emby": bool(env.get("EMBY_API_KEY", "").strip()) and (
                _configured(env, "EMBY_HOST", "EMBY_API_KEY") or "emby" in config.profiles()
            ),
        },
        "count": len(sessions),
        "sessions": sessions,
        "errors": {k: v for k, v in (("plex", plex_error), ("emby", emby_error)) if v},
    }
