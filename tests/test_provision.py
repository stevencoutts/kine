"""Provisioner behaviour that the appliance's pre-wiring depends on."""
import pathlib
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("MC_SECRET", "test-secret-value")
    monkeypatch.setattr("seed.STACK", tmp_path, raising=False)
    import seed
    monkeypatch.setattr(seed, "STACK", tmp_path)
    return tmp_path


def test_keys_are_deterministic_and_distinct(monkeypatch):
    monkeypatch.setenv("MC_SECRET", "abc")
    from keys import api_key
    assert api_key("sonarr") == api_key("sonarr")
    assert api_key("sonarr") != api_key("radarr")
    assert len(api_key("sonarr")) == 32


def test_keys_change_with_the_secret(monkeypatch):
    from keys import api_key
    monkeypatch.setenv("MC_SECRET", "one")
    first = api_key("sonarr")
    monkeypatch.setenv("MC_SECRET", "two")
    assert api_key("sonarr") != first


def test_empty_secret_is_refused_rather_than_hashed(monkeypatch):
    monkeypatch.setenv("MC_SECRET", "")
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
    idempotent, `./mc enable <app>` would duplicate download clients
    each time it ran."""
    from arrclient import ArrClient
    c = ArrClient("http://x", "k")
    c.http = _FakeHTTP(existing=[{"name": "Transmission"}])
    assert c.ensure("downloadclient", {"name": "Transmission"}) is False
    assert c.http.posted == []
    assert c.ensure("downloadclient", {"name": "NZBGet"}) is True
    assert len(c.http.posted) == 1
