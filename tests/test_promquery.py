"""Prometheus range responses become per-app sparkline series."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import promquery  # noqa: E402

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
