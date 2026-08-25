"""Provision Teamarr soccer subscriptions and channel numbering."""
from __future__ import annotations

import json
import os
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
SOCCER_TEMPLATE_SPORT = "Soccer"
SOCCER_TEMPLATE_NAME_HINTS = (
    "soccer club event",
    "soccer event",
    "default event",
)


def art_base_url_from_env() -> str:
    """Public Game-Thumbs origin for Teamarr template art paths."""
    explicit = (os.environ.get("GAME_THUMBS_PUBLIC_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    domain = (os.environ.get("KINE_DOMAIN") or "").strip().rstrip(".")
    if not domain:
        return ""
    # Traefik often isn't on 443 (host nginx already owns it). Emby/EPG
    # clients fetch absolute art URLs, so the port must be in the origin.
    port = (os.environ.get("TRAEFIK_HTTPS_PORT") or "443").strip() or "443"
    if port == "443":
        return f"https://thumbs.{domain}"
    return f"https://thumbs.{domain}:{port}"


def _pick_soccer_template_id(templates: list[Any]) -> int | None:
    """Prefer Teamarr's Soccer Club Event starter, else any event template."""
    rows = [t for t in templates if isinstance(t, dict) and t.get("id") is not None]
    by_hint: dict[str, int] = {}
    for row in rows:
        name = str(row.get("name") or "").strip().lower()
        for hint in SOCCER_TEMPLATE_NAME_HINTS:
            if hint in name and hint not in by_hint:
                by_hint[hint] = int(row["id"])
    for hint in SOCCER_TEMPLATE_NAME_HINTS:
        if hint in by_hint:
            return by_hint[hint]
    for row in rows:
        if str(row.get("template_type") or "").lower() == "event":
            return int(row["id"])
    return None


def ensure_soccer_template(http: httpx.Client, log: Callable[[str], None]) -> bool:
    """Assign a global Soccer template when none exist (required for generation)."""
    resp = http.get("/api/v1/subscription-templates")
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    existing = data.get("templates") if isinstance(data, dict) else data
    if isinstance(existing, list) and existing:
        log("teamarr: subscription template already assigned")
        return False

    resp = http.get("/api/v1/templates")
    resp.raise_for_status()
    templates = resp.json() if resp.content else []
    if isinstance(templates, dict):
        templates = templates.get("templates") or templates.get("items") or []
    template_id = _pick_soccer_template_id(templates if isinstance(templates, list) else [])
    if template_id is None:
        log("teamarr: no event template available to assign")
        return False

    resp = http.post(
        "/api/v1/subscription-templates",
        json={"template_id": template_id, "sports": [SOCCER_TEMPLATE_SPORT], "leagues": None},
    )
    resp.raise_for_status()
    log(f"teamarr: assigned template {template_id} to Soccer")
    return True


def epg_timezone_from_env() -> str:
    """Teamarr EPG display/schedule timezone — same as the appliance clock."""
    return (
        (os.environ.get("KINE_TIMEZONE") or "").strip()
        or (os.environ.get("TZ") or "").strip()
    )


def ensure_epg_settings(
    http: httpx.Client,
    log: Callable[[str], None],
    *,
    art_base_url: str = "",
    epg_timezone: str = "",
) -> bool:
    """Align Teamarr EPG settings with Kine (art CDN + timezone)."""
    base = (art_base_url or art_base_url_from_env()).strip().rstrip("/")
    tz = (epg_timezone or epg_timezone_from_env()).strip()
    if not base and not tz:
        log("teamarr: no EPG art URL or timezone to apply")
        return False

    resp = http.get("/api/v1/settings/epg")
    resp.raise_for_status()
    body = resp.json() if resp.content else {}
    if not isinstance(body, dict):
        body = {}

    changed = False
    if base:
        current = (body.get("art_base_url") or "").strip().rstrip("/")
        if current != base:
            body["art_base_url"] = base
            changed = True
            log(f"teamarr: art_base_url -> {base}")
        else:
            log("teamarr: art_base_url already set")
    if tz:
        current_tz = (body.get("epg_timezone") or "").strip()
        if current_tz != tz:
            body["epg_timezone"] = tz
            changed = True
            log(f"teamarr: epg_timezone -> {tz}")
        else:
            log("teamarr: epg_timezone already set")

    if not changed:
        return False
    resp = http.put("/api/v1/settings/epg", json=body)
    resp.raise_for_status()
    return True


def ensure_art_base_url(
    http: httpx.Client,
    log: Callable[[str], None],
    art_base_url: str = "",
) -> bool:
    """Backward-compatible wrapper for Game-Thumbs art_base_url only."""
    return ensure_epg_settings(http, log, art_base_url=art_base_url, epg_timezone="")


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
    art_base_url: str = "",
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

        ensure_soccer_template(http, log)
        ensure_epg_settings(http, log, art_base_url=art_base_url)

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
