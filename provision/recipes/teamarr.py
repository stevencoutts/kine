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


def _env_bool(key: str) -> bool:
    return (os.environ.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}


def emby_target_from_env() -> tuple[str, str] | None:
    """Helm Settings Emby (remote or bundled) as Teamarr's artwork target."""
    key = (os.environ.get("EMBY_API_KEY") or "").strip()
    host = (os.environ.get("EMBY_HOST") or "").strip().rstrip("/")
    if not key or not host:
        return None
    use_ssl = _env_bool("EMBY_USE_SSL")
    raw_port = (os.environ.get("EMBY_PORT") or "").strip()
    try:
        port = int(raw_port) if raw_port else (443 if use_ssl else 8096)
    except ValueError:
        port = 443 if use_ssl else 8096
    if use_ssl and port == 8096:
        port = 443
    scheme = "https" if use_ssl else "http"
    if (use_ssl and port == 443) or (not use_ssl and port == 80):
        url = f"{scheme}://{host}"
    else:
        url = f"{scheme}://{host}:{port}"
    return url, key


def ensure_emby_settings(
    http: httpx.Client,
    log: Callable[[str], None],
    *,
    url: str = "",
    api_key: str = "",
) -> bool:
    """Point Teamarr at the Emby from Helm Settings when host + API key are set."""
    if not url or not api_key:
        target = emby_target_from_env()
        if not target:
            log("teamarr: no Helm Emby host/API key, skipping Emby integration")
            return False
        url, api_key = target
    url = url.strip().rstrip("/")
    resp = http.get("/api/v1/settings/emby")
    resp.raise_for_status()
    current = resp.json() if resp.content else {}
    if not isinstance(current, dict):
        current = {}
    servers = current.get("servers") if isinstance(current.get("servers"), list) else []
    first = servers[0] if servers and isinstance(servers[0], dict) else {}
    current_url = str(first.get("url") or "").strip().rstrip("/")
    if current.get("enabled") is True and current_url == url:
        log(f"teamarr: Emby already {url}")
        return False
    body = {
        "enabled": True,
        "servers": [{
            "name": str(first.get("name") or "").strip() or "Helm",
            "url": url,
            "api_key": api_key,
        }],
    }
    resp = http.put("/api/v1/settings/emby", json=body)
    resp.raise_for_status()
    log(f"teamarr: Emby -> {url}")
    return True


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


def _wanted_template_art(seed: dict[str, Any]) -> tuple[Any, Any, Any]:
    pre = seed.get("pregame_fallback") if isinstance(seed.get("pregame_fallback"), dict) else {}
    return seed.get("program_art_url"), seed.get("event_channel_logo_url"), pre.get("art_url")


def ensure_template_art_urls(http: httpx.Client, log: Callable[[str], None]) -> bool:
    """Keep Boxing/UFC Game-Thumbs paths in sync after first-run seeding."""
    seeds = {s["name"].lower(): s["seed"] for s in _template_seeds()}
    changed = False
    for row in _list_templates(http):
        name = str(row.get("name") or "").strip()
        seed = seeds.get(name.lower())
        tid = row.get("id")
        if not seed or tid is None or name.lower() not in {"boxing", "ufc"}:
            continue
        want_program, want_logo, want_pre = _wanted_template_art(seed)
        resp = http.get(f"/api/v1/templates/{int(tid)}")
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        if not isinstance(body, dict):
            body = dict(row)
        cur_pre = body.get("pregame_fallback") if isinstance(body.get("pregame_fallback"), dict) else {}
        if (
            body.get("program_art_url") == want_program
            and body.get("event_channel_logo_url") == want_logo
            and cur_pre.get("art_url") == want_pre
        ):
            continue
        body["program_art_url"] = want_program
        body["event_channel_logo_url"] = want_logo
        if isinstance(body.get("pregame_fallback"), dict):
            body["pregame_fallback"] = dict(body["pregame_fallback"])
            body["pregame_fallback"]["art_url"] = want_pre
        elif isinstance(seed.get("pregame_fallback"), dict):
            body["pregame_fallback"] = dict(seed["pregame_fallback"])
        resp = http.put(f"/api/v1/templates/{int(tid)}", json=body)
        resp.raise_for_status()
        log(f"teamarr: {name} art URLs -> {want_program}")
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


DISPATCHARR_PUT_PRESERVE = (
    "default_channel_group_mode",
    "default_channel_profile_ids",
    "default_stream_profile_id",
    "default_channel_group_id",
    "cleanup_unused_logos",
    "epg_id",
)


def preserve_dispatcharr_put_fields(
    current: dict[str, Any],
    disp: dict[str, Any],
) -> dict[str, Any]:
    """Teamarr PUT replaces Dispatcharr settings; echo existing output fields."""
    out = dict(disp)
    for key in DISPATCHARR_PUT_PRESERVE:
        if key in out:
            continue
        if key not in current:
            continue
        val = current[key]
        if key == "epg_id" and val in (None, "", 0):
            continue
        out[key] = val
    return out


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


_INACTIVE_M3U_STATUSES = {"disabled", "error"}
TEAMARR_EPG_SOURCE_NAME = "teamarr"


def resolve_teamarr_epg_source_id(sources: list[Any]) -> int | None:
    """Return the Dispatcharr EPG source id named Teamarr, if present."""
    for row in sources:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        if str(row.get("name") or "").strip().lower() == TEAMARR_EPG_SOURCE_NAME:
            try:
                return int(row["id"])
            except (TypeError, ValueError):
                return None
    return None


def ensure_dispatcharr_epg_id(
    http: httpx.Client,
    log: Callable[[str], None],
    disp: dict[str, Any],
) -> dict[str, Any]:
    """Stamp Teamarr's XMLTV source onto Dispatcharr settings when missing."""
    try:
        resp = http.get("/api/v1/dispatcharr/epg-sources")
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        log(f"teamarr: could not list EPG sources ({exc})")
        return disp
    sources = _json_list(payload, "sources", "results")
    epg_id = resolve_teamarr_epg_source_id(sources)
    if epg_id is None:
        log("teamarr: no Dispatcharr EPG source named Teamarr")
        return disp
    if disp.get("epg_id") == epg_id:
        log(f"teamarr: epg_id already {epg_id}")
        return disp
    out = dict(disp)
    out["epg_id"] = epg_id
    log(f"teamarr: epg_id -> {epg_id}")
    return out


def warn_inactive_event_group_m3us(
    http: httpx.Client,
    log: Callable[[str], None],
) -> list[dict[str, Any]]:
    """Log enabled event groups whose Dispatcharr M3U account is missing or down."""
    try:
        acc_resp = http.get("/api/v1/dispatcharr/m3u-accounts")
        acc_resp.raise_for_status()
        accounts = acc_resp.json() if acc_resp.content else []
    except (httpx.HTTPError, ValueError) as exc:
        log(f"teamarr: could not list M3U accounts ({exc})")
        return []
    if isinstance(accounts, dict):
        accounts = accounts.get("results") or accounts.get("accounts") or []
    by_id: dict[int, dict[str, Any]] = {}
    for row in accounts if isinstance(accounts, list) else []:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        by_id[int(row["id"])] = row

    try:
        grp_resp = http.get("/api/v1/groups")
        grp_resp.raise_for_status()
        payload = grp_resp.json() if grp_resp.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        log(f"teamarr: could not list event groups ({exc})")
        return []
    groups = payload.get("groups") if isinstance(payload, dict) else payload
    stale: list[dict[str, Any]] = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict) or not group.get("enabled"):
            continue
        aid = group.get("m3u_account_id")
        if aid is None or aid == "":
            continue
        try:
            account = by_id.get(int(aid))
        except (TypeError, ValueError):
            account = None
        status = str((account or {}).get("status") or "").strip().lower()
        if account is not None and status not in _INACTIVE_M3U_STATUSES:
            continue
        name = str(group.get("name") or group.get("id") or "group")
        acc_name = str(
            (account or {}).get("name")
            or group.get("m3u_account_name")
            or aid
        )
        log(
            f"teamarr: event group {name!r} bound to inactive M3U "
            f"{acc_name!r} ({aid})"
        )
        stale.append(group)
    return stale


MANAGED_GROUP_SUFFIX = " | Sports"
SPORTS_GROUP_PATTERN = (
    r"(?i)(epl|football|soccer|uefa|sport|ppv|dazn|tnt|sky sport|espn|"
    r"ufc|box|mma|fight|triller|pay.?per.?view|"
    r"nba|nfl|mlb|nhl|f1|formula|cricket|rugby|mls|liga|"
    r"bundesliga|championship|premier league|bein)"
)
STREAM_HEADER_EXCLUDE = r"^#+"


def managed_event_group_name(account_name: str) -> str:
    """Kine-managed Teamarr event group name for an M3U account."""
    return f"{str(account_name or '').strip()}{MANAGED_GROUP_SUFFIX}"


def _json_list(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
    return []


def _list_m3u_accounts(http: httpx.Client) -> list[dict[str, Any]]:
    resp = http.get("/api/v1/dispatcharr/m3u-accounts")
    resp.raise_for_status()
    rows = _json_list(resp.json() if resp.content else [], "results", "accounts")
    return [r for r in rows if isinstance(r, dict)]


def _list_event_groups(http: httpx.Client, *, include_disabled: bool = False) -> list[dict[str, Any]]:
    resp = http.get("/api/v1/groups", params={"include_disabled": include_disabled})
    resp.raise_for_status()
    rows = _json_list(resp.json() if resp.content else {}, "groups")
    return [r for r in rows if isinstance(r, dict)]


def _m3u_is_active(account: dict[str, Any]) -> bool:
    status = str(account.get("status") or "").strip().lower()
    return status not in _INACTIVE_M3U_STATUSES


def _event_group_payload(account: dict[str, Any]) -> dict[str, Any]:
    name = str(account.get("name") or "").strip()
    return {
        "name": managed_event_group_name(name),
        "m3u_account_id": int(account["id"]),
        "m3u_account_name": name,
        "m3u_group_name_pattern": SPORTS_GROUP_PATTERN,
        "m3u_group_name_pattern_enabled": True,
        "stream_exclude_regex": STREAM_HEADER_EXCLUDE,
        "stream_exclude_regex_enabled": True,
        "duplicate_event_handling": "consolidate",
        "channel_assignment_mode": "auto",
        "name_match_enabled": True,
        "enabled": True,
    }


def _set_group_enabled(http: httpx.Client, group_id: int, enabled: bool) -> None:
    action = "enable" if enabled else "disable"
    resp = http.post(f"/api/v1/groups/{int(group_id)}/{action}")
    resp.raise_for_status()


def ensure_event_groups(http: httpx.Client, log: Callable[[str], None]) -> int:
    """Upsert one sports-pattern event group per active Dispatcharr M3U account."""
    try:
        accounts = _list_m3u_accounts(http)
        groups = _list_event_groups(http, include_disabled=True)
    except (httpx.HTTPError, ValueError) as exc:
        log(f"teamarr: could not list M3U accounts/groups ({exc})")
        return 0

    created = 0
    by_account: dict[int, list[dict[str, Any]]] = {}
    for group in groups:
        aid = group.get("m3u_account_id")
        if aid is None or aid == "":
            continue
        try:
            by_account.setdefault(int(aid), []).append(group)
        except (TypeError, ValueError):
            continue

    seen_active: set[int] = set()
    for account in accounts:
        if account.get("id") is None:
            continue
        try:
            aid = int(account["id"])
        except (TypeError, ValueError):
            continue
        name = str(account.get("name") or "").strip()
        if not name:
            continue
        want = managed_event_group_name(name)
        siblings = by_account.get(aid, [])
        managed = next((g for g in siblings if str(g.get("name") or "") == want), None)

        if not _m3u_is_active(account):
            if managed and managed.get("enabled"):
                try:
                    _set_group_enabled(http, int(managed["id"]), False)
                    managed["enabled"] = False
                    log(f"teamarr: disabled event group {want!r} (M3U inactive)")
                except (httpx.HTTPError, TypeError, ValueError) as exc:
                    log(f"teamarr: could not disable {want!r} ({exc})")
            continue

        seen_active.add(aid)
        payload = _event_group_payload(account)
        if managed is None:
            try:
                resp = http.post("/api/v1/groups", json=payload)
                resp.raise_for_status()
                created += 1
                log(f"teamarr: created event group {want!r}")
            except httpx.HTTPError as exc:
                log(f"teamarr: create event group {want!r} failed ({exc})")
                continue
        else:
            if not managed.get("enabled"):
                try:
                    _set_group_enabled(http, int(managed["id"]), True)
                    managed["enabled"] = True
                    log(f"teamarr: enabled event group {want!r}")
                except (httpx.HTTPError, TypeError, ValueError) as exc:
                    log(f"teamarr: could not enable {want!r} ({exc})")
            needs = any(
                managed.get(key) != value
                for key, value in payload.items()
                if key != "enabled"
            )
            if needs:
                try:
                    resp = http.put(f"/api/v1/groups/{int(managed['id'])}", json=payload)
                    resp.raise_for_status()
                    managed.update(payload)
                    log(f"teamarr: updated event group {want!r}")
                except (httpx.HTTPError, TypeError, ValueError) as exc:
                    log(f"teamarr: update event group {want!r} failed ({exc})")

        for sibling in siblings:
            sid = sibling.get("id")
            if sid is None or str(sibling.get("name") or "") == want:
                continue
            if not sibling.get("enabled"):
                continue
            try:
                _set_group_enabled(http, int(sid), False)
                sibling["enabled"] = False
                log(
                    f"teamarr: disabled overlapping event group "
                    f"{sibling.get('name')!r} on {name}"
                )
            except (httpx.HTTPError, TypeError, ValueError) as exc:
                log(f"teamarr: could not disable group {sid} ({exc})")

    for group in groups:
        gname = str(group.get("name") or "")
        if not gname.endswith(MANAGED_GROUP_SUFFIX) or not group.get("enabled"):
            continue
        aid = group.get("m3u_account_id")
        try:
            account_id = int(aid)
        except (TypeError, ValueError):
            account_id = None
        if account_id in seen_active:
            continue
        try:
            _set_group_enabled(http, int(group["id"]), False)
            group["enabled"] = False
            log(f"teamarr: disabled event group {gname!r} (M3U missing)")
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            log(f"teamarr: could not disable {gname!r} ({exc})")

    return created


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
        ensure_template_art_urls(http, log)
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

        disp = preserve_dispatcharr_put_fields(current_disp, disp)
        disp = ensure_dispatcharr_epg_id(http, log, disp)

        resp = http.put("/api/v1/settings/dispatcharr", json=disp)
        resp.raise_for_status()
        log("teamarr: Dispatcharr URL set to loopback")
        ensure_emby_settings(http, log)
        from recipes import emby as emby_recipe
        emby_recipe.sync_from_teamarr(http, log)
        ensure_event_groups(http, log)
        warn_inactive_event_group_m3us(http, log)
        return {"ok": True, "leagues": rows}
    except httpx.HTTPError as exc:
        log(f"teamarr: configure failed ({exc})")
        return {"ok": False, "reason": str(exc)}
    finally:
        if own and hasattr(http, "close"):
            http.close()
