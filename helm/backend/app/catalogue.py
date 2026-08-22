import os
import pathlib

import yaml

REPO = pathlib.Path(os.environ.get("KINE_REPO", "/repo"))

TIER_LABELS = {
    "media": "Media",
    "acquisition": "Acquisition",
    "live": "Live TV",
    "platform": "Platform",
}


def load() -> dict:
    with (REPO / "catalogue.yml").open() as fh:
        return yaml.safe_load(fh)["apps"]


def defaults() -> list[str]:
    """Apps enabled on a fresh install."""
    return [k for k, v in load().items() if v.get("default") or v.get("mandatory")]


def tier_apps(tier: str) -> dict:
    return {k: v for k, v in load().items() if v.get("tier") == tier}


def tier_default_apps(tier: str) -> list[str]:
    return [k for k, v in tier_apps(tier).items() if v.get("default") and not v.get("hidden")]


def tier_visible_apps(tier: str) -> list[str]:
    return [k for k, v in tier_apps(tier).items() if not v.get("hidden") and not v.get("mandatory")]


def resolve_deps(app_id: str, cat: dict, wanted: list[str]) -> list[str]:
    for dep in cat[app_id].get("requires", []):
        if dep not in wanted:
            wanted.append(dep)
    return wanted


def prune_orphan_gluetun(wanted: list[str], cat: dict) -> list[str]:
    if cat.get("gluetun", {}).get("mandatory"):
        return wanted
    tunnelled = {k for k, v in cat.items() if v.get("tunnelled") == "forced"}
    if tunnelled & set(wanted):
        return wanted
    return [p for p in wanted if p != "gluetun"]
