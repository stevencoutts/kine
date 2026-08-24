"""Live Transmission and NZBGet download summaries for the Apps overview."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from . import catalogue, config

TRANSMISSION_RPC = "/transmission/rpc"
NZBGET_RPC = "/jsonrpc"
NZBGET_USER = "nzbget"
NZBGET_PASSWORD = "nzbget"

# NZBGet group Status values that count as actively transferring.
_NZB_ACTIVE = frozenset({
    "DOWNLOADING",
    "PP_QUEUED",
    "LOADING_PARS",
    "VERIFYING_SOURCES",
    "REPAIRING",
    "VERIFYING_REPAIRED",
    "RENAMING",
    "UNPACKING",
    "MOVING",
    "EXECUTING_SCRIPT",
    "PP_FINISHED",
})
_NZB_DOWNLOADING = frozenset({"DOWNLOADING"})
_NZB_QUEUED = frozenset({"QUEUED", "PAUSED"})


def _internal(app_id: str) -> str:
    return (catalogue.load().get(app_id, {}).get("internal") or "").rstrip("/")


def parse_transmission_stats(payload: dict) -> dict:
    args = payload.get("arguments") or {}
    active = int(args.get("activeTorrentCount") or 0)
    paused = int(args.get("pausedTorrentCount") or 0)
    total = int(args.get("torrentCount") or 0)
    return {
        "ok": True,
        "active": active,
        "paused": paused,
        "total": total,
        "download_rate": int(args.get("downloadSpeed") or 0),
        "upload_rate": int(args.get("uploadSpeed") or 0),
    }


def parse_nzbget_groups(groups: list) -> dict:
    downloading = 0
    queued = 0
    post = 0
    for item in groups:
        if not isinstance(item, dict):
            continue
        status = str(item.get("Status") or "").upper()
        if status in _NZB_DOWNLOADING:
            downloading += 1
        elif status in _NZB_QUEUED:
            queued += 1
        elif status in _NZB_ACTIVE:
            post += 1
    active = downloading + post
    return {
        "ok": True,
        "active": active,
        "downloading": downloading,
        "queued": queued,
        "postprocessing": post,
        "total": len([g for g in groups if isinstance(g, dict)]),
    }


def parse_nzbget_status(payload: dict) -> dict:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        result = payload if isinstance(payload, dict) else {}
    return {
        "download_rate": int(result.get("DownloadRate") or 0),
        "paused": bool(result.get("DownloadPaused")),
        "standby": bool(result.get("ServerStandBy")),
    }


async def _transmission_snapshot() -> tuple[dict | None, str | None]:
    if "transmission" not in config.profiles():
        return None, None
    base = _internal("transmission")
    if not base:
        return None, "no internal URL"
    url = f"{base}{TRANSMISSION_RPC}"
    body = {"method": "session-stats"}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.post(url, json=body)
            if response.status_code == 409:
                token = response.headers.get("X-Transmission-Session-Id", "")
                response = await client.post(
                    url, json=body, headers={"X-Transmission-Session-Id": token},
                )
            response.raise_for_status()
            return parse_transmission_stats(response.json()), None
    except httpx.HTTPError as exc:
        return None, str(exc)


async def _nzbget_rpc(client: httpx.Client, base: str, method: str, params: list | None = None) -> Any:
    payload = {"jsonrpc": "2.0", "method": method, "params": params or [], "id": 1}
    response = await client.post(
        f"{base}{NZBGET_RPC}",
        json=payload,
        auth=(NZBGET_USER, NZBGET_PASSWORD),
    )
    response.raise_for_status()
    data = response.json() if response.content else {}
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data.get("result") if isinstance(data, dict) else data


async def _nzbget_snapshot() -> tuple[dict | None, str | None]:
    if "nzbget" not in config.profiles():
        return None, None
    base = _internal("nzbget")
    if not base:
        return None, "no internal URL"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            groups = await _nzbget_rpc(client, base, "listgroups", [0])
            status = await _nzbget_rpc(client, base, "status")
        summary = parse_nzbget_groups(groups if isinstance(groups, list) else [])
        rates = parse_nzbget_status({"result": status} if isinstance(status, dict) else {})
        summary.update(rates)
        return summary, None
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError) as exc:
        return None, str(exc)


async def snapshot() -> dict:
    """Aggregate download activity for the Apps overview widget."""
    profiles = set(config.profiles())
    tx_task = asyncio.create_task(_transmission_snapshot())
    nz_task = asyncio.create_task(_nzbget_snapshot())
    transmission, tx_error = await tx_task
    nzbget, nz_error = await nz_task

    active = 0
    if transmission:
        active += int(transmission.get("active") or 0)
    if nzbget:
        active += int(nzbget.get("active") or 0)

    errors = {}
    if tx_error:
        errors["transmission"] = tx_error
    if nz_error:
        errors["nzbget"] = nz_error

    return {
        "ok": True,
        "configured": {
            "transmission": "transmission" in profiles,
            "nzbget": "nzbget" in profiles,
        },
        "active": active,
        "transmission": transmission,
        "nzbget": nzbget,
        "errors": errors,
    }
