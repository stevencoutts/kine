"""Wire Dispatcharr to Emby Live TV and export API token to ECM/Teamarr."""
from __future__ import annotations

import os
from typing import Any, Callable

import httpx

from recipes import envfiles

DISPATCHARR_HDHR = "http://dispatcharr:9191/hdhr"
DISPATCHARR_BASE = "http://dispatcharr:9191"
EMBY_BASE = "http://emby:8096"


def tuner_host_payload() -> dict:
    return {
        "Type": "hdhomerun",
        "Url": DISPATCHARR_HDHR,
        "FriendlyName": "Dispatcharr",
        "ImportFavoritesOnly": False,
    }


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def tuner_already_linked(hosts: list) -> bool:
    want = _norm_url(DISPATCHARR_HDHR)
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


def configure(
    enabled: set[str],
    token: str | None,
    log: Callable[[str], None],
    *,
    emby_client: httpx.Client | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"emby_linked": False, "env_changed": []}
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

    if "emby" in enabled:
        emby_key = os.environ.get("EMBY_API_KEY", "").strip()
        if not emby_key:
            log("dispatcharr: EMBY_API_KEY not set, skipping Emby tuner")
        else:
            result["emby_linked"] = link_emby_tuner(emby_key, log, client=emby_client)

    return result
