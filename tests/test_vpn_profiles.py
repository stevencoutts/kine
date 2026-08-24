"""VPN multi-profile store and activation helpers."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import vpn_profiles  # noqa: E402

VALID_CONF = """[Interface]
PrivateKey = YJqK8nV3mP0sL2wQ9eR5tY7uI1oP3aS4dF6gH8jK0lM=
Address = 10.2.0.2/32
[Peer]
PublicKey = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx=
Endpoint = 1.2.3.4:51820
"""


def test_migrate_imports_wg0_as_default(tmp_path):
    wg = tmp_path / "config" / "gluetun" / "wireguard"
    wg.mkdir(parents=True)
    (wg / "wg0.conf").write_text(VALID_CONF)
    data = vpn_profiles.migrate_from_wg0(str(tmp_path))
    assert data["active_id"]
    assert len(data["profiles"]) == 1
    assert data["profiles"][0]["name"] == "Default"
    assert data["profiles"][0]["type"] == "wireguard"
    assert "PrivateKey =" in data["profiles"][0]["conf"]
    data2 = vpn_profiles.migrate_from_wg0(str(tmp_path))
    assert len(data2["profiles"]) == 1


def test_redact_conf_strips_private_key():
    raw = "[Interface]\nPrivateKey = SECRET\nAddress = 10.0.0.2/32\n"
    out = vpn_profiles.redact_conf(raw)
    assert "SECRET" not in out
    assert "PrivateKey" in out
    assert "Address = 10.0.0.2/32" in out


def test_summary_omits_conf(tmp_path):
    data = {
        "active_id": "1",
        "profiles": [{
            "id": "1", "name": "Default", "type": "wireguard",
            "conf": "x", "updated_at": "t",
        }],
    }
    rows = vpn_profiles.summary(data)
    assert rows[0]["name"] == "Default"
    assert rows[0]["active"] is True
    assert "conf" not in rows[0]


def test_add_and_prepare_activate(tmp_path):
    p = vpn_profiles.add_profile(str(tmp_path), "Home", VALID_CONF)
    conf, fields = vpn_profiles.prepare_activate(str(tmp_path), p["id"])
    assert "PrivateKey" in conf
    assert fields["VPN_TYPE"] == "wireguard"
    assert fields["WIREGUARD_ENDPOINT_IP"] == "1.2.3.4"


def test_delete_active_refused(tmp_path):
    p = vpn_profiles.add_profile(str(tmp_path), "Only", VALID_CONF)
    try:
        vpn_profiles.delete_profile(str(tmp_path), p["id"])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_vpn_profile_routes_exist():
    main = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    assert '@app.post("/api/vpn/profiles")' in main
    assert '@app.put("/api/vpn/profiles/{profile_id}")' in main
    assert '@app.delete("/api/vpn/profiles/{profile_id}")' in main
    assert '@app.post("/api/vpn/profiles/{profile_id}/activate")' in main
    assert '@app.post("/api/vpn/disable")' in main
    assert "vpn_profiles.migrate_from_wg0" in main
    assert "VPN_ENABLED" in main.split("vpn_profile_activate", 1)[1][:800]


def test_vpn_ui_has_profile_cards():
    frontend = (ROOT / "helm" / "frontend" / "index.html").read_text()
    assert "vpn-card" in frontend
    assert "vpn-profiles" in frontend
    assert "data-vpn-activate" in frontend
    assert "promptVpnProfile" in frontend
    assert "/vpn/profiles" in frontend
