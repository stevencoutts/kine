"""NZBGet news servers and default extension seeding."""
from __future__ import annotations

import io
import json
import pathlib
import sys
import zipfile
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))

from recipes import nzbget  # noqa: E402


def _fake_extension_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/manifest.json", json.dumps({"name": name}))
        zf.writestr(f"{name}/main.py", "print('ok')\n")
    return buf.getvalue()


def test_parse_servers_normalises_and_skips_blank_hosts():
    raw = json.dumps([
        {"host": "news.provider.example", "username": "u", "password": "p"},
        {"host": "  ", "name": "empty"},
        {"host": "my.newsserver.com", "name": "placeholder"},
        {"name": "Plain", "host": "news2.example.com", "encryption": False, "port": "119"},
    ])
    servers = nzbget.parse_servers(raw)
    assert len(servers) == 2
    assert servers[0]["host"] == "news.provider.example"
    assert servers[0]["port"] == 563
    assert servers[0]["encryption"] is True
    assert servers[1]["host"] == "news2.example.com"
    assert servers[1]["port"] == 119
    assert servers[1]["encryption"] is False


def test_apply_runtime_defaults_sets_password_and_paths(tmp_path: Path):
    conf = tmp_path / "nzbget.conf"
    conf.write_text(
        "MainDir=/config\n"
        "DestDir=/downloads/completed\n"
        "InterDir=/downloads/intermediate\n"
        "ControlUsername=nzbget\n"
        "ControlPassword=tegbzn6789\n"
        "WriteBuffer=0\n"
        "ArticleCache=0\n"
        "WriteLog=append\n"
    )
    nzbget.apply_runtime_defaults(conf)
    text = conf.read_text()
    assert "DestDir=/data/downloads/complete" in text
    assert "InterDir=/data/downloads/incomplete" in text
    assert "ControlUsername=nzbget" in text
    assert "ControlPassword=nzbget" in text
    assert "WriteBuffer=1024" in text
    assert "ArticleCache=500" in text
    assert "WriteLog=reset" in text
    assert "tegbzn6789" not in text
    assert "WriteLog=append" not in text


def test_apply_categories_matches_arr_clients(tmp_path: Path):
    conf = tmp_path / "nzbget.conf"
    conf.write_text(
        "MainDir=/config\n"
        "Category1.Name=Movies\n"
        "Category2.Name=Series\n"
    )
    nzbget.apply_categories(conf)
    text = conf.read_text()
    assert "Category1.Name=tv-sonarr" in text
    assert "Category1.DestDir=/data/downloads/complete/tv-sonarr" in text
    assert "Category2.Name=radarr" in text
    assert "Movies" not in text
    assert "Series" not in text


def test_apply_servers_writes_contiguous_blocks(tmp_path: Path):
    conf = tmp_path / "nzbget.conf"
    conf.write_text("MainDir=/config\nServer9.Host=stale.example\nOther=1\n")
    nzbget.apply_servers(conf, [
        {"host": "a.example", "name": "A", "username": "ua", "password": "pa"},
        {"host": "b.example", "encryption": False},
    ])
    text = conf.read_text()
    assert "Server9.Host" not in text
    assert "Server1.Host=a.example" in text
    assert "Server1.Name=A" in text
    assert "Server2.Host=b.example" in text
    assert "Server2.Encryption=no" in text
    assert "Other=1" in text


def test_apply_extensions_enables_three_defaults(tmp_path: Path):
    conf = tmp_path / "nzbget.conf"
    conf.write_text("MainDir=/config\nExtensions=\n")
    nzbget.apply_extensions(conf)
    text = conf.read_text()
    assert "Extensions=ExtendedUnpacker, FakeDetector, RemoveSamples" in text
    assert "ScriptDir=${MainDir}/scripts" in text
    assert "RemoveSamples:TestMode=No" in text
    assert "FakeDetector:BannedExtensions=" in text


def test_install_extensions_downloads_missing(tmp_path: Path, monkeypatch):
    scripts = tmp_path / "scripts"
    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        if "ExtendedUnpacker" in url or "extendedunpacker" in url:
            return _fake_extension_zip("ExtendedUnpacker")
        if "FakeDetector" in url or "fakedetector" in url:
            return _fake_extension_zip("FakeDetector")
        return _fake_extension_zip("RemoveSamples")

    monkeypatch.setattr(nzbget, "_download_zip", fake_download)
    installed = nzbget.install_extensions(scripts, log=lambda *_: None)
    assert installed == ["ExtendedUnpacker", "FakeDetector", "RemoveSamples"]
    assert len(calls) == 3
    for name in installed:
        assert (scripts / name / "manifest.json").is_file()
        assert (scripts / name / "main.py").is_file()

    # Second pass should not re-download.
    calls.clear()
    again = nzbget.install_extensions(scripts, log=lambda *_: None)
    assert again == installed
    assert calls == []


def test_seed_skips_when_nzbget_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(nzbget, "install_extensions", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no install")))
    nzbget.seed(tmp_path, enabled=set(), log=lambda *_: None)
    assert not (tmp_path / "config" / "nzbget").exists()


def test_seed_installs_extensions_before_conf_exists(tmp_path: Path, monkeypatch):
    logs = []
    monkeypatch.setattr(
        nzbget,
        "install_extensions",
        lambda scripts, log=print: ["ExtendedUnpacker", "FakeDetector", "RemoveSamples"],
    )
    monkeypatch.setenv("NZBGET_NEWS_SERVERS", "")
    nzbget.seed(tmp_path, enabled={"nzbget"}, log=logs.append)
    assert (tmp_path / "config" / "nzbget" / "scripts").is_dir()
    assert any("extensions ready" in line for line in logs)
    assert not (tmp_path / "config" / "nzbget" / "nzbget.conf").exists()


def test_serialize_roundtrip():
    servers = [
        {"host": "news.provider.example", "name": "Primary", "port": 563,
         "username": "u", "password": "p", "encryption": True, "connections": 12},
    ]
    assert nzbget.parse_servers(nzbget.serialize_servers(servers)) == servers
