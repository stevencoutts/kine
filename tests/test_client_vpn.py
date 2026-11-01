"""Laptop VPN plans. These must stay true so the feature cannot take
the live tunnel down with it: compose never names gluetun, routing is
only for the laptop subnet, and the relay refuses pid 1."""
import base64
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

import client_vpn  # noqa: E402


def test_x25519_matches_rfc7748():
    scalar = bytes.fromhex(
        "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
    )
    public = client_vpn.public_from_private(base64.b64encode(scalar).decode())
    assert base64.b64decode(public) == bytes.fromhex(
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    )


def test_generated_keypair_round_trips():
    private, public = client_vpn.generate_keypair()
    assert public == client_vpn.public_from_private(private)
    assert len(base64.b64decode(private)) == 32


def test_client_config_is_a_full_tunnel_with_two_endpoints():
    state = client_vpn.fresh_state("kine.local", ["emby"])
    state["lan_endpoint"] = "192.168.1.20"
    state["wan_endpoint"] = "home.example.com"
    state["host_port"] = 51820
    peer = client_vpn.add_peer(state, "Laptop")
    home = client_vpn.client_config(state, peer, "lan")
    away = client_vpn.client_config(state, peer, "wan")
    assert "Endpoint = 192.168.1.20:51820" in home
    assert "Endpoint = home.example.com:51820" in away
    assert "AllowedIPs = 0.0.0.0/0" in home
    assert f"DNS = {client_vpn.SERVER_ADDRESS}" in home
    assert state["server_private"] not in home
    assert peer["private_key"] in home
    other = client_vpn.add_peer(state, "Phone")
    assert other["address"] != peer["address"]
    assert other["private_key"] != peer["private_key"]
    again = client_vpn.client_config(state, peer, "lan")
    assert peer["private_key"] in again


def test_adding_a_peer_does_not_call_compose():
    state = client_vpn.fresh_state("kine.local", [])
    state = client_vpn.apply_settings(
        state,
        enabled=True,
        lan_endpoint="10.1.1.5",
        wan_endpoint="",
        host_port=51820,
        lan_domain="kine.local",
        subdomains=["emby"],
    )
    before = dict(state)
    client_vpn.add_peer(state, "Laptop")
    assert client_vpn.needs_compose(before, state) is None


def test_enable_builds_only_the_access_container():
    state = client_vpn.fresh_state("kine.local", [])
    updated = client_vpn.apply_settings(
        state,
        enabled=True,
        lan_endpoint="192.168.1.9",
        wan_endpoint="vpn.example",
        host_port=51821,
        lan_domain="kine.local",
        subdomains=[],
    )
    assert client_vpn.needs_compose(state, updated) == "build"
    for cmd in client_vpn.COMPOSE_ACTIONS:
        assert "gluetun" not in cmd
        assert "--force-recreate" not in cmd
        assert "restart" not in cmd
        assert cmd[-1] == "wg-access"


def test_disable_stops_only_the_access_container():
    state = client_vpn.fresh_state("kine.local", [])
    state["enabled"] = True
    state["lan_endpoint"] = "192.168.1.9"
    updated = client_vpn.apply_settings(
        state,
        enabled=False,
        lan_endpoint="192.168.1.9",
        wan_endpoint="",
        host_port=51820,
        lan_domain="kine.local",
        subdomains=[],
    )
    assert client_vpn.needs_compose(state, updated) == "stop"


def test_routes_use_the_provider_only_when_asked_and_keep_the_house_local():
    via_vpn = client_vpn.desired_routes(vpn_exit=True, gateway="172.18.0.1", iface="eth0")
    via_home = client_vpn.desired_routes(vpn_exit=False, gateway="172.18.0.1", iface="eth0")
    assert via_vpn["default"] == {"dest": "default", "dev": "wgexit"}
    assert via_home["default"]["via"] == "172.18.0.1"
    assert via_vpn["rule_from"] == client_vpn.CLIENT_CIDR
    assert any(route["dest"] == "192.168.0.0/16" for route in via_vpn["routes"])
    assert via_vpn["nat"]["source"] == client_vpn.CLIENT_CIDR
    blob = repr(via_vpn) + repr(via_home)
    assert "gluetun" not in blob
    assert "iptables -F" not in blob


def test_router_does_not_take_the_default_route():
    cfg = client_vpn.router_config("172.18.0.8")
    tun = cfg["inbounds"][0]
    assert tun["auto_route"] is False
    assert tun["interface_name"] == "wgexit"
    assert cfg["outbounds"][0]["server"] == "172.18.0.8"
    socks = client_vpn.socks_config("172.18.0.8")
    assert socks["inbounds"][0]["listen"] == "172.18.0.8"
    assert socks["inbounds"][0]["listen"] != "0.0.0.0"


def test_relay_refuses_pid_1_and_only_matches_our_helpers():
    try:
        client_vpn.socks_process_args(1)
    except ValueError:
        pass
    else:
        raise AssertionError("pid 1 must be refused")
    args = client_vpn.socks_process_args(4242)
    assert args[:4] == ["nsenter", "-t", "4242", "-n"]
    assert "kill" not in args
    assert "restart" not in args
    assert client_vpn.is_our_helper(1, b"/usr/local/bin/sing-box\0run\0-c\0/config/socks.json") is False
    assert client_vpn.is_our_helper(
        50, b"/usr/local/bin/sing-box\0run\0-c\0/config/socks.json"
    )
    assert client_vpn.is_our_helper(50, b"/usr/bin/gluetun") is False


def test_dns_answers_kine_names_and_leaves_the_rest_alone():
    query = _query("emby.kine.local", 1)
    reply = client_vpn.dns_reply(query, "192.168.1.20")
    assert reply is not None
    assert reply[2] & 0x80  # response
    assert reply[-4:] == bytes([192, 168, 1, 20])
    aaaa = _query("emby.kine.local", 28)
    empty = client_vpn.dns_reply(aaaa, "192.168.1.20")
    assert empty is not None
    assert int.from_bytes(empty[6:8], "big") == 0
    assert client_vpn.dns_reply(_query("example.com", 1), None) is None


def test_service_names_include_admin_and_catalogue():
    names = client_vpn.service_names("kine.local", ["emby", "sonarr"])
    assert "emby.kine.local" in names
    assert "kine-admin.kine.local" in names
    assert "kine.local" in names


def test_summary_hides_keys():
    state = client_vpn.fresh_state("kine.local", [])
    client_vpn.add_peer(state, "Laptop")
    public = client_vpn.summary(state, {"exit": "wan", "detail": "home"})
    blob = repr(public)
    assert state["server_private"] not in blob
    assert state["peers"][0]["private_key"] not in blob
    assert public["peers"][0]["name"] == "Laptop"
    assert public["exit"] == "off"  # feature is not enabled


def test_compose_fragment_does_not_attach_to_gluetun():
    data = yaml.safe_load((ROOT / "compose" / "core.wg-access.yml").read_text())
    svc = data["services"]["wg-access"]
    assert svc["profiles"] == ["wg-access"]
    assert "network_mode" not in svc
    assert "depends_on" not in svc
    assert "51820/udp" in " ".join(svc["ports"])
    gluetun = (ROOT / "compose" / "vpn.gluetun.yml").read_text()
    assert "wg-access" not in gluetun
    http = (ROOT / "helm" / "backend" / "app" / "client_vpn_http.py").read_text()
    assert "gluetun" not in http
    assert "force-recreate" not in http
    entry = (ROOT / "wg-access" / "entrypoint.py").read_text()
    assert "force-recreate" not in entry
    assert "docker restart" not in entry
    assert "compose" not in entry


def _query(name: str, qtype: int) -> bytes:
    packet = bytearray(b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    for label in name.split("."):
        raw = label.encode()
        packet.append(len(raw))
        packet += raw
    packet.append(0)
    packet += qtype.to_bytes(2, "big")
    packet += (1).to_bytes(2, "big")
    return bytes(packet)
