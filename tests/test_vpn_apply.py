"""Filesystem apply + peers_for for multi-Gluetun orchestration."""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import vpn_routing, wireguard  # noqa: E402

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


def test_peers_for_secondary():
    data = _sample_data()
    assert vpn_routing.peers_for(
        data, "gluetun-11111111", {"dispatcharr", "sonarr"}
    ) == ["dispatcharr"]
    assert "sonarr" in vpn_routing.peers_for(
        data, "gluetun", {"dispatcharr", "sonarr"}
    )


def test_running_secondaries():
    data = _sample_data()
    rows = vpn_routing.running_secondaries(data)
    assert len(rows) == 1
    profile, svc = rows[0]
    assert profile["id"] == SECONDARY_ID
    assert svc == "gluetun-11111111"


def test_secondary_volume_uses_stack_root_env_var():
    data = _sample_data()
    text = vpn_routing.render_override(
        data,
        enabled_apps={"dispatcharr", "gluetun"},
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    doc = yaml.safe_load(
        "services:\n  gluetun-11111111:"
        + text.split("gluetun-11111111:", 1)[1].split("\n  dispatcharr:", 1)[0]
    )
    vols = doc["services"]["gluetun-11111111"]["volumes"]
    assert vols == ["${STACK_ROOT}/config/gluetun-11111111:/gluetun"]


def test_apply_filesystem_writes_confs_and_override(tmp_path):
    stack = tmp_path / "stack"
    repo = tmp_path / "repo"
    data = _sample_data()
    vpn_routing.apply_filesystem(
        str(stack),
        repo,
        data,
        {"dispatcharr", "sonarr"},
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    primary = stack / "config" / "gluetun" / "wireguard" / "wg0.conf"
    secondary = (
        stack / "config" / "gluetun-11111111" / "wireguard" / "wg0.conf"
    )
    assert primary.is_file()
    assert "PrivateKey" in primary.read_text()
    assert secondary.is_file()
    override = repo / vpn_routing.ROUTING_GENERATED_REL
    assert override.is_file()
    text = override.read_text()
    assert "gluetun-11111111:" in text
    assert "${STACK_ROOT}/config/gluetun-11111111:/gluetun" in text
    assert "traefik.http.routers.sonarr" in text
    assert "traefik.http.routers.dispatcharr" in text


def test_write_secondary_conf(tmp_path):
    wireguard.write_secondary_conf(str(tmp_path), "abcd1234", VALID_WG)
    path = tmp_path / "config" / "gluetun-abcd1234" / "wireguard" / "wg0.conf"
    assert path.is_file()
    assert path.read_text().startswith("[Interface]")
