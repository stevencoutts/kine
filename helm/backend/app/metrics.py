"""Kine's own Prometheus exporter.

Collection is deliberately decoupled from scraping: a background task
fills a cache every 60s and /api/metrics renders whatever is in it. A
hung Sonarr must never stall a scrape or punch a hole in the graphs.
"""
from __future__ import annotations

from typing import NamedTuple


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
