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


def test_defaults_match_catalogue_flags():
    expected = {k for k, v in CATALOGUE.items() if v.get("default") or v.get("mandatory")}
    assert set(catalogue.defaults()) == expected


def test_acquisition_defaults_exclude_optional_extras():
    acq = catalogue.tier_default_apps("acquisition")
    assert acq == ["sonarr", "radarr", "prowlarr", "jackett", "transmission"]
    assert "seerr" not in acq
    assert CATALOGUE["seerr"]["default"] is False
    assert CATALOGUE["seerr"].get("tunnelled") != "forced"


def test_resolve_deps_pulls_in_gluetun():
    wanted = catalogue.resolve_deps("sonarr", CATALOGUE, ["sonarr"])
    assert "gluetun" in wanted


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
