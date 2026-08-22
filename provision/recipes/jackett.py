"""Preconfigure public Jackett indexers on a fresh stack.

Kine uses Prowlarr as the primary indexer proxy wired into Sonarr and
Radarr. Jackett is provisioned with a small set of public indexers so
users can copy Torznab feeds without manual setup. Linking Jackett into
the *arr apps remains a manual step if Prowlarr is not in use.
"""
from jackettclient import JackettClient
from keys import resolve_key

# Field ids and values match Jackett's /api/v2.0/indexers/{id}/config API.
INDEXERS = {
    "kickasstorrents-ws": {
        "sitelink": "https://kattracker.com/",
        "sortrequestedfromsite": "time_add",
        "orderrequestedfromsite": "desc",
        "tags": "",
    },
    "thepiratebay": {
        "sitelink": "https://thepiratebay.org/",
        "apiurl": "apibay.org",
        "top100": "recent",
        "tags": "",
    },
    "1337x": {
        "sitelink": "https://1337x.to/",
        "sortrequestedfromsite": "time",
        "orderrequestedfromsite": "desc",
        "tags": "",
    },
}


def configure(log) -> None:
    client = JackettClient("http://gluetun:9117", resolve_key("jackett"))
    if not client.wait():
        log("jackett: no API response, skipping wiring")
        return

    for indexer_id, settings in INDEXERS.items():
        if client.ensure_indexer(indexer_id, settings):
            log(f"jackett: configured {indexer_id}")
