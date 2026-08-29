"""Helm updates parsing and provision lock."""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import provision_lock, updates_info  # noqa: E402


SAMPLE_REPORT = """\
APP            STATUS       IMAGE
sonarr         current      lscr.io/linuxserver/sonarr:latest
radarr         UPDATE       lscr.io/linuxserver/radarr:latest
"""


def test_parse_report():
    rows = updates_info.parse_report(SAMPLE_REPORT)
    assert [r["id"] for r in rows] == ["sonarr", "radarr"]
    assert rows[0]["update_available"] is False
    assert rows[1]["update_available"] is True


def test_enrich_marks_hidden_and_unknown_as_core(monkeypatch):
    monkeypatch.setattr(updates_info.config, "read", lambda: {})
    monkeypatch.setattr(updates_info.config, "profiles", lambda: ["sonarr", "cadvisor"])
    monkeypatch.setattr(updates_info.channels, "channels", lambda: [])
    monkeypatch.setattr(
        updates_info.catalogue, "load",
        lambda: {
            "sonarr": {"name": "Sonarr", "hidden": False},
            "cadvisor": {"name": "cAdvisor", "hidden": True},
        },
    )
    rows = updates_info.enrich([
        {"id": "sonarr", "tag": "latest", "update_available": False},
        {"id": "cadvisor", "tag": "latest", "update_available": True},
        {"id": "traefik", "tag": "latest", "update_available": False},
        {"id": "dockerproxy", "tag": "0.3", "update_available": True},
    ])
    by_id = {r["id"]: r for r in rows}
    assert by_id["sonarr"]["core"] is False
    assert by_id["cadvisor"]["core"] is True
    assert by_id["traefik"]["core"] is True
    assert by_id["dockerproxy"]["host_only"] is True
    assert by_id["sonarr"]["host_only"] is False
    # Catalogue apps honour COMPOSE_PROFILES; Traefik has no profile.
    assert by_id["sonarr"]["enabled"] is True
    assert by_id["cadvisor"]["enabled"] is True
    assert by_id["traefik"]["enabled"] is True
    assert by_id["dockerproxy"]["enabled"] is True

    # cadvisor is in the catalogue but not in profiles → disabled;
    # traefik is always-on plumbing → still enabled.
    monkeypatch.setattr(updates_info.config, "profiles", lambda: [])
    rows2 = updates_info.enrich([
        {"id": "cadvisor", "tag": "latest", "update_available": True},
        {"id": "traefik", "tag": "latest", "update_available": True},
    ])
    by2 = {r["id"]: r for r in rows2}
    assert by2["cadvisor"]["enabled"] is False
    assert by2["traefik"]["enabled"] is True
    assert [r["id"] for r in updates_info.catalogue_apps(rows)] == ["sonarr"]


def test_enrich_includes_catalogue_tier(monkeypatch):
    monkeypatch.setattr(updates_info.config, "read", lambda: {})
    monkeypatch.setattr(updates_info.config, "profiles", lambda: ["sonarr", "dispatcharr", "grafana"])
    monkeypatch.setattr(updates_info.channels, "channels", lambda: [])
    monkeypatch.setattr(
        updates_info.catalogue, "load",
        lambda: {
            "sonarr": {"name": "Sonarr", "tier": "acquisition"},
            "dispatcharr": {"name": "Dispatcharr", "tier": "live"},
            "grafana": {"name": "Grafana", "tier": "metrics"},
        },
    )
    rows = updates_info.enrich([
        {"id": "sonarr", "tag": "latest", "update_available": False},
        {"id": "dispatcharr", "tag": "dev", "update_available": False},
        {"id": "grafana", "tag": "latest", "update_available": False},
    ])
    by_id = {r["id"]: r for r in rows}
    assert by_id["sonarr"]["tier"] == "acquisition"
    assert by_id["dispatcharr"]["tier"] == "live"
    assert by_id["grafana"]["tier"] == "metrics"


def test_mark_container_current_clears_pending(tmp_path, monkeypatch):
    import types
    sys.modules.setdefault(
        "croniter",
        types.SimpleNamespace(croniter=lambda *a, **k: None),
    )
    from app import scheduler

    state = tmp_path / "helm-jobs.json"
    state.write_text(json.dumps({
        "updates": {
            "ok": True,
            "pending": ["cadvisor", "sonarr"],
            "containers": [
                {"id": "cadvisor", "update_available": True,
                 "local_digest": "aaa", "remote_digest": "bbb"},
                {"id": "sonarr", "update_available": True,
                 "local_digest": "ccc", "remote_digest": "ddd"},
            ],
            "report": "",
        }
    }))
    monkeypatch.setattr(scheduler, "STATE", state)
    scheduler.mark_container_current("cadvisor")
    data = json.loads(state.read_text())
    by_id = {c["id"]: c for c in data["updates"]["containers"]}
    assert by_id["cadvisor"]["update_available"] is False
    assert by_id["cadvisor"]["local_digest"] == "bbb"
    assert data["updates"]["pending"] == ["sonarr"]


def test_parse_check_output_reads_ndjson_result_and_ignores_progress():
    blob = "\n".join([
        json.dumps({"type": "progress", "current": 1, "total": 2, "id": "sonarr"}),
        json.dumps({"type": "progress", "current": 2, "total": 2, "id": "radarr"}),
        json.dumps({
            "type": "result",
            "rows": [
                {"id": "sonarr", "update_available": False},
                {"id": "radarr", "update_available": True},
            ],
        }),
    ])
    rows = updates_info.parse_check_output(blob)
    assert [r["id"] for r in rows] == ["sonarr", "radarr"]
    assert rows[1]["update_available"] is True


def test_parse_check_output_still_accepts_legacy_json_array():
    blob = json.dumps([
        {"id": "sonarr", "update_available": False},
        {"id": "radarr", "update_available": True},
    ])
    rows = updates_info.parse_check_output(blob)
    assert [r["id"] for r in rows] == ["sonarr", "radarr"]


def test_parse_apply_line_reads_step_fractions():
    snap = updates_info.parse_apply_line("1/4 Snapshotting config before updating sonarr...")
    assert snap == {"step": 1, "steps": 4, "pct": 25, "message": "Snapshotting config before updating sonarr..."}
    pull = updates_info.parse_apply_line("2/4 Pulling sonarr image...")
    assert pull["step"] == 2 and pull["pct"] == 50
    heal = updates_info.parse_apply_line("3c/4 Healing tunnel orphans (if any)...")
    assert heal["step"] == 3 and heal["pct"] == 75
    wait = updates_info.parse_apply_line("4/4 Waiting up to 90s for sonarr to come back healthy...")
    assert wait["step"] == 4 and wait["pct"] == 90
    assert updates_info.parse_apply_line("OK: sonarr healthy on the new image") is None


def test_progress_snapshot_tracks_check_and_apply():
    updates_info.clear_progress()
    assert updates_info.progress()["busy"] is False
    updates_info.set_check_progress(current=3, total=12, app_id="prowlarr")
    snap = updates_info.progress()
    assert snap["busy"] is True
    assert snap["kind"] == "check"
    assert snap["current"] == 3
    assert snap["total"] == 12
    assert snap["id"] == "prowlarr"
    assert snap["pct"] == 25
    updates_info.clear_progress()
    updates_info.set_apply_progress(app_id="sonarr", step=2, steps=4, message="Pulling sonarr image...")
    snap = updates_info.progress()
    assert snap["kind"] == "apply"
    assert snap["id"] == "sonarr"
    assert snap["pct"] == 50
    assert "Pulling" in snap["message"]
    updates_info.clear_progress()
    assert updates_info.progress()["busy"] is False


def test_progress_tracks_parallel_applies():
    updates_info.clear_progress()
    updates_info.set_apply_progress(app_id="sonarr", step=1, steps=4, message="Snapshotting...")
    updates_info.set_apply_progress(app_id="dispatcharr", step=2, steps=4, message="Pulling...")
    snap = updates_info.progress()
    assert snap["busy"] is True
    assert set(snap["apps"]) == {"sonarr", "dispatcharr"}
    assert snap["apps"]["sonarr"]["pct"] == 25
    assert snap["apps"]["dispatcharr"]["pct"] == 50
    updates_info.clear_progress("sonarr")
    snap = updates_info.progress()
    assert "sonarr" not in snap["apps"]
    assert "dispatcharr" in snap["apps"]
    assert snap["busy"] is True
    updates_info.clear_progress("dispatcharr")
    idle = updates_info.progress()
    assert idle["busy"] is False
    assert idle["apps"] == {}


def test_parse_running_json_lines():
    ps = "\n".join([
        json.dumps({"Name": "kine-sonarr", "State": "running"}),
        json.dumps({"Name": "kine-bazarr", "State": "exited"}),
    ])
    assert updates_info.parse_running(ps) == {"sonarr"}


def test_provision_lock_serialises(tmp_path, monkeypatch):
    lock = tmp_path / "provision.lock"
    monkeypatch.setattr(provision_lock, "LOCK_PATH", lock)

    async def _run():
        import asyncio

        started = asyncio.Event()

        async def hold():
            async with provision_lock.acquire(reason="test-a"):
                started.set()
                await asyncio.sleep(0.05)

        async def collide():
            await started.wait()
            with pytest.raises(provision_lock.ProvisionBusy):
                async with provision_lock.acquire(reason="test-b"):
                    pass

        await asyncio.gather(hold(), collide())
        assert provision_lock.status()["busy"] is False

    import asyncio
    asyncio.run(_run())
