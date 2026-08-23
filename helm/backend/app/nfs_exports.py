"""NFS export listing and subfolder browsing for the Helm Settings UI.

Top-level exports come from ``showmount -e``. Subfolders are read by
temporarily mounting the export root read-only. On Docker Desktop the
in-container source IP is not the Mac's LAN address, so mounts are
denied even when the Mac is allowlisted — Helm then falls back to the
host browse agent (``scripts/nfs-browse-agent.py``) when configured.
"""
from __future__ import annotations

import json
import os
import pathlib
import posixpath
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

_SERVER_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_EXPORT_RE = re.compile(r"^(/\S*)\s*(.*)$")
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
    return [path for path, _clients in parse_showmount_rows(output)]


def parse_showmount_rows(output: str) -> list[tuple[str, str]]:
    """Parse ``showmount -e`` into ``(export_path, clients)`` rows."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("export list for"):
            continue
        m = _EXPORT_RE.match(line)
        if not m:
            continue
        path = m.group(1)
        clients = (m.group(2) or "").strip()
        if path in seen:
            continue
        seen.add(path)
        rows.append((path, clients))
    return rows


def export_label(path: str) -> str:
    """Human label for an export path.

    UniFi Drive exports end in ``/.data``; use the parent folder name
    (``media``, ``Downloads``) so the picker is readable.
    """
    parts = [p for p in path.rstrip("/").split("/") if p]
    if not parts:
        return path or "/"
    if len(parts) >= 2 and parts[-1] == ".data":
        return parts[-2]
    return parts[-1]


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


def _showmount(server: str, timeout: float = 10.0) -> str:
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
    if not parse_showmount(proc.stdout):
        raise RuntimeError(f"no exports advertised by {host}")
    return proc.stdout


def list_exports(server: str, timeout: float = 10.0) -> list[str]:
    """Return export paths advertised by ``server``."""
    return parse_showmount(_showmount(server, timeout=timeout))


def list_export_rows(server: str, timeout: float = 10.0) -> list[tuple[str, str]]:
    """Return ``(export_path, clients)`` rows from ``showmount -e``."""
    return parse_showmount_rows(_showmount(server, timeout=timeout))


@contextmanager
def _nfs_mount(server: str, export: str, clients: str = ""):
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
            raise RuntimeError(_mount_error(spec, last_error, clients))
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


def _mount_error(spec: str, last_error: str, clients: str) -> str:
    denied = "access denied" in last_error.lower() or "permission denied" in last_error.lower()
    if denied:
        allowed = f" Currently allowed clients: {clients}." if clients else ""
        return (
            f"NFS server denied mount for {spec}.{allowed} "
            "If this Mac's IP is already listed, that is expected on Docker "
            "Desktop: Helm's NFS traffic does not come from your Mac IP. "
            "Start the host browse agent with "
            "`sudo ./kine nfs-agent`, or allow your LAN subnet on the share, "
            "or append a subfolder name manually in Settings."
        )
    return (
        f"could not mount {spec} for browsing ({last_error}). "
        "Recreate the helm container after updating compose (SYS_ADMIN)."
    )


def _agent_url() -> str:
    return (os.environ.get("NFS_BROWSE_AGENT") or "").rstrip("/")


def _agent_token() -> str:
    return os.environ.get("KINE_SECRET") or os.environ.get("NFS_BROWSE_TOKEN") or ""


def browse_via_agent(server: str, path: str = "", timeout: float = 15.0) -> dict | None:
    """Return browse payload from the host agent, or None if unavailable."""
    base = _agent_url()
    token = _agent_token()
    if not base or not token:
        return None
    query = urllib.parse.urlencode({"server": server, "path": path or ""})
    req = urllib.request.Request(
        f"{base}/browse?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(str(detail) or f"host agent HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "entries" not in data:
        return None
    return data


def browse(server: str, path: str = "", timeout: float = 10.0) -> dict:
    """Browse exports or subfolders on ``server``.

    Prefers the host browse agent when ``NFS_BROWSE_AGENT`` is set (Docker
    Desktop). Otherwise mounts inside Helm.
    """
    host = validate_server(server)
    path = validate_export_path(path)

    via_agent = browse_via_agent(host, path, timeout=max(timeout, 15.0))
    if via_agent is not None:
        return via_agent

    rows = list_export_rows(host, timeout=timeout)
    exports = [export for export, _clients in rows]
    clients_by_export = dict(rows)

    if not path:
        return {
            "server": host,
            "path": "",
            "parent": None,
            "entries": [
                {
                    "name": export_label(export),
                    "path": export,
                    "detail": export,
                    "kind": "dir",
                }
                for export in sorted(exports)
            ],
        }

    root = export_root_for(path, exports)
    rel = path[len(root) :].lstrip("/")

    with _nfs_mount(host, root, clients=clients_by_export.get(root, "")) as mount_point:
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
