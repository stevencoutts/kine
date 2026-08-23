"""Seed public Cardigann indexers in Prowlarr.

Mirrors the three indexers Jackett preconfigures when enabled, but wires
them through Prowlarr so fullSync pushes them into Sonarr and Radarr.
"""
from arrclient import ArrClient

# Field values mirror Jackett's public indexer defaults, expressed in
# Prowlarr's Cardigann schema (select options are numeric indices).
INDEXERS = {
    "thepiratebay": {
        "apiurl": "apibay.org",
        "top100": 6,  # All / recent
    },
    "1337x": {
        "sort": 2,  # created / time
        "type": 1,  # desc
    },
    "kickasstorrents-ws": {
        "sort": 2,  # created / time_add
        "type": 1,  # desc
    },
}

_SKIP_FIELD_TYPES = {
    "info",
    "info_flaresolverr",
    "info_category_8000",
    "info_download",
    "info_top100",
    "info_uploader",
}


def _field_map(item: dict) -> dict[str, object]:
    return {field["name"]: field.get("value") for field in item.get("fields", [])}


def _indexer_payload(template: dict, overrides: dict[str, object]) -> dict:
    fields = []
    for field in template.get("fields", []):
        if field.get("type") in _SKIP_FIELD_TYPES:
            continue
        name = field["name"]
        fields.append({"name": name, "value": overrides.get(name, field.get("value"))})
    return {
        "enable": True,
        "name": template["name"],
        "implementation": template["implementation"],
        "configContract": template["configContract"],
        "appProfileId": 1,
        "downloadClientId": 0,
        "priority": 25,
        "fields": fields,
    }


def _matches(existing: dict, overrides: dict[str, object]) -> bool:
    values = _field_map(existing)
    return all(values.get(name) == value for name, value in overrides.items())


def _save_indexer(client: ArrClient, payload: dict) -> None:
    """Create an indexer, bypassing pre-save probes when sites block VPN egress."""
    wanted_enabled = payload.get("enable", True)
    create_payload = dict(payload)
    create_payload["enable"] = False
    client.post("indexer?forceSave=true", create_payload)
    if not wanted_enabled:
        return

    definition = next(
        field["value"]
        for field in create_payload["fields"]
        if field["name"] == "definitionFile"
    )
    created = next(
        item for item in client.get("indexer") if item.get("definitionName") == definition
    )
    enable_payload = dict(payload)
    enable_payload["id"] = created["id"]
    enable_payload["enable"] = True
    client.put(f"indexer/{created['id']}?forceSave=true", enable_payload)


def ensure_indexers(client: ArrClient, log) -> None:
    schema = {item["definitionName"]: item for item in client.get("indexer/schema")}
    existing = {
        item.get("definitionName"): item
        for item in client.get("indexer")
        if item.get("definitionName")
    }

    for definition, overrides in INDEXERS.items():
        template = schema.get(definition)
        if template is None:
            log(f"prowlarr: indexer schema missing for {definition}")
            continue

        payload = _indexer_payload(template, overrides)
        current = existing.get(definition)
        if current and _matches(current, overrides) and current.get("enable"):
            continue

        if current:
            payload["id"] = current["id"]
            client.put(f"indexer/{current['id']}?forceSave=true", payload)
            log(f"prowlarr: updated indexer {definition}")
            continue

        _save_indexer(client, payload)
        log(f"prowlarr: configured indexer {definition}")
