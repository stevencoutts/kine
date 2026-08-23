"""Wire Bazarr to Sonarr and Radarr over the shared gluetun loopback."""
from __future__ import annotations

import time

import httpx

from keys import resolve_key

BASE = "http://gluetun:6767"
SONARR_HOST = "127.0.0.1"
SONARR_PORT = 8989
RADARR_HOST = "127.0.0.1"
RADARR_PORT = 7878


def _headers(key: str) -> dict[str, str]:
    return {"X-API-KEY": key}


def _wait(key: str, timeout: int = 300) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for path in ("/api/system/ping", "/ping"):
                r = httpx.get(f"{BASE}{path}", headers=_headers(key), timeout=10.0)
                if r.status_code == 200:
                    return True
        except httpx.HTTPError:
            pass
        time.sleep(5)
    return False


def _settings_form(enabled: set[str]) -> dict[str, str]:
    data: dict[str, str] = {}
    if "sonarr" in enabled:
        data.update({
            "settings-general-use_sonarr": "true",
            "settings-sonarr-ip": SONARR_HOST,
            "settings-sonarr-port": str(SONARR_PORT),
            "settings-sonarr-base_url": "/",
            "settings-sonarr-ssl": "false",
            "settings-sonarr-apikey": resolve_key("sonarr"),
        })
    if "radarr" in enabled:
        data.update({
            "settings-general-use_radarr": "true",
            "settings-radarr-ip": RADARR_HOST,
            "settings-radarr-port": str(RADARR_PORT),
            "settings-radarr-base_url": "/",
            "settings-radarr-ssl": "false",
            "settings-radarr-apikey": resolve_key("radarr"),
        })
    return data


def _sync_libraries(key: str, log) -> None:
    for taskid in ("update_series", "update_movies"):
        try:
            r = httpx.post(
                f"{BASE}/api/system/tasks",
                headers=_headers(key),
                data={"taskid": taskid},
                timeout=120.0,
            )
            if r.status_code == 204:
                log(f"bazarr: queued {taskid}")
            else:
                log(f"bazarr: {taskid} returned HTTP {r.status_code}")
        except httpx.HTTPError as exc:
            log(f"bazarr: {taskid} failed ({exc})")


def configure(enabled: set[str], log) -> None:
    if "bazarr" not in enabled:
        return
    key = resolve_key("bazarr")
    if not _wait(key):
        log("bazarr: no API response, skipping wiring")
        return

    form = _settings_form(enabled)
    if form:
        try:
            r = httpx.post(
                f"{BASE}/api/system/settings",
                headers=_headers(key),
                data=form,
                timeout=60.0,
            )
            if r.status_code not in (200, 204):
                log(f"bazarr: settings returned HTTP {r.status_code}")
            else:
                log("bazarr: linked Sonarr/Radarr")
        except httpx.HTTPError as exc:
            log(f"bazarr: settings failed ({exc})")
            return

    _sync_libraries(key, log)
