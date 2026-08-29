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
    sort_watching_sessions,
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
                "sessionKey": "42",
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
        "Id": "sess-friends",
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
            "MediaSources": [{"Bitrate": 4_000_000, "Path": "/tv/Friends/S01E01.mkv"}],
        },
        "PlayState": {"PositionTicks": 3_300_000_000, "IsPaused": False, "PlayMethod": "DirectPlay"},
    },
    {
        "Id": "sess-comedy",
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
            "ImageTags": {"Primary": "ch-tag", "Logo": "logo-tag"},
        },
        "PlayState": {"IsPaused": False},
    },
    {
        "Id": "sess-boxing",
        "UserName": "stevec",
        "Client": "Emby for Apple TV",
        "DeviceName": "Conservatory Apple TV",
        "NowPlayingItem": {
            "Id": "2277124",
            "Name": "Moses Itauma vs Filip Hrgovic",
            "Type": "TvChannel",
            "ChannelNumber": "3000",
            "ImageTags": {},
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
    assert rows[0]["bitrate_bps"] == 8_500_000
    assert rows[0]["art_url"] == art_proxy_path("plex", "/library/metadata/100/thumb/2")
    assert rows[1]["title"] == "Dune (2021)"
    assert rows[1]["state"] == "paused"
    assert rows[1].get("bitrate_bps") in (None, 0)
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


def test_parse_plex_prefers_media_bitrate_over_session_bandwidth():
    payload = {
        "MediaContainer": {
            "Metadata": [{
                "type": "movie",
                "title": "Heat",
                "User": {"title": "a"},
                "Player": {"title": "TV", "state": "playing", "local": False},
                "Session": {"bandwidth": 12000, "location": "wan"},
                "Media": [{"bitrate": 4500, "videoResolution": "1080", "Part": [{"file": "/m.mkv"}]}],
            }]
        }
    }
    assert parse_plex_sessions(payload)[0]["bitrate_bps"] == 4_500_000


def test_parse_plex_uses_measured_session_bandwidth_when_media_bitrate_missing():
    payload = {
        "MediaContainer": {
            "Metadata": [{
                "type": "episode",
                "title": "Part Twelve",
                "grandparentTitle": "Your Honor",
                "parentIndex": 2,
                "index": 2,
                "User": {"title": "a"},
                "Player": {"title": "TV", "state": "playing", "local": False},
                "Session": {"bandwidth": 1879, "location": "wan"},
                "Media": [{
                    "videoResolution": "480",
                    "videoCodec": "h264",
                    "Part": [{"file": "/tv/x.mkv", "Stream": [
                        {"streamType": 1, "bitrate": None, "decision": "transcode"},
                    ]}],
                }],
            }]
        }
    }
    row = parse_plex_sessions(payload)[0]
    assert row["bitrate_bps"] == 1_879_000


def test_parse_plex_uses_video_stream_bitrate_before_session():
    payload = {
        "MediaContainer": {
            "Metadata": [{
                "type": "movie",
                "title": "Heat",
                "User": {"title": "a"},
                "Player": {"title": "TV", "state": "playing"},
                "Session": {"bandwidth": 40000, "location": "lan"},
                "Media": [{
                    "videoResolution": "1080",
                    "Part": [{"file": "/m.mkv", "Stream": [
                        {"streamType": 1, "bitrate": 7200},
                        {"streamType": 2, "bitrate": 192},
                    ]}],
                }],
            }]
        }
    }
    assert parse_plex_sessions(payload)[0]["bitrate_bps"] == 7_200_000


def test_parse_plex_estimates_bitrate_when_lan_session_bandwidth_is_a_cap():
    """LAN Session.bandwidth is often a client max (40 Mbps), not measured rate."""
    payload = {
        "MediaContainer": {
            "Metadata": [{
                "type": "episode",
                "title": "Episode 238",
                "grandparentTitle": "QI XL",
                "parentIndex": 2026,
                "index": 238,
                "User": {"title": "a"},
                "Player": {"title": "Apple TV", "state": "playing", "local": True},
                "Session": {"bandwidth": 40000, "location": "lan"},
                "Media": [{
                    "videoResolution": "720",
                    "width": 1280,
                    "height": 720,
                    "container": "mpegts",
                    "videoCodec": "h264",
                    "Part": [{"Stream": [{"streamType": 1, "bitrate": None}]}],
                }],
            }]
        }
    }
    row = parse_plex_sessions(payload)[0]
    assert row["bitrate_bps"] == 5_000_000  # 720p estimate
    assert "5 Mbps" in (row.get("quality") or "")


def test_parse_emby_skips_idle_and_enriches():
    rows = parse_emby_sessions(EMBY_SAMPLE)
    assert len(rows) == 3
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
    assert rows[0]["formats"] == ["1080p", "H264", "AC3", "4 Mbps"]
    assert rows[0]["bitrate_bps"] == 4_000_000
    assert rows[0]["art_url"].startswith("/api/watching/art/emby?")
    assert "item_id=series-1" in rows[0]["art_url"]
    assert "tag=series-tag" in rows[0]["art_url"]
    assert rows[1]["kind"] == "channel"
    assert rows[1]["channel"] == "COMEDY CENTRAL"
    assert "COMEDY CENTRAL" in rows[1]["title"]
    assert "item_id=ch-1" in rows[1]["art_url"]
    assert "image=Logo" in rows[1]["art_url"]
    boxing = next(r for r in rows if r.get("session_id") == "sess-boxing")
    assert boxing["kind"] == "channel"
    assert "image=Primary" in boxing["art_url"]
    assert "item_id=2277124" in boxing["art_url"]
    assert "image=Logo" not in boxing["art_url"]


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


def test_sort_watching_sessions_longest_running_first():
    """Emby/Plex shuffle session order each poll; keep longest-running first."""
    rows = [
        {"title": "new", "position_ms": 60_000, "session_id": "b", "user": "a"},
        {"title": "old", "position_ms": 3_600_000, "session_id": "a", "user": "b"},
        {"title": "live", "position_ms": None, "session_id": "c", "user": "c"},
    ]
    ordered = sort_watching_sessions(rows)
    assert [r["title"] for r in ordered] == ["old", "new", "live"]
    # Stable when positions match.
    tied = [
        {"title": "z", "position_ms": 100, "session_id": "2", "user": "u"},
        {"title": "a", "position_ms": 100, "session_id": "1", "user": "u"},
    ]
    assert [r["session_id"] for r in sort_watching_sessions(tied)] == ["1", "2"]


def test_emby_and_plex_expose_session_id():
    plex = parse_plex_sessions(PLEX_SAMPLE)
    assert plex[0]["session_id"] == "42"
    emby = parse_emby_sessions(EMBY_SAMPLE)
    assert emby[0]["session_id"] == "sess-friends"
    assert emby[1]["session_id"] == "sess-comedy"
