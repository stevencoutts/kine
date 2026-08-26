"""Dispatcharr Emby tuner + token env helpers."""
import pathlib
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))

from recipes.dispatcharr import (  # noqa: E402
    dispatcharr_hdhr,
    tuner_already_linked,
    tuner_host_payload,
)


def test_tuner_host_payload_shape():
    body = tuner_host_payload()
    assert body["Type"] == "hdhomerun"
    assert body["Url"] == dispatcharr_hdhr()
    assert body["FriendlyName"] == "Dispatcharr"
    assert body["ImportFavoritesOnly"] is False


def test_tuner_already_linked_matches_url():
    assert tuner_already_linked([
        {"Url": "http://other:5004", "Type": "hdhomerun"},
    ]) is False
    assert tuner_already_linked([
        {"Url": dispatcharr_hdhr(), "Type": "hdhomerun"},
    ]) is True
    assert tuner_already_linked([
        {"Url": dispatcharr_hdhr() + "/", "Type": "hdhomerun"},
    ]) is True


def test_write_dispatcharr_token_sets_url_and_token(tmp_path, monkeypatch):
    from recipes import envfiles
    monkeypatch.setattr(envfiles, "STACK", tmp_path)
    changed = envfiles.write_dispatcharr_token("ecm", "abc-token", lambda m: None)
    assert changed is True
    text = (tmp_path / "config" / "ecm" / "ecm.env").read_text()
    assert "DISPATCHARR_URL=http://127.0.0.1:9191" in text
    assert "DISPATCHARR_TOKEN=abc-token" in text
    changed2 = envfiles.write_dispatcharr_token("ecm", "abc-token", lambda m: None)
    assert changed2 is False


def test_write_dispatcharr_token_preserves_extra_keys(tmp_path, monkeypatch):
    from recipes import envfiles
    monkeypatch.setattr(envfiles, "STACK", tmp_path)
    d = tmp_path / "config" / "ecm"
    d.mkdir(parents=True)
    (d / "ecm.env").write_text("OTHER=1\nDISPATCHARR_TOKEN=\n")
    envfiles.write_dispatcharr_token("ecm", "tok", lambda m: None)
    text = (d / "ecm.env").read_text()
    assert "OTHER=1" in text
    assert "DISPATCHARR_TOKEN=tok" in text


def test_write_dispatcharr_token_seeds_ecm_settings_json(tmp_path, monkeypatch):
    import json
    from recipes import envfiles
    monkeypatch.setattr(envfiles, "STACK", tmp_path)
    d = tmp_path / "config" / "ecm"
    d.mkdir(parents=True)
    (d / "settings.json").write_text(json.dumps({
        "url": "",
        "auth_method": "password",
        "theme": "dark",
        "dispatcharr_api_key": "",
    }))
    changed = envfiles.write_dispatcharr_token("ecm", "abc-token", lambda m: None)
    assert changed is True
    data = json.loads((d / "settings.json").read_text())
    assert data["url"] == "http://127.0.0.1:9191"
    assert data["auth_method"] == "api_key"
    assert data["dispatcharr_api_key"] == "abc-token"
    assert data["api_key"] == "abc-token"
    assert data["theme"] == "dark"
    assert envfiles.write_dispatcharr_token("ecm", "abc-token", lambda m: None) is False


def test_write_ecm_settings_skips_empty_token(tmp_path, monkeypatch):
    import json
    from recipes import envfiles
    monkeypatch.setattr(envfiles, "STACK", tmp_path)
    d = tmp_path / "config" / "ecm"
    d.mkdir(parents=True)
    (d / "settings.json").write_text(json.dumps({
        "url": "http://127.0.0.1:9191",
        "auth_method": "api_key",
        "dispatcharr_api_key": "keep-me",
    }))
    assert envfiles.write_ecm_dispatcharr_settings("", lambda m: None) is False
    data = json.loads((d / "settings.json").read_text())
    assert data["dispatcharr_api_key"] == "keep-me"


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x" if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=httpx.Request("GET", "http://x"), response=httpx.Response(self.status_code)
            )

    def json(self):
        return self._payload


class _FakeEmby:
    def __init__(self):
        self.hosts = []
        self.posts = []

    def get(self, path):
        assert path == "/LiveTv/TunerHosts"
        return _FakeResp(200, list(self.hosts))

    def post(self, path, json=None):
        assert path == "/LiveTv/TunerHosts"
        self.posts.append(json)
        self.hosts.append(json)
        return _FakeResp(200, json)

    def close(self):
        pass


def test_configure_links_emby_once(tmp_path, monkeypatch):
    from recipes import dispatcharr, envfiles

    monkeypatch.setattr(envfiles, "STACK", tmp_path)
    monkeypatch.setenv("EMBY_API_KEY", "emby-key")
    monkeypatch.setenv("DISPATCHARR_TOKEN", "disp-tok")
    fake = _FakeEmby()
    logs = []
    enabled = {"dispatcharr", "emby", "ecm"}
    r1 = dispatcharr.configure(enabled, "disp-tok", logs.append, emby_client=fake)
    assert r1["emby_linked"] is True
    assert fake.posts == [dispatcharr.tuner_host_payload()]
    assert "ecm" in r1["env_changed"]
    r2 = dispatcharr.configure(enabled, "disp-tok", logs.append, emby_client=fake)
    assert r2["emby_linked"] is True
    assert len(fake.posts) == 1
    assert r2["env_changed"] == []


def test_configure_skips_without_token(tmp_path, monkeypatch):
    from recipes import dispatcharr, envfiles

    monkeypatch.setattr(envfiles, "STACK", tmp_path)
    monkeypatch.delenv("DISPATCHARR_TOKEN", raising=False)
    logs = []
    r = dispatcharr.configure({"dispatcharr", "ecm"}, None, logs.append)
    assert r["env_changed"] == []
    assert any("no API token" in m for m in logs)
