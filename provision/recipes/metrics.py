"""Seed Prometheus scrape config and Grafana provisioning.

Dashboards are kept in the repo and copied in, not created through
Grafana's API, so they stay reviewable in git instead of living inside
grafana.db. Grafana runs as uid 472 rather than the stack PUID, so the
directories it writes to are chowned explicitly.
"""
from __future__ import annotations

import os
import pathlib
import shutil

import yaml

GRAFANA_UID = 472
GRAFANA_GID = 472
ASSETS = pathlib.Path(__file__).resolve().parents[1] / "assets" / "grafana"

PROMETHEUS_CONFIG = {
    "global": {"scrape_interval": "15s", "evaluation_interval": "15s"},
    "scrape_configs": [
        {
            "job_name": "prometheus",
            "static_configs": [{"targets": ["localhost:9090"]}],
        },
        {
            "job_name": "cadvisor",
            "static_configs": [{"targets": ["cadvisor:8080"]}],
        },
        {
            "job_name": "node",
            "static_configs": [{"targets": ["node-exporter:9100"]}],
        },
        {
            # Matches the collector's own 60s tick; scraping faster only
            # re-reads the same cache.
            "job_name": "kine",
            "scrape_interval": "60s",
            "metrics_path": "/api/metrics",
            "static_configs": [{"targets": ["helm:8600"]}],
        },
    ],
}

DATASOURCE = {
    "apiVersion": 1,
    "datasources": [
        {
            "name": "Prometheus",
            "type": "prometheus",
            "access": "proxy",
            "url": "http://prometheus:9090",
            # Fixed, because dashboards reference this uid and
            # provisioning fails opaquely if it drifts.
            "uid": "kine-prom",
            "isDefault": True,
            "editable": False,
        }
    ],
}

DASHBOARD_PROVIDER = {
    "apiVersion": 1,
    "providers": [
        {
            "name": "kine",
            "orgId": 1,
            "folder": "Kine",
            "type": "file",
            "disableDeletion": False,
            "updateIntervalSeconds": 30,
            "allowUiUpdates": False,
            "options": {
                "path": "/etc/grafana/dashboards",
                "foldersFromFilesStructure": False,
            },
        }
    ],
}


def _write_yaml(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _chown(path: pathlib.Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid)
    except (OSError, AttributeError):
        # Non-root test runs and Docker Desktop cannot chown; Grafana
        # only needs this on a real appliance.
        pass


def seed(stack: pathlib.Path, enabled: set[str], log=print) -> None:
    if not {"grafana", "prometheus"} & set(enabled):
        return

    prom_dir = stack / "config" / "prometheus"
    (prom_dir / "data").mkdir(parents=True, exist_ok=True)
    _write_yaml(prom_dir / "prometheus.yml", PROMETHEUS_CONFIG)
    log("  prometheus: wrote scrape config")

    grafana = stack / "config" / "grafana"
    _write_yaml(grafana / "provisioning" / "datasources" / "prometheus.yml", DATASOURCE)
    _write_yaml(grafana / "provisioning" / "dashboards" / "kine.yml", DASHBOARD_PROVIDER)
    # Grafana logs a scary error for every provisioning directory it does
    # not find, and the mount is read-only so it cannot create them.
    for empty in ("plugins", "alerting", "notifiers", "access-control"):
        (grafana / "provisioning" / empty).mkdir(parents=True, exist_ok=True)

    dashboards = grafana / "dashboards"
    dashboards.mkdir(parents=True, exist_ok=True)
    count = 0
    for src in sorted((ASSETS / "dashboards").glob("*.json")):
        shutil.copyfile(src, dashboards / src.name)
        count += 1
    log(f"  grafana: provisioned datasource and {count} dashboards")

    (grafana / "data").mkdir(parents=True, exist_ok=True)
    for path in (grafana, grafana / "data", grafana / "dashboards",
                 grafana / "provisioning"):
        _chown(path, GRAFANA_UID, GRAFANA_GID)
