"""Parse Plex and Emby now-playing payloads without hitting a server."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app.watching import (  # noqa: E402
    media_base,
    parse_emby_sessions,
    parse_plex_sessions,
    progress_pct,
)


PLEX_SAMPLE = {
    "MediaContainer": {
        "size": 2,
        "Metadata": [
            {
                "type": "episode",
                "title": "Pilot",
                "grandparentTitle": "Severance",
                "parentIndex": 1,
                "index": 1,
                "year": 2022,
                "duration": 3600000,
                "viewOffset": 900000,
                "User": {"title": "steve"},
                "Player": {"title": "Living Room", "state": "playing"},
            },
            {
                "type": "movie",
                "title": "Dune",
                "year": 2021,
                "duration": 9300000,
                "viewOffset": 120000,
                "User": {"title": "kate"},
                "Player": {"title": "Plex for Apple TV", "state": "paused"},
            },
        ],
    }
}

EMBY_SAMPLE = [
    {
        "UserName": "admin",
        "Client": "Emby for iOS",
        "DeviceName": "iPhone",
        "NowPlayingItem": {
            "Name": "The One Where Monica Gets a Roommate",
            "Type": "Episode",
            "SeriesName": "Friends",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
            "ProductionYear": 1994,
            "RunTimeTicks": 1_320_000_000_000,
        },
        "PlayState": {"PositionTicks": 330_000_000_000, "IsPaused": False},
    },
    {
        "UserName": "idle",
        "Client": "Emby Web",
        "NowPlayingItem": None,
    },
]


def test_media_base_https_and_http():
    assert media_base("emby.example.com", "443", True) == "https://emby.example.com:443"
    assert media_base("10.0.0.8", 32400, False) == "http://10.0.0.8:32400"


def test_parse_plex_sessions_episode_and_movie():
    rows = parse_plex_sessions(PLEX_SAMPLE)
    assert len(rows) == 2
    assert rows[0]["server"] == "plex"
    assert rows[0]["title"] == "Severance — S01E01 Pilot"
    assert rows[0]["user"] == "steve"
    assert rows[0]["player"] == "Living Room"
    assert rows[0]["state"] == "playing"
    assert rows[0]["progress"] == 25
    assert rows[1]["title"] == "Dune (2021)"
    assert rows[1]["state"] == "paused"


def test_parse_plex_empty_and_single_item():
    assert parse_plex_sessions({}) == []
    single = {
        "MediaContainer": {
            "Metadata": {
                "type": "movie",
                "title": "Heat",
                "User": {"title": "a"},
                "Player": {"title": "TV", "state": "playing"},
            }
        }
    }
    assert parse_plex_sessions(single)[0]["title"] == "Heat"


def test_parse_emby_skips_idle_sessions():
    rows = parse_emby_sessions(EMBY_SAMPLE)
    assert len(rows) == 1
    assert rows[0]["server"] == "emby"
    assert rows[0]["title"] == "Friends — S01E01 The One Where Monica Gets a Roommate"
    assert rows[0]["user"] == "admin"
    assert rows[0]["player"] == "iPhone"
    assert rows[0]["state"] == "playing"
    assert rows[0]["progress"] == 25


def test_progress_pct_guards():
    assert progress_pct(0, 0) is None
    assert progress_pct(50, 100) == 50
    assert progress_pct(150, 100) == 100
