"""Background jobs.

Two of them, and both are deliberately conservative:

  update-check   resolves image digests and records what has moved.
                 It never pulls and never recreates anything. The
                 decision to apply an update stays with a human,
                 because an unattended update on a media stack breaks
                 things overnight, mid-import, while nobody is watching.

  backup         a scheduled config snapshot, so the rollback path the
                 updater depends on is never more than a day stale.

  seerr-wire     re-runs provision wire when Seerr's wizard has finished
                 but Sonarr/Radarr are not linked yet (enable runs wire
                 before Sign In, so the first link needs a later pass).
"""
import asyncio
import contextlib
import json
import pathlib
import time
from datetime import datetime, timezone

import httpx
from croniter import croniter

from . import compose, config, metrics, provision_lock, updates_info

STATE = pathlib.Path("/stack/helm-jobs.json")
SEERR_SETTINGS = pathlib.Path("/stack/config/seerr/settings.json")


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    with contextlib.suppress(OSError):
        STATE.write_text(json.dumps(data, indent=2))


def _next(expr: str) -> float:
    return croniter(expr, datetime.now(timezone.utc)).get_next(float)


async def _update_check() -> None:
    payload = await updates_info.fetch(compose, refresh=True)
    data = _load()
    data["updates"] = {
        "checked": payload["checked"],
        "ok": payload["ok"],
        "pending": payload["pending"],
        "report": payload["report"],
        "containers": payload["containers"],
    }
    _save(data)


async def _backup() -> None:
    code, out = await compose.script("backup.sh", timeout=1800)
    data = _load()
    data["backup"] = {
        "ran": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": code == 0,
        "path": out.strip().splitlines()[-1] if code == 0 and out.strip() else None,
    }
    _save(data)


def _seerr_api_key() -> str | None:
    if not SEERR_SETTINGS.is_file():
        return None
    try:
        return json.loads(SEERR_SETTINGS.read_text()).get("main", {}).get("apiKey") or None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


async def _seerr_needs_wire() -> bool:
    if "seerr" not in config.profiles():
        return False
    api_key = _seerr_api_key()
    if not api_key:
        return False
    headers = {"X-Api-Key": api_key}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            me = await client.get("http://seerr:5055/api/v1/auth/me", headers=headers)
            if me.status_code != 200:
                return False
            for path in ("radarr", "sonarr"):
                resp = await client.get(
                    f"http://seerr:5055/api/v1/settings/{path}",
                    headers=headers,
                )
                if resp.status_code != 200 or not resp.json():
                    return True
    except httpx.HTTPError:
        return False
    return False


async def wire_seerr_if_ready() -> None:
    if not await _seerr_needs_wire():
        return
    if provision_lock.status().get("busy"):
        return
    try:
        async with provision_lock.acquire(reason="seerr auto-wire"):
            code, out = await compose.run("run", "--rm", "provision", "wire", timeout=900)
    except provision_lock.ProvisionBusy:
        return
    data = _load()
    data["seerr_wire"] = {
        "ran": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": code == 0,
        "log": out[-500:] if out else "",
    }
    _save(data)


async def _seerr_wire_loop() -> None:
    await asyncio.sleep(45)
    while True:
        try:
            await wire_seerr_if_ready()
        except Exception as exc:  # noqa: BLE001 — must not kill the loop
            data = _load()
            data.setdefault("errors", {})["seerr_wire"] = str(exc)
            _save(data)
        await asyncio.sleep(120)


async def _loop(name: str, cron_key: str, default: str, job) -> None:
    while True:
        expr = config.read().get(cron_key, default) or default
        try:
            delay = max(30.0, _next(expr) - time.time())
        except (ValueError, KeyError):
            # A malformed cron expression should not silently disable
            # the job or spin the loop. Fall back and say so in state.
            data = _load()
            data.setdefault("errors", {})[name] = f"invalid cron {expr!r}, using {default!r}"
            _save(data)
            delay = max(30.0, _next(default) - time.time())
        await asyncio.sleep(delay)
        try:
            await job()
        except Exception as exc:  # a failed job must not kill the loop
            data = _load()
            data.setdefault("errors", {})[name] = str(exc)
            _save(data)


def start(app) -> None:
    app.state.jobs = [
        asyncio.create_task(
            _loop("updates", "HELM_UPDATE_CHECK_CRON", "0 4 * * *", _update_check)
        ),
        asyncio.create_task(
            _loop("backup", "HELM_BACKUP_CRON", "30 3 * * *", _backup)
        ),
        asyncio.create_task(_seerr_wire_loop()),
        asyncio.create_task(metrics.collector_loop()),
    ]


async def stop(app) -> None:
    for task in getattr(app.state, "jobs", []):
        task.cancel()
    for task in getattr(app.state, "jobs", []):
        with contextlib.suppress(asyncio.CancelledError):
            await task


def status() -> dict:
    return _load()
