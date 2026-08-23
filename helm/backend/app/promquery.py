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

CPU_QUERY = (
    'sum by(name) (rate(container_cpu_usage_seconds_total{name=~"kine-.*"}[5m])) * 100'
)
MEM_QUERY = 'container_memory_working_set_bytes{name=~"kine-.*"}'


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
