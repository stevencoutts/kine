#!/usr/bin/env python3
"""Host-side NFS browse agent for Helm.

Helm in Docker cannot browse NFS using the host LAN IP. This agent runs on
the host network and reads folders from existing host mounts under
DATA_ROOT when possible, falling back to a temporary read-only NFS mount.
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
    token = (os.environ.get("KINE_SECRET") or os.environ.get("NFS_BROWSE_TOKEN") or "").strip()
    if token:
        return token
    env_path = ROOT / ".env"
    if not env_path.is_file():
        raise SystemExit(f"missing {env_path}; run install.sh first")
    for line in env_path.read_text().splitlines():
        if line.startswith("KINE_SECRET="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("KINE_SECRET not set in .env")


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("DATA_ROOT", "NFS_SERVER", "NFS_TV", "NFS_MOVIES", "NFS_DOWNLOADS", "NFS_CACHE"):
        value = os.environ.get(key, "").strip()
        if value:
            out[key] = value
    env_path = ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in out:
                out[key] = value.strip().strip('"').strip("'")
    return out


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


def configured_mounts(server: str) -> list[tuple[str, pathlib.Path]]:
    """Known server:export -> local paths from .env and mount-media.sh layout."""
    env = load_env()
    if env.get("NFS_SERVER", "") != server:
        return []
    data_root = pathlib.Path(env.get("DATA_ROOT", "/srv/media-data"))
    pairs = [
        (env.get("NFS_TV", ""), data_root / "media" / "tv"),
        (env.get("NFS_MOVIES", ""), data_root / "media" / "movies"),
        (env.get("NFS_DOWNLOADS", ""), data_root / "downloads"),
        (env.get("NFS_CACHE", ""), data_root / "cache" / "tdarr"),
    ]
    out: list[tuple[str, pathlib.Path]] = []
    for export, local in pairs:
        export = export.strip()
        if export and local.is_dir():
            out.append((export, local))
    return out


def local_dir_for(server: str, nfs_path: str) -> pathlib.Path | None:
    """Map an NFS path to a readable directory on this host, if already mounted."""
    nfs_path = nfs_path.rstrip("/")
    best: pathlib.Path | None = None
    best_len = -1
    for export, local in configured_mounts(server):
        export = export.rstrip("/")
        if nfs_path == export:
            if len(export) > best_len:
                best = local
                best_len = len(export)
        elif nfs_path.startswith(export + "/"):
            rel = nfs_path[len(export) :].lstrip("/")
            candidate = local.joinpath(*rel.split("/")) if rel else local
            if len(export) > best_len and candidate.is_dir():
                best = candidate
                best_len = len(export)
    return best


def discover_local_entries(server: str, export_root: str) -> list[dict]:
    """List folders visible under DATA_ROOT on the host (existing NFS mounts)."""
    env = load_env()
    nfs_server = env.get("NFS_SERVER", "")
    if nfs_server and nfs_server != server:
        return []
    data_root = pathlib.Path(env.get("DATA_ROOT", "/srv/media-data"))
    if not data_root.is_dir():
        return []
    root = export_root.rstrip("/")
    entries: list[dict] = []
    seen: set[str] = set()

    def add(name: str, rel: str) -> None:
        if name in seen:
            return
        local = data_root / rel
        if local.is_dir():
            seen.add(name)
            entries.append({"name": name, "path": f"{root}/{rel}", "kind": "dir"})

    add("tv", "media/tv")
    add("movies", "media/movies")
    add("downloads", "downloads")
    add("cache", "cache/tdarr")
    media = data_root / "media"
    if media.is_dir():
        for ent in sorted(os.scandir(media), key=lambda item: item.name.lower()):
            if ent.is_dir() and ent.name not in seen:
                seen.add(ent.name)
                entries.append(
                    {
                        "name": ent.name,
                        "path": f"{root}/media/{ent.name}",
                        "kind": "dir",
                    }
                )
    return entries


def virtual_export_entries(server: str, export_root: str) -> list[dict]:
    """Infer subfolders under an export root from configured host mounts."""
    prefix = export_root.rstrip("/") + "/"
    seen: dict[str, str] = {}
    for export, _local in configured_mounts(server):
        export = export.rstrip("/")
        if export == export_root.rstrip("/"):
            continue
        if not export.startswith(prefix):
            continue
        rest = export[len(prefix) :]
        name = rest.split("/")[0]
        if name:
            seen[name] = f"{export_root.rstrip('/')}/{name}"
    return [
        {"name": name, "path": path, "kind": "dir"}
        for name, path in sorted(seen.items(), key=lambda item: item[0].lower())
    ]


def list_dir_entries(local_dir: pathlib.Path, nfs_path: str, root: str, rel: str) -> list[dict]:
    entries = []
    for ent in sorted(os.scandir(local_dir), key=lambda item: item.name.lower()):
        if ent.is_dir(follow_symlinks=False):
            subpath = (
                f"{root}/{ent.name}"
                if not rel
                else f"{nfs_path.rstrip('/')}/{ent.name}"
            )
            entries.append(
                {
                    "name": ent.name,
                    "path": posixpath.normpath(subpath),
                    "kind": "dir",
                }
            )
    return entries


def mount_export(server: str, export: str, mount_point: pathlib.Path) -> None:
    spec = f"{server}:{export}"
    last_error = "mount failed"
    if platform.system() == "Darwin":
        cmd = [
            "mount_nfs",
            "-o",
            "ro,soft,nolocks,intr",
            spec,
            str(mount_point),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        if proc.returncode == 0:
            return
        last_error = (proc.stderr or proc.stdout or last_error).strip()
    else:
        mount = shutil.which("mount")
        opts_base = ["ro", "soft", "timeo=10", "retrans=2", "nolock", "tcp"]
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
                return
            last_error = (proc.stderr or proc.stdout or last_error).strip()
    raise RuntimeError(last_error)


def unmount(mount_point: pathlib.Path) -> None:
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
    via = "host-mount"

    local = local_dir_for(host, path)
    if not local and rel:
        # e.g. NFS path .../media/TV maps to DATA_ROOT/media/tv on the host
        env = load_env()
        data_root = pathlib.Path(env.get("DATA_ROOT", "/srv/media-data"))
        suffix = path[len(root) :].lstrip("/")
        if suffix:
            candidate = data_root / suffix
            if candidate.is_dir():
                local = candidate
                via = "host-mount"
    if local and local.is_dir():
        entries = list_dir_entries(local, path, root, rel)
    elif not rel:
        entries = virtual_export_entries(host, root) or discover_local_entries(host, root)
        via = "host-mount-index"
        if not entries:
            local = local_dir_for(host, root)
            if local and local.is_dir():
                entries = list_dir_entries(local, path, root, rel)
                via = "host-mount"
            else:
                via = "host-agent"
                mount_point = pathlib.Path(tempfile.mkdtemp(prefix="kine-nfs-agent-"))
                try:
                    mount_export(host, root, mount_point)
                    entries = list_dir_entries(mount_point, path, root, rel)
                finally:
                    unmount(mount_point)
                    try:
                        mount_point.rmdir()
                    except OSError:
                        pass
    else:
        via = "host-agent"
        mount_point = pathlib.Path(tempfile.mkdtemp(prefix="kine-nfs-agent-"))
        try:
            mount_export(host, root, mount_point)
            target = mount_point.joinpath(*rel.split("/")) if rel else mount_point
            if not target.is_dir():
                raise RuntimeError(f"not a directory: {path}")
            entries = list_dir_entries(target, path, root, rel)
        finally:
            unmount(mount_point)
            try:
                mount_point.rmdir()
            except OSError:
                pass

    parent = "" if path == root else posixpath.normpath(posixpath.join(path, ".."))
    if parent == path:
        parent = ""
    return {
        "server": host,
        "path": path,
        "parent": parent if path != root else "",
        "entries": entries,
        "via": via,
    }


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


def bind_host() -> str:
    explicit = os.environ.get("NFS_BROWSE_BIND", "").strip()
    if explicit:
        return explicit
    return "127.0.0.1" if platform.system() == "Darwin" else "0.0.0.0"


def main() -> None:
    global _TOKEN
    if os.geteuid() != 0:
        raise SystemExit("run with sudo so NFS mounts are allowed on this host")
    _TOKEN = load_token()
    host = bind_host()
    server = ThreadingHTTPServer((host, PORT), Handler)
    print(f"kine nfs-browse-agent listening on {host}:{PORT}", flush=True)
    if host == "127.0.0.1":
        print("Helm reaches this via host.docker.internal (Docker Desktop).", flush=True)
    else:
        print(
            "Helm reaches this via NFS_BROWSE_AGENT "
            "(typically http://host.docker.internal:8611).",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()
