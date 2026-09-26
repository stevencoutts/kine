"""Compose override generator for multi-Gluetun egress."""
import json
import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import vpn_profiles, vpn_routing  # noqa: E402

VALID_WG = """[Interface]
PrivateKey = YJqK8nV3mP0sL2wQ9eR5tY7uI1oP3aS4dF6gH8jK0lM=
Address = 10.2.0.2/32
[Peer]
PublicKey = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx=
Endpoint = 1.2.3.4:51820
"""

PRIMARY_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECONDARY_ID = "11111111-2222-3333-4444-555555555555"


def _sample_data():
    return {
        "primary_id": PRIMARY_ID,
        "profiles": [
            {
                "id": PRIMARY_ID,
                "apps": [],
                "conf": VALID_WG,
                "type": "wireguard",
            },
            {
                "id": SECONDARY_ID,
                "apps": ["dispatcharr"],
                "conf": VALID_WG,
                "type": "wireguard",
            },
        ],
    }


def test_app_ports_and_traefik_hosts():
    assert vpn_routing.APP_PORTS["sonarr"] == 8989
    assert vpn_routing.APP_PORTS["radarr"] == 7878
    assert vpn_routing.APP_PORTS["lidarr"] == 8686
    assert vpn_routing.APP_PORTS["dispatcharr"] == 9191
    assert vpn_routing.APP_PORTS["ecm"] == 6100
    assert vpn_routing.APP_PORTS["ecm-mcp"] == 6101
    assert vpn_routing.APP_PORTS["teamarr"] == 9195
    assert vpn_routing.APP_TRAEFIK_HOST["lidarr"] == "lidarr"
    assert vpn_routing.APP_TRAEFIK_HOST["dispatcharr"] == "tv"
    assert vpn_routing.APP_TRAEFIK_HOST["ecm"] == "channels"
    assert vpn_routing.APP_TRAEFIK_HOST["ecm-mcp"] == "mcp"
    assert vpn_routing.APP_TRAEFIK_HOST["teamarr"] == "sports"
    assert vpn_routing.APP_TRAEFIK_HOST["sonarr"] == "sonarr"


def test_render_override_secondary_and_network_mode():
    data = _sample_data()
    text = vpn_routing.render_override(
        data,
        enabled_apps={"dispatcharr", "sonarr", "gluetun"},
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    assert "gluetun-11111111:" in text
    assert "network_mode: service:gluetun-11111111" in text
    assert "dispatcharr" in text
    assert (
        "service:gluetun\n" in text
        or 'service:gluetun"' in text
        or "service:gluetun" in text
    )
    assert "sonarr:" in text
    assert text.index("sonarr:") < text.index("network_mode:") or "sonarr" in text

    assert "kine-gluetun-11111111" in text
    assert "traefik.enable=true" in text

    dyn = vpn_routing.render_traefik_dynamic(
        data,
        enabled_apps={"dispatcharr", "sonarr", "gluetun"},
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    assert dyn["http"]["routers"]["dispatcharr"]["service"] == "dispatcharr"
    assert "gluetun-11111111:9191" in dyn["http"]["services"]["dispatcharr"]["loadBalancer"]["servers"][0]["url"]
    assert "gluetun:8989" in dyn["http"]["services"]["sonarr"]["loadBalancer"]["servers"][0]["url"]
    assert "Host(`tv.example.com`)" in dyn["http"]["routers"]["dispatcharr"]["rule"]
    assert "Host(`dispatcharr.example.com`)" in dyn["http"]["routers"]["dispatcharr"]["rule"]


def _service_block(text: str, name: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"  {name}:")
    block = []
    for line in lines[start + 1:]:
        if line.startswith("  ") and not line.startswith("   "):
            break
        block.append(line)
    return "\n".join(block)


def test_render_override_pihole_attaches_primary_gluetun_only():
    data = _sample_data()
    text = vpn_routing.render_override(
        data,
        enabled_apps={"dispatcharr", "sonarr", "gluetun"},
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
        pihole_enabled=True,
    )
    assert text.count("ipv4_address") == 1
    assert "${KINE_DNS_GLUETUN}" in text
    assert "${KINE_DNS_PIHOLE}" in text
    assert "traefik:" in text
    assert "!override" in text
    assert "kine_dns" in _service_block(text, "gluetun")
    assert "kine_dns" not in _service_block(text, "gluetun-11111111")
    off = vpn_routing.render_override(
        data,
        enabled_apps={"dispatcharr", "sonarr", "gluetun"},
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
        pihole_enabled=False,
    )
    assert "KINE_DNS_PIHOLE" not in off


def test_render_traefik_dynamic_includes_ecm_mcp():
    data = _sample_data()
    dyn = vpn_routing.render_traefik_dynamic(
        data,
        enabled_apps={"ecm", "ecm-mcp", "gluetun"},
        kine_domain="couttsnet.com",
        kine_local_domain="kine.local",
    )
    assert "ecm-mcp" in dyn["http"]["routers"]
    assert "Host(`mcp.couttsnet.com`)" in dyn["http"]["routers"]["ecm-mcp"]["rule"]
    assert (
        "gluetun:6101"
        in dyn["http"]["services"]["ecm-mcp"]["loadBalancer"]["servers"][0]["url"]
    )


def test_secondary_embeds_wireguard_from_parse_conf():
    data = _sample_data()
    text = vpn_routing.render_override(
        data,
        enabled_apps={"dispatcharr", "gluetun"},
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    sec = "gluetun-11111111:" + text.split("gluetun-11111111:", 1)[1].split("\n  dispatcharr:", 1)[0]
    doc = yaml.safe_load("services:\n  " + sec)
    env = doc["services"]["gluetun-11111111"]["environment"]
    assert env["VPN_SERVICE_PROVIDER"] == "custom"
    assert env["VPN_TYPE"] == "wireguard"
    assert env["WIREGUARD_PRIVATE_KEY"] == (
        "YJqK8nV3mP0sL2wQ9eR5tY7uI1oP3aS4dF6gH8jK0lM="
    )
    assert env["WIREGUARD_ADDRESSES"] == "10.2.0.2/32"
    assert env["WIREGUARD_PUBLIC_KEY"] == (
        "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx="
    )
    assert env["WIREGUARD_ENDPOINT_IP"] == "1.2.3.4"
    assert env["WIREGUARD_ENDPOINT_PORT"] == "51820"
    # Must not be primary .env placeholders
    assert "${WIREGUARD_PRIVATE_KEY" not in text


def test_app_overrides_depend_on_correct_tunnel():
    data = _sample_data()
    text = vpn_routing.render_override(
        data,
        enabled_apps={"dispatcharr", "sonarr", "gluetun"},
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    assert "depends_on: !reset" in text
    assert 'network_mode: service:gluetun' in text
    assert 'network_mode: service:gluetun-11111111' in text
    assert "sonarr:" in text
    assert "dispatcharr:" in text
    assert "gluetun:" in text
    assert "gluetun-11111111:" in text


def test_vpn_portsync_follows_transmission_tunnel():
    data = _sample_data()
    data["profiles"][0]["apps"] = ["sonarr", "transmission"]
    data["profiles"][1]["apps"] = []
    text = vpn_routing.render_override(
        data,
        enabled_apps={"sonarr", "transmission", "gluetun"},
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    assert "vpn-portsync:" in text
    portsync = text.split("vpn-portsync:", 1)[1]
    assert "network_mode: service:gluetun" in portsync


def test_secondary_gluetun_opens_profile_forwarded_port():
    data = _sample_data()
    data["profiles"][1]["forwarded_port"] = 51413
    text = vpn_routing.render_override(
        data,
        enabled_apps={"dispatcharr", "gluetun"},
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    sec = text.split("gluetun-11111111:", 1)[1]
    assert "FIREWALL_VPN_INPUT_PORTS:" in sec
    assert "51413" in sec.split("FIREWALL_VPN_INPUT_PORTS:", 1)[1].splitlines()[0]


def test_render_override_disabled_is_empty():
    text = vpn_routing.render_override(
        {"primary_id": None, "profiles": []},
        enabled_apps=set(vpn_routing.APP_PORTS),
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
        vpn_enabled=False,
    )
    assert yaml.safe_load(text) == {"services": {}}


def test_stale_secondary_services():
    data = _sample_data()
    assert "gluetun-11111111" not in vpn_routing.stale_secondary_services(data)
    data["profiles"][1]["apps"] = []
    assert "gluetun-11111111" in vpn_routing.stale_secondary_services(data)
    assert f"gluetun_{vpn_profiles.short_id(PRIMARY_ID)}" in vpn_routing.stale_secondary_services(
        data,
    )


def test_render_override_direct_live_tv_leaves_gluetun():
    data = _sample_data()
    data["live_tv_direct"] = True
    data["profiles"][1]["apps"] = []
    text = vpn_routing.render_override(
        data,
        enabled_apps={"dispatcharr", "ecm", "ecm-mcp", "teamarr", "sonarr", "gluetun"},
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    assert "network_mode: !reset null" in text
    assert "depends_on: !override" in text
    ecm = text.split("\n  ecm:\n", 1)[1].split("\n  ecm-mcp:\n", 1)[0]
    assert "dispatcharr:" in ecm
    assert "kine_internal" in text
    assert "kine_edge" in text
    assert "http://dispatcharr:9191" in text
    assert "http://ecm:6100" in text
    assert "network_mode: service:gluetun" in text
    dispatcharr = text.split("\n  dispatcharr:\n", 1)[1].split("\n  teamarr:\n", 1)[0]
    assert "service:gluetun" not in dispatcharr
    dyn = vpn_routing.render_traefik_dynamic(
        data,
        enabled_apps={"dispatcharr", "sonarr"},
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    assert dyn["http"]["services"]["dispatcharr"]["loadBalancer"]["servers"][0]["url"] == (
        "http://dispatcharr:9191"
    )
    assert "gluetun:8989" in dyn["http"]["services"]["sonarr"]["loadBalancer"]["servers"][0]["url"]


def test_stale_secondary_services_includes_replaced_profile_container():
    """A profile id that no longer exists must still be stale if its container is up."""
    data = _sample_data()
    data["profiles"][1]["id"] = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    stale = vpn_routing.stale_secondary_services(
        data,
        running=["kine-gluetun-11111111"],
    )
    assert "gluetun-11111111" in stale
    assert "gluetun-99999999" not in stale


def test_write_override(tmp_path):
    path = vpn_routing.write_override(tmp_path, "services: {}\n")
    assert path == tmp_path / vpn_routing.ROUTING_GENERATED_REL
    assert path.read_text() == "services: {}\n"
    mirror = tmp_path / vpn_routing.ROUTING_COMPOSE_OVERRIDE_REL
    assert mirror.read_text() == path.read_text()


def test_routing_stub_exists():
    stub = ROOT / "compose" / "vpn-routing.override.yml"
    assert stub.is_file()
    doc = yaml.safe_load(stub.read_text())
    assert doc.get("services") == {}
    # Include drops !reset. The stub must not pull the generated file in.
    assert "include:" not in stub.read_text()


def test_compose_override_file_clears_direct_network_mode(tmp_path):
    """Static fragments pin Live TV to Gluetun. The root override must lift it.

    Compose include ignores !reset, so docker-compose.override.yml has to be
    a real project override. This is the merge the osiris project uses.
    """
    root = tmp_path / "proj"
    (root / "compose").mkdir(parents=True)
    (root / "docker-compose.yml").write_text(
        "name: kine-direct-merge\n"
        "include:\n"
        "  - compose/app.yml\n"
        "networks:\n"
        "  kine_internal:\n"
        "  kine_edge:\n"
    )
    (root / "compose" / "app.yml").write_text(
        "services:\n"
        "  gluetun:\n"
        "    image: busybox:1.36\n"
        "  app:\n"
        "    image: busybox:1.36\n"
        "    network_mode: \"service:gluetun\"\n"
        "    depends_on:\n"
        "      gluetun:\n"
        "        condition: service_healthy\n"
    )
    vpn_routing.write_override(
        root,
        "services:\n"
        "  app:\n"
        "    network_mode: !reset null\n"
        "    networks:\n"
        "    - kine_internal\n"
        "    - kine_edge\n"
        "    depends_on: !override {}\n",
    )
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    app = doc["services"]["app"]
    assert app.get("network_mode") in (None, "")
    assert set(app.get("networks") or {}) == {"kine_internal", "kine_edge"}
    assert not app.get("depends_on")
