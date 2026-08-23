"""Library rescan triggers after NFS mount changes."""
import pathlib
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import library_rescan  # noqa: E402


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("KINE_SECRET", "test-secret-value")
    monkeypatch.setenv("KINE_ROOT", str(tmp_path))
    monkeypatch.setattr(library_rescan, "STACK", tmp_path)
    return tmp_path


def _seed_arr_key(stack: pathlib.Path, app: str, key: str) -> None:
    cfg_dir = stack / "config" / app
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.xml").write_text(
        f'<?xml version="1.0"?><Config><ApiKey>{key}</ApiKey></Config>'
    )


def test_after_nfs_mount_skips_downloads_only_change(monkeypatch):
    monkeypatch.setattr(library_rescan.config, "profiles", lambda: ["sonarr"])
    result = library_rescan.after_nfs_mount({"NFS_DOWNLOADS", "NFS_CACHE"})
    assert result["skipped"] is True
    assert result["results"] == []


def test_after_nfs_mount_triggers_enabled_apps(monkeypatch, stack):
    _seed_arr_key(stack, "sonarr", "sonarr-key")
    _seed_arr_key(stack, "radarr", "radarr-key")
    monkeypatch.setattr(
        library_rescan.config,
        "profiles",
        lambda: ["sonarr", "radarr", "gluetun"],
    )
    monkeypatch.setattr(
        library_rescan.catalogue,
        "load",
        lambda: {
            "sonarr": {"internal": "http://gluetun:8989", "api": "v3"},
            "radarr": {"internal": "http://gluetun:7878", "api": "v3"},
        },
    )
    calls: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        class Response:
            status_code = 201
            text = ""
        return Response()

    monkeypatch.setattr(library_rescan.httpx, "post", fake_post)
    result = library_rescan.after_nfs_mount({"NFS_MEDIA"})
    assert result["ok"] is True
    assert {item["app"] for item in result["results"]} == {"sonarr", "radarr"}
    assert calls[0][0].endswith("/api/v3/command")
    assert calls[0][1]["json"]["name"] == "RescanSeries"
    assert calls[1][1]["json"]["name"] == "RescanMovie"


def test_arr_key_prefers_existing_config(stack):
    _seed_arr_key(stack, "sonarr", "live-key")
    assert library_rescan._arr_key("sonarr") == "live-key"
