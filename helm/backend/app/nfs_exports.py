"""List NFS exports for the Helm Settings browse UI.

Uses ``showmount -e`` when available (nfs-common in the Helm image).
Mounting remains a host-side concern via ``scripts/mount-media.sh``.
"""
from __future__ import annotations

import re
import shutil
import subprocess

_SERVER_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_EXPORT_RE = re.compile(r"^(/\S*)")


def validate_server(server: str) -> str:
    server = (server or "").strip()
    if not server:
        raise ValueError("NFS server is required")
    if not _SERVER_RE.match(server) or ".." in server:
        raise ValueError("invalid NFS server hostname")
    return server


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
