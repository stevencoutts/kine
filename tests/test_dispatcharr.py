"""Dispatcharr Emby tuner + token env helpers."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))

from recipes.dispatcharr import (  # noqa: E402
    DISPATCHARR_HDHR,
    tuner_already_linked,
    tuner_host_payload,
)


def test_tuner_host_payload_shape():
    body = tuner_host_payload()
    assert body["Type"] == "hdhomerun"
    assert body["Url"] == DISPATCHARR_HDHR
    assert body["FriendlyName"] == "Dispatcharr"
    assert body["ImportFavoritesOnly"] is False


def test_tuner_already_linked_matches_url():
    assert tuner_already_linked([
        {"Url": "http://other:5004", "Type": "hdhomerun"},
    ]) is False
    assert tuner_already_linked([
        {"Url": DISPATCHARR_HDHR, "Type": "hdhomerun"},
    ]) is True
    assert tuner_already_linked([
        {"Url": DISPATCHARR_HDHR + "/", "Type": "hdhomerun"},
    ]) is True


def test_write_dispatcharr_token_sets_url_and_token(tmp_path, monkeypatch):
    from recipes import envfiles
    monkeypatch.setattr(envfiles, "STACK", tmp_path)
    changed = envfiles.write_dispatcharr_token("ecm", "abc-token", lambda m: None)
    assert changed is True
    text = (tmp_path / "config" / "ecm" / "ecm.env").read_text()
    assert "DISPATCHARR_URL=http://dispatcharr:9191" in text
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
