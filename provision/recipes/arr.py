"""Wire the *arr applications to their download clients and libraries.

This is the bulk of what "pre-configured to talk to each other" means:
a fresh install comes up with root folders set, download clients
registered, remote path mappings correct, and Prowlarr already pushing
indexers into Sonarr and Radarr. The user adds their indexer accounts
and nothing else.
"""
from __future__ import annotations

import os
import pathlib

import httpx

import tunnel_hosts
from arrclient import ArrClient
from keys import resolve_key

# The *arr apps see the shared volume as /data. Transmission and NZBGet
# see the same files at /data/downloads. Because both mounts come from
# the same DATA_ROOT on one filesystem, the paths line up exactly and no
# remote path mapping is needed. This is the whole reason for the single
# /data mount convention: get it wrong and every import becomes a copy.
ROOT_FOLDERS = {
    "sonarr": "/data/media/tv",
    "radarr": "/data/media/movies",
    "lidarr": "/data/media/music",
}

APP_PORTS = {
    "sonarr": 8989,
    "radarr": 7878,
    "lidarr": 8686,
}

APP_API = {
    "sonarr": "v3",
    "radarr": "v3",
    "lidarr": "v1",
}

DOWNLOAD_CATEGORIES = {
    "sonarr": "tv-sonarr",
    "radarr": "radarr",
    "lidarr": "lidarr",
}

BEETS_HOOK_PATH = "/config/kine-beets-hook.sh"
_BEETS_HOOK_SRC = (
    pathlib.Path(__file__).resolve().parents[1] / "assets" / "lidarr" / "kine-beets-hook.sh"
)


def transmission_client(category: str) -> dict:
    return {
        "enable": True,
        "protocol": "torrent",
        "priority": 1,
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
        "name": "Transmission",
        "implementation": "Transmission",
        "configContract": "TransmissionSettings",
        "fields": [
            # Sonarr and Transmission share gluetun's namespace, so
            # this is a genuine loopback call, not a network hop.
            {"name": "host", "value": "localhost"},
            {"name": "port", "value": 9091},
            {"name": "useSsl", "value": False},
            {"name": "urlBase", "value": "/transmission/"},
            {"name": "category", "value": category},
            {"name": "directory", "value": "/data/downloads/complete"},
        ],
    }


def nzbget_client(category: str) -> dict:
    # Sonarr uses tvCategory; Radarr movieCategory; Lidarr musicCategory.
    category_field = {
        "tv-sonarr": "tvCategory",
        "radarr": "movieCategory",
        "lidarr": "musicCategory",
    }.get(category, "movieCategory")
    return {
        "enable": True,
        "protocol": "usenet",
        "priority": 1,
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
        "name": "NZBGet",
        "implementation": "Nzbget",
        "configContract": "NzbgetSettings",
        "fields": [
            {"name": "host", "value": "localhost"},
            {"name": "port", "value": 6789},
            {"name": "useSsl", "value": False},
            {"name": "username", "value": "nzbget"},
            {"name": "password", "value": "nzbget"},
            {"name": category_field, "value": category},
        ],
    }


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _app_events(app: str) -> dict:
    """Notification event flags shared by Plex/Emby connections."""
    events = {
        "onGrab": False,
        "onDownload": True,
        "onUpgrade": True,
        # Release-level trigger (Sonarr 4 / Radarr 5+); harmless if ignored.
        "onImportComplete": True,
        "onRename": True,
        "onHealthIssue": False,
        "onApplicationUpdate": False,
        "includeHealthWarnings": False,
        "updateLibrary": True,
    }
    if app == "sonarr":
        events.update({
            "onSeriesDelete": False,
            "onEpisodeFileDelete": False,
            "onEpisodeFileDeleteForUpgrade": True,
        })
    elif app == "radarr":
        events.update({
            "onMovieAdded": False,
            "onMovieDelete": False,
            "onMovieFileDelete": False,
            "onMovieFileDeleteForUpgrade": True,
        })
    else:
        # Lidarr
        events.update({
            "onArtistDelete": False,
            "onAlbumDelete": False,
            "onTrackFileDelete": False,
            "onTrackFileDeleteForUpgrade": True,
        })
    return events


def _path_map_fields(server: str, app: str) -> list[dict]:
    """Optional mapFrom/mapTo when remote library mounts differ from *arr paths."""
    kind = {"sonarr": "TV", "radarr": "MOVIES", "lidarr": "MUSIC"}[app]
    prefix = f"{server.upper()}_{kind}_MAP"
    map_from = os.environ.get(f"{prefix}_FROM", "").strip()
    map_to = os.environ.get(f"{prefix}_TO", "").strip()
    if not map_from or not map_to:
        return []
    return [
        {"name": "mapFrom", "value": map_from},
        {"name": "mapTo", "value": map_to},
    ]


def plex_notification(
    app: str, host: str, port: int, token: str, *, use_ssl: bool = False
) -> dict:
    return {
        "name": "Plex",
        "implementation": "PlexServer",
        "configContract": "PlexServerSettings",
        **_app_events(app),
        "fields": [
            {"name": "host", "value": host},
            {"name": "port", "value": port},
            {"name": "useSsl", "value": use_ssl},
            {"name": "authToken", "value": token},
            *_path_map_fields("plex", app),
        ],
    }


def beets_notification() -> dict:
    return {
        "name": "Beets",
        "implementation": "CustomScript",
        "configContract": "CustomScriptSettings",
        "onGrab": False,
        "onReleaseImport": True,
        "onUpgrade": True,
        "onRename": True,
        "onTrackRetag": False,
        "onHealthIssue": False,
        "onApplicationUpdate": False,
        "includeHealthWarnings": False,
        "fields": [
            {"name": "path", "value": BEETS_HOOK_PATH},
        ],
    }


def install_lidarr_beets_hook(stack: pathlib.Path | None = None) -> pathlib.Path:
    """Lidarr Custom Script body. Beet itself runs in the beets container."""
    root = pathlib.Path(stack) if stack is not None else pathlib.Path("/stack")
    dest = root / "config" / "lidarr" / "kine-beets-hook.sh"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_BEETS_HOOK_SRC.read_text())
    dest.chmod(0o755)
    return dest


def emby_notification(
    app: str, host: str, port: int, api_key: str, *, use_ssl: bool = False
) -> dict:
    return {
        "name": "Emby",
        "implementation": "MediaBrowser",
        "configContract": "MediaBrowserSettings",
        **_app_events(app),
        "fields": [
            {"name": "host", "value": host},
            {"name": "port", "value": port},
            {"name": "useSsl", "value": use_ssl},
            {"name": "apiKey", "value": api_key},
            {"name": "notify", "value": False},
            *_path_map_fields("emby", app),
        ],
    }


def _plex_config() -> tuple[str, int, str, bool] | None:
    host = os.environ.get("PLEX_HOST", "").strip()
    token = os.environ.get("PLEX_TOKEN", "").strip()
    if not host or not token:
        return None
    use_ssl = _env_bool("PLEX_USE_SSL")
    port = _env_int("PLEX_PORT", 443 if use_ssl else 32400)
    if use_ssl and port == 32400:
        port = 443
    return (host, port, token, use_ssl)


def _emby_config(enabled: set[str]) -> tuple[str, int, str, bool] | None:
    token = os.environ.get("EMBY_API_KEY", "").strip()
    if not token:
        return None
    host = os.environ.get("EMBY_HOST", "").strip()
    use_ssl = _env_bool("EMBY_USE_SSL")
    port = _env_int("EMBY_PORT", 443 if use_ssl else 8096)
    if not host and "emby" in enabled:
        domain = os.environ.get("KINE_DOMAIN", "").strip()
        if domain:
            host = f"emby.{domain}"
            port = 443
            use_ssl = True
        else:
            host = "emby"
    if not host:
        return None
    # Traefik serves Emby on 443; 8096 is only the container port.
    if use_ssl and port == 8096:
        port = 443
    return (host, port, token, use_ssl)


def _sync_one_notification(
    client: ArrClient, app: str, log, name: str, payload: dict | None
) -> None:
    if payload:
        try:
            action = client.upsert("notification", payload)
            log(f"{app}: notification {name} ({action})")
        except httpx.HTTPStatusError as exc:
            from arrclient import http_error_detail

            log(f"{app}: notification {name} failed ({http_error_detail(exc)})")
        return
    if client.remove_named("notification", name):
        log(f"{app}: removed notification {name}")


def _sync_media_notifications(client: ArrClient, app: str, enabled: set[str], log) -> None:
    plex = _plex_config()
    if plex:
        host, port, token, use_ssl = plex
        _sync_one_notification(
            client, app, log, "Plex",
            plex_notification(app, host, port, token, use_ssl=use_ssl),
        )
    else:
        _sync_one_notification(client, app, log, "Plex", None)

    emby = _emby_config(enabled)
    if emby:
        host, port, key, use_ssl = emby
        _sync_one_notification(
            client, app, log, "Emby",
            emby_notification(app, host, port, key, use_ssl=use_ssl),
        )
    else:
        _sync_one_notification(client, app, log, "Emby", None)


def _bazarr_webhook(app: str, bazarr_key: str) -> dict:
    """Sonarr/Radarr Webhook → Bazarr. No updateLibrary (Plex/Emby only)."""
    hook = "sonarr" if app == "sonarr" else "radarr"
    events = {
        "onGrab": False,
        "onDownload": True,
        "onUpgrade": True,
        "onRename": True,
        "onHealthIssue": False,
        "onApplicationUpdate": False,
        "includeHealthWarnings": False,
    }
    if app == "sonarr":
        events.update({
            "onSeriesDelete": False,
            "onEpisodeFileDelete": False,
            "onEpisodeFileDeleteForUpgrade": True,
        })
    else:
        events.update({
            "onMovieAdded": False,
            "onMovieDelete": False,
            "onMovieFileDelete": False,
            "onMovieFileDeleteForUpgrade": True,
        })
    return {
        "name": "Bazarr",
        "implementation": "Webhook",
        "configContract": "WebhookSettings",
        **events,
        "fields": [
            {
                "name": "url",
                "value": f"http://127.0.0.1:6767/api/webhooks/{hook}?apikey={bazarr_key}",
            },
            {"name": "method", "value": 1},
        ],
    }


def _lidarr_root_folder(client: ArrClient) -> dict:
    """Lidarr requires name + default quality/metadata profile ids."""
    quality = client.get("qualityprofile")
    metadata = client.get("metadataprofile")
    if not quality or not metadata:
        raise RuntimeError("lidarr: no quality/metadata profiles yet")
    return {
        "path": ROOT_FOLDERS["lidarr"],
        "name": "Music",
        "defaultQualityProfileId": quality[0]["id"],
        "defaultMetadataProfileId": metadata[0]["id"],
    }


def configure(app: str, enabled: set[str], log) -> None:
    client = ArrClient(
        # The provisioner sits on kine_internal, outside the tunnel, so it
        # reaches the tier 2 apps at gluetun's address: that container is
        # the one that actually holds their sockets.
        tunnel_hosts.internal_base_for_app(app, APP_PORTS[app]),
        resolve_key(app),
        api=APP_API[app],
    )
    if not client.wait():
        log(f"{app}: no API response, skipping wiring")
        return

    category = DOWNLOAD_CATEGORIES[app]

    root_payload = (
        _lidarr_root_folder(client) if app == "lidarr"
        else {"path": ROOT_FOLDERS[app]}
    )
    if client.ensure("rootfolder", root_payload, match_on="path"):
        log(f"{app}: root folder {ROOT_FOLDERS[app]}")

    if "transmission" in enabled:
        if client.ensure("downloadclient", transmission_client(category)):
            log(f"{app}: download client Transmission")

    if "nzbget" in enabled:
        action = client.upsert("downloadclient", nzbget_client(category))
        if action == "created":
            log(f"{app}: download client NZBGet")
        elif action == "updated":
            log(f"{app}: updated NZBGet download client")

    # Completed download handling on, unmonitor nothing, no analytics.
    # Lidarr: never write/scrub audio tags — beets owns tag content.
    host_patches: list[tuple[str, dict]] = [
        ("config/downloadclient", {"enableCompletedDownloadHandling": True}),
        ("config/host", {"analyticsEnabled": False}),
    ]
    if app == "lidarr":
        host_patches.append((
            "config/metadataProvider",
            {"writeAudioTags": "no", "scrubAudioTags": False},
        ))
    for cfg, patch in host_patches:
        try:
            current = client.get(cfg)
            current.update(patch)
            client.put(f"{cfg}/{current['id']}", current)
            if cfg == "config/metadataProvider":
                log("lidarr: write audio tags -> Never")
        except Exception as exc:  # noqa: BLE001 — host config schemas differ
            log(f"{app}: skipped {cfg} ({exc})")

    _sync_media_notifications(client, app, enabled, log)

    if app == "lidarr":
        if "beets" in enabled:
            install_lidarr_beets_hook()
            _sync_one_notification(client, app, log, "Beets", beets_notification())
        else:
            _sync_one_notification(client, app, log, "Beets", None)

    # Bazarr only knows Sonarr/Radarr.
    if app in ("sonarr", "radarr"):
        if "bazarr" in enabled:
            _sync_one_notification(
                client, app, log, "Bazarr", _bazarr_webhook(app, resolve_key("bazarr"))
            )
        else:
            _sync_one_notification(client, app, log, "Bazarr", None)
