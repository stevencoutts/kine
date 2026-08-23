"""Wire Seerr to Sonarr and Radarr over kine_internal.

Seerr is not VPN-tunnelled. It reaches the gluetun-namespaced *arr apps at
http://gluetun:8989 / http://gluetun:7878.

The settings API (`POST /api/v1/settings/{radarr,sonarr}`) requires an admin
user (created at wizard Sign In) and Seerr's own `X-Api-Key`. Media-server
setup and finishing the wizard are still interactive; once Sign In has
created user id 1, this recipe can add the services — including while the
UI is still on Configure Services.

Quality profiles prefer Recyclarr/TRaSH names (WEB-1080p / HD Bluray + WEB)
over the stock HD-1080p fallback. Existing servers are updated via PUT when
the active profile is wrong.
"""
from __future__ import annotations

import json
import os
import time

import httpx

import keys
from keys import resolve_key

BASE = "http://seerr:5055"

# Preferred first: Recyclarr TRaSH Guide profiles, then stock *arr defaults.
SERVERS = {
    "radarr": {
        "name": "Radarr",
        "hostname": "gluetun",
        "port": 7878,
        "path": "radarr",
        "directory": "/data/media/movies",
        "profiles": (
            "HD Bluray + WEB",
            "HD Bluray+WEB",
            "HD-1080p",
            "HD - 720p/1080p",
        ),
        "minimumAvailability": "released",
        "extra": {},
    },
    "sonarr": {
        "name": "Sonarr",
        "hostname": "gluetun",
        "port": 8989,
        "path": "sonarr",
        "directory": "/data/media/tv",
        "profiles": (
            "WEB-1080p",
            "WEB 1080p",
            "HD-1080p",
            "HD - 720p/1080p",
        ),
        "extra": {"enableSeasonFolders": True},
    },
}


def _seerr_api_key() -> str | None:
    cfg = keys.STACK / "config" / "seerr" / "settings.json"
    if not cfg.exists():
        return None
    try:
        return json.loads(cfg.read_text()).get("main", {}).get("apiKey") or None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _wait(http: httpx.Client, timeout: int = 300) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = http.get("/api/v1/settings/public")
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(5)
    return False


def _pick_profile(profiles: list[dict], preferred: tuple[str, ...]) -> dict | None:
    by_name = {p.get("name"): p for p in profiles if p.get("name")}
    for name in preferred:
        if name in by_name:
            return by_name[name]
    for p in profiles:
        name = p.get("name") or ""
        if name and name != "Any":
            return p
    return profiles[0] if profiles else None


def _pick_directory(folders: list[dict], wanted: str) -> str | None:
    paths = [f.get("path") for f in folders if f.get("path")]
    if wanted in paths:
        return wanted
    return paths[0] if paths else None


def _external_url(app: str) -> str:
    domain = os.environ.get("KINE_DOMAIN", "").strip()
    if not domain:
        return ""
    return f"http://{app}.{domain}"


def _find_linked(existing: list[dict], hostname: str, port: int) -> dict | None:
    for item in existing:
        if item.get("hostname") == hostname and int(item.get("port") or 0) == port:
            if not item.get("is4k", False):
                return item
    return None


def _already_linked(existing: list[dict], hostname: str, port: int) -> bool:
    return _find_linked(existing, hostname, port) is not None


def _build_payload(
    app: str,
    meta: dict,
    arr_key: str,
    discovered: dict,
    profile: dict,
    directory: str,
) -> dict:
    payload = {
        "name": meta["name"],
        "hostname": meta["hostname"],
        "port": meta["port"],
        "apiKey": arr_key,
        "useSsl": False,
        "baseUrl": discovered.get("urlBase") or "",
        "activeProfileId": profile["id"],
        "activeProfileName": profile["name"],
        "activeDirectory": directory,
        "is4k": False,
        "isDefault": True,
        "syncEnabled": True,
        "preventSearch": False,
        "tag": None,
        "externalUrl": _external_url(app),
        **meta["extra"],
    }
    if app == "radarr":
        payload["minimumAvailability"] = meta["minimumAvailability"]

    language_profiles = discovered.get("languageProfiles") or []
    if language_profiles:
        payload["activeLanguageProfileId"] = language_profiles[0]["id"]

    return payload


def _ensure_server(http: httpx.Client, app: str, log) -> None:
    meta = SERVERS[app]
    existing = http.get(f"/api/v1/settings/{meta['path']}").json()
    linked = _find_linked(existing, meta["hostname"], meta["port"])

    arr_key = resolve_key(app)
    test_body = {
        "hostname": meta["hostname"],
        "port": meta["port"],
        "apiKey": arr_key,
        "useSsl": False,
        "baseUrl": "",
    }
    test = http.post(f"/api/v1/settings/{meta['path']}/test", json=test_body)
    if test.status_code >= 400:
        log(f"seerr: {app} test failed ({test.status_code}); skipping")
        return
    discovered = test.json()

    profile = _pick_profile(discovered.get("profiles") or [], meta["profiles"])
    directory = _pick_directory(discovered.get("rootFolders") or [], meta["directory"])
    if not profile or not directory:
        log(f"seerr: {app} missing quality profile or root folder; skipping")
        return

    payload = _build_payload(app, meta, arr_key, discovered, profile, directory)

    if linked is not None:
        if (
            int(linked.get("activeProfileId") or 0) == int(profile["id"])
            and linked.get("activeProfileName") == profile["name"]
        ):
            return
        server_id = linked["id"]
        updated = http.put(
            f"/api/v1/settings/{meta['path']}/{server_id}",
            json={**payload, "id": server_id},
        )
        if updated.status_code == 200:
            log(
                f"seerr: updated {meta['name']} profile to {profile['name']} "
                f"(id {profile['id']})"
            )
        else:
            log(f"seerr: failed to update {app} profile ({updated.status_code})")
        return

    created = http.post(f"/api/v1/settings/{meta['path']}", json=payload)
    if created.status_code in (200, 201):
        log(
            f"seerr: linked {meta['name']} "
            f"({meta['hostname']}:{meta['port']}, profile {profile['name']})"
        )
    else:
        log(f"seerr: failed to add {app} ({created.status_code})")


def configure(enabled: set[str], log) -> None:
    targets = [app for app in ("radarr", "sonarr") if app in enabled]
    if not targets:
        log("seerr: sonarr/radarr not enabled, skipping service link")
        return

    api_key = _seerr_api_key()
    if not api_key:
        log("seerr: no settings.json API key yet, skipping")
        return

    http = httpx.Client(
        base_url=BASE,
        headers={"X-Api-Key": api_key},
        timeout=60.0,
        follow_redirects=True,
    )
    if not _wait(http):
        log("seerr: no API response, skipping wiring")
        return

    me = http.get("/api/v1/auth/me")
    if me.status_code != 200:
        log(
            "seerr: admin user not ready (finish wizard Sign In), "
            "re-run ./kine provision after steps 1–3"
        )
        return

    for app in targets:
        try:
            _ensure_server(http, app, log)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log(f"seerr: {app} wiring failed ({exc})")
