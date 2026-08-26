"""Detect and heal apps still pinned to a dead gluetun network namespace."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import tunnel_heal  # noqa: E402


def test_container_id_from_network_mode():
    assert tunnel_heal.container_id("container:abc123def") == "abc123def"
    assert tunnel_heal.container_id("container:abc123def456") == "abc123def456"
    assert tunnel_heal.container_id("service:gluetun") is None
    assert tunnel_heal.container_id("bridge") is None
    assert tunnel_heal.container_id("") is None


def test_orphan_services_when_pinned_to_old_gluetun():
    orphans = tunnel_heal.orphan_services(
        gluetun_id="livegluetunid01",
        network_modes={
            "sonarr": "container:deadgluetunid99",
            "radarr": "container:livegluetunid01",
            "prowlarr": "container:deadgluetunid99",
            "emby": "kine_edge",
        },
        tunnelled={"sonarr", "radarr", "prowlarr", "nzbget"},
    )
    assert orphans == ["prowlarr", "sonarr"]


def test_orphan_services_empty_when_gluetun_missing():
    assert tunnel_heal.orphan_services(
        gluetun_id=None,
        network_modes={"sonarr": "container:anything"},
        tunnelled={"sonarr"},
    ) == []


def test_orphan_services_ignores_non_tunnelled():
    assert tunnel_heal.orphan_services(
        gluetun_id="live",
        network_modes={"emby": "container:other"},
        tunnelled={"sonarr"},
    ) == []


def test_updates_script_heals_after_every_apply():
    script = (ROOT / "scripts" / "updates.sh").read_text()
    assert "heal-tunnel-orphans" in script or "tunnel_heal" in script
    assert "heal" in script.lower()


def test_apply_update_heals_tunnel_orphans():
    main = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    apply = main.split("@app.post(\"/api/updates/{app_id}\")", 1)[1].split(
        "@app.", 1
    )[0]
    assert "tunnel_heal" in apply or "heal_orphans" in apply
