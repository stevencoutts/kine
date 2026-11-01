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


def test_client_config_is_one_full_tunnel_to_a_hostname():
    state = client_vpn.fresh_state("couttsnet.com", ["emby"])
    state["lan_endpoint"] = "10.100.100.34"
    state["wan_endpoint"] = "203.0.113.10"
    state["host_port"] = 51820
    peer = client_vpn.add_peer(state, "steven macbook")
    assert state["endpoint_host"] == ""
    try:
        client_vpn.client_config(state, peer)
    except ValueError as exc:
        assert "hostname" in str(exc).lower()
    else:
        raise AssertionError("a missing hostname must not invent an address")
    try:
        client_vpn.validate_endpoint_host("10.100.100.34")
    except ValueError:
        pass
    else:
        raise AssertionError("an IP is not an endpoint hostname")
    state["endpoint_host"] = "vpn.example.net"
    text = client_vpn.client_config(state, peer)
    assert "Endpoint = vpn.example.net:51820" in text
    assert "10.100.100.34" not in text
    assert "203.0.113.10" not in text
    assert "couttsnet.com" not in text.split("Endpoint = ", 1)[1]
    assert "AllowedIPs = 0.0.0.0/0" in text
    assert f"DNS = {client_vpn.SERVER_ADDRESS}" in text
    assert state["server_private"] not in text
    assert peer["private_key"] in text
    assert client_vpn.config_filename(peer) == "kine-steven-macbook.conf"
    other = client_vpn.add_peer(state, "Phone")
    assert other["address"] != peer["address"]
    again = client_vpn.client_config(state, peer)
    assert peer["private_key"] in again


def test_search_domain_is_not_copied_into_the_endpoint_hostname():
    state = client_vpn.fresh_state("couttsnet.com", [])
    state["endpoint_host"] = "wg.example.net"
    kept = client_vpn.apply_settings(state, enabled=True, host_port=51820)
    assert kept["endpoint_host"] == "wg.example.net"
    assert kept["lan_domain"] == "couttsnet.com"
    named = client_vpn.apply_settings(
        state,
        enabled=True,
        host_port=51820,
        endpoint_host="vpn.example.net",
    )
    assert named["endpoint_host"] == "vpn.example.net"
    assert named["lan_domain"] == "couttsnet.com"
    assert "lan_endpoint" not in named
    assert "wan_endpoint" not in named


def test_settings_keep_the_device_and_do_not_store_address_placeholders():
    state = client_vpn.fresh_state("couttsnet.com")
    state["endpoint_host"] = "vpn.example.net"
    state["wan_endpoint"] = "203.0.113.10"
    client_vpn.add_peer(state, "steven macbook")
    updated = client_vpn.apply_settings(state, enabled=True, host_port=51820)
    assert updated["peers"][0]["name"] == "steven macbook"
    assert updated["endpoint_host"] == "vpn.example.net"
    assert updated["endpoint_host"] != "vpn.couttsnet.com"
    assert "wan_endpoint" not in updated
    assert "lan_endpoint" not in updated
    assert "203.0.113.10" not in repr(updated)
    assert "203.0.113.10" not in client_vpn.client_config(updated, updated["peers"][0])
    cleared = client_vpn.fresh_state("couttsnet.com")
    saved = client_vpn.apply_settings(
        cleared,
        enabled=True,
        host_port=51820,
        endpoint_host="vpn.couttsnet.com",
    )
    assert "lan_endpoint" not in saved
    assert "wan_endpoint" not in saved
    assert "203.0.113.10" not in repr(saved)
    assert "vpn.example.net" not in repr(saved)


def test_adding_a_peer_does_not_call_compose():
    state = client_vpn.fresh_state("kine.local", [])
    state["endpoint_host"] = "vpn.example.net"
    state = client_vpn.apply_settings(state, enabled=True, host_port=51820)
    before = dict(state)
    client_vpn.add_peer(state, "Laptop")
    assert client_vpn.needs_compose(before, state) is None


def test_enable_starts_only_the_access_container():
    state = client_vpn.fresh_state("kine.local", [])
    updated = client_vpn.apply_settings(
        state,
        enabled=True,
        host_port=51821,
        endpoint_host="vpn.example.net",
    )
    assert client_vpn.needs_compose(state, updated) == "up"
    for cmd in client_vpn.COMPOSE_ACTIONS:
        assert "--build" not in cmd
        assert "build" not in cmd
        assert "gluetun" not in cmd
        assert "--force-recreate" not in cmd
        assert "restart" not in cmd
        assert cmd[-1] == "wg-access"


def test_enable_without_a_hostname_does_not_require_addresses():
    state = client_vpn.fresh_state("couttsnet.com")
    try:
        client_vpn.apply_settings(state, enabled=True, host_port=51820, endpoint_host="")
    except ValueError as exc:
        assert "hostname" in str(exc).lower()
    else:
        raise AssertionError("enabling still needs the endpoint hostname")


def test_port_change_starts_only_the_access_container():
    state = client_vpn.fresh_state("kine.local", [])
    state["enabled"] = True
    state["endpoint_host"] = "vpn.example.net"
    state["host_port"] = 51820
    updated = client_vpn.apply_settings(state, enabled=True, host_port=51821)
    assert client_vpn.needs_compose(state, updated) == "up"


def test_failed_start_keeps_the_device_and_hostname():
    state = client_vpn.fresh_state("couttsnet.com", ["emby"])
    state["endpoint_host"] = "vpn.example.net"
    client_vpn.add_peer(state, "steven macbook")
    updated = client_vpn.apply_settings(state, enabled=True, host_port=51820)
    kept = client_vpn.failed_start_state(state, updated)
    assert kept["enabled"] is False
    assert kept["endpoint_host"] == "vpn.example.net"
    assert kept["lan_domain"] == "couttsnet.com"
    assert kept["peers"][0]["name"] == "steven macbook"
    assert kept["peers"][0]["address"] == state["peers"][0]["address"]
    assert kept["server_private"] == state["server_private"]


def test_disable_stops_only_the_access_container():
    state = client_vpn.fresh_state("kine.local", [])
    state["enabled"] = True
    state["endpoint_host"] = "vpn.example.net"
    updated = client_vpn.apply_settings(state, enabled=False, host_port=51820)
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


def test_gluetun_process_matches_entrypoint_or_hostname():
    assert client_vpn.process_is_gluetun(
        b"HOSTNAME=1bb4cabdc64f\0PATH=/\0", b"/gluetun-entrypoint\0"
    )
    assert client_vpn.process_is_gluetun(
        b"HOSTNAME=kine-gluetun\0", b"/bin/sh\0"
    )
    assert client_vpn.process_is_gluetun(b"HOSTNAME=kine-helm\0", b"/usr/bin/python\0") is False
    assert client_vpn.process_is_gluetun(b"", b"/usr/bin/gluetun-entrypoint-helper\0") is False


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


def test_internal_zone_uses_the_lan_resolver_not_the_provider_exit():
    zones = client_vpn.internal_zones(
        "couttsnet.com",
        "search couttsnet.com\nnameserver 10.100.100.1\n",
    )
    assert "couttsnet.com" in zones
    assert client_vpn.name_in_internal_zone("emby.couttsnet.com", zones)
    assert client_vpn.name_in_internal_zone("printer.couttsnet.com", zones)
    assert client_vpn.name_in_internal_zone("notcouttsnet.com", zones) is False
    assert client_vpn.name_in_internal_zone("example.com", zones) is False
    assert client_vpn.dns_exit("emby.couttsnet.com", zones, vpn_exit=True) == "internal"
    assert client_vpn.dns_exit("example.com", zones, vpn_exit=True) == "vpn"
    assert client_vpn.dns_exit("example.com", zones, vpn_exit=False) == "plain"


def test_internal_resolver_prefers_the_lan_nameserver():
    resolv = "nameserver 127.0.0.53\nnameserver 1.1.1.1\nnameserver 10.100.100.1\n"
    addrs = "2: eno1    inet 10.100.100.34/24 brd 10.100.100.255 scope global eno1\n"
    assert client_vpn.internal_resolver(resolv, addrs) == "10.100.100.1"


def test_internal_resolver_uses_this_machine_when_the_host_resolver_is_loopback():
    resolv = "nameserver 127.0.0.53\nnameserver 8.8.8.8\n"
    addrs = (
        "2: docker0    inet 172.17.0.1/16 scope global docker0\n"
        "3: br-aabbccdd0011    inet 172.18.0.1/16 scope global br-aabbccdd0011\n"
        "4: wg0    inet 10.66.66.1/24 scope global wg0\n"
        "5: eno1    inet 10.100.100.34/24 scope global eno1\n"
    )
    assert client_vpn.internal_resolver(resolv, addrs) == "10.100.100.34"


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
    assert "build" not in svc
    assert svc["image"] == "kine/wg-access:local"
    assert svc["pull_policy"] == "never"
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
    assert "dns_reply" not in entry
    assert "dns_exit" in entry
    http = (ROOT / "helm" / "backend" / "app" / "client_vpn_http.py").read_text()
    assert "lan_endpoint" not in http
    assert "wan_endpoint" not in http
