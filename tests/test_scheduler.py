"""Helm scheduler behaviour."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))
SCHEDULER = (ROOT / "helm" / "backend" / "app" / "scheduler.py").read_text()


def test_scheduler_polls_seerr_until_arr_linked():
    assert "_seerr_needs_wire" in SCHEDULER
    assert "wire_seerr_if_ready" in SCHEDULER
    assert "_seerr_wire_loop" in SCHEDULER
    assert 'asyncio.create_task(_seerr_wire_loop())' in SCHEDULER


def test_scheduler_polls_dispatcharr_wire():
    assert "_dispatcharr_needs_wire" in SCHEDULER
    assert "wire_dispatcharr_if_ready" in SCHEDULER
    assert "_dispatcharr_wire_loop" in SCHEDULER
    assert 'asyncio.create_task(_dispatcharr_wire_loop())' in SCHEDULER
    assert "/api/v1/settings/{path}" in SCHEDULER


def test_scheduler_refreshes_emby_event_channel_art():
    """Emby keeps yesterday's logo on reused Teamarr channel numbers."""
    assert "sync_emby_event_channel_art" in SCHEDULER
    assert "_emby_event_art_loop" in SCHEDULER
    assert 'asyncio.create_task(_emby_event_art_loop())' in SCHEDULER
    assert "emby-livetv-art" in SCHEDULER


def test_peer_url_stale_reads_teamarr_database(tmp_path, monkeypatch):
    """Teamarr dials settings.dispatcharr_url, not DISPATCHARR_URL in the env file."""
    from app import scheduler

    db = tmp_path / "teamarr.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE settings (dispatcharr_url TEXT)")
    conn.execute(
        "INSERT INTO settings (dispatcharr_url) VALUES ('http://dispatcharr:9191')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(scheduler, "_teamarr_dispatcharr_db", lambda: db)
    monkeypatch.setattr(
        scheduler.tunnel_hosts, "load_profiles",
        lambda: {"live_tv_direct": False, "profiles": []},
    )
    assert scheduler._peer_urls_stale() is True

    conn = sqlite3.connect(db)
    conn.execute("UPDATE settings SET dispatcharr_url = 'http://127.0.0.1:9191'")
    conn.commit()
    conn.close()
    assert scheduler._peer_urls_stale() is False


def test_scheduler_hunts_missing_arr_items():
    """RSS never backfills; Helm searches a small missing batch on a cron."""
    assert "arr_hunt" in SCHEDULER
    assert "hunt_missing" in SCHEDULER
    assert 'HELM_ARR_HUNT_CRON' in SCHEDULER
    assert 'asyncio.create_task(' in SCHEDULER
    assert "_arr_hunt" in SCHEDULER
    assert "run_arr_hunt" in SCHEDULER


def test_dispatcharr_token_not_on_argv():
    """The token must travel in the subprocess env, never in `ps`-visible argv."""
    assert "DISPATCHARR_TOKEN={" not in SCHEDULER
    assert 'extra_env={"DISPATCHARR_TOKEN": token}' in SCHEDULER
