"""Stop catalogue apps that are running while disabled in COMPOSE_PROFILES.

``docker compose up -d <service>`` starts a named service even when its
profile is off. That is how Jackett/Unpackerr (and previously Grafana)
came back after tunnel recreates or Update All. Disable removes the
profile; this module sweeps leftover containers.
"""
from __future__ import annotations

import json
from typing import Iterable


def running_service_names(compose_ps_output: str) -> set[str]:
    """Parse ``docker compose ps --format json`` into running service names."""
    running: set[str] = set()
    text = (compose_ps_output or "").strip()
    if not text:
        return running
    # Compose may emit one JSON object per line, or a single JSON array.
    if text.startswith("["):
        try:
            rows = json.loads(text)
        except json.JSONDecodeError:
            return running
        if not isinstance(rows, list):
            return running
        for row in rows:
            if not isinstance(row, dict):
                continue
            if (row.get("State") or "").lower() != "running":
                continue
            name = (row.get("Service") or "").strip()
            if name:
                running.add(name)
        return running
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if (row.get("State") or "").lower() != "running":
            continue
        name = (row.get("Service") or "").strip()
        if name:
            running.add(name)
    return running


def disabled_running_services(
    *,
    catalogue_ids: Iterable[str],
    enabled: Iterable[str],
    running: Iterable[str],
) -> list[str]:
    """Catalogue apps that are not enabled but still have a container up."""
    on = set(enabled)
    up = set(running)
    return sorted(app for app in set(catalogue_ids) if app not in on and app in up)
