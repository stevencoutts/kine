"""Exporter rendering and parsing, with no network anywhere."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import metrics  # noqa: E402


def test_render_emits_help_and_type_once_per_metric():
    out = metrics.render([
        metrics.Sample("kine_app_up", {"app": "sonarr"}, 1),
        metrics.Sample("kine_app_up", {"app": "radarr"}, 0),
    ])
    assert out.count("# TYPE kine_app_up gauge") == 1
    assert out.count("# HELP kine_app_up") == 1
    assert 'kine_app_up{app="sonarr"} 1' in out
    assert 'kine_app_up{app="radarr"} 0' in out
    assert out.endswith("\n")


def test_render_escapes_label_values():
    out = metrics.render([
        metrics.Sample("kine_streams_active", {"user": 'a"b\\c'}, 1),
    ])
    assert 'user="a\\"b\\\\c"' in out


def test_render_writes_bare_name_when_unlabelled():
    out = metrics.render([metrics.Sample("kine_app_up", {}, 1)])
    assert "kine_app_up 1" in out


def test_render_marks_counters_as_counters():
    out = metrics.render([
        metrics.Sample("kine_collect_errors_total", {"app": "sonarr"}, 3),
    ])
    assert "# TYPE kine_collect_errors_total counter" in out


def test_every_declared_metric_is_namespaced_and_documented():
    for name in metrics.METRIC_TYPES:
        assert name.startswith("kine_")
        assert metrics.METRIC_HELP.get(name)


SONARR_SERIES = [
    {"statistics": {"episodeCount": 20, "episodeFileCount": 18, "sizeOnDisk": 500}},
    {"statistics": {"episodeCount": 10, "episodeFileCount": 10, "sizeOnDisk": 300}},
]


def test_parse_arr_counts_series_episodes_and_gaps():
    out = metrics.parse_arr(
        "sonarr",
        counts={"items": SONARR_SERIES},
        queue={"totalRecords": 3},
        missing={"totalRecords": 7},
    )
    by = {(s.name, s.labels.get("kind")): s.value for s in out}
    assert by[("kine_library_items", "series")] == 2
    assert by[("kine_library_items", "episodes")] == 30
    assert by[("kine_library_missing", "episodes")] == 7
    assert by[("kine_queue_items", None)] == 3
    assert next(s.value for s in out if s.name == "kine_library_bytes") == 800
    assert all(s.labels.get("app") == "sonarr" for s in out)


def test_parse_arr_handles_radarr_movie_shape():
    movies = {"items": [
        {"hasFile": True, "sizeOnDisk": 1000},
        {"hasFile": False, "sizeOnDisk": 0},
    ]}
    out = metrics.parse_arr(
        "radarr", counts=movies, queue={"totalRecords": 0}, missing={"totalRecords": 1}
    )
    by = {(s.name, s.labels.get("kind")): s.value for s in out}
    assert by[("kine_library_items", "movies")] == 2
    assert by[("kine_library_missing", "movies")] == 1
    assert next(s.value for s in out if s.name == "kine_library_bytes") == 1000


def test_parse_arr_tolerates_empty_payloads():
    out = metrics.parse_arr("sonarr", counts={}, queue={}, missing={})
    by = {(s.name, s.labels.get("kind")): s.value for s in out}
    assert by[("kine_library_items", "series")] == 0
    assert by[("kine_queue_items", None)] == 0


def test_parse_bazarr_splits_series_and_movies():
    out = metrics.parse_bazarr({"data": [{}, {}]}, {"data": [{}]})
    by = {s.labels["kind"]: s.value for s in out}
    assert by == {"series": 2, "movies": 1}
    assert all(s.name == "kine_subtitles_wanted" for s in out)


def test_parse_prowlarr_counts_enabled_and_totals():
    indexers = [{"enable": True}, {"enable": True}, {"enable": False}]
    stats = {"indexers": [
        {"numberOfQueries": 10, "numberOfGrabs": 2},
        {"numberOfQueries": 5, "numberOfGrabs": 1},
    ]}
    out = metrics.parse_prowlarr(indexers, stats)
    by = {s.name: s.value for s in out}
    assert by["kine_indexers_enabled"] == 2
    assert by["kine_indexer_queries_total"] == 15
    assert by["kine_indexer_grabs_total"] == 3


def test_parse_transmission_reads_rates_and_states():
    out = metrics.parse_transmission({
        "arguments": {
            "downloadSpeed": 1200,
            "uploadSpeed": 300,
            "activeTorrentCount": 4,
            "pausedTorrentCount": 2,
            "torrentCount": 6,
        }
    })
    rates = {s.labels["direction"]: s.value for s in out if s.name == "kine_download_rate_bytes"}
    states = {s.labels["state"]: s.value for s in out if s.name == "kine_torrents"}
    assert rates == {"down": 1200, "up": 300}
    assert states == {"active": 4, "paused": 2, "total": 6}


def test_parse_streams_groups_by_server_state_and_user():
    snapshot = {"sessions": [
        {"server": "plex", "state": "playing", "user": "steve"},
        {"server": "plex", "state": "playing", "user": "steve"},
        {"server": "emby", "state": "paused", "user": "kate"},
    ]}
    out = metrics.parse_streams(snapshot)
    by = {(s.labels["server"], s.labels["state"], s.labels["user"]): s.value for s in out}
    assert by[("plex", "playing", "steve")] == 2
    assert by[("emby", "paused", "kate")] == 1


def test_parse_streams_reports_nothing_when_idle():
    assert metrics.parse_streams({"sessions": []}) == []


def test_parse_updates_flags_pending_containers():
    payload = {"containers": [
        {"id": "sonarr", "update_available": True, "status": "update"},
        {"id": "radarr", "update_available": False, "status": "current"},
    ]}
    by = {s.labels["app"]: s.value for s in metrics.parse_updates(payload)}
    assert by == {"sonarr": 1, "radarr": 0}
