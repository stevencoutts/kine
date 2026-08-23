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
