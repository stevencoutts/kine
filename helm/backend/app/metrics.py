"""Kine's own Prometheus exporter.

Collection is deliberately decoupled from scraping: a background task
fills a cache every 60s and /api/metrics renders whatever is in it. A
hung Sonarr must never stall a scrape or punch a hole in the graphs.
"""
from __future__ import annotations

import asyncio
import time
from typing import NamedTuple

import httpx

from . import appkeys, catalogue, compose, config, updates_info, watching


class Sample(NamedTuple):
    name: str
    labels: dict[str, str]
    value: float


METRIC_TYPES = {
    "kine_streams_active": "gauge",
    "kine_app_up": "gauge",
    "kine_update_pending": "gauge",
    "kine_library_items": "gauge",
    "kine_library_missing": "gauge",
    "kine_library_bytes": "gauge",
    "kine_queue_items": "gauge",
    "kine_subtitles_wanted": "gauge",
    "kine_indexers_enabled": "gauge",
    "kine_download_rate_bytes": "gauge",
    "kine_torrents": "gauge",
    "kine_collect_duration_seconds": "gauge",
    "kine_indexer_queries_total": "counter",
    "kine_indexer_grabs_total": "counter",
    "kine_collect_errors_total": "counter",
}

METRIC_HELP = {
    "kine_streams_active": "Sessions currently open on a media server",
    "kine_app_up": "1 when the app answered its API, 0 when it did not",
    "kine_update_pending": "1 when a newer image digest is available",
    "kine_library_items": "Items known to the PVR",
    "kine_library_missing": "Monitored items with no file on disk",
    "kine_library_bytes": "Bytes on disk according to the PVR",
    "kine_queue_items": "Items in the PVR download queue",
    "kine_subtitles_wanted": "Episodes or films missing wanted subtitles",
    "kine_indexers_enabled": "Indexers currently enabled",
    "kine_download_rate_bytes": "Current transfer rate in bytes per second",
    "kine_torrents": "Torrents by state",
    "kine_collect_duration_seconds": "Time the last collection of an app took",
    "kine_indexer_queries_total": "Indexer queries since the indexer was added",
    "kine_indexer_grabs_total": "Indexer grabs since the indexer was added",
    "kine_collect_errors_total": "Failed collections since Helm started",
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


ARR_ITEM_KIND = {"sonarr": "series", "radarr": "movies"}


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_arr(app: str, *, counts: dict, queue: dict, missing: dict) -> list[Sample]:
    items = counts.get("items") or []
    kind = ARR_ITEM_KIND.get(app, "items")
    out = [Sample("kine_library_items", {"app": app, "kind": kind}, len(items))]

    size = sum(
        _num((item.get("statistics") or {}).get("sizeOnDisk", item.get("sizeOnDisk")))
        for item in items
    )
    out.append(Sample("kine_library_bytes", {"app": app}, size))

    if kind == "series":
        episodes = sum(_num((i.get("statistics") or {}).get("episodeCount")) for i in items)
        out.append(Sample("kine_library_items", {"app": app, "kind": "episodes"}, episodes))
        gap_kind = "episodes"
    else:
        gap_kind = "movies"
    out.append(
        Sample("kine_library_missing", {"app": app, "kind": gap_kind},
               _num(missing.get("totalRecords")))
    )
    out.append(Sample("kine_queue_items", {"app": app}, _num(queue.get("totalRecords"))))
    return out


def parse_bazarr(series: dict, movies: dict) -> list[Sample]:
    return [
        Sample("kine_subtitles_wanted", {"kind": "series"}, len(series.get("data") or [])),
        Sample("kine_subtitles_wanted", {"kind": "movies"}, len(movies.get("data") or [])),
    ]


def parse_prowlarr(indexers: list, stats: dict) -> list[Sample]:
    rows = stats.get("indexers") or []
    return [
        Sample("kine_indexers_enabled", {"app": "prowlarr"},
               sum(1 for i in indexers if i.get("enable"))),
        Sample("kine_indexer_queries_total", {"app": "prowlarr"},
               sum(_num(r.get("numberOfQueries")) for r in rows)),
        Sample("kine_indexer_grabs_total", {"app": "prowlarr"},
               sum(_num(r.get("numberOfGrabs")) for r in rows)),
    ]


def parse_transmission(session_stats: dict) -> list[Sample]:
    args = session_stats.get("arguments") or {}
    client = {"client": "transmission"}
    return [
        Sample("kine_download_rate_bytes", {**client, "direction": "down"},
               _num(args.get("downloadSpeed"))),
        Sample("kine_download_rate_bytes", {**client, "direction": "up"},
               _num(args.get("uploadSpeed"))),
        Sample("kine_torrents", {**client, "state": "active"},
               _num(args.get("activeTorrentCount"))),
        Sample("kine_torrents", {**client, "state": "paused"},
               _num(args.get("pausedTorrentCount"))),
        Sample("kine_torrents", {**client, "state": "total"},
               _num(args.get("torrentCount"))),
    ]


def parse_streams(snapshot: dict) -> list[Sample]:
    counts: dict[tuple[str, str, str], int] = {}
    for session in snapshot.get("sessions") or []:
        key = (
            session.get("server") or "unknown",
            session.get("state") or "playing",
            session.get("user") or "unknown",
        )
        counts[key] = counts.get(key, 0) + 1
    return [
        Sample("kine_streams_active", {"server": s, "state": st, "user": u}, n)
        for (s, st, u), n in sorted(counts.items())
    ]


def parse_updates(payload: dict) -> list[Sample]:
    """Rows come from updates.sh check-json, keyed by service id."""
    out = []
    for row in payload.get("containers") or []:
        app = row.get("id")
        if not app:
            continue
        pending = row.get("update_available")
        if pending is None:
            pending = row.get("status") == "update"
        out.append(Sample("kine_update_pending", {"app": app}, 1 if pending else 0))
    return out


def render(samples: list[Sample]) -> str:
    lines: list[str] = []
    for name in METRIC_TYPES:
        matching = [s for s in samples if s.name == name]
        if not matching:
            continue
        lines.append(f"# HELP {name} {METRIC_HELP[name]}")
        lines.append(f"# TYPE {name} {METRIC_TYPES[name]}")
        for sample in matching:
            if sample.labels:
                pairs = ",".join(
                    f'{k}="{_escape(str(v))}"' for k, v in sorted(sample.labels.items())
                )
                lines.append(f"{name}{{{pairs}}} {_format_value(sample.value)}")
            else:
                lines.append(f"{name} {_format_value(sample.value)}")
    return "\n".join(lines) + "\n"


CACHE: list[Sample] = []
COLLECT_INTERVAL = 60.0
ERRORS: dict[str, int] = {}
ARR_API = {"sonarr": "v3", "radarr": "v3", "prowlarr": "v1"}


def _enabled_apps() -> list[str]:
    return list(config.profiles())


async def _get_json(url: str, headers: dict, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json() if response.content else {}


def _base(app: str) -> str:
    return (catalogue.load().get(app, {}).get("internal") or "").rstrip("/")


async def _collect_arr(app: str) -> list[Sample]:
    key, base = appkeys.key_for(app), _base(app)
    if not key or not base:
        return []
    api = ARR_API[app]
    headers = {"X-Api-Key": key}
    resource = "series" if app == "sonarr" else "movie"
    items = await _get_json(f"{base}/api/{api}/{resource}", headers)
    queue = await _get_json(f"{base}/api/{api}/queue", headers, {"pageSize": 1})
    missing = await _get_json(f"{base}/api/{api}/wanted/missing", headers, {"pageSize": 1})
    counts = {"items": items if isinstance(items, list) else []}
    return parse_arr(
        app,
        counts=counts,
        queue=queue if isinstance(queue, dict) else {},
        missing=missing if isinstance(missing, dict) else {},
    )


async def _collect_bazarr(_app: str) -> list[Sample]:
    key, base = appkeys.bazarr_key(), _base("bazarr")
    if not key or not base:
        return []
    headers = {"X-API-KEY": key}
    series = await _get_json(f"{base}/api/episodes/wanted", headers)
    movies = await _get_json(f"{base}/api/movies/wanted", headers)
    return parse_bazarr(
        series if isinstance(series, dict) else {},
        movies if isinstance(movies, dict) else {},
    )


async def _collect_prowlarr(_app: str) -> list[Sample]:
    key, base = appkeys.arr_key("prowlarr"), _base("prowlarr")
    if not key or not base:
        return []
    headers = {"X-Api-Key": key}
    indexers = await _get_json(f"{base}/api/v1/indexer", headers)
    stats = await _get_json(f"{base}/api/v1/indexerstats", headers)
    return parse_prowlarr(
        indexers if isinstance(indexers, list) else [],
        stats if isinstance(stats, dict) else {},
    )


async def _collect_transmission(_app: str) -> list[Sample]:
    base = _base("transmission")
    if not base:
        return []
    url = f"{base}/transmission/rpc"
    body = {"method": "session-stats"}
    async with httpx.AsyncClient(timeout=8.0) as client:
        # Transmission answers the first call with 409 and the session id
        # it wants echoed back. There is no way to skip the handshake.
        response = await client.post(url, json=body)
        if response.status_code == 409:
            token = response.headers.get("X-Transmission-Session-Id", "")
            response = await client.post(
                url, json=body, headers={"X-Transmission-Session-Id": token}
            )
        response.raise_for_status()
        return parse_transmission(response.json())


async def _collect_streams(_label: str) -> list[Sample]:
    return parse_streams(await watching.snapshot())


async def _collect_updates(_label: str) -> list[Sample]:
    return parse_updates(await updates_info.fetch(compose, refresh=False))


async def _probe(app: str) -> list[Sample]:
    base = _base(app)
    if not base:
        return []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(base, follow_redirects=True)
        up = 1 if response.status_code < 500 else 0
    except httpx.HTTPError:
        up = 0
    return [Sample("kine_app_up", {"app": app}, up)]


COLLECTORS = {
    "sonarr": _collect_arr,
    "radarr": _collect_arr,
    "prowlarr": _collect_prowlarr,
    "bazarr": _collect_bazarr,
    "transmission": _collect_transmission,
}

# Run regardless of which apps are enabled.
GLOBAL_COLLECTORS = {"streams": _collect_streams, "updates": _collect_updates}


async def collect_once() -> None:
    started = time.monotonic()
    samples: list[Sample] = []
    enabled = _enabled_apps()

    jobs = [(name, fn) for name, fn in GLOBAL_COLLECTORS.items()]
    jobs += [(app, COLLECTORS[app]) for app in enabled if app in COLLECTORS]

    for label, fn in jobs:
        job_started = time.monotonic()
        try:
            samples.extend(await fn(label))
        except Exception:  # noqa: BLE001 — one bad app must not lose the rest
            ERRORS[label] = ERRORS.get(label, 0) + 1
        samples.append(
            Sample("kine_collect_duration_seconds", {"app": label},
                   round(time.monotonic() - job_started, 3))
        )

    for app in enabled:
        try:
            samples.extend(await _probe(app))
        except Exception:  # noqa: BLE001
            ERRORS[app] = ERRORS.get(app, 0) + 1

    samples.extend(
        Sample("kine_collect_errors_total", {"app": app}, count)
        for app, count in ERRORS.items()
    )
    samples.append(
        Sample("kine_collect_duration_seconds", {"app": "total"},
               round(time.monotonic() - started, 3))
    )
    CACHE[:] = samples


def export() -> str:
    return render(CACHE)


async def collector_loop() -> None:
    while True:
        try:
            await collect_once()
        except Exception:  # noqa: BLE001 — the loop must outlive any failure
            pass
        await asyncio.sleep(COLLECT_INTERVAL)
