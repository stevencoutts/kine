"""Single-flight guard for provision seed/wire runs.

Overlapping `docker compose run provision` jobs were taking down tunnelled
apps mid-wire and leaving Sonarr/Bazarr stopped. Helm serialises every
seed/wire sequence behind one file lock on the stack volume.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOCK_PATH = Path("/stack/provision.lock")


class ProvisionBusy(Exception):
    def __init__(self, detail: str = "Provisioning already in progress"):
        self.detail = detail
        super().__init__(detail)


def _read_meta() -> dict:
    try:
        return json.loads(LOCK_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def status() -> dict:
    """Return whether a provision run holds the lock."""
    if not LOCK_PATH.is_file():
        return {"busy": False}
    meta = _read_meta()
    fd = os.open(LOCK_PATH, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return {"busy": False}
        except BlockingIOError:
            return {"busy": True, **meta}
    finally:
        os.close(fd)


@contextlib.asynccontextmanager
async def acquire(*, reason: str = "wire"):
    """Hold the provision lock for a seed/wire sequence."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
        "pid": os.getpid(),
    }
    LOCK_PATH.write_text(json.dumps(meta))
    fd = os.open(LOCK_PATH, os.O_RDWR)
    try:
        try:
            await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            held = _read_meta()
            who = held.get("reason") or "another operation"
            raise ProvisionBusy(f"Provisioning already in progress ({who})") from exc
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
            LOCK_PATH.unlink(missing_ok=True)
