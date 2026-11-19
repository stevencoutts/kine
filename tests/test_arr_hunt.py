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


def test_lidarr_uses_api_v1_port_and_album_search():
    assert arr_hunt.ARR_API["lidarr"] == "v1"
    assert arr_hunt.ARR_PORTS["lidarr"] == 8686
    assert "lidarr" in arr_hunt.APPS
    assert arr_hunt.album_search_payload([3, 4]) == {
        "name": "AlbumSearch",
        "albumIds": [3, 4],
    }


def test_lidarr_queue_and_missing_params():
    assert arr_hunt.queue_params("lidarr")["includeUnknownArtistItems"] is True
    assert "includeUnknownMovieItems" not in arr_hunt.queue_params("lidarr")
    missing = arr_hunt.missing_params("lidarr")
    assert missing["sortKey"] == "releaseDate"
    # the whole wanted list, because Lidarr rotates through it
    assert missing["pageSize"] >= 500
    assert arr_hunt.missing_params("sonarr")["pageSize"] == 100


def test_queued_album_ids():
    assert arr_hunt.queued_album_ids([{"albumId": 5}, {"albumId": None}, {}]) == {5}


def test_plan_lidarr_skips_when_queue_is_deep():
    assert arr_hunt.plan_lidarr(
        {"records": [{"id": 1, "monitored": True}]},
        {"totalRecords": 30, "records": []},
        slot=0,
    ) is None


def test_plan_lidarr_excludes_queued_and_unmonitored():
    missing = {"records": [
        {"id": 1, "monitored": True},
        {"id": 2, "monitored": True},
        {"id": 3, "monitored": False},
        {"id": 4, "monitored": True},
    ]}
    queue = {"totalRecords": 1, "records": [{"albumId": 2}]}
    assert arr_hunt.plan_lidarr(missing, queue, slot=0) == [1, 4]


def test_plan_lidarr_rotates_through_the_whole_list_and_wraps():
    missing = {"records": [{"id": i, "monitored": True} for i in range(1, 21)]}
    queue = {"totalRecords": 0, "records": []}
    assert arr_hunt.plan_lidarr(missing, queue, batch=8, slot=0) == list(range(1, 9))
    assert arr_hunt.plan_lidarr(missing, queue, batch=8, slot=1) == list(range(9, 17))
    assert arr_hunt.plan_lidarr(missing, queue, batch=8, slot=2) == [17, 18, 19, 20, 1, 2, 3, 4]


def test_plan_lidarr_covers_every_album_before_repeating():
    missing = {"records": [{"id": i, "monitored": True} for i in range(1, 331)]}
    queue = {"totalRecords": 0, "records": []}
    seen: set[int] = set()
    for slot in range(42):  # 42 x 8 = 336 >= 330
        seen.update(arr_hunt.plan_lidarr(missing, queue, batch=8, slot=slot))
    assert seen == set(range(1, 331))


def test_plan_lidarr_small_list_has_no_duplicates_and_empty_list_is_empty():
    queue = {"totalRecords": 0, "records": []}
    three = {"records": [{"id": i, "monitored": True} for i in (7, 8, 9)]}
    assert sorted(arr_hunt.plan_lidarr(three, queue, batch=8, slot=5)) == [7, 8, 9]
    assert arr_hunt.plan_lidarr({"records": []}, queue, slot=3) == []


def test_hunt_missing_runs_lidarr_when_enabled(monkeypatch):
    import asyncio

    monkeypatch.setattr(arr_hunt.config, "profiles", lambda: ["lidarr"])

    async def fake_hunt_one(app):
        return {"searched": 8, "queue": 0, "missing": 330} if app == "lidarr" else {}

    monkeypatch.setattr(arr_hunt, "_hunt_one", fake_hunt_one)
    result = asyncio.run(arr_hunt.hunt_missing())
    assert result["ok"] is True
    assert result["lidarr"]["searched"] == 8
    assert result["sonarr"] == {"skipped": "disabled"}
    assert result["radarr"] == {"skipped": "disabled"}
