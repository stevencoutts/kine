"""Parse Plex and Emby now-playing payloads without hitting a server."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app.watching import (  # noqa: E402
    art_proxy_path,
    format_duration_ms,
    format_duration_ticks,
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
                "title": "Trust Fall",
                "grandparentTitle": "Lanterns",
                "parentIndex": 1,
                "index": 2,
                "year": 2025,
                "duration": 3600000,
                "viewOffset": 900000,
                "librarySectionTitle": "TV",
                "thumb": "/library/metadata/200/thumb/1",
                "grandparentThumb": "/library/metadata/100/thumb/2",
                "User": {"title": "steve"},
                "Player": {"title": "Living Room", "product": "Plex for Apple TV", "platform": "tvOS", "state": "playing"},
                "Media": [{
                    "videoResolution": "1080",
                    "videoCodec": "hevc",
                    "audioCodec": "aac",
                    "bitrate": 8500,
                    "Part": [{"file": "/tv/Lanterns/S01E02.mkv"}],
                }],
            },
            {
                "type": "movie",
                "title": "Dune",
                "year": 2021,
                "duration": 9300000,
                "viewOffset": 120000,
                "thumb": "/library/metadata/300/thumb/3",
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
            "Id": "ep-1",
            "Name": "The One Where Monica Gets a Roommate",
            "Type": "Episode",
            "SeriesName": "Friends",
            "SeriesId": "series-1",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
            "ProductionYear": 1994,
            "RunTimeTicks": 13_200_000_000,
            "Path": "/tv/Friends/S01E01.mkv",
            "Height": 1080,
            "ImageTags": {"Primary": "ep-tag"},
            "SeriesPrimaryImageTag": "series-tag",
            "MediaStreams": [
                {"Type": "Video", "Codec": "h264"},
                {"Type": "Audio", "Codec": "ac3"},
            ],
        },
        "PlayState": {"PositionTicks": 3_300_000_000, "IsPaused": False, "PlayMethod": "DirectPlay"},
    },
    {
        "UserName": "paula",
        "Client": "Emby Theater",
        "DeviceName": "Living Room Apple TV",
        "NowPlayingItem": {
            "Id": "ch-1",
            "Name": "COMEDY CENTRAL",
            "Type": "TvChannel",
            "ChannelName": "COMEDY CENTRAL",
            "ChannelNumber": "401",
            "ChannelId": "ch-1",
            "ImageTags": {"Primary": "ch-tag"},
        },
        "PlayState": {"IsPaused": False},
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


def test_format_duration():
    assert format_duration_ms(900000) == "15:00"
    assert format_duration_ms(3600000) == "1:00:00"
    assert format_duration_ticks(3_300_000_000) == "5:30"


def test_parse_plex_sessions_episode_and_movie():
    rows = parse_plex_sessions(PLEX_SAMPLE)
    assert len(rows) == 2
    assert rows[0]["server"] == "plex"
    assert rows[0]["kind"] == "episode"
    assert rows[0]["title"] == "Lanterns — S01E02 Trust Fall"
    assert rows[0]["user"] == "steve"
    assert rows[0]["player"] == "Living Room"
    assert rows[0]["state"] == "playing"
    assert rows[0]["progress"] == 25
    assert rows[0]["position_label"] == "15:00"
    assert rows[0]["remaining_label"] == "45:00"
    assert rows[0]["source"] == "S01E02.mkv"
    assert rows[0]["quality"] and "1080p" in rows[0]["quality"]
    assert rows[0]["resolution"] == "1080p"
    assert rows[0]["video_codec"] == "HEVC"
    assert rows[0]["audio_codec"] == "AAC"
    assert rows[0]["formats"] == ["1080p", "HEVC", "AAC", "8 Mbps"]
    assert rows[0]["stream"] == "direct"
    assert rows[0]["art_url"] == art_proxy_path("plex", "/library/metadata/100/thumb/2")
    assert rows[1]["title"] == "Dune (2021)"
    assert rows[1]["state"] == "paused"
    assert rows[1]["art_url"] == art_proxy_path("plex", "/library/metadata/300/thumb/3")


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


def test_parse_emby_skips_idle_and_enriches():
    rows = parse_emby_sessions(EMBY_SAMPLE)
    assert len(rows) == 2
    assert rows[0]["server"] == "emby"
    assert rows[0]["kind"] == "episode"
    assert rows[0]["title"] == "Friends — S01E01 The One Where Monica Gets a Roommate"
    assert rows[0]["user"] == "admin"
    assert rows[0]["player"] == "iPhone"
    assert rows[0]["state"] == "playing"
    assert rows[0]["progress"] == 25
    assert rows[0]["position_label"] == "5:30"
    assert rows[0]["source"] == "S01E01.mkv"
    assert rows[0]["stream"] == "direct"
    assert rows[0]["resolution"] == "1080p"
    assert rows[0]["video_codec"] == "H264"
    assert rows[0]["audio_codec"] == "AC3"
    assert rows[0]["formats"] == ["1080p", "H264", "AC3"]
    assert rows[0]["art_url"].startswith("/api/watching/art/emby?")
    assert "item_id=series-1" in rows[0]["art_url"]
    assert "tag=series-tag" in rows[0]["art_url"]
    assert rows[1]["kind"] == "channel"
    assert rows[1]["channel"] == "COMEDY CENTRAL"
    assert "COMEDY CENTRAL" in rows[1]["title"]
    assert "item_id=ch-1" in rows[1]["art_url"]


def test_art_proxy_path_builds_safe_query():
    assert art_proxy_path("plex", "/library/metadata/1/thumb/2") == (
        "/api/watching/art/plex?path=%2Flibrary%2Fmetadata%2F1%2Fthumb%2F2"
    )
    assert art_proxy_path("plex", "http://evil") is None
    assert art_proxy_path("plex", "../x") is None


def test_progress_pct_guards():
    assert progress_pct(0, 0) is None
    assert progress_pct(50, 100) == 50
    assert progress_pct(150, 100) == 100
