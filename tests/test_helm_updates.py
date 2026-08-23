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


def test_pending_ids():
    rows = updates_info.parse_report(SAMPLE_REPORT)
    assert updates_info.pending_ids(rows) == ["radarr"]


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
