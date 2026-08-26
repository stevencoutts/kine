"""Prometheus range responses become per-app sparkline series."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import promquery  # noqa: E402


def test_parse_instant_reads_labelled_and_scalar_values():
    payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"server": "plex"}, "value": [1, "2"]},
                {"metric": {"server": "emby"}, "value": [1, "1"]},
            ],
        },
    }
    assert promquery.parse_instant(payload) == [
        {"labels": {"server": "plex"}, "value": 2.0},
        {"labels": {"server": "emby"}, "value": 1.0},
    ]


def test_parse_instant_scalar_when_unlabelled():
    payload = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "38.5"]}]},
    }
    assert promquery.parse_instant(payload) == [{"labels": {}, "value": 38.5}]


def test_parse_instant_survives_errors():
    assert promquery.parse_instant({"status": "error"}) == []
    assert promquery.parse_instant({}) == []


def test_build_overview_shapes_streamers_downloads_and_nfs():
    overview = promquery.build_overview(
        unique_users=2,
        sessions_by_server={"plex": 3, "emby": 1},
        rate_by_direction={"down": 1_500_000, "up": 80_000},
        total_by_direction={"down": 9e12, "up": 2e12},
        nfs={
            "mountpoint": "/srv/media-data/media",
            "size_bytes": 29e12,
            "avail_bytes": 18e12,
        },
        cpu_pct=38.2,
        mem_pct=68.0,
    )
    assert overview["streamers"] == {
        "unique": 2,
        "sessions": 4,
        "by_server": {"plex": 3, "emby": 1},
    }
    assert overview["downloads"]["rate_down"] == 1_500_000
    assert overview["downloads"]["rate_up"] == 80_000
    assert overview["downloads"]["total_down"] == 9e12
    assert overview["downloads"]["total_up"] == 2e12
    assert overview["nfs_disk"]["used_pct"] == round(100 * (29e12 - 18e12) / 29e12, 1)
    assert overview["nfs_disk"]["mountpoint"] == "/srv/media-data/media"
    assert overview["host"] == {"cpu_pct": 38.2, "mem_pct": 68.0}


def test_nfs_mountpoint_prefers_data_root_media():
    assert promquery.nfs_media_mountpoint({"DATA_ROOT": "/srv/media-data"}) == "/srv/media-data/media"
    assert promquery.nfs_media_mountpoint({}) == "/srv/media-data/media"

RANGE_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"name": "kine-sonarr"},
                "values": [[1700000000, "1.5"], [1700000300, "2.25"]],
            },
            {
                "metric": {"name": "kine-radarr"},
                "values": [[1700000000, "0"], [1700000300, "0.5"]],
            },
        ],
    },
}


def test_parse_range_strips_the_container_prefix():
    assert set(promquery.parse_range(RANGE_RESPONSE)) == {"sonarr", "radarr"}


def test_parse_range_returns_floats_in_order():
    assert promquery.parse_range(RANGE_RESPONSE)["sonarr"] == [1.5, 2.25]


def test_parse_range_survives_an_error_response():
    assert promquery.parse_range({"status": "error"}) == {}


def test_parse_range_survives_an_empty_payload():
    assert promquery.parse_range({}) == {}


def test_parse_range_skips_unparseable_points():
    payload = {
        "status": "success",
        "data": {"result": [
            {"metric": {"name": "kine-x"}, "values": [[1, "NaN"], [2, "3"]]}
        ]},
    }
    assert promquery.parse_range(payload)["x"] == [3.0]


def test_parse_range_ignores_series_with_no_name():
    payload = {
        "status": "success",
        "data": {"result": [{"metric": {}, "values": [[1, "1"]]}]},
    }
    assert promquery.parse_range(payload) == {}
