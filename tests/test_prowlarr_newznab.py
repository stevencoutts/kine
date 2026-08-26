"""Prowlarr Newznab indexers configured from Helm / .env."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))

from recipes import prowlarr_newznab as nz  # noqa: E402


def test_parse_indexers_normalises_althub_shape():
    raw = '[{"name":"Althub","url":"https://api.althub.co.za","api_key":"secret"}]'
    rows = nz.parse_indexers(raw)
    assert len(rows) == 1
    assert rows[0]["name"] == "Althub"
    assert rows[0]["url"] == "https://api.althub.co.za"
    assert rows[0]["api_key"] == "secret"
    assert rows[0]["api_path"] == "/api"
    assert rows[0]["enable_rss"] is True
    assert 2000 in rows[0]["categories"]
    assert 5000 in rows[0]["categories"]


def test_parse_indexers_skips_placeholder_and_incomplete():
    assert nz.parse_indexers('[{"name":"X","url":"https://api.example.com"}]') == []
    assert nz.parse_indexers('[{"name":"X","url":"","api_key":"k"}]') == []
    assert nz.parse_indexers("") == []
    assert nz.parse_indexers("not-json") == []


def test_serialize_roundtrip_keeps_key():
    rows = [{
        "name": "Althub",
        "url": "https://api.althub.co.za",
        "api_key": "abc",
        "api_path": "/api",
        "enable_rss": True,
        "enable_automatic_search": True,
        "enable_interactive_search": True,
        "categories": [2000, 5000],
    }]
    again = nz.parse_indexers(nz.serialize_indexers(rows))
    assert again[0]["api_key"] == "abc"
    assert again[0]["categories"] == [2000, 5000]


def test_merge_keeps_existing_password_when_blank():
    existing = nz.parse_indexers(
        '[{"name":"Althub","url":"https://api.althub.co.za","api_key":"keep-me"}]'
    )
    incoming = [{
        "name": "Althub",
        "url": "https://api.althub.co.za",
        "api_key": "",
    }]
    merged = nz.merge_indexers(incoming, existing)
    assert merged[0]["api_key"] == "keep-me"


def test_newznab_payload_fields():
    row = nz.parse_indexers(
        '[{"name":"Althub","url":"https://api.althub.co.za/","api_key":"k"}]'
    )[0]
    payload = nz.newznab_payload(row)
    assert payload["name"] == "Althub"
    assert payload["implementation"] == "Newznab"
    assert payload["configContract"] == "NewznabSettings"
    assert payload["protocol"] == "usenet"
    assert payload["appProfileId"] == 1
    assert payload["redirect"] is True
    fields = {f["name"]: f["value"] for f in payload["fields"]}
    assert fields["baseUrl"] == "https://api.althub.co.za"
    assert fields["apiPath"] == "/api"
    assert fields["apiKey"] == "k"
    assert 2000 in fields["categories"]


def test_ensure_newznab_creates_and_updates():
    class Fake:
        def __init__(self):
            self.items = []
            self.posts = []
            self.puts = []

        def get(self, path):
            assert path == "indexer"
            return list(self.items)

        def post(self, path, payload):
            assert "forceSave" in path
            self.posts.append(payload)
            row = {**payload, "id": len(self.items) + 1}
            self.items.append(row)
            return row

        def put(self, path, payload):
            assert "forceSave" in path
            self.puts.append(payload)
            for i, item in enumerate(self.items):
                if item["id"] == payload["id"]:
                    self.items[i] = payload
                    return payload
            raise AssertionError("missing id")

    client = Fake()
    row = nz.parse_indexers(
        '[{"name":"Althub","url":"https://api.althub.co.za","api_key":"k1"}]'
    )[0]
    logs = []
    assert nz.ensure_newznab_indexers(client, [row], logs.append) == ["created:Althub"]
    assert len(client.posts) == 1
    row2 = {**row, "api_key": "k2"}
    assert nz.ensure_newznab_indexers(client, [row2], logs.append) == ["updated:Althub"]
    assert client.items[0]["fields"]
    fields = {f["name"]: f["value"] for f in client.items[0]["fields"]}
    assert fields["apiKey"] == "k2"


def test_env_example_and_compose_document_key():
    text = (ROOT / ".env.example").read_text()
    assert "PROWLARR_NEWZNAB_INDEXERS=" in text
    compose = (ROOT / "compose" / "core.provision.yml").read_text()
    assert "PROWLARR_NEWZNAB_INDEXERS" in compose
