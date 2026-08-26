"""Small read-only client for the sparklines on the Apps page.

One range query covers every container, because twenty per-card requests
would be twenty round trips for a graph two centimetres wide.
"""
from __future__ import annotations

import math
import time

import httpx

from . import config

PROMETHEUS = "http://prometheus:9090"
WINDOW_SECONDS = 3 * 60 * 60
STEP_SECONDS = 300
CACHE_TTL = 30.0
_cache: dict[str, object] = {"at": 0.0, "data": {"apps": {}}}
_overview_cache: dict[str, object] = {"at": 0.0, "data": {}}

CPU_QUERY = (
    'sum by(name) (rate(container_cpu_usage_seconds_total{name=~"kine-.*"}[5m])) * 100'
)
MEM_QUERY = 'container_memory_working_set_bytes{name=~"kine-.*"}'

HOST_CPU = '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
HOST_MEM = "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)"
STREAM_UNIQUE = "count(count by (user) (kine_streams_active))"
STREAM_BY_SERVER = "sum by (server) (kine_streams_active)"
RATE_BY_DIR = "sum by (direction) (kine_download_rate_bytes)"
TOTAL_BY_DIR = "sum by (direction) (kine_download_bytes_total)"


def parse_range(payload: dict) -> dict[str, list[float]]:
    if payload.get("status") != "success":
        return {}
    out: dict[str, list[float]] = {}
    for series in (payload.get("data") or {}).get("result") or []:
        name = (series.get("metric") or {}).get("name") or ""
        app = name.removeprefix("kine-")
        if not app:
            continue
        values = []
        for point in series.get("values") or []:
            try:
                value = float(point[1])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isnan(value) or math.isinf(value):
                continue
            values.append(value)
        out[app] = values
    return out


def parse_instant(payload: dict) -> list[dict]:
    if payload.get("status") != "success":
        return []
    out: list[dict] = []
    for series in (payload.get("data") or {}).get("result") or []:
        try:
            value = float((series.get("value") or [None, None])[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isnan(value) or math.isinf(value):
            continue
        out.append({"labels": dict(series.get("metric") or {}), "value": value})
    return out


def nfs_media_mountpoint(env: dict | None = None) -> str:
    root = ((env or {}).get("DATA_ROOT") or "/srv/media-data").rstrip("/")
    return f"{root}/media"


def build_overview(
    *,
    unique_users: int,
    sessions_by_server: dict[str, float],
    rate_by_direction: dict[str, float],
    total_by_direction: dict[str, float],
    nfs: dict | None,
    cpu_pct: float | None,
    mem_pct: float | None,
) -> dict:
    sessions = {k: int(v) for k, v in sorted(sessions_by_server.items())}
    used_pct = None
    nfs_disk = None
    if nfs and nfs.get("size_bytes"):
        size = float(nfs["size_bytes"])
        avail = float(nfs.get("avail_bytes") or 0)
        used = max(0.0, size - avail)
        used_pct = round(100 * used / size, 1) if size else 0.0
        nfs_disk = {
            "mountpoint": nfs.get("mountpoint") or "",
            "size_bytes": size,
            "avail_bytes": avail,
            "used_bytes": used,
            "used_pct": used_pct,
        }
    return {
        "streamers": {
            "unique": int(unique_users),
            "sessions": sum(sessions.values()),
            "by_server": sessions,
        },
        "downloads": {
            "rate_down": float(rate_by_direction.get("down") or 0),
            "rate_up": float(rate_by_direction.get("up") or 0),
            "total_down": float(total_by_direction.get("down") or 0),
            "total_up": float(total_by_direction.get("up") or 0),
        },
        "nfs_disk": nfs_disk,
        "host": {
            "cpu_pct": None if cpu_pct is None else round(float(cpu_pct), 1),
            "mem_pct": None if mem_pct is None else round(float(mem_pct), 1),
        },
    }


async def _range(client: httpx.AsyncClient, query: str) -> dict[str, list[float]]:
    now = int(time.time())
    response = await client.get(
        f"{PROMETHEUS}/api/v1/query_range",
        params={
            "query": query,
            "start": now - WINDOW_SECONDS,
            "end": now,
            "step": STEP_SECONDS,
        },
    )
    response.raise_for_status()
    return parse_range(response.json())


async def _instant(client: httpx.AsyncClient, query: str) -> list[dict]:
    response = await client.get(
        f"{PROMETHEUS}/api/v1/query",
        params={"query": query},
    )
    response.raise_for_status()
    return parse_instant(response.json())


def _scalar(rows: list[dict]) -> float | None:
    if not rows:
        return None
    return float(rows[0]["value"])


def _by_label(rows: list[dict], key: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        label = (row.get("labels") or {}).get(key)
        if label:
            out[str(label)] = float(row["value"])
    return out


async def card_series() -> dict:
    if time.monotonic() - float(_cache["at"]) < CACHE_TTL:
        return _cache["data"]  # type: ignore[return-value]
    if "prometheus" not in config.profiles():
        return {"apps": {}}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            cpu = await _range(client, CPU_QUERY)
            mem = await _range(client, MEM_QUERY)
    except httpx.HTTPError:
        # The Apps page must render with or without metrics.
        return {"apps": {}}
    apps = {
        app: {"cpu": cpu.get(app, []), "mem": mem.get(app, [])}
        for app in set(cpu) | set(mem)
    }
    _cache["at"] = time.monotonic()
    _cache["data"] = {"apps": apps}
    return _cache["data"]  # type: ignore[return-value]


async def overview() -> dict:
    if time.monotonic() - float(_overview_cache["at"]) < CACHE_TTL:
        return _overview_cache["data"]  # type: ignore[return-value]
    empty = build_overview(
        unique_users=0,
        sessions_by_server={},
        rate_by_direction={},
        total_by_direction={},
        nfs=None,
        cpu_pct=None,
        mem_pct=None,
    )
    if "prometheus" not in config.profiles():
        return empty
    mount = nfs_media_mountpoint(config.read())
    # Escape for PromQL label matcher (mount paths have no quotes normally).
    mount_lit = mount.replace("\\", "\\\\").replace('"', '\\"')
    nfs_size_q = (
        f'node_filesystem_size_bytes{{fstype="nfs",mountpoint="{mount_lit}"}}'
    )
    nfs_avail_q = (
        f'node_filesystem_avail_bytes{{fstype="nfs",mountpoint="{mount_lit}"}}'
    )
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            unique = _scalar(await _instant(client, STREAM_UNIQUE)) or 0
            by_server = _by_label(await _instant(client, STREAM_BY_SERVER), "server")
            rates = _by_label(await _instant(client, RATE_BY_DIR), "direction")
            totals = _by_label(await _instant(client, TOTAL_BY_DIR), "direction")
            cpu = _scalar(await _instant(client, HOST_CPU))
            mem = _scalar(await _instant(client, HOST_MEM))
            size = _scalar(await _instant(client, nfs_size_q))
            avail = _scalar(await _instant(client, nfs_avail_q))
    except httpx.HTTPError:
        return empty
    nfs = None
    if size is not None:
        nfs = {
            "mountpoint": mount,
            "size_bytes": size,
            "avail_bytes": avail or 0,
        }
    data = build_overview(
        unique_users=int(unique),
        sessions_by_server=by_server,
        rate_by_direction=rates,
        total_by_direction=totals,
        nfs=nfs,
        cpu_pct=cpu,
        mem_pct=mem,
    )
    _overview_cache["at"] = time.monotonic()
    _overview_cache["data"] = data
    return data
