"""Provisioner behaviour that the appliance's pre-wiring depends on."""
import json
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


def test_seerr_seed_prepares_node_owned_config(stack, monkeypatch):
    import seed

    calls = []
    monkeypatch.setattr(seed.os, "chown", lambda path, uid, gid: calls.append((path, uid, gid)))
    seed.seed_seerr()
    cfg = stack / "config" / "seerr"
    logs = cfg / "logs"
    assert cfg.is_dir()
    assert logs.is_dir()
    assert all(uid == seed.SEERR_UID and gid == seed.SEERR_GID for _, uid, gid in calls)
    assert cfg in {path for path, _, _ in calls}


def test_bazarr_seed_writes_nested_config_with_derived_key(stack):
    import yaml
    import seed
    from keys import api_key, resolve_key

    seed.seed_arr("sonarr")
    seed.seed_arr("radarr")
    seed.seed_bazarr({"sonarr", "radarr", "bazarr"})
    cfg = stack / "config" / "bazarr" / "config" / "config.yaml"
    assert cfg.is_file()
    data = yaml.safe_load(cfg.read_text())
    assert data["auth"]["apikey"] == api_key("bazarr")
    assert data["general"]["use_sonarr"] is True
    assert data["sonarr"]["ip"] == "127.0.0.1"
    assert data["sonarr"]["apikey"] == resolve_key("sonarr")
    assert data["radarr"]["port"] == 7878


def test_resolve_key_reads_bazarr_yaml(stack, monkeypatch):
    import yaml
    import keys

    monkeypatch.setattr(keys, "STACK", stack)
    cfg = stack / "config" / "bazarr" / "config"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(yaml.safe_dump({"auth": {"apikey": "live-bazarr-key"}}))
    assert keys.resolve_key("bazarr") == "live-bazarr-key"


def test_bazarr_webhook_points_at_loopback_with_api_key():
    from recipes.arr import _bazarr_webhook

    payload = _bazarr_webhook("sonarr", "baz-key")
    fields = {f["name"]: f["value"] for f in payload["fields"]}
    assert payload["implementation"] == "Webhook"
    assert "127.0.0.1:6767/api/webhooks/sonarr?apikey=baz-key" in fields["url"]


def test_bazarr_english_profile_includes_forced():
    from recipes.bazarr import english_forced_profile

    profile = english_forced_profile()
    assert profile["name"] == "English"
    assert profile["profileId"] == 1
    items = {(i["language"], i["forced"]) for i in profile["items"]}
    assert ("en", "False") in items
    assert ("en", "True") in items


def test_bazarr_wanted_providers_include_opensubtitles_when_creds(monkeypatch):
    from recipes import bazarr

    monkeypatch.delenv("OPENSUBTITLES_USERNAME", raising=False)
    monkeypatch.delenv("OPENSUBTITLES_PASSWORD", raising=False)
    assert "opensubtitlescom" not in bazarr._wanted_providers()
    assert "gestdown" in bazarr._wanted_providers()

    monkeypatch.setenv("OPENSUBTITLES_USERNAME", "user")
    monkeypatch.setenv("OPENSUBTITLES_PASSWORD", "pass")
    assert bazarr._wanted_providers()[-1] == "opensubtitlescom"


def test_bazarr_providers_need_defaults_for_empty_or_joined():
    from recipes.bazarr import _providers_need_defaults

    assert _providers_need_defaults({"general": {"enabled_providers": []}}) is True
    assert _providers_need_defaults({
        "general": {"enabled_providers": ["gestdown,tvsubtitles"]},
    }) is True
    assert _providers_need_defaults({
        "general": {"enabled_providers": ["gestdown", "tvsubtitles"]},
    }) is False


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

        def upsert(self, path, payload, match_on="name"):
            posted.append((path, payload.get(match_on), payload.get("implementation")))
            return "created"

        def remove_named(self, path, name, match_on="name"):
            posted.append((path, f"remove:{name}", None))
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


def test_plex_and_emby_notification_payloads():
    from recipes.arr import emby_notification, plex_notification

    plex = plex_notification("sonarr", "10.0.0.5", 32400, "tok", use_ssl=False)
    assert plex["name"] == "Plex"
    assert plex["implementation"] == "PlexServer"
    assert plex["onDownload"] is True
    assert plex["onUpgrade"] is True
    assert plex["onRename"] is True
    assert plex["updateLibrary"] is True
    fields = {f["name"]: f["value"] for f in plex["fields"]}
    assert fields == {"host": "10.0.0.5", "port": 32400, "useSsl": False, "authToken": "tok"}
    assert "onSeriesDelete" in plex or "onMovieDelete" in plex

    emby = emby_notification("radarr", "emby", 8096, "key", use_ssl=False)
    assert emby["name"] == "Emby"
    assert emby["implementation"] == "MediaBrowser"
    assert emby["updateLibrary"] is True
    efields = {f["name"]: f["value"] for f in emby["fields"]}
    assert efields["host"] == "emby"
    assert efields["apiKey"] == "key"
    assert efields["port"] == 8096


def test_emby_config_uses_443_for_domain_ssl(monkeypatch):
    from recipes.arr import _emby_config

    monkeypatch.setenv("EMBY_API_KEY", "key")
    monkeypatch.setenv("EMBY_HOST", "emby.couttsnet.com")
    monkeypatch.setenv("EMBY_PORT", "8096")
    monkeypatch.setenv("EMBY_USE_SSL", "true")
    host, port, key, use_ssl = _emby_config(set())
    assert host == "emby.couttsnet.com"
    assert port == 443
    assert use_ssl is True


def test_plex_config_uses_443_when_ssl(monkeypatch):
    from recipes.arr import _plex_config

    monkeypatch.setenv("PLEX_HOST", "plex.couttsnet.com")
    monkeypatch.setenv("PLEX_TOKEN", "tok")
    monkeypatch.setenv("PLEX_PORT", "32400")
    monkeypatch.setenv("PLEX_USE_SSL", "true")
    host, port, token, use_ssl = _plex_config()
    assert port == 443
    assert use_ssl is True


def test_notification_failure_does_not_abort_other_connections(monkeypatch):
    import httpx
    import recipes.arr as arr

    class FakeClient:
        def __init__(self, base, key):
            pass

        def wait(self):
            return True

        def ensure(self, path, payload, match_on="name"):
            return False

        def upsert(self, path, payload, match_on="name"):
            if payload.get("name") == "Plex":
                req = httpx.Request("POST", "http://x/api/v3/notification")
                resp = httpx.Response(400, request=req, json=[{
                    "errorMessage": "Unable to connect to Plex Media Server",
                }])
                raise httpx.HTTPStatusError("bad", request=req, response=resp)
            return "updated"

        def remove_named(self, path, name, match_on="name"):
            return False

        def get(self, path):
            return {"id": 1}

        def put(self, path, payload):
            return payload

    monkeypatch.setattr(arr, "ArrClient", FakeClient)
    monkeypatch.setattr(arr, "resolve_key", lambda app: "k")
    monkeypatch.setenv("PLEX_HOST", "bad.example")
    monkeypatch.setenv("PLEX_TOKEN", "tok")
    monkeypatch.setenv("EMBY_HOST", "emby.couttsnet.com")
    monkeypatch.setenv("EMBY_PORT", "443")
    monkeypatch.setenv("EMBY_API_KEY", "key")
    monkeypatch.setenv("EMBY_USE_SSL", "true")
    logs = []
    arr.configure("radarr", {"radarr"}, logs.append)
    assert any("notification Plex failed" in m for m in logs)
    assert any("notification Emby (updated)" in m for m in logs)
    assert not any("wiring failed" in m for m in logs)


def test_emby_config_defaults_bundled_to_domain_443(monkeypatch):
    from recipes.arr import _emby_config

    monkeypatch.setenv("EMBY_API_KEY", "key")
    monkeypatch.delenv("EMBY_HOST", raising=False)
    monkeypatch.setenv("KINE_DOMAIN", "couttsnet.com")
    host, port, key, use_ssl = _emby_config({"emby"})
    assert host == "emby.couttsnet.com"
    assert port == 443
    assert use_ssl is True


def test_arr_wiring_upserts_media_server_notifications(monkeypatch):
    import recipes.arr as arr

    actions = []

    class FakeClient:
        def __init__(self, base, key):
            pass

        def wait(self):
            return True

        def ensure(self, path, payload, match_on="name"):
            return False

        def upsert(self, path, payload, match_on="name"):
            actions.append(("upsert", path, payload["name"], payload["implementation"]))
            return "updated"

        def remove_named(self, path, name, match_on="name"):
            actions.append(("remove", path, name, None))
            return False

        def get(self, path):
            return {"id": 1}

        def put(self, path, payload):
            return payload

    monkeypatch.setattr(arr, "ArrClient", FakeClient)
    monkeypatch.setattr(arr, "resolve_key", lambda app: "k")
    monkeypatch.setenv("PLEX_HOST", "plex.lan")
    monkeypatch.setenv("PLEX_PORT", "32400")
    monkeypatch.setenv("PLEX_TOKEN", "plex-tok")
    monkeypatch.setenv("PLEX_USE_SSL", "false")
    monkeypatch.setenv("EMBY_HOST", "emby.couttsnet.com")
    monkeypatch.setenv("EMBY_PORT", "443")
    monkeypatch.setenv("EMBY_API_KEY", "emby-key")
    monkeypatch.setenv("EMBY_USE_SSL", "true")
    logs = []
    arr.configure("sonarr", {"sonarr"}, logs.append)
    assert ("upsert", "notification", "Plex", "PlexServer") in actions
    assert ("upsert", "notification", "Emby", "MediaBrowser") in actions
    assert any("sonarr: notification Plex" in m for m in logs)
    assert any("sonarr: notification Emby" in m for m in logs)


def test_arr_wiring_removes_media_server_notifications_when_cleared(monkeypatch):
    import recipes.arr as arr

    actions = []

    class FakeClient:
        def __init__(self, base, key):
            pass

        def wait(self):
            return True

        def ensure(self, path, payload, match_on="name"):
            return False

        def upsert(self, path, payload, match_on="name"):
            raise AssertionError("should not upsert")

        def remove_named(self, path, name, match_on="name"):
            actions.append((path, name))
            return True

        def get(self, path):
            return {"id": 1}

        def put(self, path, payload):
            return payload

    monkeypatch.setattr(arr, "ArrClient", FakeClient)
    monkeypatch.setattr(arr, "resolve_key", lambda app: "k")
    for key in (
        "PLEX_HOST", "PLEX_TOKEN", "PLEX_PORT", "PLEX_USE_SSL",
        "EMBY_HOST", "EMBY_API_KEY", "EMBY_PORT", "EMBY_USE_SSL",
    ):
        monkeypatch.delenv(key, raising=False)
    logs = []
    arr.configure("radarr", {"radarr", "emby"}, logs.append)
    assert ("notification", "Plex") in actions
    assert ("notification", "Emby") in actions


def test_arr_client_upsert_updates_existing():
    from arrclient import ArrClient

    class FakeHTTP:
        def __init__(self):
            self.posts = []
            self.puts = []

        def get(self, url):
            class R:
                status_code = 200
                content = b"[]"

                def json(_self):
                    return [{"id": 9, "name": "Plex", "host": "old"}]

                def raise_for_status(_self):
                    pass

            return R()

        def post(self, url, json):
            self.posts.append(json)
            class R:
                status_code = 200
                content = b"{}"
                def json(_self): return {}
                def raise_for_status(_self): pass
            return R()

        def put(self, url, json):
            self.puts.append((url, json))
            class R:
                status_code = 200
                content = b"{}"
                def json(_self): return json
                def raise_for_status(_self): pass
            return R()

    c = ArrClient("http://x", "k")
    c.http = FakeHTTP()
    assert c.upsert("notification", {"name": "Plex", "host": "new"}) == "updated"
    assert c.http.posts == []
    assert len(c.http.puts) == 1
    assert c.http.puts[0][1]["id"] == 9
    assert c.http.puts[0][1]["host"] == "new"


def test_arr_client_remove_named():
    from arrclient import ArrClient

    class FakeHTTP:
        def __init__(self):
            self.deleted = []

        def get(self, url):
            class R:
                def json(_self):
                    return [{"id": 3, "name": "Emby"}]

                def raise_for_status(_self):
                    pass

            return R()

        def delete(self, url):
            self.deleted.append(url)
            class R:
                def raise_for_status(_self): pass
            return R()

    c = ArrClient("http://x", "k")
    c.http = FakeHTTP()
    assert c.remove_named("notification", "Emby") is True
    assert c.http.deleted == ["http://x/api/v3/notification/3"]
    assert c.remove_named("notification", "Missing") is False


def test_provision_compose_passes_media_server_env():
    text = (ROOT / "compose" / "core.provision.yml").read_text()
    assert "PLEX_HOST" in text
    assert "PLEX_TOKEN" in text
    assert "EMBY_HOST" in text
    assert "EMBY_API_KEY" in text


def test_provision_wire_includes_bazarr():
    text = (ROOT / "provision" / "provision.py").read_text()
    assert "bazarr.configure" in text
    assert "recipes/bazarr.py" in str(list((ROOT / "provision" / "recipes").glob("bazarr.py"))[0])


def test_env_example_documents_media_servers():
    text = (ROOT / ".env.example").read_text()
    assert "PLEX_HOST=" in text
    assert "PLEX_TOKEN=" in text
    assert "EMBY_HOST=" in text
    assert "EMBY_API_KEY=" in text


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


def test_recyclarr_seed_writes_trash_guide_config(stack, monkeypatch):
    import keys
    import seed
    from keys import api_key
    from recipes import recyclarr

    monkeypatch.setenv("KINE_SECRET", "test-secret-value")
    monkeypatch.setattr(keys, "STACK", stack)
    seed.seed_all({"recyclarr"})
    cfg = stack / "config" / "recyclarr" / "recyclarr.yml"
    secrets = stack / "config" / "recyclarr" / "secrets.yml"
    body = cfg.read_text()
    assert recyclarr.SONARR_PROFILE in body
    assert recyclarr.RADARR_PROFILE in body
    assert "127.0.0.1:8989" not in secrets.read_text()
    assert "gluetun:8989" in secrets.read_text()
    assert api_key("sonarr") in secrets.read_text()
    assert api_key("radarr") in secrets.read_text()


def test_recyclarr_seed_never_overwrites_existing_config(stack, monkeypatch):
    import keys
    import seed

    monkeypatch.setenv("KINE_SECRET", "test-secret-value")
    monkeypatch.setattr(keys, "STACK", stack)
    cfg_dir = stack / "config" / "recyclarr"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "recyclarr.yml").write_text("custom: true\n")
    seed.seed_all({"recyclarr"})
    assert (cfg_dir / "recyclarr.yml").read_text() == "custom: true\n"


def test_recyclarr_configure_uses_resolve_key(stack, monkeypatch):
    import keys
    from recipes import recyclarr

    monkeypatch.setattr(keys, "STACK", stack)
    logs = []
    monkeypatch.setattr(recyclarr, "resolve_key", lambda app: f"live-{app}")
    recyclarr.configure(logs.append)
    secrets = (stack / "config" / "recyclarr" / "secrets.yml").read_text()
    assert "live-sonarr" in secrets
    assert "live-radarr" in secrets
    assert any("recyclarr: wrote secrets.yml" in m for m in logs)


def test_seerr_servers_use_gluetun_and_kine_paths():
    from recipes.seerr import SERVERS

    assert SERVERS["sonarr"]["hostname"] == "gluetun"
    assert SERVERS["sonarr"]["port"] == 8989
    assert SERVERS["sonarr"]["directory"] == "/data/media/tv"
    assert SERVERS["sonarr"]["profiles"][0] == "WEB-1080p"
    assert SERVERS["radarr"]["hostname"] == "gluetun"
    assert SERVERS["radarr"]["port"] == 7878
    assert SERVERS["radarr"]["directory"] == "/data/media/movies"
    assert SERVERS["radarr"]["profiles"][0] == "HD Bluray + WEB"


def test_seerr_picks_1080p_profiles_preferring_trash_names():
    from recipes.seerr import SERVERS, _pick_profile

    profiles = [
        {"id": 1, "name": "Any"},
        {"id": 4, "name": "HD-1080p"},
        {"id": 7, "name": "WEB-1080p"},
        {"id": 8, "name": "HD Bluray + WEB"},
    ]
    assert (
        _pick_profile(profiles, SERVERS["sonarr"]["profiles"])["name"] == "WEB-1080p"
    )
    assert (
        _pick_profile(profiles, SERVERS["radarr"]["profiles"])["name"]
        == "HD Bluray + WEB"
    )
    # Recyclarr name wins when both Recyclarr and stock HD-1080p exist
    both = [
        {"id": 4, "name": "HD-1080p"},
        {"id": 7, "name": "WEB-1080p"},
    ]
    assert _pick_profile(both, SERVERS["sonarr"]["profiles"])["id"] == 7
    # Alias without space around +
    assert (
        _pick_profile(
            [{"id": 9, "name": "HD Bluray+WEB"}, {"id": 4, "name": "HD-1080p"}],
            SERVERS["radarr"]["profiles"],
        )["id"]
        == 9
    )
    # Fallback when Recyclarr profile is absent
    assert (
        _pick_profile(
            [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}],
            SERVERS["radarr"]["profiles"],
        )["name"]
        == "HD-1080p"
    )


def test_seerr_already_linked_matches_non_4k_gluetun():
    from recipes.seerr import _already_linked, _find_linked

    existing = [
        {"hostname": "gluetun", "port": 7878, "is4k": False, "id": 0},
        {"hostname": "gluetun", "port": 7878, "is4k": True, "id": 1},
    ]
    assert _already_linked(existing, "gluetun", 7878) is True
    assert _find_linked(existing, "gluetun", 7878)["id"] == 0
    assert _already_linked([], "gluetun", 7878) is False


def test_seerr_configure_posts_radarr_and_sonarr(stack, monkeypatch):
    import keys
    from recipes import seerr

    monkeypatch.setattr(keys, "STACK", stack)
    monkeypatch.setenv("KINE_DOMAIN", "example.test")
    cfg = stack / "config" / "seerr"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(
        json.dumps({"main": {"apiKey": "seerr-test-key"}})
    )

    posted = []
    putted = []
    existing = {"radarr": [], "sonarr": []}

    class FakeResp:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload if payload is not None else {}

        def json(self):
            return self._payload

    def _test_payload(app):
        directory = "/data/media/movies" if app == "radarr" else "/data/media/tv"
        recyclarr = "HD Bluray + WEB" if app == "radarr" else "WEB-1080p"
        return {
            "profiles": [
                {"id": 1, "name": "Any"},
                {"id": 4, "name": "HD-1080p"},
                {"id": 7, "name": recyclarr},
            ],
            "rootFolders": [{"id": 1, "path": directory}],
            "languageProfiles": None,
            "urlBase": "",
        }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            assert kwargs["headers"]["X-Api-Key"] == "seerr-test-key"

        def get(self, path):
            if path == "/api/v1/settings/public":
                return FakeResp(200, {"initialized": False})
            if path == "/api/v1/auth/me":
                return FakeResp(200, {"id": 1})
            if path.endswith("/settings/radarr"):
                return FakeResp(200, existing["radarr"])
            if path.endswith("/settings/sonarr"):
                return FakeResp(200, existing["sonarr"])
            raise AssertionError(path)

        def post(self, path, json=None):
            if path.endswith("/test"):
                app = "radarr" if "radarr" in path else "sonarr"
                return FakeResp(200, _test_payload(app))
            posted.append((path, json))
            app = "radarr" if "radarr" in path else "sonarr"
            existing[app].append({**json, "id": 0})
            return FakeResp(201, {**json, "id": 0})

        def put(self, path, json=None):
            assert "id" not in json
            putted.append((path, json))
            app = "radarr" if "radarr" in path else "sonarr"
            server_id = int(path.rstrip("/").split("/")[-1])
            existing[app] = [{**json, "id": server_id}]
            return FakeResp(200, {**json, "id": server_id})

    monkeypatch.setattr(seerr.httpx, "Client", FakeClient)
    monkeypatch.setattr(seerr, "resolve_key", lambda app: f"key-{app}")
    monkeypatch.setattr(seerr, "_wait", lambda http, timeout=300: True)

    logs = []
    seerr.configure({"seerr", "sonarr", "radarr"}, logs.append)

    assert any(p[0].endswith("/settings/radarr") for p in posted)
    assert any(p[0].endswith("/settings/sonarr") for p in posted)
    radarr = next(p[1] for p in posted if p[0].endswith("/settings/radarr"))
    sonarr = next(p[1] for p in posted if p[0].endswith("/settings/sonarr"))
    assert radarr["hostname"] == "gluetun"
    assert radarr["port"] == 7878
    assert radarr["apiKey"] == "key-radarr"
    assert radarr["activeDirectory"] == "/data/media/movies"
    assert radarr["isDefault"] is True
    assert radarr["is4k"] is False
    assert radarr["activeProfileId"] == 7
    assert radarr["activeProfileName"] == "HD Bluray + WEB"
    assert sonarr["hostname"] == "gluetun"
    assert sonarr["port"] == 8989
    assert sonarr["apiKey"] == "key-sonarr"
    assert sonarr["activeDirectory"] == "/data/media/tv"
    assert sonarr["enableSeasonFolders"] is True
    assert sonarr["activeProfileId"] == 7
    assert sonarr["activeProfileName"] == "WEB-1080p"
    assert any("seerr: linked Radarr" in m for m in logs)
    assert any("seerr: linked Sonarr" in m for m in logs)

    # Idempotent when profiles already match
    logs2 = []
    seerr.configure({"seerr", "sonarr", "radarr"}, logs2.append)
    assert putted == []
    assert not any("linked" in m or "updated" in m for m in logs2)


def test_seerr_configure_updates_wrong_profile(stack, monkeypatch):
    import keys
    from recipes import seerr

    monkeypatch.setattr(keys, "STACK", stack)
    cfg = stack / "config" / "seerr"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(
        json.dumps({"main": {"apiKey": "seerr-test-key"}})
    )

    existing = {
        "radarr": [
            {
                "id": 0,
                "hostname": "gluetun",
                "port": 7878,
                "is4k": False,
                "activeProfileId": 4,
                "activeProfileName": "HD-1080p",
            }
        ],
        "sonarr": [
            {
                "id": 0,
                "hostname": "gluetun",
                "port": 8989,
                "is4k": False,
                "activeProfileId": 4,
                "activeProfileName": "HD-1080p",
            }
        ],
    }
    putted = []

    class FakeResp:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload if payload is not None else {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            if path == "/api/v1/auth/me":
                return FakeResp(200, {"id": 1})
            if path.endswith("/settings/radarr"):
                return FakeResp(200, existing["radarr"])
            if path.endswith("/settings/sonarr"):
                return FakeResp(200, existing["sonarr"])
            return FakeResp(200, {})

        def post(self, path, json=None):
            assert path.endswith("/test")
            app = "radarr" if "radarr" in path else "sonarr"
            directory = "/data/media/movies" if app == "radarr" else "/data/media/tv"
            recyclarr = "HD Bluray + WEB" if app == "radarr" else "WEB-1080p"
            return FakeResp(
                200,
                {
                    "profiles": [
                        {"id": 4, "name": "HD-1080p"},
                        {"id": 7, "name": recyclarr},
                    ],
                    "rootFolders": [{"id": 1, "path": directory}],
                    "urlBase": "",
                },
            )

        def put(self, path, json=None):
            assert "id" not in json
            putted.append((path, json))
            return FakeResp(200, json)

    monkeypatch.setattr(seerr.httpx, "Client", FakeClient)
    monkeypatch.setattr(seerr, "resolve_key", lambda app: f"key-{app}")
    monkeypatch.setattr(seerr, "_wait", lambda http, timeout=300: True)

    logs = []
    seerr.configure({"seerr", "sonarr", "radarr"}, logs.append)

    assert len(putted) == 2
    by_app = {("radarr" if "radarr" in p else "sonarr"): b for p, b in putted}
    assert by_app["radarr"]["activeProfileName"] == "HD Bluray + WEB"
    assert by_app["radarr"]["activeProfileId"] == 7
    assert by_app["sonarr"]["activeProfileName"] == "WEB-1080p"
    assert by_app["sonarr"]["activeProfileId"] == 7
    assert any("updated Radarr profile to HD Bluray + WEB" in m for m in logs)
    assert any("updated Sonarr profile to WEB-1080p" in m for m in logs)


def test_seerr_configure_skips_until_admin_exists(stack, monkeypatch):
    import keys
    from recipes import seerr

    monkeypatch.setattr(keys, "STACK", stack)
    cfg = stack / "config" / "seerr"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(
        json.dumps({"main": {"apiKey": "seerr-test-key"}})
    )

    class FakeResp:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            if path == "/api/v1/auth/me":
                return FakeResp(403, {"error": "denied"})
            return FakeResp(200, {})

        def post(self, path, json=None):
            raise AssertionError("should not post services before admin")

    monkeypatch.setattr(seerr.httpx, "Client", FakeClient)
    monkeypatch.setattr(seerr, "_wait", lambda http, timeout=300: True)
    logs = []
    seerr.configure({"seerr", "sonarr"}, logs.append)
    assert any("admin user not ready" in m for m in logs)


# ── metrics seeding ─────────────────────────────────────────────
def test_metrics_seed_writes_scrape_targets(tmp_path):
    import yaml
    from recipes import metrics as metrics_recipe
    metrics_recipe.seed(tmp_path, {"grafana", "prometheus"}, log=lambda *_: None)
    cfg = yaml.safe_load((tmp_path / "config" / "prometheus" / "prometheus.yml").read_text())
    jobs = {j["job_name"]: j for j in cfg["scrape_configs"]}
    assert "cadvisor:8080" in jobs["cadvisor"]["static_configs"][0]["targets"]
    assert "node-exporter:9100" in jobs["node"]["static_configs"][0]["targets"]
    assert "helm:8600" in jobs["kine"]["static_configs"][0]["targets"]
    assert jobs["kine"]["metrics_path"] == "/api/metrics"


def test_metrics_seed_pins_the_datasource_uid(tmp_path):
    import yaml
    from recipes import metrics as metrics_recipe
    metrics_recipe.seed(tmp_path, {"grafana"}, log=lambda *_: None)
    ds = yaml.safe_load(
        (tmp_path / "config" / "grafana" / "provisioning" / "datasources"
         / "prometheus.yml").read_text()
    )
    assert ds["datasources"][0]["uid"] == "kine-prom"
    assert ds["datasources"][0]["url"] == "http://prometheus:9090"


def test_metrics_seed_copies_dashboards(tmp_path):
    from recipes import metrics as metrics_recipe
    metrics_recipe.seed(tmp_path, {"grafana"}, log=lambda *_: None)
    copied = {p.name for p in (tmp_path / "config" / "grafana" / "dashboards").glob("*.json")}
    assert "kine-overview.json" in copied


def test_metrics_seed_is_idempotent(tmp_path):
    from recipes import metrics as metrics_recipe
    metrics_recipe.seed(tmp_path, {"grafana"}, log=lambda *_: None)
    first = (tmp_path / "config" / "prometheus" / "prometheus.yml").read_text()
    metrics_recipe.seed(tmp_path, {"grafana"}, log=lambda *_: None)
    assert (tmp_path / "config" / "prometheus" / "prometheus.yml").read_text() == first


def test_metrics_seed_does_nothing_when_the_tier_is_off(tmp_path):
    from recipes import metrics as metrics_recipe
    metrics_recipe.seed(tmp_path, {"sonarr"}, log=lambda *_: None)
    assert not (tmp_path / "config" / "prometheus").exists()
