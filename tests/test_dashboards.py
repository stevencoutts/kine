"""Dashboards must parse, be uniquely identified, and only ask for
metrics that something actually exports.

A typo in a PromQL expression is silent: the panel renders "No data" and
looks like a broken exporter rather than a broken dashboard.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import metrics  # noqa: E402

DASHBOARD_DIR = ROOT / "provision" / "assets" / "grafana" / "dashboards"
DASHBOARDS = sorted(DASHBOARD_DIR.glob("*.json"))
EXPECTED_UIDS = {"kine-overview", "kine-containers", "kine-host", "kine-media"}
# cAdvisor and node-exporter name their own metrics; we only own kine_*.
EXTERNAL_PREFIXES = ("container_", "node_", "machine_", "scrape_")
# Panel ids the Helm Stats page embeds by number.
EMBEDDED_OVERVIEW_PANELS = {1, 2, 3, 4, 7, 8, 9, 10}


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def _panels(dashboard: dict):
    for panel in dashboard.get("panels", []):
        yield panel
        yield from panel.get("panels", [])


def _expressions(dashboard: dict):
    for panel in _panels(dashboard):
        for target in panel.get("targets", []):
            if target.get("expr"):
                yield target["expr"]


def test_there_are_four_dashboards():
    assert len(DASHBOARDS) == 4


def test_every_dashboard_parses_and_is_uniquely_identified():
    uids = set()
    for path in DASHBOARDS:
        data = _load(path)
        assert data["uid"] not in uids, f"duplicate uid in {path.name}"
        uids.add(data["uid"])
        assert data["title"]
    assert uids == EXPECTED_UIDS


def test_every_panel_uses_the_pinned_datasource():
    for path in DASHBOARDS:
        for panel in _panels(_load(path)):
            ds = panel.get("datasource")
            if ds is None:
                continue
            assert ds.get("uid") == "kine-prom", f"{path.name}: {panel.get('title')}"


def test_every_panel_has_a_unique_id_and_a_title():
    for path in DASHBOARDS:
        panels = list(_panels(_load(path)))
        ids = [p["id"] for p in panels]
        assert len(ids) == len(set(ids)), f"duplicate panel id in {path.name}"
        assert all(p.get("title") is not None for p in panels)


def test_every_panel_has_at_least_one_target():
    for path in DASHBOARDS:
        for panel in _panels(_load(path)):
            assert panel.get("targets"), f"{path.name}: {panel.get('title')} has no query"


def test_dashboards_only_reference_metrics_that_exist():
    known = set(metrics.METRIC_TYPES)
    for path in DASHBOARDS:
        for expr in _expressions(_load(path)):
            for name in re.findall(r"\b[a-z][a-z0-9_]*_[a-z0-9_]+\b", expr):
                if name.startswith(EXTERNAL_PREFIXES) or not name.startswith("kine_"):
                    continue
                assert name in known, f"{path.name} references unknown metric {name}"


def test_the_panels_helm_embeds_all_exist():
    overview = _load(DASHBOARD_DIR / "kine-overview.json")
    ids = {p["id"] for p in _panels(overview)}
    assert EMBEDDED_OVERVIEW_PANELS <= ids
