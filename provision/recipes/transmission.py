"""Align Transmission download paths with the shared /data mount.

Linuxserver's image ships with /downloads/* defaults, but Sonarr and
Radarr see the same files at /data/downloads/*. Both must agree or
Sonarr's health check fails and imports copy instead of hardlink.
"""
import json
import pathlib

import httpx

RPC = "http://gluetun:9091/transmission/rpc"
SETTINGS = pathlib.Path("/stack/config/transmission/settings.json")
DOWNLOAD_DIR = "/data/downloads/complete"
INCOMPLETE_DIR = "/data/downloads/incomplete"
WANTED = {
    "download-dir": DOWNLOAD_DIR,
    "incomplete-dir": INCOMPLETE_DIR,
    "incomplete-dir-enabled": True,
}


def _persist_settings(log) -> bool:
    if not SETTINGS.exists():
        return False
    try:
        settings = json.loads(SETTINGS.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if all(settings.get(k) == v for k, v in WANTED.items()):
        return False
    settings.update(WANTED)
    SETTINGS.write_text(json.dumps(settings, indent=4) + "\n")
    log(f"transmission: settings.json -> {DOWNLOAD_DIR}")
    return True


def _rpc(client: httpx.Client, method: str, arguments: dict | None = None) -> dict:
    payload = {"method": method}
    if arguments:
        payload["arguments"] = arguments
    r = client.post(RPC, json=payload)
    if r.status_code == 409:
        sid = r.headers.get("X-Transmission-Session-Id", "")
        r = client.post(RPC, json=payload, headers={"X-Transmission-Session-Id": sid})
    r.raise_for_status()
    return r.json()


def configure(log) -> None:
    changed = _persist_settings(log)
    try:
        with httpx.Client(timeout=30.0) as client:
            session = _rpc(client, "session-get").get("arguments", {})
            if all(session.get(k) == v for k, v in WANTED.items()):
                if not changed:
                    log("transmission: download paths already aligned")
                return
            _rpc(client, "session-set", WANTED)
            log(f"transmission: download-dir -> {DOWNLOAD_DIR}")
    except httpx.HTTPError as exc:
        if not changed:
            log(f"transmission: wiring failed ({exc})")
