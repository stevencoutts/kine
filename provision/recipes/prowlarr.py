"""Register Sonarr and Radarr as Prowlarr applications.

Once this is done, every indexer the user adds in Prowlarr appears in
both *arr apps automatically. It is the single highest-value piece of
pre-wiring in the stack.
"""
import os

from arrclient import ArrClient, http_error_detail
from keys import resolve_key
from prowlarr_indexers import ensure_indexers
from recipes import prowlarr_newznab

TARGETS = {
    "sonarr": ("Sonarr", "SonarrSettings", "http://localhost:8989"),
    "radarr": ("Radarr", "RadarrSettings", "http://localhost:7878"),
}


def transmission_client() -> dict:
    return {
        "enable": True,
        "protocol": "torrent",
        "priority": 1,
        "categories": [],
        "tags": [],
        "name": "Transmission",
        "implementation": "Transmission",
        "configContract": "TransmissionSettings",
        "fields": [
            # Prowlarr and Transmission share gluetun's namespace.
            {"name": "host", "value": "localhost"},
            {"name": "port", "value": 9091},
            {"name": "useSsl", "value": False},
            {"name": "urlBase", "value": "/transmission/"},
            {"name": "username", "value": ""},
            {"name": "password", "value": ""},
            {"name": "category", "value": ""},
            {"name": "directory", "value": ""},
            {"name": "priority", "value": 0},
            {"name": "addPaused", "value": False},
        ],
    }


def configure(enabled: set[str], log) -> None:
    client = ArrClient("http://gluetun:9696", resolve_key("prowlarr"), api="v1", timeout=120.0)
    if not client.wait():
        log("prowlarr: no API response, skipping wiring")
        return

    for app, (impl, contract, url) in TARGETS.items():
        if app not in enabled:
            continue
        payload = {
            "name": impl,
            "syncLevel": "fullSync",
            "implementation": impl,
            "configContract": contract,
            "fields": [
                # Both URLs below are resolved by Prowlarr and the *arr app
                # from inside the tunnel, so they are loopback.
                {"name": "prowlarrUrl", "value": "http://localhost:9696"},
                {"name": "baseUrl", "value": url},
                {"name": "apiKey", "value": resolve_key(app)},
            ],
        }
        if client.ensure("applications", payload):
            log(f"prowlarr: linked {impl}")

    ensure_indexers(client, log)

    rows = prowlarr_newznab.parse_indexers(os.environ.get(prowlarr_newznab.ENV_KEY, ""))
    if rows:
        try:
            prowlarr_newznab.ensure_newznab_indexers(client, rows, log)
        except Exception as exc:  # noqa: BLE001
            detail = http_error_detail(exc) if hasattr(exc, "response") else str(exc)
            log(f"prowlarr: newznab failed ({detail})")

    if "transmission" in enabled:
        if client.ensure("downloadclient", transmission_client()):
            log("prowlarr: download client Transmission")
