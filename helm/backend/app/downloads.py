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
ITEM_LIMIT = 20

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
_NZB_POST = _NZB_ACTIVE - _NZB_DOWNLOADING

_TX_STATUS = {
    0: "paused",
    1: "queued",
    2: "checking",
    3: "queued",
    4: "downloading",
    5: "queued",
    6: "seeding",
}


def _internal(app_id: str) -> str:
    return (catalogue.load().get(app_id, {}).get("internal") or "").rstrip("/")


def format_eta_seconds(seconds: int | float | None) -> str | None:
    if seconds is None:
        return None
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _u64(lo: Any, hi: Any = 0) -> int:
    try:
        return (int(hi or 0) << 32) + int(lo or 0)
    except (TypeError, ValueError):
        return 0


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


def parse_transmission_items(torrents: list) -> list[dict]:
    rows: list[dict] = []
    for item in torrents:
        if not isinstance(item, dict):
            continue
        try:
            pct = float(item.get("percentDone") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        progress = max(0, min(100, round(pct * 100)))
        status = item.get("status")
        try:
            status_i = int(status)
        except (TypeError, ValueError):
            status_i = -1
        state = _TX_STATUS.get(status_i, "unknown")
        eta = item.get("eta")
        try:
            eta_i = int(eta) if eta is not None else None
        except (TypeError, ValueError):
            eta_i = None
        name = (item.get("name") or "Untitled").strip() or "Untitled"
        rows.append({
            "id": item.get("id"),
            "client": "transmission",
            "name": name,
            "progress": progress,
            "download_rate": int(item.get("rateDownload") or 0),
            "upload_rate": int(item.get("rateUpload") or 0),
            "eta": eta_i if eta_i is not None and eta_i >= 0 else None,
            "eta_label": format_eta_seconds(eta_i),
            "state": state,
            "error": (item.get("errorString") or "").strip() or None,
        })
    # Prefer downloading/seeding activity, then by download rate.
    priority = {"downloading": 0, "checking": 1, "seeding": 2, "queued": 3, "paused": 4}
    rows.sort(key=lambda r: (
        priority.get(r["state"], 9),
        -(r.get("download_rate") or 0),
        -(r.get("upload_rate") or 0),
    ))
    return rows[:ITEM_LIMIT]


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


def parse_nzbget_items(groups: list) -> list[dict]:
    rows: list[dict] = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        status = str(item.get("Status") or "").upper()
        if status in _NZB_DOWNLOADING:
            state = "downloading"
        elif status in _NZB_QUEUED:
            state = "queued" if status == "QUEUED" else "paused"
        elif status in _NZB_POST:
            state = "post-processing"
        else:
            state = status.lower() or "unknown"

        total = _u64(item.get("FileSizeLo"), item.get("FileSizeHi"))
        remaining = _u64(item.get("RemainingSizeLo"), item.get("RemainingSizeHi"))
        if total > 0:
            done = max(0, total - remaining)
            progress = max(0, min(100, round(100 * done / total)))
        elif remaining == 0 and status in _NZB_POST:
            progress = 100
        else:
            progress = 0

        rate = int(item.get("DownloadRate") or 0)
        eta_i = None
        if rate > 0 and remaining > 0:
            eta_i = round(remaining / rate)

        name = (item.get("NZBName") or item.get("NZBNicename") or "Untitled").strip() or "Untitled"
        rows.append({
            "id": item.get("NZBID"),
            "client": "nzbget",
            "name": name,
            "progress": progress,
            "download_rate": rate,
            "upload_rate": 0,
            "eta": eta_i,
            "eta_label": format_eta_seconds(eta_i),
            "state": state,
            "error": None,
            "status_raw": status,
        })
    priority = {"downloading": 0, "post-processing": 1, "queued": 2, "paused": 3}
    rows.sort(key=lambda r: (
        priority.get(r["state"], 9),
        -(r.get("download_rate") or 0),
    ))
    return rows[:ITEM_LIMIT]


def parse_nzbget_status(payload: dict) -> dict:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        result = payload if isinstance(payload, dict) else {}
    return {
        "download_rate": int(result.get("DownloadRate") or 0),
        "paused": bool(result.get("DownloadPaused")),
        "standby": bool(result.get("ServerStandBy")),
    }


async def _transmission_rpc(client: httpx.AsyncClient, url: str, method: str, arguments: dict | None = None) -> dict:
    body = {"method": method, "arguments": arguments or {}}
    response = await client.post(url, json=body)
    if response.status_code == 409:
        token = response.headers.get("X-Transmission-Session-Id", "")
        response = await client.post(
            url, json=body, headers={"X-Transmission-Session-Id": token},
        )
    response.raise_for_status()
    return response.json() if response.content else {}


async def _transmission_snapshot() -> tuple[dict | None, str | None]:
    if "transmission" not in config.profiles():
        return None, None
    base = _internal("transmission")
    if not base:
        return None, "no internal URL"
    url = f"{base}{TRANSMISSION_RPC}"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            stats_raw = await _transmission_rpc(client, url, "session-stats")
            items_raw = await _transmission_rpc(client, url, "torrent-get", {
                "fields": [
                    "id", "name", "percentDone", "rateDownload", "rateUpload",
                    "eta", "status", "errorString",
                ],
            })
        summary = parse_transmission_stats(stats_raw)
        torrents = (items_raw.get("arguments") or {}).get("torrents") or []
        summary["items"] = parse_transmission_items(torrents if isinstance(torrents, list) else [])
        return summary, None
    except httpx.HTTPError as exc:
        return None, str(exc)


async def _nzbget_rpc(client: httpx.AsyncClient, base: str, method: str, params: list | None = None) -> Any:
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
        group_list = groups if isinstance(groups, list) else []
        summary = parse_nzbget_groups(group_list)
        rates = parse_nzbget_status({"result": status} if isinstance(status, dict) else {})
        summary.update(rates)
        summary["items"] = parse_nzbget_items(group_list)
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
    items: list[dict] = []
    if transmission:
        active += int(transmission.get("active") or 0)
        items.extend(transmission.get("items") or [])
    if nzbget:
        active += int(nzbget.get("active") or 0)
        items.extend(nzbget.get("items") or [])

    # Merge and re-rank across clients for the panel.
    priority = {
        "downloading": 0,
        "checking": 1,
        "post-processing": 2,
        "seeding": 3,
        "queued": 4,
        "paused": 5,
    }
    items.sort(key=lambda r: (
        priority.get(r.get("state"), 9),
        -(r.get("download_rate") or 0),
        -(r.get("upload_rate") or 0),
    ))
    items = items[:ITEM_LIMIT]

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
        "items": items,
        "transmission": transmission,
        "nzbget": nzbget,
        "errors": errors,
    }
