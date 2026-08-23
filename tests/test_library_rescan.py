"""Library import and rescan triggers after NFS mount changes."""
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

    def fake_get(url, **kwargs):
        class Response:
            status_code = 200

            def json(self):
                if url.endswith("/rootfolder"):
                    if "8989" in url:
                        return [{"path": "/data/media/tv", "unmappedFolders": []}]
                    return [{"path": "/data/media/movies", "unmappedFolders": []}]
                if url.endswith("/qualityprofile"):
                    return [{"id": 1, "name": "WEB-1080p"}]
                return []

        return Response()

    posts: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))

        class Response:
            status_code = 201
            text = ""
            content = b"[]"

            def json(self):
                return []

        return Response()

    monkeypatch.setattr(library_rescan.httpx, "get", fake_get)
    monkeypatch.setattr(library_rescan.httpx, "post", fake_post)
    result = library_rescan.after_nfs_mount({"NFS_MEDIA"})
    assert result["ok"] is True
    assert {item["app"] for item in result["results"]} == {"sonarr", "radarr"}
    command_posts = [
        call for call in posts if call[0].endswith("/api/v3/command")
    ]
    assert len(command_posts) == 2
    assert command_posts[0][1]["json"]["name"] == "RescanSeries"
    assert command_posts[1][1]["json"]["name"] == "RescanMovie"
    assert "nothing to import" in result["results"][0]["message"]
    assert "nothing to import" in result["results"][1]["message"]


def test_import_arr_library_posts_unmapped_movies(monkeypatch):
    lookups: list[str] = []

    def fake_get(url, **kwargs):
        class Response:
            status_code = 200

            def json(self):
                params = kwargs.get("params") or {}
                if url.endswith("/rootfolder"):
                    return [
                        {
                            "path": "/data/media/movies",
                            "unmappedFolders": [
                                {
                                    "name": "The Matrix (1999)",
                                    "path": "/data/media/movies/The Matrix (1999)",
                                }
                            ],
                        }
                    ]
                if url.endswith("/qualityprofile"):
                    return [{"id": 4, "name": "HD Bluray + WEB"}]
                if url.endswith("/lookup"):
                    lookups.append(params["term"])
                    return [
                        {
                            "title": "The Matrix",
                            "year": 1999,
                            "tmdbId": 603,
                            "titleSlug": "603",
                        }
                    ]
                if url.endswith("/movie"):
                    return []
                return []

        return Response()

    posts: list[dict] = []

    def fake_post(url, **kwargs):
        posts.append(kwargs)

        class Response:
            status_code = 201
            content = b"[{}]"
            text = "[{}]"

            def json(self):
                return [{}]

        return Response()

    monkeypatch.setattr(library_rescan.httpx, "get", fake_get)
    monkeypatch.setattr(library_rescan.httpx, "post", fake_post)
    ok, message = library_rescan._import_arr_library(
        "radarr",
        "http://gluetun:7878",
        "v3",
        "radarr-key",
    )
    assert ok is True
    assert message == "imported 1"
    assert lookups == ["The Matrix (1999)"]
    assert posts[0]["json"][0]["path"] == "/data/media/movies/The Matrix (1999)"
    assert posts[0]["json"][0]["rootFolderPath"] == "/data/media/movies"
    assert posts[0]["json"][0]["qualityProfileId"] == 4


def test_sonarr_unmapped_expands_series_folder(monkeypatch):
    def fake_get(url, **kwargs):
        class Response:
            status_code = 200

            def json(self):
                params = kwargs.get("params") or {}
                if url.endswith("/rootfolder"):
                    return [
                        {
                            "path": "/data/media/tv",
                            "unmappedFolders": [
                                {
                                    "name": "Series",
                                    "path": "/data/media/tv/Series",
                                }
                            ],
                        }
                    ]
                if url.endswith("/series"):
                    return []
                if url.endswith("/filesystem"):
                    assert params["path"] == "/data/media/tv/Series/"
                    return {
                        "parent": "/data/media/tv/",
                        "directories": [
                            {
                                "type": "folder",
                                "path": "/data/media/tv/Series/Breaking Bad/",
                                "name": "Breaking Bad",
                            }
                        ],
                    }
                return []

        return Response()

    monkeypatch.setattr(library_rescan.httpx, "get", fake_get)
    unmapped = library_rescan._sonarr_unmapped(
        "http://gluetun:8989",
        "v3",
        "sonarr-key",
        "/data/media/tv",
    )
    assert unmapped == [
        {
            "name": "Breaking Bad",
            "path": "/data/media/tv/Series/Breaking Bad",
        }
    ]


def test_arr_key_prefers_existing_config(stack):
    _seed_arr_key(stack, "sonarr", "live-key")
    assert library_rescan._arr_key("sonarr") == "live-key"
