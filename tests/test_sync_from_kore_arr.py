"""Unit tests for kore → kine *arr sync helpers."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sync_from_kore_arr",
    ROOT / "scripts" / "sync-from-kore-arr.py",
)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(mod)


def test_find_althub_by_name_and_url():
    rows = [
        {"name": "Prowlarr", "fields": [{"name": "baseUrl", "value": "http://prowlarr:9696"}]},
        {"name": "Althub", "fields": [{"name": "baseUrl", "value": "https://api.althub.co.za"},
                                      {"name": "apiKey", "value": "abc"}]},
    ]
    found = mod.find_althub(rows)
    assert found and found["name"] == "Althub"


def test_indexer_payload_strips_id_keeps_key():
    source = {
        "id": 5,
        "name": "Althub",
        "implementation": "Newznab",
        "configContract": "NewznabSettings",
        "protocol": "usenet",
        "enableRss": True,
        "enableAutomaticSearch": True,
        "enableInteractiveSearch": True,
        "priority": 25,
        "fields": [
            {"name": "baseUrl", "value": "https://api.althub.co.za"},
            {"name": "apiKey", "value": "secret"},
            {"name": "categories", "value": [5000]},
        ],
    }
    payload = mod.indexer_payload_from_source(source, api_key="secret")
    assert "id" not in payload
    assert payload["name"] == "Althub"
    fields = {f["name"]: f["value"] for f in payload["fields"]}
    assert fields["apiKey"] == "secret"
    assert fields["categories"] == [5000]


def test_redacted_api_key_requires_override():
    source = {
        "name": "Althub",
        "implementation": "Newznab",
        "configContract": "NewznabSettings",
        "protocol": "usenet",
        "fields": [{"name": "apiKey", "value": "********"}],
    }
    try:
        mod.indexer_payload_from_source(source)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "redacted" in str(exc)
