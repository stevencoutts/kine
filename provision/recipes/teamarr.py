"""Provision Teamarr soccer subscriptions and channel numbering."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

import tunnel_hosts

STACK = Path("/stack")
# Live-TV affinity keeps dispatcharr/ecm/teamarr in one gluetun namespace;
# same-container callers reach Dispatcharr on loopback, not kine_internal DNS.
DISPATCHARR_LOOPBACK = "http://127.0.0.1:9191"
BLOCK_SIZE = 20
BASE_START = 2000

# Select Leagues mode in Teamarr UI is soccer_mode="manual".
SOCCER_MODE = "manual"

# Channels / Output defaults (from production kore Teamarr).
DEFAULT_CHANNEL_GROUP_MODE = "{sport} | {league}"
DEFAULT_CHANNEL_PROFILE_IDS: list[str] = ["{sport}"]
DEFAULT_STREAM_PROFILE_NAMES: tuple[str, ...] = ("stable", "ffmpeg")

DEFAULT_FOLLOWED_TEAMS: list[dict[str, str]] = [
    {"provider": "espn", "team_id": "364", "name": "Liverpool"},
    {"provider": "espn", "team_id": "366", "name": "Sunderland"},
    {"provider": "espn", "team_id": "19973", "name": "Arsenal"},
]

# Named event templates + league/sport bindings (kore production).
DEFAULT_TEMPLATE_ASSIGNMENTS: list[dict[str, Any]] = [
    {
        "name": "EPL Default",
        "sports": None,
        "leagues": ["eng.1", "eng.league_cup", "eng.fa"],
    },
    {
        "name": "Europe",
        "sports": ["soccer"],
        "leagues": [
            "uefa.champions",
            "uefa.champions_qual",
            "uefa.europa.conf",
            "uefa.europa",
        ],
    },
    {
        "name": "World Cup",
        "sports": ["soccer"],
        "leagues": ["fifa.world", "fifa.wcq.ply"],
    },
    {
        "name": "Boxing",
        "sports": ["boxing"],
        "leagues": ["boxing"],
    },
    {
        "name": "UFC",
        "sports": ["mma"],
        "leagues": ["ufc"],
    },
]

_PLACEHOLDER_TEMPLATE_NAMES = {
    "soccer club event (starter)",
    "soccer club event",
    "default event (starter)",
    "default event",
}


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


def epg_timezone_from_env() -> str:
    """Teamarr EPG display/schedule timezone — same as the appliance clock."""
    return (
        (os.environ.get("KINE_TIMEZONE") or "").strip()
        or (os.environ.get("TZ") or "").strip()
    )


def resolve_stream_profile_id(profiles: list[Any]) -> int | None:
    """Prefer Stable (kore), else ffmpeg — first match by case-insensitive name."""
    by_name: dict[str, int] = {}
    for row in profiles:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        name = str(row.get("name") or "").strip().lower()
        if name and name not in by_name:
            by_name[name] = int(row["id"])
    for want in DEFAULT_STREAM_PROFILE_NAMES:
        if want in by_name:
            return by_name[want]
    return None


def _template_seeds() -> list[dict[str, Any]]:
    path = Path(__file__).with_name("teamarr_template_seeds.json")
    try:
        data = json.loads(path.read_text() or "[]")
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        seed = row.get("seed") if isinstance(row.get("seed"), dict) else row
        name = str(seed.get("name") or row.get("name") or "").strip()
        if not name:
            continue
        out.append({"name": name, "seed": dict(seed)})
    return out


def _list_templates(http: httpx.Client) -> list[dict[str, Any]]:
    resp = http.get("/api/v1/templates")
    resp.raise_for_status()
    data = resp.json() if resp.content else []
    if isinstance(data, dict):
        data = data.get("templates") or data.get("items") or []
    return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


def _list_assignments(http: httpx.Client) -> list[dict[str, Any]]:
    resp = http.get("/api/v1/subscription-templates")
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    existing = data.get("templates") if isinstance(data, dict) else data
    return [t for t in existing if isinstance(t, dict)] if isinstance(existing, list) else []


def _is_placeholder_assignments(assignments: list[dict[str, Any]]) -> bool:
    """True when empty or only the single auto-seeded Soccer starter."""
    if not assignments:
        return True
    if len(assignments) != 1:
        return False
    row = assignments[0]
    name = str(row.get("template_name") or "").strip().lower()
    leagues = row.get("leagues")
    if leagues not in (None, [], ()):
        return False
    if name in _PLACEHOLDER_TEMPLATE_NAMES:
        return True
    sports = row.get("sports") or []
    if isinstance(sports, list) and len(sports) == 1:
        return str(sports[0]).strip().lower() == "soccer"
    return False


def _ensure_named_template(
    http: httpx.Client,
    log: Callable[[str], None],
    *,
    name: str,
    seeds_by_name: dict[str, dict[str, Any]],
    existing: list[dict[str, Any]],
) -> int | None:
    for row in existing:
        if str(row.get("name") or "").strip().lower() == name.lower() and row.get("id") is not None:
            return int(row["id"])
    seed = seeds_by_name.get(name.lower())
    if not seed:
        log(f"teamarr: no seed payload for template {name!r}")
        return None
    body = dict(seed)
    body["name"] = name
    resp = http.post("/api/v1/templates", json=body)
    resp.raise_for_status()
    created = resp.json() if resp.content else {}
    tid = created.get("id") if isinstance(created, dict) else None
    if tid is None:
        log(f"teamarr: create template {name!r} returned no id")
        return None
    existing.append({"id": int(tid), "name": name, "template_type": body.get("template_type")})
    log(f"teamarr: created template {name!r} ({tid})")
    return int(tid)


def ensure_default_templates(http: httpx.Client, log: Callable[[str], None]) -> bool:
    """Create kore-style templates and assign them when still on first-run placeholders."""
    assignments = _list_assignments(http)
    if not _is_placeholder_assignments(assignments):
        log("teamarr: subscription templates already customized")
        return False

    for row in assignments:
        aid = row.get("id")
        if aid is None:
            continue
        resp = http.delete(f"/api/v1/subscription-templates/{int(aid)}")
        resp.raise_for_status()
        log(f"teamarr: removed placeholder template assignment {aid}")

    templates = _list_templates(http)
    seeds_by_name = {s["name"].lower(): s["seed"] for s in _template_seeds()}
    changed = False
    for spec in DEFAULT_TEMPLATE_ASSIGNMENTS:
        name = str(spec["name"])
        tid = _ensure_named_template(
            http, log, name=name, seeds_by_name=seeds_by_name, existing=templates,
        )
        if tid is None:
            continue
        resp = http.post(
            "/api/v1/subscription-templates",
            json={
                "template_id": tid,
                "sports": spec.get("sports"),
                "leagues": spec.get("leagues"),
            },
        )
        resp.raise_for_status()
        log(f"teamarr: assigned template {name!r} ({tid})")
        changed = True
    return changed


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


def _channel_output_is_stock(disp: dict[str, Any]) -> bool:
    mode = str(disp.get("default_channel_group_mode") or "").strip().lower()
    profiles = disp.get("default_channel_profile_ids")
    stream_id = disp.get("default_stream_profile_id")
    stock_mode = mode in ("", "static")
    stock_profiles = profiles in (None, [], ())
    stock_stream = stream_id in (None, "")
    return stock_mode and stock_profiles and stock_stream


def apply_channel_output_defaults(
    disp: dict[str, Any],
    http: httpx.Client,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Stamp Channels/Output defaults onto a Dispatcharr settings PUT body when stock."""
    if not _channel_output_is_stock(disp):
        log("teamarr: channel output already customized")
        return disp

    disp = dict(disp)
    disp["default_channel_group_mode"] = DEFAULT_CHANNEL_GROUP_MODE
    disp["default_channel_profile_ids"] = list(DEFAULT_CHANNEL_PROFILE_IDS)
    try:
        resp = http.get("/api/v1/dispatcharr/stream-profiles")
        resp.raise_for_status()
        profiles = resp.json() if resp.content else []
    except httpx.HTTPError as exc:
        log(f"teamarr: stream profiles unavailable ({exc})")
        profiles = []
    if not isinstance(profiles, list):
        profiles = []
    stream_id = resolve_stream_profile_id(profiles)
    if stream_id is not None:
        disp["default_stream_profile_id"] = stream_id
        log(f"teamarr: stream profile -> {stream_id}")
    else:
        log("teamarr: no Stable/ffmpeg stream profile found")
    log(f"teamarr: channel group mode -> {DEFAULT_CHANNEL_GROUP_MODE}")
    log(f"teamarr: channel profiles -> {DEFAULT_CHANNEL_PROFILE_IDS}")
    return disp


# Reserved channel blocks (soccer 20-wide from 2000; combat matches kore).
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
    {"id": "boxing", "name": "Boxing", "channel_start": 3000},
    {"id": "ufc", "name": "Ultimate Fighting Championship", "channel_start": 3500},
]

_RESERVED = {row["id"]: int(row["channel_start"]) for row in DEFAULT_LEAGUES}
_LAST_RESERVED = max(_RESERVED.values())  # 3500


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
    base_url: str | None = None,
    client: httpx.Client | None = None,
    wait_timeout: float = 120.0,
) -> dict[str, Any]:
    """Apply sports subscription + channel numbering (+ Dispatcharr URL)."""
    rows = assign_channel_starts(leagues or load_leagues()["leagues"])
    own = client is None
    teamarr_base = base_url or tunnel_hosts.internal_base_for_app("teamarr", 9195)
    http = client or httpx.Client(base_url=teamarr_base, timeout=30.0)
    try:
        if not wait_ready(http, timeout=wait_timeout):
            log("teamarr: not ready in time")
            return {"ok": False, "reason": "not ready"}

        followed = list(DEFAULT_FOLLOWED_TEAMS)
        try:
            cur = http.get("/api/v1/sports-subscription")
            if cur.status_code == 200:
                body = cur.json() if cur.content else {}
                existing = body.get("soccer_followed_teams") if isinstance(body, dict) else None
                if isinstance(existing, list) and existing:
                    followed = existing
                    log("teamarr: keeping existing followed teams")
        except (httpx.HTTPError, ValueError):
            pass

        sub_body = {
            "leagues": [r["id"] for r in rows],
            "soccer_mode": SOCCER_MODE,
            "soccer_followed_teams": followed,
        }
        resp = http.put("/api/v1/sports-subscription", json=sub_body)
        resp.raise_for_status()
        log(f"teamarr: subscribed {len(rows)} league(s)")

        starts = {r["id"]: r["channel_start"] for r in rows}
        num_body = {
            "global_channel_mode": "manual",
            "league_channel_starts": starts,
        }
        resp = http.put("/api/v1/settings/channel-numbering", json=num_body)
        resp.raise_for_status()
        log("teamarr: channel numbering set (manual blocks)")

        ensure_default_templates(http, log)
        ensure_epg_settings(http, log, art_base_url=art_base_url)

        resp = http.get("/api/v1/settings/dispatcharr")
        resp.raise_for_status()
        current_disp = resp.json() if resp.content else {}
        if not isinstance(current_disp, dict):
            current_disp = {}

        # Connection fields only — never echo GET's redacted password.
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

        if _channel_output_is_stock(current_disp):
            stamped = apply_channel_output_defaults(current_disp, http, log)
            for key in (
                "default_channel_group_mode",
                "default_channel_profile_ids",
                "default_stream_profile_id",
            ):
                if key in stamped:
                    disp[key] = stamped[key]
        else:
            log("teamarr: channel output already customized")

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
