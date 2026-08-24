import os
import pathlib

import yaml

REPO = pathlib.Path(os.environ.get("KINE_REPO", "/repo"))

TIER_LABELS = {
    "media": "Media",
    "acquisition": "Acquisition",
    "process": "Process",
    "live": "Live TV",
    "metrics": "Metrics",
    "platform": "Platform",
}


def load() -> dict:
    with (REPO / "catalogue.yml").open() as fh:
        return yaml.safe_load(fh)["apps"]


def defaults() -> list[str]:
    """Catalogue defaults selected when their sections are enabled."""
    return [k for k, v in load().items() if v.get("default") or v.get("mandatory")]


def tier_apps(tier: str) -> dict:
    return {k: v for k, v in load().items() if v.get("tier") == tier}


def tier_default_apps(tier: str) -> list[str]:
    return [k for k, v in tier_apps(tier).items() if v.get("default") and not v.get("hidden")]


def tier_visible_apps(tier: str) -> list[str]:
    return [k for k, v in tier_apps(tier).items() if not v.get("hidden") and not v.get("mandatory")]


def resolve_deps(app_id: str, cat: dict, wanted: list[str]) -> list[str]:
    """Pull in requires, and their requires, until nothing new appears.

    One level is not enough: Grafana needs Prometheus, which needs the
    exporters, and stopping halfway starts a dashboard with no data.
    """
    queue = [app_id]
    seen = set()
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for dep in cat.get(current, {}).get("requires", []):
            if dep not in wanted:
                wanted.append(dep)
            queue.append(dep)
    return wanted


def prune_orphan_gluetun(wanted: list[str], cat: dict) -> list[str]:
    if cat.get("gluetun", {}).get("mandatory"):
        return wanted
    tunnelled = {k for k, v in cat.items() if v.get("tunnelled") == "forced"}
    if tunnelled & set(wanted):
        return wanted
    return [p for p in wanted if p != "gluetun"]


def prune_orphan_deps(wanted: list[str], cat: dict) -> list[str]:
    """Drop hidden plumbing that nothing remaining still requires.

    Enabling Grafana pulls in Prometheus and the exporters. Disabling only
    the visible app used to leave those three running forever; this walks
    the requires graph and removes anything hidden that lost its last
    dependent. Mandatory services are never touched.
    """
    wanted = list(wanted)
    changed = True
    while changed:
        changed = False
        needed: set[str] = set()
        for app in wanted:
            for dep in cat.get(app, {}).get("requires", []):
                needed.add(dep)
        next_wanted = []
        for app in wanted:
            meta = cat.get(app, {})
            if meta.get("mandatory") or not meta.get("hidden") or app in needed:
                next_wanted.append(app)
            else:
                changed = True
        wanted = next_wanted
    return prune_orphan_gluetun(wanted, cat)
