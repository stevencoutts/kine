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


def test_migrate_schema_active_id_to_primary():
    raw = {
        "active_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "profiles": [{
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "name": "Default",
            "type": "wireguard",
            "conf": "x",
            "updated_at": "t",
        }],
    }
    data = vpn_profiles.migrate_schema(raw)
    assert data["primary_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert "active_id" not in data
    assert data["profiles"][0]["apps"] == []


def test_tunnel_service_leftovers_use_primary():
    data = {
        "primary_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "profiles": [
            {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "apps": []},
            {"id": "11111111-2222-3333-4444-555555555555", "apps": ["dispatcharr"]},
        ],
    }
    assert vpn_profiles.tunnel_service(data, "sonarr") == "gluetun"
    assert vpn_profiles.tunnel_service(data, "dispatcharr") == "gluetun_11111111"


def test_short_id():
    assert vpn_profiles.short_id("11111111-2222-3333-4444-555555555555") == "11111111"


def test_short_id_non_uuid_fallback():
    assert vpn_profiles.short_id("profileabc123") == "profilea"
    assert vpn_profiles.short_id("ab") == "ab000000"


def test_migrate_schema_normalizes_malformed_apps():
    raw = {
        "primary_id": "1",
        "profiles": [{
            "id": "1",
            "name": "Default",
            "apps": "dispatcharr",
        }],
    }
    data = vpn_profiles.migrate_schema(raw)
    assert data["profiles"][0]["apps"] == []
    assert raw["profiles"][0]["apps"] == "dispatcharr"


def test_migrate_imports_wg0_as_default(tmp_path):
    wg = tmp_path / "config" / "gluetun" / "wireguard"
    wg.mkdir(parents=True)
    (wg / "wg0.conf").write_text(VALID_CONF)
    data = vpn_profiles.migrate_from_wg0(str(tmp_path))
    assert data["primary_id"]
    assert "active_id" not in data
    assert len(data["profiles"]) == 1
    assert data["profiles"][0]["name"] == "Default"
    assert data["profiles"][0]["type"] == "wireguard"
    assert data["profiles"][0]["apps"] == []
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
        "primary_id": "1",
        "profiles": [{
            "id": "1", "name": "Default", "type": "wireguard",
            "conf": "x", "updated_at": "t", "apps": ["sonarr"],
        }],
    }
    rows = vpn_profiles.summary(data)
    assert rows[0]["name"] == "Default"
    assert rows[0]["primary"] is True
    assert rows[0]["apps"] == ["sonarr"]
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


def test_vpn_tunnel_group_only_includes_enabled_profiles():
    main = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    assert "def _vpn_tunnel_group" in main
    assert "_tunnelled_profiles()" in main.split("def _vpn_tunnel_group", 1)[1].split("\n\n", 1)[0]


def test_kine_vpn_group_filters_by_compose_profiles():
    kine = (ROOT / "kine").read_text()
    assert "COMPOSE_PROFILES=" in kine.split("vpn_group()", 1)[1].split("\n}\n", 1)[0]


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


def test_vpn_ui_collapses_detail_into_active_profile():
    """Live tunnel state belongs on the active profile card, not a second widget."""
    frontend = (ROOT / "helm" / "frontend" / "index.html").read_text()
    vpn = frontend.split("render.vpn = async () => {", 1)[1].split("\nconst fmtBytes", 1)[0]
    assert 'id="leak"' in vpn
    assert "Public IP" in vpn
    # Old standalone hero used connection type as the card title with a status dot.
    assert 'vpn-card-title"><span class="dot' not in vpn
    assert "<h2>VPN</h2>" not in vpn
    # Detail / tunnel actions are rendered for the active profile only.
    assert "p.active" in vpn
    assert "vpn-active-detail" in vpn
