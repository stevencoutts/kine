"""Wire Dispatcharr to Emby Live TV and export API token to ECM/Teamarr."""
from __future__ import annotations

import os
from typing import Any, Callable

import httpx

import tunnel_hosts
from recipes import envfiles

# Emby and provision sit on kine_internal; Dispatcharr shares its tunnel namespace.
EMBY_BASE = "http://emby:8096"


def dispatcharr_base() -> str:
    return tunnel_hosts.internal_base_for_app("dispatcharr", 9191)


def dispatcharr_hdhr() -> str:
    return f"{dispatcharr_base()}/hdhr"


def tuner_host_payload() -> dict:
    return {
        "Type": "hdhomerun",
        "Url": dispatcharr_hdhr(),
        "FriendlyName": "Dispatcharr",
        "ImportFavoritesOnly": False,
    }


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def tuner_already_linked(hosts: list) -> bool:
    want = _norm_url(dispatcharr_hdhr())
    for host in hosts:
        if not isinstance(host, dict):
            continue
        if _norm_url(str(host.get("Url") or "")) == want:
            return True
    return False


def _resolve_token(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("DISPATCHARR_TOKEN", "").strip()
    if env:
        return env
    for app in ("ecm", "teamarr"):
        path = envfiles.STACK / "config" / app / f"{app}.env"
        existing = envfiles._read_env(path)
        tok = (existing.get("DISPATCHARR_TOKEN") or "").strip()
        if tok:
            return tok
    return ""


def link_emby_tuner(
    api_key: str,
    log: Callable[[str], None],
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Ensure Emby has Dispatcharr HDHomeRun. Return True if linked (new or existing)."""
    headers = {"X-Emby-Token": api_key}
    own = client is None
    http = client or httpx.Client(base_url=EMBY_BASE, timeout=30.0, headers=headers)
    try:
        resp = http.get("/LiveTv/TunerHosts")
        if resp.status_code == 404:
            hosts: list = []
        else:
            resp.raise_for_status()
            data = resp.json() if resp.content else []
            hosts = data if isinstance(data, list) else []
        if tuner_already_linked(hosts):
            log("dispatcharr: Emby tuner already linked")
            return True
        created = http.post("/LiveTv/TunerHosts", json=tuner_host_payload())
        created.raise_for_status()
        log("dispatcharr: linked Emby HDHomeRun tuner")
        return True
    except httpx.HTTPError as exc:
        log(f"dispatcharr: Emby tuner link failed ({exc})")
        return False
    finally:
        if own:
            http.close()


def _source_rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return [row for row in payload["results"] if isinstance(row, dict)]
    return []


def reconcile_teamarr_epg(
    token: str,
    log: Callable[[str], None],
    *,
    client: httpx.Client | None = None,
) -> int:
    """Point Dispatcharr's Teamarr XMLTV source at the host that works now.

    Direct mode breaks ``127.0.0.1:9195`` because Teamarr is no longer in
    Dispatcharr's network namespace. Tunnelled mode breaks ``teamarr:9195``
    because that name does not resolve inside Gluetun.
    """
    data = tunnel_hosts.load_profiles()
    own = client is None
    http = client or httpx.Client(
        base_url=dispatcharr_base(),
        timeout=30.0,
        headers={"X-API-Key": token, "Accept": "application/json"},
    )
    changed = 0
    try:
        resp = http.get("/api/epg/sources/")
        resp.raise_for_status()
        rows = _source_rows(resp.json() if resp.content else [])
        for row in rows:
            url = str(row.get("url") or "")
            fixed = tunnel_hosts.align_peer_url(url, data, "teamarr", 9195)
            if not fixed:
                continue
            source_id = row.get("id")
            if source_id is None:
                continue
            patched = http.patch(
                f"/api/epg/sources/{source_id}/",
                json={"url": fixed},
            )
            patched.raise_for_status()
            name = row.get("name") or source_id
            log(f"dispatcharr: EPG {name} URL set to {fixed}")
            changed += 1
            try:
                refresh = http.post("/api/epg/import/", json={"id": source_id})
                refresh.raise_for_status()
            except httpx.HTTPError as exc:
                log(f"dispatcharr: EPG refresh failed ({exc})")
        return changed
    finally:
        if own and hasattr(http, "close"):
            http.close()


def configure(
    enabled: set[str],
    token: str | None,
    log: Callable[[str], None],
    *,
    emby_client: httpx.Client | None = None,
    epg_client: httpx.Client | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"emby_linked": False, "env_changed": [], "epg_updated": 0}
    if "dispatcharr" not in enabled:
        return result

    resolved = _resolve_token(token)
    if not resolved:
        log("dispatcharr: no API token yet, skipping")
        return result

    for app in ("ecm", "teamarr"):
        if app not in enabled:
            continue
        if envfiles.write_dispatcharr_token(app, resolved, log):
            result["env_changed"].append(app)

    if "teamarr" in enabled:
        try:
            result["epg_updated"] = reconcile_teamarr_epg(
                resolved, log, client=epg_client,
            )
        except httpx.HTTPError as exc:
            log(f"dispatcharr: Teamarr EPG URL update failed ({exc})")

    if "emby" in enabled:
        emby_key = os.environ.get("EMBY_API_KEY", "").strip()
        if not emby_key:
            log("dispatcharr: EMBY_API_KEY not set, skipping Emby tuner")
        else:
            result["emby_linked"] = link_emby_tuner(emby_key, log, client=emby_client)

    return result
