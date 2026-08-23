"""Provisioner behaviour that the appliance's pre-wiring depends on."""
import pathlib
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("KINE_SECRET", "test-secret-value")
    monkeypatch.setattr("seed.STACK", tmp_path, raising=False)
    import seed
    monkeypatch.setattr(seed, "STACK", tmp_path)
    return tmp_path


def test_keys_are_deterministic_and_distinct(monkeypatch):
    monkeypatch.setenv("KINE_SECRET", "abc")
    from keys import api_key
    assert api_key("sonarr") == api_key("sonarr")
    assert api_key("sonarr") != api_key("radarr")
    assert len(api_key("sonarr")) == 32


def test_keys_change_with_the_secret(monkeypatch):
    from keys import api_key
    monkeypatch.setenv("KINE_SECRET", "one")
    first = api_key("sonarr")
    monkeypatch.setenv("KINE_SECRET", "two")
    assert api_key("sonarr") != first


def test_empty_secret_is_refused_rather_than_hashed(monkeypatch):
    monkeypatch.setenv("KINE_SECRET", "")
    from keys import api_key
    with pytest.raises(RuntimeError):
        api_key("sonarr")


def test_seeding_writes_the_derived_key(stack):
    import seed
    from keys import api_key
    seed.seed_arr("sonarr")
    cfg = stack / "config" / "sonarr" / "config.xml"
    assert ET.parse(cfg).getroot().findtext("ApiKey") == api_key("sonarr")


def test_seeding_never_overwrites_an_existing_key(stack):
    """A restore, or a second install pass, must not re-key an app that
    external clients are already talking to."""
    import seed
    d = stack / "config" / "radarr"
    d.mkdir(parents=True)
    (d / "config.xml").write_text(
        '<?xml version="1.0"?><Config><ApiKey>preexisting-key-do-not-touch</ApiKey></Config>'
    )
    seed.seed_arr("radarr")
    assert "preexisting-key-do-not-touch" in (d / "config.xml").read_text()


def test_seeded_config_disables_analytics_and_browser_launch(stack):
    import seed
    seed.seed_arr("prowlarr")
    root = ET.parse(stack / "config" / "prowlarr" / "config.xml").getroot()
    assert root.findtext("AnalyticsEnabled") == "False"
    assert root.findtext("LaunchBrowser") == "False"
    assert root.findtext("BindAddress") == "*"


def test_download_clients_use_loopback_not_service_names():
    """Inside the shared namespace, a service name would resolve to
    nothing. This is the wiring most likely to be broken by a careless
    edit, so it is asserted rather than assumed."""
    from recipes.arr import nzbget_client, transmission_client
    for client in (transmission_client("tv-sonarr"), nzbget_client("tv-sonarr")):
        host = [f for f in client["fields"] if f["name"] == "host"][0]["value"]
        assert host == "localhost", f"{client['name']} addresses {host}"


def test_transmission_client_directory_matches_shared_data_mount():
    from recipes.arr import transmission_client
    from recipes.transmission import DOWNLOAD_DIR, INCOMPLETE_DIR

    directory = [f for f in transmission_client("tv-sonarr")["fields"]
                 if f["name"] == "directory"][0]["value"]
    assert directory == DOWNLOAD_DIR
    assert DOWNLOAD_DIR.startswith("/data/downloads/")
    assert INCOMPLETE_DIR.startswith("/data/downloads/")


def test_transmission_seed_uses_data_download_paths(stack):
    import json
    import seed
    seed.seed_transmission()
    settings = json.loads((stack / "config" / "transmission" / "settings.json").read_text())
    assert settings["download-dir"] == "/data/downloads/complete"
    assert settings["incomplete-dir"] == "/data/downloads/incomplete"


def test_transmission_seed_never_overwrites_existing_settings(stack):
    import json
    import seed
    d = stack / "config" / "transmission"
    d.mkdir(parents=True)
    (d / "settings.json").write_text(json.dumps({"download-dir": "/downloads/complete"}))
    seed.seed_transmission()
    assert json.loads((d / "settings.json").read_text())["download-dir"] == "/downloads/complete"


def test_transmission_configure_sets_paths_via_rpc(monkeypatch):
    import recipes.transmission as transmission

    calls = []

    class FakeResponse:
        def __init__(self, status_code=200, headers=None, body=None):
            self.status_code = status_code
            self.headers = headers or {}
            self.content = b"{}"
            self._body = body or {"arguments": {"download-dir": "/downloads/complete"}}

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json, headers=None):
            calls.append((json.get("method"), json.get("arguments")))
            if json.get("method") == "session-get":
                return FakeResponse(body={"arguments": {
                    "download-dir": "/downloads/complete",
                    "incomplete-dir": "/downloads/incomplete",
                    "incomplete-dir-enabled": True,
                }})
            return FakeResponse()

    monkeypatch.setattr(transmission.httpx, "Client", FakeClient)
    logs = []
    transmission.configure(logs.append)
    assert ("session-set", {
        "download-dir": "/data/downloads/complete",
        "incomplete-dir": "/data/downloads/incomplete",
        "incomplete-dir-enabled": True,
    }) in calls
    assert any("download-dir -> /data/downloads/complete" in m for m in logs)


def test_ensure_data_tree_creates_category_directories(monkeypatch):
    created = []

    def track_mkdir(self, *args, **kwargs):
        created.append(str(self))

    monkeypatch.setattr("provision.pathlib.Path.mkdir", track_mkdir, raising=False)
    from provision import ensure_data_tree

    ensure_data_tree()
    assert "/data/downloads/complete/tv-sonarr" in created
    assert "/data/downloads/complete/radarr" in created


def test_prowlarr_links_both_pvrs_over_loopback():
    from recipes.prowlarr import TARGETS
    for app, (_, _, url) in TARGETS.items():
        assert url.startswith("http://localhost:"), f"{app} linked as {url}"


def test_root_folders_sit_under_the_single_data_mount():
    from recipes.arr import ROOT_FOLDERS
    for app, path in ROOT_FOLDERS.items():
        assert path.startswith("/data/media/"), f"{app} root folder is {path}"


class _FakeHTTP:
    def __init__(self, existing):
        self.existing = existing
        self.posted = []

    def get(self, url):
        class R:
            status_code = 200
            content = b"[]"
            def json(_self): return self.existing
            def raise_for_status(_self): pass
        return R()

    def post(self, url, json):
        self.posted.append(json)
        class R:
            status_code = 200
            content = b"{}"
            def json(_self): return {}
            def raise_for_status(_self): pass
        return R()


def test_ensure_is_idempotent():
    """Every provisioning write goes through ensure(). If it were not
    idempotent, `./kine enable <app>` would duplicate download clients
    each time it ran."""
    from arrclient import ArrClient
    c = ArrClient("http://x", "k")
    c.http = _FakeHTTP(existing=[{"name": "Transmission"}])
    assert c.ensure("downloadclient", {"name": "Transmission"}) is False
    assert c.http.posted == []
    assert c.ensure("downloadclient", {"name": "NZBGet"}) is True
    assert len(c.http.posted) == 1


def test_resolve_key_prefers_live_config_over_derived(stack, monkeypatch):
    """Apps started before seed keep a random key in config.xml. Wire must
    authenticate with that key, not the derived one, or download-client
    registration never runs."""
    import keys
    monkeypatch.setattr(keys, "STACK", stack)
    d = stack / "config" / "sonarr"
    d.mkdir(parents=True)
    (d / "config.xml").write_text(
        '<?xml version="1.0"?><Config><ApiKey>live-key-from-running-app</ApiKey></Config>'
    )
    assert keys.resolve_key("sonarr") == "live-key-from-running-app"
    assert keys.resolve_key("sonarr") != keys.api_key("sonarr")


def test_resolve_key_falls_back_to_derived_when_unseeded(stack, monkeypatch):
    import keys
    monkeypatch.setattr(keys, "STACK", stack)
    assert keys.resolve_key("radarr") == keys.api_key("radarr")


def test_resolve_key_prefers_jackett_server_config(stack, monkeypatch):
    import keys
    monkeypatch.setattr(keys, "STACK", stack)
    cfg_dir = stack / "config" / "jackett" / "Jackett"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "ServerConfig.json").write_text(
        '{"APIKey": "live-jackett-key-from-running-app"}'
    )
    assert keys.resolve_key("jackett") == "live-jackett-key-from-running-app"
    assert keys.resolve_key("jackett") != keys.api_key("jackett")


def test_jackett_seed_writes_derived_api_key(stack):
    import json
    import seed
    from keys import api_key
    seed.seed_jackett()
    cfg = stack / "config" / "jackett" / "Jackett" / "ServerConfig.json"
    assert json.loads(cfg.read_text())["APIKey"] == api_key("jackett")


def test_jackett_seed_never_overwrites_an_existing_key(stack):
    import json
    import seed
    cfg_dir = stack / "config" / "jackett" / "Jackett"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "ServerConfig.json").write_text(
        '{"APIKey": "preexisting-jackett-key-do-not-touch"}'
    )
    seed.seed_jackett()
    assert (
        json.loads((cfg_dir / "ServerConfig.json").read_text())["APIKey"]
        == "preexisting-jackett-key-do-not-touch"
    )


def test_jackett_public_indexers_match_screenshot_settings():
    from recipes.jackett import INDEXERS
    assert INDEXERS["kickasstorrents-ws"]["sitelink"] == "https://kattracker.com/"
    assert INDEXERS["kickasstorrents-ws"]["sortrequestedfromsite"] == "time_add"
    assert INDEXERS["kickasstorrents-ws"]["orderrequestedfromsite"] == "desc"
    assert INDEXERS["thepiratebay"]["sitelink"] == "https://thepiratebay.org/"
    assert INDEXERS["thepiratebay"]["apiurl"] == "apibay.org"
    assert INDEXERS["thepiratebay"]["top100"] == "recent"
    assert "1337x" in INDEXERS


def test_jackett_ensure_indexer_is_idempotent():
    from jackettclient import JackettClient

    class FakeClient(JackettClient):
        def __init__(self):
            self.configured = set()
            self.values = {}
            self.posted = []

        def configured_ids(self):
            return set(self.configured)

        def config_values(self, indexer_id):
            return dict(self.values)

        def apply_config(self, indexer_id, settings):
            self.posted.append((indexer_id, settings))
            self.configured.add(indexer_id)
            self.values.update(settings)

    client = FakeClient()
    settings = {"sitelink": "https://kattracker.com/", "tags": ""}
    assert client.ensure_indexer("kickasstorrents-ws", settings) is True
    assert client.ensure_indexer("kickasstorrents-ws", settings) is False
    assert len(client.posted) == 1


def test_arr_wiring_registers_transmission_for_sonarr_and_radarr(monkeypatch):
    """Both PVRs share the same download-client ensure path."""
    import recipes.arr as arr

    posted = []

    class FakeClient:
        def __init__(self, base, key):
            self.base, self.key = base, key

        def wait(self):
            return True

        def ensure(self, path, payload, match_on="name"):
            posted.append((path, payload.get(match_on), payload.get("implementation")))
            return True

        def get(self, path):
            return {"id": 1}

        def put(self, path, payload):
            return payload

    monkeypatch.setattr(arr, "ArrClient", FakeClient)
    monkeypatch.setattr(arr, "resolve_key", lambda app: f"key-{app}")
    logs = []
    enabled = {"sonarr", "radarr", "transmission"}
    arr.configure("sonarr", enabled, logs.append)
    arr.configure("radarr", enabled, logs.append)

    clients = [(p, name, impl) for p, name, impl in posted if p == "downloadclient"]
    assert ("downloadclient", "Transmission", "Transmission") in clients
    assert clients.count(("downloadclient", "Transmission", "Transmission")) == 2
    assert any("sonarr: download client Transmission" in m for m in logs)
    assert any("radarr: download client Transmission" in m for m in logs)


def test_prowlarr_public_indexers_match_jackett_defaults():
    from prowlarr_indexers import INDEXERS

    assert INDEXERS["thepiratebay"]["apiurl"] == "apibay.org"
    assert INDEXERS["thepiratebay"]["top100"] == 6
    assert INDEXERS["1337x"]["sort"] == 2
    assert INDEXERS["1337x"]["type"] == 1
    assert INDEXERS["kickasstorrents-ws"]["sort"] == 2
    assert INDEXERS["kickasstorrents-ws"]["type"] == 1


def test_prowlarr_transmission_client_uses_loopback():
    from recipes.prowlarr import transmission_client

    client = transmission_client()
    host = next(f for f in client["fields"] if f["name"] == "host")["value"]
    assert host == "localhost"
    assert client["categories"] == []
    assert client["protocol"] == "torrent"


def test_prowlarr_ensure_indexers_is_idempotent():
    from prowlarr_indexers import INDEXERS, ensure_indexers

    class FakeClient:
        def __init__(self):
            self.indexers = []
            self.posted = []
            self.puts = []

        def get(self, path):
            if path == "indexer/schema":
                return [
                    {
                        "definitionName": name,
                        "name": name.title(),
                        "implementation": "Cardigann",
                        "configContract": "CardigannSettings",
                        "fields": [
                            {"name": "definitionFile", "value": name, "type": "textbox"},
                            *[
                                {"name": key, "value": value, "type": "select"}
                                for key, value in overrides.items()
                            ],
                        ],
                    }
                    for name, overrides in INDEXERS.items()
                ]
            if path == "indexer":
                return self.indexers
            raise AssertionError(path)

        def post(self, path, payload):
            self.posted.append((path, payload["enable"]))
            self.indexers.append(
                {
                    "id": len(self.indexers) + 1,
                    "definitionName": payload["fields"][0]["value"],
                    "enable": payload["enable"],
                    "fields": payload["fields"],
                }
            )

        def put(self, path, payload):
            self.puts.append((path, payload.get("enable")))
            for item in self.indexers:
                if item["id"] == payload["id"]:
                    item.update({"enable": payload["enable"], "fields": payload["fields"]})

    client = FakeClient()
    logs = []
    ensure_indexers(client, logs.append)
    ensure_indexers(client, logs.append)
    assert len(client.posted) == 3
    assert len(client.puts) == 3
    assert client.puts[0][1] is True
    assert logs.count("prowlarr: configured indexer thepiratebay") == 1


def test_prowlarr_configure_wires_indexers_and_transmission(monkeypatch):
    import recipes.prowlarr as prowlarr

    posted = []

    class FakeClient:
        def __init__(self, base, key, api="v1", timeout=30.0):
            self.base, self.key, self.api, self.timeout = base, key, api, timeout
            self.indexers = []

        def wait(self):
            return True

        def ensure(self, path, payload, match_on="name"):
            posted.append((path, payload.get(match_on), payload.get("implementation")))
            return path == "downloadclient"

        def get(self, path):
            if path == "indexer/schema":
                return [
                    {
                        "definitionName": "thepiratebay",
                        "name": "The Pirate Bay",
                        "implementation": "Cardigann",
                        "configContract": "CardigannSettings",
                        "fields": [
                            {"name": "definitionFile", "value": "thepiratebay", "type": "textbox"},
                            {"name": "apiurl", "value": "apibay.org", "type": "textbox"},
                            {"name": "top100", "value": 6, "type": "select"},
                        ],
                    }
                ]
            if path == "indexer":
                return self.indexers
            raise AssertionError(path)

        def post(self, path, payload):
            posted.append((path, payload.get("name")))
            self.indexers.append(
                {
                    "id": len(self.indexers) + 1,
                    "definitionName": "thepiratebay",
                    "enable": payload.get("enable"),
                    "fields": payload["fields"],
                }
            )

        def put(self, path, payload):
            posted.append((path, payload.get("name")))

    monkeypatch.setattr(prowlarr, "ArrClient", FakeClient)
    monkeypatch.setattr(prowlarr, "resolve_key", lambda app: f"key-{app}")
    logs = []
    prowlarr.configure({"sonarr", "radarr", "transmission"}, logs.append)

    assert ("applications", "Sonarr", "Sonarr") in posted
    assert ("applications", "Radarr", "Radarr") in posted
    assert ("downloadclient", "Transmission", "Transmission") in posted
    assert any("prowlarr: configured indexer thepiratebay" in m for m in logs)
    assert any("prowlarr: download client Transmission" in m for m in logs)
