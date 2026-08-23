"""NFS export listing and subfolder browsing for the Helm Settings UI.

Top-level exports come from ``showmount -e``. Subfolders are read by
temporarily mounting the export root inside Helm (read-only) and listing
directories. Host-side mounting remains ``scripts/mount-media.sh``.
"""
from __future__ import annotations

import os
import pathlib
import posixpath
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager

_SERVER_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_EXPORT_RE = re.compile(r"^(/\S*)")
_PATH_RE = re.compile(r"^/[^\0]*$")


def validate_server(server: str) -> str:
    server = (server or "").strip()
    if not server:
        raise ValueError("NFS server is required")
    if not _SERVER_RE.match(server) or ".." in server:
        raise ValueError("invalid NFS server hostname")
    return server


def validate_export_path(path: str) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    if not path.startswith("/") or ".." in path or not _PATH_RE.match(path):
        raise ValueError("invalid NFS export path")
    return path


def parse_showmount(output: str) -> list[str]:
    """Parse ``showmount -e`` text into export path strings."""
    exports: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("export list for"):
            continue
        m = _EXPORT_RE.match(line)
        if not m:
            continue
        path = m.group(1)
        if path not in exports:
            exports.append(path)
    return exports


def export_root_for(path: str, exports: list[str]) -> str:
    """Return the longest advertised export prefix for ``path``."""
    matches = [
        export
        for export in exports
        if path == export or path.startswith(export.rstrip("/") + "/")
    ]
    if not matches:
        raise ValueError(f"path {path!r} is not under any advertised export")
    return max(matches, key=len)


def parent_path(path: str, exports: list[str]) -> str | None:
    """Parent directory on the NFS server, or ``""`` for the export list."""
    if not path:
        return None
    root = export_root_for(path, exports)
    if path == root:
        return ""
    parent = posixpath.normpath(posixpath.join(path, ".."))
    if parent == path:
        return ""
    if parent == root or parent.startswith(root.rstrip("/") + "/"):
        return parent
    return root


def list_exports(server: str, timeout: float = 10.0) -> list[str]:
    """Return export paths advertised by ``server``.

    Raises ValueError for bad input and RuntimeError when showmount is
    missing or the probe fails.
    """
    host = validate_server(server)
    binary = shutil.which("showmount")
    if not binary:
        raise RuntimeError(
            "showmount is not available in Helm; rebuild the helm image "
            "with nfs-common installed"
        )
    try:
        proc = subprocess.run(
            [binary, "-e", host],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"timed out listing exports on {host}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "showmount failed").strip()
        raise RuntimeError(detail)
    exports = parse_showmount(proc.stdout)
    if not exports:
        raise RuntimeError(f"no exports advertised by {host}")
    return exports


@contextmanager
def _nfs_mount(server: str, export: str):
    mount = shutil.which("mount")
    umount = shutil.which("umount")
    if not mount or not umount:
        raise RuntimeError("mount/umount are not available in Helm")

    mount_point = pathlib.Path(tempfile.mkdtemp(prefix="kine-nfs-browse-"))
    spec = f"{server}:{export}"
    opts_base = ["ro", "soft", "timeo=10", "retrans=2", "nolock"]
    last_error = "mount failed"
    mounted = False
    try:
        for vers in ("4", "3"):
            proc = subprocess.run(
                [
                    mount,
                    "-t",
                    "nfs",
                    "-o",
                    ",".join([*opts_base, f"nfsvers={vers}"]),
                    spec,
                    str(mount_point),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0:
                mounted = True
                break
            last_error = (proc.stderr or proc.stdout or last_error).strip()
        if not mounted:
            raise RuntimeError(
                f"could not mount {spec} for browsing ({last_error}). "
                "Recreate the helm container after updating compose (SYS_ADMIN)."
            )
        yield mount_point
    finally:
        if mounted:
            subprocess.run(
                [umount, str(mount_point)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        if mount_point.exists():
            try:
                mount_point.rmdir()
            except OSError:
                pass


def browse(server: str, path: str = "", timeout: float = 10.0) -> dict:
    """Browse exports or subfolders on ``server``.

    When ``path`` is empty, returns advertised exports. Otherwise mounts
    the export root read-only and lists child directories at ``path``.
    """
    host = validate_server(server)
    path = validate_export_path(path)
    exports = list_exports(host, timeout=timeout)

    if not path:
        return {
            "server": host,
            "path": "",
            "parent": None,
            "entries": [
                {"name": _basename(export) or export, "path": export, "kind": "dir"}
                for export in sorted(exports)
            ],
        }

    root = export_root_for(path, exports)
    rel = path[len(root) :].lstrip("/")

    with _nfs_mount(host, root) as mount_point:
        target = mount_point.joinpath(*rel.split("/")) if rel else mount_point
        if not target.is_dir():
            raise RuntimeError(f"not a directory: {path}")
        entries = []
        try:
            for ent in sorted(os.scandir(target), key=lambda item: item.name.lower()):
                if ent.is_dir(follow_symlinks=False):
                    subpath = (
                        f"{root}/{ent.name}"
                        if not rel
                        else f"{path.rstrip('/')}/{ent.name}"
                    )
                    entries.append(
                        {
                            "name": ent.name,
                            "path": posixpath.normpath(subpath),
                            "kind": "dir",
                        }
                    )
        except PermissionError as exc:
            raise RuntimeError(f"permission denied reading {path}") from exc

    return {
        "server": host,
        "path": path,
        "parent": parent_path(path, exports),
        "entries": entries,
    }


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]
