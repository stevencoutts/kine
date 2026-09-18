"""Conservative missing-episode/movie search planning."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import arr_hunt  # noqa: E402


def test_skips_when_download_queue_is_already_busy():
    assert arr_hunt.should_skip(25, limit=25) is True
    assert arr_hunt.should_skip(24, limit=25) is False


def test_queued_episode_ids_include_pack_members():
    records = [
        {"episodeId": 10},
        {"episodeId": 11, "episodes": [{"id": 11}, {"id": 12}]},
    ]
    assert arr_hunt.queued_episode_ids(records) == {10, 11, 12}


def test_pick_missing_skips_queued_unmonitored_and_specials():
    missing = [
        {"id": 1, "monitored": True, "seasonNumber": 1},
        {"id": 2, "monitored": True, "seasonNumber": 1},
        {"id": 3, "monitored": False, "seasonNumber": 1},
        {"id": 4, "monitored": True, "seasonNumber": 0},
        {"id": 5, "monitored": True, "seasonNumber": 2},
    ]
    assert arr_hunt.pick_missing_ids(missing, queued={2}, limit=10) == [1, 5]


def test_pick_missing_respects_batch_limit():
    missing = [{"id": i, "monitored": True, "seasonNumber": 1} for i in range(1, 20)]
    assert arr_hunt.pick_missing_ids(missing, queued=set(), limit=3) == [1, 2, 3]


def test_plan_sonarr_returns_none_when_queue_is_deep():
    assert arr_hunt.plan_sonarr(
        {"records": [{"id": 1, "monitored": True, "seasonNumber": 1}]},
        {"totalRecords": 40, "records": []},
    ) is None


def test_plan_sonarr_excludes_queued_and_caps_batch():
    missing = {"records": [
        {"id": 1, "monitored": True, "seasonNumber": 1},
        {"id": 2, "monitored": True, "seasonNumber": 1},
        {"id": 3, "monitored": True, "seasonNumber": 1},
    ]}
    queue = {"totalRecords": 1, "records": [{"episodeId": 2}]}
    assert arr_hunt.plan_sonarr(missing, queue, batch=8) == [1, 3]


def test_plan_returns_empty_when_everything_is_already_queued():
    missing = {"records": [{"id": 1, "monitored": True, "seasonNumber": 1}]}
    queue = {"totalRecords": 1, "records": [{"episodeId": 1}]}
    assert arr_hunt.plan_sonarr(missing, queue) == []


def test_queued_movie_ids_and_plan_radarr():
    missing = {"records": [
        {"id": 9, "monitored": True},
        {"id": 8, "monitored": True},
        {"id": 7, "monitored": False},
    ]}
    queue = {"totalRecords": 1, "records": [{"movieId": 9}]}
    assert arr_hunt.queued_movie_ids(queue["records"]) == {9}
    assert arr_hunt.plan_radarr(missing, queue, batch=8) == [8]


def test_search_payloads():
    assert arr_hunt.episode_search_payload([1, 2]) == {
        "name": "EpisodeSearch",
        "episodeIds": [1, 2],
    }
    assert arr_hunt.movie_search_payload([5]) == {
        "name": "MoviesSearch",
        "movieIds": [5],
    }


def test_queue_and_missing_query_params_differ_per_app():
    assert arr_hunt.queue_params("sonarr")["includeUnknownSeriesItems"] is True
    assert "includeUnknownMovieItems" not in arr_hunt.queue_params("sonarr")
    assert arr_hunt.queue_params("radarr")["includeUnknownMovieItems"] is True
    assert arr_hunt.missing_params("sonarr")["sortKey"] == "airDateUtc"
    assert arr_hunt.missing_params("radarr")["sortKey"] == "year"
