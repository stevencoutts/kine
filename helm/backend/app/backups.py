"""List and validate Kine config snapshots under STACK_ROOT/backups."""
from __future__ import annotations

import os
import pathlib
import re
from datetime import datetime, timezone

from . import config

NAME_RE = re.compile(r"^kine-\d{8}-\d{6}\.tar\.gz$")


def backups_dir() -> pathlib.Path:
    env = config.read()
    root = env.get("STACK_ROOT") or os.environ.get("STACK_ROOT") or "/stack"
    return pathlib.Path(root) / "backups"


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("Invalid backup name")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("Invalid backup name")
    return name


def resolve(name: str) -> pathlib.Path:
    """Return an existing snapshot path under backups/, or raise."""
    safe = validate_name(name)
    path = (backups_dir() / safe).resolve()
    root = backups_dir().resolve()
    if not str(path).startswith(str(root) + os.sep) and path != root:
        raise ValueError("Invalid backup path")
    if not path.is_file():
        raise FileNotFoundError(f"Backup not found: {safe}")
    return path


def list_snapshots() -> list[dict]:
    """Newest-first list of local snapshots."""
    root = backups_dir()
    if not root.is_dir():
        return []
    rows: list[dict] = []
    for path in root.glob("kine-*.tar.gz"):
        if not path.is_file() or not NAME_RE.match(path.name):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        created = None
        m = re.match(r"^kine-(\d{8})-(\d{6})\.tar\.gz$", path.name)
        if m:
            try:
                created = datetime.strptime(
                    m.group(1) + m.group(2), "%Y%m%d%H%M%S"
                ).replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
            except ValueError:
                created = None
        rows.append({
            "name": path.name,
            "path": str(path),
            "size_bytes": st.st_size,
            "mtime": mtime.isoformat(timespec="seconds"),
            "created": created or mtime.isoformat(timespec="seconds"),
        })
    rows.sort(key=lambda r: r["name"], reverse=True)
    return rows
