"""Wire the *arr applications to their download clients and libraries.

This is the bulk of what "pre-configured to talk to each other" means:
a fresh install comes up with root folders set, download clients
registered, remote path mappings correct, and Prowlarr already pushing
indexers into Sonarr and Radarr. The user adds their indexer accounts
and nothing else.
"""
from arrclient import ArrClient
from keys import api_key

# The *arr apps see the shared volume as /data. Transmission and NZBGet
# see the same files at /data/downloads. Because both mounts come from
# the same DATA_ROOT on one filesystem, the paths line up exactly and no
# remote path mapping is needed. This is the whole reason for the single
# /data mount convention: get it wrong and every import becomes a copy.
ROOT_FOLDERS = {
    "sonarr": "/data/media/tv",
    "radarr": "/data/media/movies",
}


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
            # Transmission lives inside gluetun's namespace, so it is
            # addressed as gluetun on the internal network.
            {"name": "host", "value": "gluetun"},
            {"name": "port", "value": 9091},
            {"name": "useSsl", "value": False},
            {"name": "urlBase", "value": "/transmission/"},
            {"name": "category", "value": category},
            {"name": "directory", "value": "/data/downloads/complete"},
        ],
    }


def nzbget_client(category: str) -> dict:
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
            {"name": "host", "value": "gluetun"},
            {"name": "port", "value": 6789},
            {"name": "useSsl", "value": False},
            {"name": "username", "value": "nzbget"},
            {"name": "password", "value": "tegbzn6789"},
            {"name": "category", "value": category},
        ],
    }


def configure(app: str, enabled: set[str], log) -> None:
    client = ArrClient(
        {"sonarr": "http://sonarr:8989", "radarr": "http://radarr:7878"}[app],
        api_key(app),
    )
    if not client.wait():
        log(f"{app}: no API response, skipping wiring")
        return

    category = "tv-sonarr" if app == "sonarr" else "radarr"

    if client.ensure("rootfolder", {"path": ROOT_FOLDERS[app]}, match_on="path"):
        log(f"{app}: root folder {ROOT_FOLDERS[app]}")

    if "transmission" in enabled:
        if client.ensure("downloadclient", transmission_client(category)):
            log(f"{app}: download client Transmission")

    if "nzbget" in enabled:
        if client.ensure("downloadclient", nzbget_client(category)):
            log(f"{app}: download client NZBGet")

    # Completed download handling on, unmonitor nothing, no analytics.
    for cfg, patch in (
        ("config/downloadclient", {"enableCompletedDownloadHandling": True}),
        ("config/host", {"analyticsEnabled": False}),
    ):
        current = client.get(cfg)
        current.update(patch)
        client.put(f"{cfg}/{current['id']}", current)
