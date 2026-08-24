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
