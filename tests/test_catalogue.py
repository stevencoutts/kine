"""Catalogue helpers used by Helm tier controls."""
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

import catalogue  # noqa: E402

CATALOGUE = yaml.safe_load((ROOT / "catalogue.yml").read_text())["apps"]


@pytest.fixture(autouse=True)
def repo_root(monkeypatch):
    monkeypatch.setenv("KINE_REPO", str(ROOT))
    monkeypatch.setattr(catalogue, "REPO", ROOT)


def test_nzbget_is_off_by_default():
    assert CATALOGUE["nzbget"]["default"] is False


def test_emby_is_off_by_default():
    assert CATALOGUE["emby"]["default"] is False
    assert catalogue.tier_default_apps("media") == []


def test_defaults_match_catalogue_flags():
    expected = {k for k, v in CATALOGUE.items() if v.get("default") or v.get("mandatory")}
    assert set(catalogue.defaults()) == expected


def test_acquisition_defaults_exclude_optional_extras():
    acq = catalogue.tier_default_apps("acquisition")
    assert acq == ["sonarr", "radarr", "prowlarr", "transmission", "recyclarr"]
    assert "jackett" not in acq
    assert CATALOGUE["jackett"]["default"] is False
    assert "seerr" not in acq
    assert CATALOGUE["seerr"]["default"] is False
    assert CATALOGUE["seerr"].get("tunnelled") != "forced"


def test_process_tier_defaults_to_tdarr():
    assert catalogue.tier_default_apps("process") == ["tdarr"]
    assert CATALOGUE["tdarr"]["tier"] == "process"


def test_resolve_deps_pulls_in_gluetun():
    wanted = catalogue.resolve_deps("sonarr", CATALOGUE, ["sonarr"])
    assert "gluetun" in wanted


def test_resolve_deps_follows_multi_hop_chains():
    cat = {
        "grafana": {"requires": ["prometheus"]},
        "prometheus": {"requires": ["cadvisor", "node-exporter"]},
        "cadvisor": {},
        "node-exporter": {},
    }
    wanted = catalogue.resolve_deps("grafana", cat, ["grafana"])
    assert set(wanted) == {"grafana", "prometheus", "cadvisor", "node-exporter"}


def test_resolve_deps_survives_a_dependency_cycle():
    cat = {"a": {"requires": ["b"]}, "b": {"requires": ["a"]}}
    assert set(catalogue.resolve_deps("a", cat, ["a"])) == {"a", "b"}


def test_prune_orphan_gluetun_keeps_mandatory_tunnel_when_empty():
    pruned = catalogue.prune_orphan_gluetun(["emby", "gluetun"], CATALOGUE)
    assert pruned == ["emby", "gluetun"]


def test_prune_orphan_gluetun_keeps_tunnel_with_acquisition():
    pruned = catalogue.prune_orphan_gluetun(["sonarr", "gluetun"], CATALOGUE)
    assert "gluetun" in pruned


def test_load_reads_repo_catalogue():
    apps = catalogue.load()
    assert "emby" in apps
    assert apps["emby"]["tier"] == "media"


def test_metrics_tier_is_labelled_for_the_gui():
    assert catalogue.TIER_LABELS["metrics"] == "Metrics"


def test_prune_orphan_deps_drops_metrics_chain_when_grafana_goes():
    cat = {
        "grafana": {"requires": ["prometheus"]},
        "prometheus": {"requires": ["cadvisor", "node-exporter"], "hidden": True},
        "cadvisor": {"hidden": True},
        "node-exporter": {"hidden": True},
        "sonarr": {},
    }
    pruned = catalogue.prune_orphan_deps(
        ["sonarr", "grafana", "prometheus", "cadvisor", "node-exporter"], cat,
    )
    # grafana is still wanted here — nothing should drop
    assert "prometheus" in pruned

    pruned = catalogue.prune_orphan_deps(
        ["sonarr", "prometheus", "cadvisor", "node-exporter"], cat,
    )
    assert pruned == ["sonarr"]


def test_prune_orphan_deps_keeps_shared_hidden_dep():
    cat = {
        "a": {"requires": ["shared"]},
        "b": {"requires": ["shared"]},
        "shared": {"hidden": True},
    }
    assert catalogue.prune_orphan_deps(["a", "shared"], cat) == ["a", "shared"]
    assert catalogue.prune_orphan_deps(["shared"], cat) == []
