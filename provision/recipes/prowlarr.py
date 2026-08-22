"""Register Sonarr and Radarr as Prowlarr applications.

Once this is done, every indexer the user adds in Prowlarr appears in
both *arr apps automatically. It is the single highest-value piece of
pre-wiring in the stack.
"""
from arrclient import ArrClient
from keys import api_key

TARGETS = {
    "sonarr": ("Sonarr", "SonarrSettings", "http://sonarr:8989"),
    "radarr": ("Radarr", "RadarrSettings", "http://radarr:7878"),
}


def configure(enabled: set[str], log) -> None:
    client = ArrClient("http://prowlarr:9696", api_key("prowlarr"), api="v1")
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
                {"name": "prowlarrUrl", "value": "http://prowlarr:9696"},
                {"name": "baseUrl", "value": url},
                {"name": "apiKey", "value": api_key(app)},
            ],
        }
        if client.ensure("applications", payload):
            log(f"prowlarr: linked {impl}")
