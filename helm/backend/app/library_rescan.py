"""Trigger library rescans after NFS media mounts are applied."""
from __future__ import annotations

import hashlib
import os
import pathlib
import xml.etree.ElementTree as ET

import httpx
import yaml

from . import catalogue, config

STACK = pathlib.Path(os.environ.get("KINE_ROOT", "/stack"))
MEDIA_NFS_KEYS = frozenset({"NFS_MEDIA", "NFS_TV", "NFS_MOVIES"})


def _secret() -> str:
    secret = os.environ.get("KINE_SECRET") or config.read().get("KINE_SECRET", "")
    if not secret:
        raise RuntimeError("KINE_SECRET is not set")
    return secret


def _derived_key(app: str) -> str:
    return hashlib.sha256(f"{_secret()}:{app}".encode()).hexdigest()[:32]


def _arr_key(app: str) -> str | None:
    cfg = STACK / "config" / app / "config.xml"
    if cfg.is_file():
        try:
            existing = ET.parse(cfg).getroot().findtext("ApiKey")
            if existing:
                return existing
        except ET.ParseError:
            pass
    try:
        return _derived_key(app)
    except RuntimeError:
        return None


def _bazarr_key() -> str | None:
    for path in (
        STACK / "config" / "bazarr" / "config" / "config.yaml",
        STACK / "config" / "bazarr" / "config.yaml",
    ):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
            key = (data.get("auth") or {}).get("apikey")
            if key:
                return str(key)
        except (OSError, yaml.YAMLError):
            continue
    return None


def _post_arr_command(base: str, api: str, key: str, name: str) -> tuple[bool, str]:
    url = f"{base.rstrip('/')}/api/{api}/command"
    try:
        response = httpx.post(
            url,
            headers={"X-Api-Key": key},
            json={"name": name},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return False, str(exc)
    if response.status_code in (200, 201, 202):
        return True, f"queued {name}"
    detail = response.text.strip().splitlines()[0] if response.text else ""
    return False, f"HTTP {response.status_code}" + (f" ({detail})" if detail else "")


def _refresh_emby(base: str) -> tuple[bool, str]:
    try:
        response = httpx.post(
            f"{base.rstrip('/')}/Library/Refresh",
            params={
                "Recursive": "true",
                "MetadataRefreshMode": "FullRefresh",
                "ImageRefreshMode": "FullRefresh",
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return False, str(exc)
    if response.status_code in (200, 204):
        return True, "library refresh queued"
    if response.status_code == 401:
        return False, "needs Emby API key (refresh skipped)"
    detail = response.text.strip().splitlines()[0] if response.text else ""
    return False, f"HTTP {response.status_code}" + (f" ({detail})" if detail else "")


def _run_bazarr_task(base: str, key: str, taskid: str) -> tuple[bool, str]:
    url = f"{base.rstrip('/')}/api/system/tasks"
    try:
        response = httpx.post(
            url,
            headers={"X-API-KEY": key},
            data={"taskid": taskid},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return False, str(exc)
    if response.status_code == 204:
        return True, f"queued {taskid}"
    detail = response.text.strip().splitlines()[0] if response.text else ""
    return False, f"{taskid}: HTTP {response.status_code}" + (f" ({detail})" if detail else "")


def _sync_bazarr(base: str, key: str) -> tuple[bool, str]:
    messages: list[str] = []
    ok_any = False
    for taskid in ("update_series", "update_movies"):
        ok, message = _run_bazarr_task(base, key, taskid)
        ok_any = ok_any or ok
        messages.append(message)
    return ok_any, "; ".join(messages)


def after_nfs_mount(changed_keys: set[str] | None = None) -> dict:
    """Rescan libraries in enabled apps after media NFS mounts change."""
    if changed_keys is not None and not (set(changed_keys) & MEDIA_NFS_KEYS):
        return {"ok": True, "skipped": True, "results": []}

    enabled = set(config.profiles())
    cat = catalogue.load()
    results: list[dict] = []

    if "sonarr" in enabled and "sonarr" in cat:
        key = _arr_key("sonarr")
        if key:
            ok, message = _post_arr_command(
                cat["sonarr"]["internal"],
                cat["sonarr"].get("api", "v3"),
                key,
                "RescanSeries",
            )
            results.append({"app": "sonarr", "ok": ok, "message": message})

    if "radarr" in enabled and "radarr" in cat:
        key = _arr_key("radarr")
        if key:
            ok, message = _post_arr_command(
                cat["radarr"]["internal"],
                cat["radarr"].get("api", "v3"),
                key,
                "RescanMovie",
            )
            results.append({"app": "radarr", "ok": ok, "message": message})

    if "emby" in enabled and "emby" in cat:
        ok, message = _refresh_emby(cat["emby"]["internal"])
        results.append({"app": "emby", "ok": ok, "message": message})

    if "bazarr" in enabled and "bazarr" in cat:
        key = _bazarr_key()
        if key:
            ok, message = _sync_bazarr(cat["bazarr"]["internal"], key)
            results.append({"app": "bazarr", "ok": ok, "message": message})

    ok = not results or any(item["ok"] for item in results)
    return {"ok": ok, "results": results}
