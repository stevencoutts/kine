"""Parse Plex and Emby now-playing payloads without hitting a server."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app.watching import (  # noqa: E402
    art_proxy_path,
    attach_live_providers,
    dispatcharr_logo_path,
    format_duration_ms,
    format_duration_ticks,
    index_live_providers,
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


def test_parse_plex_ignores_int32_max_bitrate_sentinel():
    """Live/TV often reports 2147483647 kbps (INT32_MAX); that is not 2147484 Mbps."""
    payload = {
        "MediaContainer": {
            "Metadata": [{
                "type": "live",
                "title": "UCL Soccer",
                "channelTitle": "UCL Soccer",
                "User": {"title": "a"},
                "Player": {"title": "TV", "state": "playing"},
                "Media": [{
                    "bitrate": 2147483647,
                    "videoResolution": "1080",
                    "videoCodec": "h264",
                    "Part": [{"Stream": [
                        {"streamType": 1, "bitrate": 2147483647, "decision": "transcode"},
                    ]}],
                }],
            }]
        }
    }
    row = parse_plex_sessions(payload)[0]
    assert row["bitrate_bps"] == 8_000_000
    assert "8 Mbps" in row["formats"]
    assert all("2147484" not in str(x) for x in row["formats"])


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


def test_live_session_gets_the_m3u_account_actually_serving_it():
    """Library plays have no IPTV provider. Live cards show the active account."""
    stats = {
        "live": {
            "channels": [
                {
                    "channel_id": "11111111-1111-1111-1111-111111111111",
                    "channel_name": "Sky Sports Main Event",
                    "stream_id": 9,
                },
                {
                    "channel_id": "22222222-2222-2222-2222-222222222222",
                    "channel_name": "Chelsea",
                    "stream_id": 10,
                },
            ]
        }
    }
    streams = [
        {"id": 9, "m3u_account": 5},
        {"id": 10, "m3u_account": {"id": 3, "name": "Lumen-direct"}},
    ]
    accounts = [{"id": 5, "name": "Strong8K"}, {"id": 3, "name": "Lumen-direct"}]
    channels = [
        {"uuid": "11111111-1111-1111-1111-111111111111", "channel_number": 401},
        {"uuid": "22222222-2222-2222-2222-222222222222", "channel_number": 3000.0},
    ]
    rows = index_live_providers(stats, streams, accounts, channels)
    sessions = [
        {"kind": "episode", "title": "Lanterns", "channel": None},
        {"kind": "channel", "title": "Sky Sports Main Event", "channel": "Sky Sports Main Event"},
        {
            "kind": "channel",
            "title": "Moses Itauma vs Filip Hrgovic",
            "channel": "3000",
            "channel_number": "3000",
        },
        {
            "kind": "channel",
            "title": "Sky Sports Main Event",
            "channel": "Sky Sports Main Event",
            "channel_id": "11111111-1111-1111-1111-111111111111",
        },
        {
            "kind": "episode",
            "title": "UCL Soccer",
            "channel_id": "11111111-1111-1111-1111-111111111111",
        },
    ]
    attach_live_providers(sessions, rows)
    assert "provider" not in sessions[0]
    assert sessions[1]["provider"] == "Strong8K"
    assert sessions[2]["provider"] == "Lumen-direct"
    assert sessions[3]["provider"] == "Strong8K"
    assert sessions[4]["provider"] == "Strong8K"


def test_plex_livetv_program_uses_the_provider_on_that_channel():
    """Plex live TV is type=movie with no channel fields. The guide title identifies it."""
    sessions = parse_plex_sessions({
        "MediaContainer": {
            "Metadata": [
                {
                    "type": "movie",
                    "title": "Insidious: The Last Key",
                    "year": 2018,
                    "live": "1",
                    "key": "/livetv/sessions/42cc5c96-6178-41b1-a50c-01613b9403aa",
                    "User": {"title": "coutts99"},
                    "Player": {"title": "Apple TV", "product": "Plex for Apple TV", "state": "playing"},
                    "Media": [{
                        "origin": "livetv",
                        "videoResolution": "1080",
                        "videoCodec": "h264",
                        "audioCodec": "aac",
                    }],
                },
                {
                    "type": "movie",
                    "title": "Insidious: The Last Key",
                    "year": 2018,
                    "User": {"title": "coutts99"},
                    "Player": {"title": "Apple TV", "state": "playing"},
                    "Media": [{"videoResolution": "1080", "Part": [{"file": "/movies/Insidious.mkv"}]}],
                },
            ],
        }
    })
    live, library = sessions
    assert live["kind"] == "channel"
    assert live["program"] == "Insidious: The Last Key"
    assert "(2018)" not in live["title"]
    assert library["kind"] == "movie"
    rows = index_live_providers(
        {"live": {"channels": [{
            "channel_id": "57e1a8b5-b4e9-45ce-9941-9c8d0d27f95b",
            "channel_name": "SKY CINEMA HITS",
            "stream_id": 28107,
        }]}},
        [{"id": 28107, "m3u_account": 5}],
        [{"id": 5, "name": "Strong8K"}],
        [{"uuid": "57e1a8b5-b4e9-45ce-9941-9c8d0d27f95b", "channel_number": 303, "logo_id": 260}],
        [{"channel_uuid": "57e1a8b5-b4e9-45ce-9941-9c8d0d27f95b", "title": "Insidious: The Last Key"}],
    )
    attach_live_providers(sessions, rows)
    assert live["provider"] == "Strong8K"
    assert live["title"] == "SKY CINEMA HITS — Insidious: The Last Key"
    assert live["channel"] == "SKY CINEMA HITS"
    assert live["channel_number"] != "303"
    assert live["art_url"] == "/api/watching/art/dispatcharr?logo_id=260"
    assert "provider" not in library
    assert library["art_url"] != live["art_url"]


def test_emby_live_title_is_channel_and_programme_without_the_number():
    sessions = parse_emby_sessions([{
        "Id": "sess-bbc",
        "UserName": "paula",
        "Client": "Emby for Apple TV",
        "DeviceName": "Living Room Apple TV",
        "NowPlayingItem": {
            "Id": "3689240",
            "Name": "BBC 1",
            "Type": "TvChannel",
            "ChannelNumber": "101",
            "Number": "101",
        },
        "PlayState": {"IsPaused": False},
    }])
    bbc = sessions[0]
    assert bbc["title"] == "BBC 1"
    assert bbc["channel"] == "BBC 1"
    assert bbc["channel_number"] == "101"
    assert bbc.get("source") != "101"
    rows = index_live_providers(
        {"live": {"channels": [{
            "channel_id": "2a800fd4-44c0-4366-9400-5dd5d0d5d9e8",
            "channel_name": "BBC 1",
            "stream_id": 24978,
        }]}},
        [{"id": 24978, "m3u_account": 5}],
        [{"id": 5, "name": "Strong8K"}],
        [{"uuid": "2a800fd4-44c0-4366-9400-5dd5d0d5d9e8", "channel_number": 101, "logo_id": 12}],
        [{"channel_uuid": "2a800fd4-44c0-4366-9400-5dd5d0d5d9e8", "title": "The Celebrity Inner Circle"}],
    )
    attach_live_providers(sessions, rows)
    assert bbc["provider"] == "Strong8K"
    assert bbc["title"] == "BBC 1 — The Celebrity Inner Circle"
    assert bbc["channel"] == "BBC 1"
    assert "101" not in bbc["title"]
    assert dispatcharr_logo_path(12) == "/api/watching/art/dispatcharr?logo_id=12"
    assert dispatcharr_logo_path("nope") is None
    # Emby already has its own channel image; don't replace it with the playlist logo.
    assert not str(bbc.get("art_url") or "").startswith("/api/watching/art/dispatcharr")


def test_parse_live_sessions_keep_tuner_channel_id_and_number():
    plex = parse_plex_sessions({
        "MediaContainer": {
            "Metadata": [{
                "type": "live",
                "title": "UCL Soccer",
                "channelTitle": "Sky Sports Main Event",
                "channelIdentifier": "401",
                "User": {"title": "steve"},
                "Player": {"title": "Apple TV", "state": "playing"},
                "Media": [{
                    "Part": [{
                        "file": "http://dispatcharr:9191/proxy/ts/stream/11111111-1111-1111-1111-111111111111",
                    }],
                }],
            }]
        }
    })
    assert plex[0]["kind"] == "channel"
    assert plex[0]["channel_number"] == "401"
    assert plex[0]["channel_id"] == "11111111-1111-1111-1111-111111111111"
    emby = parse_emby_sessions(EMBY_SAMPLE)
    boxing = next(r for r in emby if r["session_id"] == "sess-boxing")
    assert boxing["channel_number"] == "3000"
    assert emby[0]["channel_number"] is None
