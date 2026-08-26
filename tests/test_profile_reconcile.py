"""Stop catalogue containers that are running while their profile is off."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import profile_reconcile  # noqa: E402


def test_disabled_running_services():
    found = profile_reconcile.disabled_running_services(
        catalogue_ids={"sonarr", "jackett", "unpackerr", "grafana"},
        enabled={"sonarr", "grafana"},
        running={"sonarr", "jackett", "unpackerr", "traefik"},
    )
    assert found == ["jackett", "unpackerr"]


def test_disabled_running_empty_when_aligned():
    assert profile_reconcile.disabled_running_services(
        catalogue_ids={"sonarr", "jackett"},
        enabled={"sonarr"},
        running={"sonarr"},
    ) == []


def test_disabled_running_ignores_non_catalogue_containers():
    assert profile_reconcile.disabled_running_services(
        catalogue_ids={"sonarr"},
        enabled={"sonarr"},
        running={"sonarr", "traefik", "helm"},
    ) == []


def test_parse_compose_ps_service_names():
    blob = (
        '{"Name":"kine-jackett","Service":"jackett","State":"running"}\n'
        '{"Name":"kine-sonarr","Service":"sonarr","State":"running"}\n'
        '{"Name":"kine-old","Service":"unpackerr","State":"exited"}\n'
    )
    assert profile_reconcile.running_service_names(blob) == {"jackett", "sonarr"}


def test_main_reconciles_disabled_runners():
    main = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    assert "profile_reconcile" in main
    assert "reconcile_disabled_running" in main
