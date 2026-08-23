"""Import and rescan libraries after NFS media mounts are applied."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import yaml

from . import catalogue, config

STACK = pathlib.Path(os.environ.get("KINE_ROOT", "/stack"))
MEDIA_NFS_KEYS = frozenset({"NFS_MEDIA", "NFS_TV", "NFS_MOVIES"})
ROOT_FOLDERS = {
    "sonarr": "/data/media/tv",
    "radarr": "/data/media/movies",
}
QUALITY_PROFILE_NAMES = {
    "sonarr": ("WEB-1080p", "HD-1080p", "Any"),
    "radarr": ("HD Bluray + WEB", "HD-1080p", "HD - 720p/1080p", "Any"),
}
IMPORT_CHUNK = 50
LOOKUP_WORKERS = 8
SKIP_DIR_NAMES = frozenset({
    "$recycle.bin",
    "system volume information",
    "recycler",
    "lost+found",
    ".appledb",
    ".appledesktop",
    ".appledouble",
    "@eadir",
    ".grab",
})


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


def _arr_url(base: str, api: str, path: str) -> str:
    return f"{base.rstrip('/')}/api/{api}/{path.lstrip('/')}"


def _arr_headers(key: str) -> dict[str, str]:
    return {"X-Api-Key": key}


def _normalize_path(path: str) -> str:
    return path.rstrip("/")


def _arr_get(
    base: str,
    api: str,
    key: str,
    path: str,
    params: dict | None = None,
    timeout: float = 30.0,
) -> object:
    try:
        response = httpx.get(
            _arr_url(base, api, path),
            headers=_arr_headers(key),
            params=params or {},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(str(exc)) from exc
    if response.status_code != 200:
        detail = response.text.strip().splitlines()[0] if response.text else ""
        raise RuntimeError(
            f"HTTP {response.status_code}" + (f" ({detail})" if detail else "")
        )
    return response.json()


def _quality_profile_id(
    base: str,
    api: str,
    key: str,
    preferred_names: tuple[str, ...],
) -> int:
    profiles = _arr_get(base, api, key, "qualityprofile")
    if not isinstance(profiles, list) or not profiles:
        return 1
    for name in preferred_names:
        for profile in profiles:
            if profile.get("name") == name:
                return int(profile["id"])
    return int(profiles[0]["id"])


def _filesystem_dirs(base: str, api: str, key: str, path: str) -> list[str]:
    query_path = path if path.endswith("/") else f"{path}/"
    entries = _arr_get(
        base,
        api,
        key,
        "filesystem",
        {"path": query_path, "includeFiles": False},
        timeout=120.0,
    )
    directories: list[dict] = []
    if isinstance(entries, dict):
        directories = entries.get("directories") or []
    elif isinstance(entries, list):
        directories = entries

    result: list[str] = []
    for entry in directories:
        if entry.get("type") != "folder":
            continue
        folder_path = entry.get("path")
        if not folder_path:
            continue
        name = (entry.get("name") or pathlib.PurePosixPath(folder_path).name).lower()
        if name in SKIP_DIR_NAMES or name.startswith("."):
            continue
        result.append(_normalize_path(folder_path))
    return result


def _existing_paths(base: str, api: str, key: str, collection: str) -> set[str]:
    items = _arr_get(base, api, key, collection, timeout=120.0)
    if not isinstance(items, list):
        return set()
    return {
        _normalize_path(item["path"])
        for item in items
        if item.get("path")
    }


def _root_unmapped(
    base: str,
    api: str,
    key: str,
    root_path: str,
) -> list[dict]:
    roots = _arr_get(base, api, key, "rootfolder", timeout=60.0)
    if not isinstance(roots, list):
        return []
    for root in roots:
        if _normalize_path(root.get("path", "")) == _normalize_path(root_path):
            unmapped = root.get("unmappedFolders")
            return unmapped if isinstance(unmapped, list) else []
    return []


def _sonarr_unmapped(
    base: str,
    api: str,
    key: str,
    root_path: str,
) -> list[dict]:
    unmapped = _root_unmapped(base, api, key, root_path)
    if len(unmapped) == 1 and unmapped[0].get("name", "").lower() == "series":
        series_root = unmapped[0]["path"]
        known = _existing_paths(base, api, key, "series")
        return [
            {
                "name": pathlib.PurePosixPath(folder_path).name,
                "path": folder_path,
            }
            for folder_path in _filesystem_dirs(base, api, key, series_root)
            if _normalize_path(folder_path) not in known
        ]
    return unmapped


def _arr_post(
    base: str,
    api: str,
    key: str,
    path: str,
    payload: dict | list,
    timeout: float = 30.0,
) -> tuple[int, str]:
    try:
        response = httpx.post(
            _arr_url(base, api, path),
            headers=_arr_headers(key),
            json=payload,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return 0, str(exc)
    return response.status_code, response.text


def _ensure_root_folder(base: str, api: str, key: str, root_path: str) -> None:
    roots = _arr_get(base, api, key, "rootfolder", timeout=60.0)
    if not isinstance(roots, list):
        return
    if any(
        _normalize_path(root.get("path", "")) == _normalize_path(root_path)
        for root in roots
    ):
        return
    status, detail = _arr_post(base, api, key, "rootfolder", {"path": root_path})
    if status not in (200, 201):
        raise RuntimeError(
            f"root folder {root_path}: HTTP {status}"
            + (f" ({detail.strip().splitlines()[0]})" if detail.strip() else "")
        )


def _lookup(
    base: str,
    api: str,
    key: str,
    kind: str,
    term: str,
) -> dict | None:
    if len(term) < 3:
        return None
    results = _arr_get(
        base,
        api,
        key,
        f"{kind}/lookup",
        {"term": term},
        timeout=30.0,
    )
    if isinstance(results, list) and results:
        return results[0]
    return None


def _existing_ids(base: str, api: str, key: str, app: str) -> set[int]:
    collection = "series" if app == "sonarr" else "movie"
    id_key = "tvdbId" if app == "sonarr" else "tmdbId"
    items = _arr_get(base, api, key, collection, timeout=120.0)
    if not isinstance(items, list):
        return set()
    return {int(item[id_key]) for item in items if item.get(id_key)}


def _payload_id(app: str, item: dict) -> int | None:
    key = "tvdbId" if app == "sonarr" else "tmdbId"
    value = item.get(key)
    return int(value) if value else None


def _import_one(
    base: str,
    api: str,
    key: str,
    kind: str,
    item: dict,
) -> tuple[bool, str]:
    status, detail = _arr_post(base, api, key, f"{kind}/import", [item], timeout=120.0)
    if status in (200, 201, 202):
        return True, "ok"
    if status == 409:
        return False, "already exists"
    snippet = detail.strip().splitlines()[0] if detail.strip() else ""
    return False, f"HTTP {status}" + (f" ({snippet})" if snippet else "")


def _import_chunk(
    base: str,
    api: str,
    key: str,
    kind: str,
    chunk: list[dict],
) -> tuple[bool, str, int]:
    status, detail = _arr_post(
        base,
        api,
        key,
        f"{kind}/import",
        chunk,
        timeout=120.0,
    )
    if status in (200, 201, 202):
        if detail.strip():
            try:
                body = json.loads(detail)
            except json.JSONDecodeError:
                body = None
            if isinstance(body, list):
                return True, "ok", len(body)
        return True, "ok", len(chunk)
    if status == 409:
        imported = 0
        conflicts = 0
        for item in chunk:
            ok, message = _import_one(base, api, key, kind, item)
            if ok:
                imported += 1
            elif message == "already exists":
                conflicts += 1
            else:
                return False, message, imported
        suffix = f", skipped {conflicts} duplicates" if conflicts else ""
        return True, f"ok{suffix}", imported
    snippet = detail.strip().splitlines()[0] if detail.strip() else ""
    return False, f"HTTP {status}" + (f" ({snippet})" if snippet else ""), 0


def _import_arr_library(
    app: str,
    base: str,
    api: str,
    key: str,
) -> tuple[bool, str]:
    root_path = ROOT_FOLDERS[app]
    kind = "series" if app == "sonarr" else "movie"
    try:
        _ensure_root_folder(base, api, key, root_path)
        unmapped = (
            _sonarr_unmapped(base, api, key, root_path)
            if app == "sonarr"
            else _root_unmapped(base, api, key, root_path)
        )
    except RuntimeError as exc:
        return False, f"scan failed: {exc}"

    if not unmapped:
        return True, "nothing to import"

    try:
        profile_id = _quality_profile_id(base, api, key, QUALITY_PROFILE_NAMES[app])
    except RuntimeError as exc:
        return False, f"quality profiles: {exc}"

    payloads: list[dict] = []
    skipped = 0
    lookup_jobs: list[tuple[dict, str]] = []
    for folder in unmapped:
        folder_path = folder.get("path")
        if not folder_path:
            skipped += 1
            continue
        term = folder.get("name") or pathlib.PurePosixPath(folder_path).name
        lookup_jobs.append((folder, term))

    with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
        futures = {
            pool.submit(_lookup, base, api, key, kind, term): (folder, term)
            for folder, term in lookup_jobs
        }
        for future in as_completed(futures):
            folder, term = futures[future]
            folder_path = folder.get("path")
            try:
                match = future.result()
            except RuntimeError:
                skipped += 1
                continue
            if not match:
                skipped += 1
                continue

            item = dict(match)
            item.pop("id", None)
            item["path"] = folder_path
            item["rootFolderPath"] = root_path
            item["qualityProfileId"] = profile_id
            item["monitored"] = True
            item["tags"] = item.get("tags") or []

            if app == "radarr":
                item["minimumAvailability"] = item.get("minimumAvailability") or "released"
                item["addOptions"] = {
                    "monitor": "movieOnly",
                    "searchForMovie": False,
                }
            else:
                item["seasonFolder"] = item.get("seasonFolder", True)
                item["seriesType"] = item.get("seriesType") or "standard"
                item["monitorNewItems"] = item.get("monitorNewItems") or "all"
                item["addOptions"] = {
                    "monitor": "all",
                    "searchForMissingEpisodes": False,
                    "searchForCutoffUnmetEpisodes": False,
                }

            payloads.append(item)

    seen: set[int] = set()
    deduped: list[dict] = []
    for item in payloads:
        item_id = _payload_id(app, item)
        if item_id is None or item_id in seen:
            skipped += 1
            continue
        seen.add(item_id)
        deduped.append(item)
    payloads = deduped

    try:
        known_ids = _existing_ids(base, api, key, app)
    except RuntimeError:
        known_ids = set()
    if known_ids:
        before = len(payloads)
        payloads = [item for item in payloads if _payload_id(app, item) not in known_ids]
        skipped += before - len(payloads)

    if not payloads:
        suffix = f" ({skipped} folders unmatched)" if skipped else ""
        return True, f"nothing to import{suffix}"

    imported = 0
    for offset in range(0, len(payloads), IMPORT_CHUNK):
        chunk = payloads[offset : offset + IMPORT_CHUNK]
        ok, message, count = _import_chunk(base, api, key, kind, chunk)
        if not ok:
            return False, f"import failed after {imported}: {message}"
        imported += count

    parts = [f"imported {imported}"]
    if skipped:
        parts.append(f"skipped {skipped} unmatched")
    return True, ", ".join(parts)


def _post_arr_command(base: str, api: str, key: str, name: str) -> tuple[bool, str]:
    url = _arr_url(base, api, "command")
    try:
        response = httpx.post(
            url,
            headers=_arr_headers(key),
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
    api_key = (
        os.environ.get("EMBY_API_KEY", "").strip()
        or config.read().get("EMBY_API_KEY", "").strip()
    )
    if not api_key:
        return False, "needs Emby API key (refresh skipped)"
    headers = {"X-Emby-Token": api_key}
    try:
        response = httpx.post(
            f"{base.rstrip('/')}/Library/Refresh",
            headers=headers,
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
        return False, "Emby API key rejected"
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
    """Import and rescan libraries in enabled apps after media NFS mounts change."""
    if changed_keys is not None and not (set(changed_keys) & MEDIA_NFS_KEYS):
        return {"ok": True, "skipped": True, "results": []}

    enabled = set(config.profiles())
    cat = catalogue.load()
    results: list[dict] = []

    if "sonarr" in enabled and "sonarr" in cat:
        key = _arr_key("sonarr")
        if key:
            base = cat["sonarr"]["internal"]
            api = cat["sonarr"].get("api", "v3")
            import_ok, import_msg = _import_arr_library("sonarr", base, api, key)
            rescan_ok, rescan_msg = _post_arr_command(base, api, key, "RescanSeries")
            results.append(
                {
                    "app": "sonarr",
                    "ok": import_ok and rescan_ok,
                    "message": f"{import_msg}; {rescan_msg}",
                }
            )

    if "radarr" in enabled and "radarr" in cat:
        key = _arr_key("radarr")
        if key:
            base = cat["radarr"]["internal"]
            api = cat["radarr"].get("api", "v3")
            import_ok, import_msg = _import_arr_library("radarr", base, api, key)
            rescan_ok, rescan_msg = _post_arr_command(base, api, key, "RescanMovie")
            results.append(
                {
                    "app": "radarr",
                    "ok": import_ok and rescan_ok,
                    "message": f"{import_msg}; {rescan_msg}",
                }
            )

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
