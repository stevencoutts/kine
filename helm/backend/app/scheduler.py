"""Background jobs.

Two of them, and both are deliberately conservative:

  update-check   resolves image digests and records what has moved.
                 It never pulls and never recreates anything. The
                 decision to apply an update stays with a human,
                 because an unattended update on a media stack breaks
                 things overnight, mid-import, while nobody is watching.

  backup         a scheduled config snapshot, so the rollback path the
                 updater depends on is never more than a day stale.
"""
import asyncio
import contextlib
import json
import pathlib
import time
from datetime import datetime, timezone

from croniter import croniter

from . import compose, config

STATE = pathlib.Path("/stack/helm-jobs.json")


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
    code, out = await compose.script("updates.sh", "check", timeout=600)
    pending = [
        line.split()[0]
        for line in out.splitlines()
        if " UPDATE " in f" {line} "
    ]
    data = _load()
    data["updates"] = {
        "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": code == 0,
        "pending": pending,
        "report": out,
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
    ]


async def stop(app) -> None:
    for task in getattr(app.state, "jobs", []):
        task.cancel()
    for task in getattr(app.state, "jobs", []):
        with contextlib.suppress(asyncio.CancelledError):
            await task


def status() -> dict:
    return _load()
