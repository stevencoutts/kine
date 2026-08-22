"""Register Sonarr and Radarr as Prowlarr applications.

Once this is done, every indexer the user adds in Prowlarr appears in
both *arr apps automatically. It is the single highest-value piece of
pre-wiring in the stack.
"""
from arrclient import ArrClient
from keys import resolve_key

TARGETS = {
    "sonarr": ("Sonarr", "SonarrSettings", "http://localhost:8989"),
    "radarr": ("Radarr", "RadarrSettings", "http://localhost:7878"),
}


def configure(enabled: set[str], log) -> None:
    client = ArrClient("http://gluetun:9696", resolve_key("prowlarr"), api="v1")
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
