"""Parse Transmission and NZBGet download summaries without hitting a server."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app.downloads import (  # noqa: E402
    format_eta_seconds,
    parse_nzbget_groups,
    parse_nzbget_items,
    parse_nzbget_status,
    parse_transmission_items,
    parse_transmission_stats,
)


def test_parse_transmission_stats():
    row = parse_transmission_stats({
        "arguments": {
            "activeTorrentCount": 3,
            "pausedTorrentCount": 2,
            "torrentCount": 5,
            "downloadSpeed": 1_500_000,
            "uploadSpeed": 40_000,
        }
    })
    assert row["active"] == 3
    assert row["paused"] == 2
    assert row["total"] == 5
    assert row["download_rate"] == 1_500_000
    assert row["upload_rate"] == 40_000


def test_parse_transmission_items():
    rows = parse_transmission_items([
        {
            "id": 1,
            "name": "Show.S01E01.mkv",
            "percentDone": 0.42,
            "rateDownload": 2_000_000,
            "rateUpload": 50_000,
            "eta": 120,
            "status": 4,
            "errorString": "",
        },
        {
            "id": 2,
            "name": "Movie.1080p",
            "percentDone": 1.0,
            "rateDownload": 0,
            "rateUpload": 100_000,
            "eta": -1,
            "status": 6,
        },
        {
            "id": 3,
            "name": "Paused.Thing",
            "percentDone": 0.1,
            "rateDownload": 0,
            "rateUpload": 0,
            "eta": -1,
            "status": 0,
        },
    ])
    assert len(rows) == 3
    assert rows[0]["client"] == "transmission"
    assert rows[0]["name"] == "Show.S01E01.mkv"
    assert rows[0]["progress"] == 42
    assert rows[0]["download_rate"] == 2_000_000
    assert rows[0]["upload_rate"] == 50_000
    assert rows[0]["eta_label"] == "2:00"
    assert rows[0]["state"] == "downloading"
    assert rows[1]["state"] == "seeding"
    assert rows[1]["progress"] == 100
    assert rows[2]["state"] == "paused"


def test_parse_nzbget_groups_counts_states():
    groups = [
        {"Status": "DOWNLOADING", "NZBName": "a"},
        {"Status": "QUEUED", "NZBName": "b"},
        {"Status": "PAUSED", "NZBName": "c"},
        {"Status": "UNPACKING", "NZBName": "d"},
    ]
    row = parse_nzbget_groups(groups)
    assert row["downloading"] == 1
    assert row["queued"] == 2
    assert row["postprocessing"] == 1
    assert row["active"] == 2
    assert row["total"] == 4


def test_parse_nzbget_items():
    groups = [
        {
            "NZBID": 10,
            "NZBName": "Series.S02E03",
            "Status": "DOWNLOADING",
            "FileSizeLo": 1_000_000_000,
            "FileSizeHi": 0,
            "RemainingSizeLo": 250_000_000,
            "RemainingSizeHi": 0,
            "DownloadRate": 5_000_000,
        },
        {
            "NZBID": 11,
            "NZBName": "Unpack.Me",
            "Status": "UNPACKING",
            "FileSizeLo": 100,
            "FileSizeHi": 0,
            "RemainingSizeLo": 0,
            "RemainingSizeHi": 0,
            "DownloadRate": 0,
        },
        {
            "NZBID": 12,
            "NZBName": "Queued.Thing",
            "Status": "QUEUED",
            "FileSizeLo": 500,
            "FileSizeHi": 0,
            "RemainingSizeLo": 500,
            "RemainingSizeHi": 0,
            "DownloadRate": 0,
        },
    ]
    rows = parse_nzbget_items(groups)
    assert len(rows) == 3
    assert rows[0]["client"] == "nzbget"
    assert rows[0]["name"] == "Series.S02E03"
    assert rows[0]["state"] == "downloading"
    assert rows[0]["progress"] == 75
    assert rows[0]["download_rate"] == 5_000_000
    assert rows[0]["eta_label"]  # rate-based remaining
    assert rows[1]["state"] == "post-processing"
    assert rows[1]["progress"] == 100
    assert rows[2]["state"] == "queued"


def test_format_eta_seconds():
    assert format_eta_seconds(45) == "0:45"
    assert format_eta_seconds(125) == "2:05"
    assert format_eta_seconds(3725) == "1:02:05"
    assert format_eta_seconds(-1) is None
    assert format_eta_seconds(None) is None


def test_parse_nzbget_status_rate():
    row = parse_nzbget_status({"result": {"DownloadRate": 8000, "DownloadPaused": False}})
    assert row["download_rate"] == 8000
    assert row["paused"] is False


def test_nzbget_snapshot_fetches_rpc_in_parallel(monkeypatch):
    """listgroups + status must overlap; sequential calls routinely exceed the old 6s timeout."""
    import asyncio
    import time

    from app import catalogue, config, downloads

    monkeypatch.setattr(config, "profiles", lambda: ["nzbget"])
    monkeypatch.setattr(
        catalogue, "load",
        lambda: {"nzbget": {"internal": "http://gluetun:6789"}},
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    starts: list[float] = []

    async def fake_rpc(client, base, method, params=None):
        starts.append(time.perf_counter())
        await asyncio.sleep(0.12)
        if method == "listgroups":
            return []
        if method == "status":
            return {"DownloadRate": 42, "DownloadPaused": False, "ServerStandBy": False}
        raise AssertionError(method)

    monkeypatch.setattr(downloads.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(downloads, "_nzbget_rpc", fake_rpc)

    async def _run():
        t0 = time.perf_counter()
        summary, err = await downloads._nzbget_snapshot()
        elapsed = time.perf_counter() - t0
        assert err is None
        assert summary["download_rate"] == 42
        assert len(starts) == 2
        # Both RPCs started before either finished (overlap), so wall time << 2 * 0.12s.
        assert abs(starts[0] - starts[1]) < 0.05
        assert elapsed < 0.22

    asyncio.run(_run())


def test_frontend_has_downloads_overview():
    frontend = (ROOT / "helm" / "frontend" / "index.html").read_text()
    backend = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    assert "downloadsPanel" in frontend
    assert 'data-overview="downloads"' in frontend
    assert "/downloads" in frontend
    assert '@app.get("/api/downloads")' in backend
    assert "dl-card" in frontend
    assert "parse_transmission_items" in (ROOT / "helm" / "backend" / "app" / "downloads.py").read_text()
    assert "items" in (ROOT / "helm" / "backend" / "app" / "downloads.py").read_text()
