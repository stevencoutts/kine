"""Provision Teamarr soccer subscriptions and channel numbering."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

STACK = Path("/stack")
TEAMARR_BASE = "http://gluetun:9195"
DISPATCHARR_LOOPBACK = "http://127.0.0.1:9191"
BLOCK_SIZE = 20
BASE_START = 2000

# Select Leagues mode in Teamarr UI is soccer_mode="manual".
SOCCER_MODE = "manual"

# Reserved 20-number blocks starting at 2000 (product defaults).
DEFAULT_LEAGUES: list[dict[str, Any]] = [
    {"id": "eng.1", "name": "English Premier League", "channel_start": 2000},
    {"id": "eng.fa", "name": "FA Cup", "channel_start": 2020},
    {"id": "eng.league_cup", "name": "Carabao Cup", "channel_start": 2040},
    {"id": "uefa.champions", "name": "UEFA Champions League", "channel_start": 2060},
    {"id": "uefa.champions_qual", "name": "UEFA Champions League Qualifying", "channel_start": 2080},
    {"id": "uefa.europa", "name": "UEFA Europa League", "channel_start": 2100},
    {"id": "uefa.europa.conf", "name": "UEFA Europa Conference League", "channel_start": 2120},
    {"id": "fifa.world", "name": "FIFA World Cup", "channel_start": 2140},
    {"id": "fifa.wcq.ply", "name": "FIFA World Cup Qualifying - Playoff Tournament", "channel_start": 2160},
]

_RESERVED = {row["id"]: int(row["channel_start"]) for row in DEFAULT_LEAGUES}
_LAST_RESERVED = max(_RESERVED.values())  # 2160


def _leagues_path() -> Path:
    return STACK / "config" / "teamarr" / "leagues.json"


def assign_channel_starts(leagues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach channel_start to each selected league (reserved or next free block)."""
    if not leagues:
        raise ValueError("select at least one league")
    out: list[dict[str, Any]] = []
    next_extra = _LAST_RESERVED + BLOCK_SIZE
    for raw in leagues:
        lid = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or lid).strip() or lid
        if not lid:
            raise ValueError("league id required")
        if lid in _RESERVED:
            start = _RESERVED[lid]
        else:
            start = next_extra
            next_extra += BLOCK_SIZE
        out.append({"id": lid, "name": name, "channel_start": int(start)})
    return out


def save_leagues(leagues: list[dict[str, Any]]) -> Path:
    rows = assign_channel_starts(leagues)
    path = _leagues_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "soccer_mode": SOCCER_MODE,
        "leagues": rows,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(body, indent=2) + "\n")
    return path


def load_leagues() -> dict[str, Any]:
    path = _leagues_path()
    if not path.is_file():
        return {
            "soccer_mode": SOCCER_MODE,
            "leagues": [dict(row) for row in DEFAULT_LEAGUES],
            "updated_at": None,
        }
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    leagues = data.get("leagues") if isinstance(data.get("leagues"), list) else []
    cleaned = []
    for row in leagues:
        if not isinstance(row, dict):
            continue
        lid = str(row.get("id") or "").strip()
        if not lid:
            continue
        cleaned.append({
            "id": lid,
            "name": str(row.get("name") or lid),
            "channel_start": int(row.get("channel_start") or _RESERVED.get(lid) or BASE_START),
        })
    if not cleaned:
        cleaned = [dict(row) for row in DEFAULT_LEAGUES]
    return {
        "soccer_mode": SOCCER_MODE,
        "leagues": cleaned,
        "updated_at": data.get("updated_at"),
    }


def wait_ready(
    client: httpx.Client,
    *,
    timeout: float = 120.0,
    interval: float = 2.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/health")
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                startup = data.get("startup") if isinstance(data, dict) else {}
                if isinstance(startup, dict) and startup.get("is_ready"):
                    return True
                if isinstance(data, dict) and data.get("status") == "healthy":
                    return True
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(interval)
    return False


def configure(
    leagues: list[dict[str, Any]] | None,
    log: Callable[[str], None],
    *,
    dispatcharr_token: str = "",
    dispatcharr_username: str = "",
    dispatcharr_password: str = "",
    base_url: str = TEAMARR_BASE,
    client: httpx.Client | None = None,
    wait_timeout: float = 120.0,
) -> dict[str, Any]:
    """Apply sports subscription + channel numbering (+ Dispatcharr URL)."""
    rows = assign_channel_starts(leagues or load_leagues()["leagues"])
    own = client is None
    http = client or httpx.Client(base_url=base_url, timeout=30.0)
    try:
        if not wait_ready(http, timeout=wait_timeout):
            log("teamarr: not ready in time")
            return {"ok": False, "reason": "not ready"}

        sub_body = {
            "leagues": [r["id"] for r in rows],
            "soccer_mode": SOCCER_MODE,
            "soccer_followed_teams": [],
        }
        resp = http.put("/api/v1/sports-subscription", json=sub_body)
        resp.raise_for_status()
        log(f"teamarr: subscribed {len(rows)} soccer league(s)")

        starts = {r["id"]: r["channel_start"] for r in rows}
        num_body = {
            "global_channel_mode": "manual",
            "league_channel_starts": starts,
        }
        resp = http.put("/api/v1/settings/channel-numbering", json=num_body)
        resp.raise_for_status()
        log("teamarr: channel numbering set (manual, 2000s blocks)")

        disp: dict[str, Any] = {
            "enabled": True,
            "url": DISPATCHARR_LOOPBACK,
        }
        if dispatcharr_username:
            disp["username"] = dispatcharr_username
        if dispatcharr_password:
            disp["password"] = dispatcharr_password
        # Teamarr authenticates via JWT username/password only; the API token
        # is for ECM / Helm X-API-Key clients and is unused here.
        _ = dispatcharr_token
        resp = http.put("/api/v1/settings/dispatcharr", json=disp)
        resp.raise_for_status()
        log("teamarr: Dispatcharr URL set to loopback")
        return {"ok": True, "leagues": rows}
    except httpx.HTTPError as exc:
        log(f"teamarr: configure failed ({exc})")
        return {"ok": False, "reason": str(exc)}
    finally:
        if own and hasattr(http, "close"):
            http.close()
