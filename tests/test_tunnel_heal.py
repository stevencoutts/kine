"""Detect and heal apps still pinned to a dead gluetun network namespace."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import tunnel_heal, vpn_routing  # noqa: E402


def test_container_id_from_network_mode():
    assert tunnel_heal.container_id("container:abc123def") == "abc123def"
    assert tunnel_heal.container_id("container:abc123def456") == "abc123def456"
    assert tunnel_heal.container_id("service:gluetun") is None
    assert tunnel_heal.container_id("bridge") is None
    assert tunnel_heal.container_id("") is None


def test_orphan_services_when_pinned_to_old_gluetun():
    orphans = tunnel_heal.orphan_services(
        expected_id="livegluetunid01",
        network_modes={
            "sonarr": "container:deadgluetunid99",
            "radarr": "container:livegluetunid01",
            "prowlarr": "container:deadgluetunid99",
            "emby": "kine_edge",
        },
        peers={"sonarr", "radarr", "prowlarr", "nzbget"},
    )
    assert orphans == ["prowlarr", "sonarr"]


def test_orphan_services_empty_when_gluetun_missing():
    assert tunnel_heal.orphan_services(
        expected_id=None,
        network_modes={"sonarr": "container:anything"},
        peers={"sonarr"},
    ) == []


def test_orphan_services_ignores_non_tunnelled():
    assert tunnel_heal.orphan_services(
        expected_id="live",
        network_modes={"emby": "container:other"},
        peers={"sonarr"},
    ) == []


def test_orphan_services_secondary_tunnel_only():
    """Two tunnels: only the secondary peer pinned to a dead id is listed."""
    modes = {
        "sonarr": "container:primarylive01",
        "radarr": "container:primarylive01",
        "dispatcharr": "container:deadsecondary99",
        "ecm": "container:secondarylive02",
        "teamarr": "container:secondarylive02",
    }
    assert tunnel_heal.orphan_services(
        expected_id="primarylive01",
        network_modes=modes,
        peers={"sonarr", "radarr"},
    ) == []
    assert tunnel_heal.orphan_services(
        expected_id="secondarylive02",
        network_modes=modes,
        peers={"dispatcharr", "ecm", "teamarr"},
    ) == ["dispatcharr"]


def test_container_to_service():
    assert tunnel_heal.container_to_service("kine-gluetun") == "gluetun"
    assert tunnel_heal.container_to_service("kine-gluetun-11111111") == "gluetun-11111111"
    assert tunnel_heal.container_to_service("kine-sonarr") is None


def test_container_name_for_tunnel_service():
    assert vpn_routing.container_name_for_tunnel_service("gluetun") == "kine-gluetun"
    assert (
        vpn_routing.container_name_for_tunnel_service("gluetun-11111111")
        == "kine-gluetun-11111111"
    )


def test_discover_gluetun_services():
    def runner(cmd: list[str]):
        from types import SimpleNamespace

        assert cmd[:3] == ["docker", "ps", "--filter"]
        return SimpleNamespace(
            returncode=0,
            stdout="kine-gluetun\nkine-gluetun-11111111\nkine-gluetun\n",
            stderr="",
        )

    assert tunnel_heal.discover_gluetun_services(runner=runner) == [
        "gluetun",
        "gluetun-11111111",
    ]


def test_heal_all_multi_tunnel():
    calls: list[list[str]] = []

    def runner(cmd: list[str]):
        calls.append(cmd)
        if cmd[:3] == ["docker", "inspect", "--format"]:
            field = cmd[3]
            name = cmd[4]
            ids = {
                "kine-gluetun": "primarylive01",
                "kine-gluetun-11111111": "secondarylive02",
                "kine-sonarr": "primarylive01",
                "kine-radarr": "primarylive01",
                "kine-dispatcharr": "deadsecondary99",
                "kine-ecm": "secondarylive02",
            }
            if field == "{{.Id}}":
                from types import SimpleNamespace
                cid = ids.get(name)
                return SimpleNamespace(returncode=0 if cid else 1, stdout=cid or "", stderr="")
            if field == "{{.HostConfig.NetworkMode}}":
                modes = {
                    "kine-sonarr": "container:primarylive01",
                    "kine-radarr": "container:primarylive01",
                    "kine-dispatcharr": "container:deadsecondary99",
                    "kine-ecm": "container:secondarylive02",
                }
                from types import SimpleNamespace
                mode = modes.get(name, "")
                return SimpleNamespace(returncode=0, stdout=mode, stderr="")
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = tunnel_heal.heal_all(
        {
            "gluetun": {"sonarr", "radarr"},
            "gluetun-11111111": {"dispatcharr", "ecm"},
        },
        runner=runner,
    )
    assert result["ok"] is True
    assert result["healed"] == ["dispatcharr"]
    recreate = [c for c in calls if c[:2] == ["docker", "compose"]]
    assert recreate
    assert "dispatcharr" in recreate[0]
    assert "sonarr" not in recreate[0]
    assert "radarr" not in recreate[0]


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
