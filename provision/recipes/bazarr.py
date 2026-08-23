"""Wire Bazarr to Sonarr/Radarr and apply sensible subtitle defaults."""
from __future__ import annotations

import json
import os
import time
from urllib.parse import urlencode

import httpx

from keys import resolve_key

BASE = "http://gluetun:6767"
SONARR_HOST = "127.0.0.1"
SONARR_PORT = 8989
RADARR_HOST = "127.0.0.1"
RADARR_PORT = 7878

# Free providers that need no account. OpenSubtitles.com is added when
# OPENSUBTITLES_USERNAME / OPENSUBTITLES_PASSWORD are set in .env.
DEFAULT_PROVIDERS = ("gestdown", "tvsubtitles", "yifysubtitles")
DEFAULT_PROFILE_NAME = "English"
DEFAULT_PROFILE_ID = 1


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


def _post_settings(key: str, pairs: list[tuple[str, str]]) -> httpx.Response:
    """POST form fields, allowing repeated keys (Bazarr list settings)."""
    body = urlencode(pairs)
    return httpx.post(
        f"{BASE}/api/system/settings",
        headers={**_headers(key), "Content-Type": "application/x-www-form-urlencoded"},
        content=body,
        timeout=60.0,
    )


def _opensubtitles_creds() -> tuple[str, str] | None:
    user = os.environ.get("OPENSUBTITLES_USERNAME", "").strip()
    password = os.environ.get("OPENSUBTITLES_PASSWORD", "").strip()
    if not user or not password:
        return None
    return user, password


def _wanted_providers() -> list[str]:
    providers = list(DEFAULT_PROVIDERS)
    if _opensubtitles_creds():
        providers.append("opensubtitlescom")
    return providers


def english_forced_profile() -> dict:
    """Language profile: English + English forced (en:forced)."""
    return {
        "profileId": DEFAULT_PROFILE_ID,
        "name": DEFAULT_PROFILE_NAME,
        "cutoff": None,
        "items": [
            {
                "id": 1,
                "language": "en",
                "audio_exclude": "False",
                "hi": "False",
                "forced": "False",
            },
            {
                "id": 2,
                "language": "en",
                "audio_exclude": "False",
                "hi": "False",
                "forced": "True",
            },
        ],
        "mustContain": [],
        "mustNotContain": [],
        "originalFormat": False,
        "tag": None,
    }


def _settings_form(enabled: set[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if "sonarr" in enabled:
        pairs.extend([
            ("settings-general-use_sonarr", "true"),
            ("settings-sonarr-ip", SONARR_HOST),
            ("settings-sonarr-port", str(SONARR_PORT)),
            ("settings-sonarr-base_url", "/"),
            ("settings-sonarr-ssl", "false"),
            ("settings-sonarr-apikey", resolve_key("sonarr")),
        ])
    if "radarr" in enabled:
        pairs.extend([
            ("settings-general-use_radarr", "true"),
            ("settings-radarr-ip", RADARR_HOST),
            ("settings-radarr-port", str(RADARR_PORT)),
            ("settings-radarr-base_url", "/"),
            ("settings-radarr-ssl", "false"),
            ("settings-radarr-apikey", resolve_key("radarr")),
        ])
    return pairs


def _providers_need_defaults(settings: dict) -> bool:
    current = settings.get("general", {}).get("enabled_providers") or []
    if not current:
        return True
    # Earlier probe accidentally stored one comma-joined string.
    if len(current) == 1 and "," in str(current[0]):
        return True
    return False


def _apply_defaults(key: str, log) -> None:
    settings = httpx.get(
        f"{BASE}/api/system/settings", headers=_headers(key), timeout=30.0
    ).json()
    pairs: list[tuple[str, str]] = []

    if _providers_need_defaults(settings):
        for name in _wanted_providers():
            pairs.append(("settings-general-enabled_providers", name))
        log(f"bazarr: enabled providers {', '.join(_wanted_providers())}")
    elif _opensubtitles_creds():
        current = list(settings.get("general", {}).get("enabled_providers") or [])
        if "opensubtitlescom" not in current and "opensubtitlescom" not in [
            str(x) for x in current
        ]:
            for name in [*current, "opensubtitlescom"]:
                pairs.append(("settings-general-enabled_providers", str(name)))
            log("bazarr: added opensubtitlescom provider")

    creds = _opensubtitles_creds()
    if creds:
        user, password = creds
        pairs.extend([
            ("settings-opensubtitlescom-username", user),
            ("settings-opensubtitlescom-password", password),
            ("settings-opensubtitlescom-use_hash", "true"),
        ])
        log("bazarr: OpenSubtitles.com credentials set")

    pairs.append(("languages-enabled", "en"))

    profiles = httpx.get(
        f"{BASE}/api/system/languages/profiles",
        headers=_headers(key),
        timeout=30.0,
    ).json()
    if not profiles:
        pairs.append(("languages-profiles", json.dumps([english_forced_profile()])))
        log("bazarr: language profile English (en + en:forced)")
    else:
        # Keep existing profiles; still ensure defaults point at profile 1
        # when a Kine profile already exists.
        pass

    pairs.extend([
        ("settings-general-serie_default_enabled", "true"),
        ("settings-general-serie_default_profile", str(DEFAULT_PROFILE_ID)),
        ("settings-general-movie_default_enabled", "true"),
        ("settings-general-movie_default_profile", str(DEFAULT_PROFILE_ID)),
    ])

    r = _post_settings(key, pairs)
    if r.status_code not in (200, 204):
        log(f"bazarr: defaults returned HTTP {r.status_code}: {r.text[:200]}")
    else:
        log("bazarr: defaults applied")


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
            r = _post_settings(key, form)
            if r.status_code not in (200, 204):
                log(f"bazarr: settings returned HTTP {r.status_code}")
            else:
                log("bazarr: linked Sonarr/Radarr")
        except httpx.HTTPError as exc:
            log(f"bazarr: settings failed ({exc})")
            return

    try:
        _apply_defaults(key, log)
    except httpx.HTTPError as exc:
        log(f"bazarr: defaults failed ({exc})")

    _sync_libraries(key, log)
