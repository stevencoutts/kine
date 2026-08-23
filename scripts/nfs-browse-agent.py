#!/usr/bin/env python3
"""Host-side NFS browse agent for Helm on Docker Desktop.

Helm runs inside a VM whose source IP is not your Mac's LAN address, so
UniFi/NFS deny in-container mounts even when this Mac is allowlisted.
This agent mounts from the host (your Mac/Linux box) and returns folder
listings over HTTP on 127.0.0.1:8611.

Run (needs root for mount):
  sudo ./scripts/nfs-browse-agent.py

Helm reaches it via host.docker.internal (see NFS_BROWSE_AGENT).
"""
from __future__ import annotations

import json
import os
import pathlib
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("NFS_BROWSE_PORT", "8611"))
_TOKEN = ""
_PATH_RE = re.compile(r"^/[^\0]*$")
_SERVER_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def load_token() -> str:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        raise SystemExit(f"missing {env_path}; run install.sh first")
    for line in env_path.read_text().splitlines():
        if line.startswith("KINE_SECRET="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("KINE_SECRET not set in .env")


def validate_server(server: str) -> str:
    server = (server or "").strip()
    if not server or not _SERVER_RE.match(server) or ".." in server:
        raise ValueError("invalid NFS server")
    return server


def validate_path(path: str) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    if not path.startswith("/") or ".." in path or not _PATH_RE.match(path):
        raise ValueError("invalid path")
    return path


def showmount_exports(server: str) -> list[str]:
    binary = shutil.which("showmount")
    if not binary:
        raise RuntimeError("showmount not found (install nfs client tools)")
    proc = subprocess.run(
        [binary, "-e", server],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "showmount failed").strip())
    exports: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("export list for"):
            continue
        path = line.split()[0]
        if path.startswith("/") and path not in exports:
            exports.append(path)
    if not exports:
        raise RuntimeError(f"no exports on {server}")
    return exports


def export_root_for(path: str, exports: list[str]) -> str:
    matches = [
        export
        for export in exports
        if path == export or path.startswith(export.rstrip("/") + "/")
    ]
    if not matches:
        raise ValueError(f"path {path!r} is not under any advertised export")
    return max(matches, key=len)


def export_label(path: str) -> str:
    parts = [p for p in path.rstrip("/").split("/") if p]
    if len(parts) >= 2 and parts[-1] == ".data":
        return parts[-2]
    return parts[-1] if parts else path


def mount_export(server: str, export: str, mount_point: pathlib.Path) -> None:
    spec = f"{server}:{export}"
    if platform.system() == "Darwin":
        cmd = [
            "mount_nfs",
            "-o",
            "ro,soft,nolocks,intr",
            spec,
            str(mount_point),
        ]
    else:
        mount = shutil.which("mount")
        cmd = [
            mount,
            "-t",
            "nfs",
            "-o",
            "ro,soft,timeo=10,retrans=2,nolock,nfsvers=3,tcp",
            spec,
            str(mount_point),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "mount failed").strip()
        raise RuntimeError(detail)


def unmount(mount_point: pathlib.Path) -> None:
    if platform.system() == "Darwin":
        subprocess.run(
            ["umount", str(mount_point)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    else:
        subprocess.run(
            ["umount", str(mount_point)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


def browse(server: str, path: str) -> dict:
    host = validate_server(server)
    path = validate_path(path)
    exports = showmount_exports(host)
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
            "via": "host-agent",
        }

    root = export_root_for(path, exports)
    rel = path[len(root) :].lstrip("/")
    mount_point = pathlib.Path(tempfile.mkdtemp(prefix="kine-nfs-agent-"))
    try:
        mount_export(host, root, mount_point)
        target = mount_point.joinpath(*rel.split("/")) if rel else mount_point
        if not target.is_dir():
            raise RuntimeError(f"not a directory: {path}")
        entries = []
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
        parent = "" if path == root else posixpath.normpath(posixpath.join(path, ".."))
        if parent == path:
            parent = ""
        return {
            "server": host,
            "path": path,
            "parent": parent if path != root else "",
            "entries": entries,
            "via": "host-agent",
        }
    finally:
        unmount(mount_point)
        try:
            mount_point.rmdir()
        except OSError:
            pass


import os  # noqa: E402  — used by browse()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"detail":"unauthorized"}')

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {_TOKEN}":
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/health"):
            self._send(200, {"ok": True, "service": "kine-nfs-browse-agent"})
            return
        if parsed.path != "/browse":
            self._send(404, {"detail": "not found"})
            return
        qs = parse_qs(parsed.query)
        server = (qs.get("server") or [""])[0]
        path = (qs.get("path") or [""])[0]
        try:
            self._send(200, browse(server, path))
        except ValueError as exc:
            self._send(400, {"detail": str(exc)})
        except RuntimeError as exc:
            self._send(502, {"detail": str(exc)})


def main() -> None:
    global _TOKEN
    if os.geteuid() != 0:
        raise SystemExit("run with sudo so NFS mounts are allowed on this host")
    _TOKEN = load_token()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"kine nfs-browse-agent listening on 127.0.0.1:{PORT}", flush=True)
    print("Helm reaches this via host.docker.internal (Docker Desktop).", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()
