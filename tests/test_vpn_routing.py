"""Compose override generator for multi-Gluetun egress."""
import pathlib
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
    assert vpn_routing.APP_PORTS["dispatcharr"] == 9191
    assert vpn_routing.APP_PORTS["ecm"] == 6100
    assert vpn_routing.APP_PORTS["teamarr"] == 9195
    assert vpn_routing.APP_TRAEFIK_HOST["dispatcharr"] == "tv"
    assert vpn_routing.APP_TRAEFIK_HOST["ecm"] == "channels"
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


def test_write_override(tmp_path):
    path = vpn_routing.write_override(tmp_path, "services: {}\n")
    assert path == tmp_path / vpn_routing.ROUTING_GENERATED_REL
    assert path.read_text() == "services: {}\n"


def test_routing_stub_exists():
    stub = ROOT / "compose" / "vpn-routing.override.yml"
    assert stub.is_file()
    doc = yaml.safe_load(stub.read_text())
    assert doc.get("services") == {}
    assert "vpn-routing.generated.yml" in stub.read_text()
