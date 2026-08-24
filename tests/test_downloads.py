"""Parse Transmission and NZBGet download summaries without hitting a server."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app.downloads import (  # noqa: E402
    parse_nzbget_groups,
    parse_nzbget_status,
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


def test_parse_nzbget_status_rate():
    row = parse_nzbget_status({"result": {"DownloadRate": 8000, "DownloadPaused": False}})
    assert row["download_rate"] == 8000
    assert row["paused"] is False


def test_frontend_has_downloads_overview():
    frontend = (ROOT / "helm" / "frontend" / "index.html").read_text()
    backend = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    assert "downloadsPanel" in frontend
    assert 'data-overview="downloads"' in frontend
    assert "/downloads" in frontend
    assert '@app.get("/api/downloads")' in backend
