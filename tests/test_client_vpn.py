"""Client VPN plans. These must stay true so the feature cannot take
the live tunnel down with it: compose never names gluetun, routing is
only for the client subnet, and the relay refuses pid 1."""
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
    client_vpn.add_peer(state, "Tablet")
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
    client_vpn.add_peer(state, "Tablet")
    public = client_vpn.summary(state, {"exit": "wan", "detail": "home"})
    blob = repr(public)
    assert state["server_private"] not in blob
    assert state["peers"][0]["private_key"] not in blob
    assert public["peers"][0]["name"] == "Tablet"
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


# Values read from osiris: Kine's Docker networks, the gluetun tunnel and
# the host's interfaces as wg-access reports them.
DOCKER_TEXT = (
    "kine_edge 192.168.144.0/20 \n"
    "kine_internal 192.168.128.0/20 \n"
    "kine_ctrl 192.168.160.0/20 \n"
    "kine_dns 172.16.53.0/27 \n"
)
ENV = {
    "KINE_DNS_SUBNET": "172.16.53.0/27",
    "WIREGUARD_ADDRESSES": "10.13.37.143/24",
    "KINE_LAN_IP": "10.100.100.34",
}
HOST_ADDRS = (
    "4: br0    inet 10.100.100.34/24 brd 10.100.100.255 scope global br0\n"
    "19: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n"
)
STATUS = {"exit": "vpn", "host_networks": client_vpn.host_networks_from_addrs(HOST_ADDRS)}


def _reserved(**overrides):
    kwargs = {"docker_text": DOCKER_TEXT, "env": ENV, "status": STATUS}
    kwargs.update(overrides)
    return client_vpn.reserved_networks(**kwargs)


def _rejects(cidr, needle, reserved=None, devices=0):
    try:
        client_vpn.validate_client_cidr(
            cidr, _reserved() if reserved is None else reserved, devices=devices,
        )
    except ValueError as exc:
        assert needle in str(exc), str(exc)
        return str(exc)
    raise AssertionError(f"{cidr} should be rejected")


def test_default_subnet_is_unchanged_for_existing_state():
    legacy = client_vpn.fresh_state("couttsnet.com")
    legacy.pop("client_cidr")
    legacy["endpoint_host"] = "vpn.example.net"
    peer = client_vpn.add_peer(legacy, "steven macbook")
    assert peer["address"] == "10.66.66.2"
    assert client_vpn.server_address(legacy) == "10.66.66.1" == client_vpn.SERVER_ADDRESS
    assert str(client_vpn.client_network(legacy)) == client_vpn.CLIENT_CIDR == "10.66.66.0/24"
    saved = client_vpn.apply_settings(
        legacy, enabled=True, host_port=51820, client_cidr="10.66.66.0/24", reserved=_reserved(),
    )
    assert saved["peers"][0]["address"] == "10.66.66.2"
    assert "needs_config" not in saved["peers"][0]
    assert client_vpn.needs_compose(saved, saved) is None
    text = client_vpn.client_config(saved, saved["peers"][0])
    assert "Address = 10.66.66.2/32" in text
    assert "DNS = 10.66.66.1" in text
    routes = client_vpn.desired_routes(vpn_exit=True, gateway="192.168.144.1", iface="eth0")
    assert routes["rule_from"] == routes["nat"]["source"] == "10.66.66.0/24"
    assert client_vpn.summary(saved, None)["client_cidr"] == "10.66.66.0/24"


def test_custom_subnet_is_used_everywhere():
    state = client_vpn.fresh_state("couttsnet.com")
    state["endpoint_host"] = "vpn.example.net"
    state = client_vpn.apply_settings(
        state, enabled=True, host_port=51820, client_cidr="10.77.0.0/24", reserved=_reserved(),
    )
    assert state["client_cidr"] == "10.77.0.0/24"
    first = client_vpn.add_peer(state, "Phone")
    second = client_vpn.add_peer(state, "Tablet")
    assert client_vpn.server_address(state) == "10.77.0.1"
    assert (first["address"], second["address"]) == ("10.77.0.2", "10.77.0.3")
    conf = client_vpn.client_config(state, first)
    assert "DNS = 10.77.0.1" in conf and "Address = 10.77.0.2/32" in conf
    assert "AllowedIPs = 10.77.0.3/32" in client_vpn.server_config(state)
    plan = client_vpn.desired_routes(
        vpn_exit=False, gateway="192.168.144.1", iface="eth0", cidr=state["client_cidr"],
    )
    assert plan["rule_from"] == plan["nat"]["source"] == "10.77.0.0/24"
    assert {"dest": "10.77.0.0/24", "dev": "wg0"} in plan["routes"]
    assert "10.66.66" not in repr(plan) + conf
    small = client_vpn.validate_client_cidr("192.168.200.0/29", _reserved())
    assert small == "192.168.200.0/29"
    assert client_vpn.validate_client_cidr("10.200.0.0/16", _reserved()) == "10.200.0.0/16"


def test_subnet_must_be_private_sized_and_a_network_address():
    _rejects("8.8.8.0/24", "private")
    _rejects("100.64.0.0/24", "private")
    _rejects("10.66.0.0/15", "/16")
    _rejects("10.66.66.0/30", "/29")
    _rejects("10.66.66.5/24", "10.66.66.0/24")
    _rejects("10.66.66.0", "prefix")
    _rejects("not a subnet", "IPv4 network")
    _rejects("192.168.0.0/16", "whole private range")


def test_subnet_too_small_for_the_devices_is_rejected():
    _rejects("10.66.70.0/29", "too small for the server and 6 device(s)", devices=6)
    assert client_vpn.validate_client_cidr("10.66.70.0/29", _reserved(), devices=5)
    state = client_vpn.fresh_state("kine.local")
    for n in range(6):
        client_vpn.add_peer(state, f"dev{n}")
    try:
        client_vpn.apply_settings(state, enabled=False, host_port=51820, client_cidr="10.66.70.0/29")
    except ValueError as exc:
        assert "too small" in str(exc)
    else:
        raise AssertionError("six devices do not fit in a /29 with the server")
    assert [p["address"] for p in state["peers"]][0] == "10.66.66.2"


def test_subnet_overlap_names_each_network():
    reserved = _reserved()
    assert "kine_edge" in _rejects("192.168.150.0/24", "kine_edge (192.168.144.0/20)", reserved)
    _rejects("192.168.130.0/24", "kine_internal (192.168.128.0/20)", reserved)
    _rejects("192.168.160.0/24", "kine_ctrl (192.168.160.0/20)", reserved)
    _rejects("172.16.53.0/24", "kine_dns (172.16.53.0/27)", reserved)
    _rejects("10.13.37.0/24", "the gluetun tunnel address (10.13.37.0/24)", reserved)
    _rejects("10.100.100.128/25", "the host network on br0 (10.100.100.0/24)", reserved)
    _rejects("172.17.4.0/24", "the host network on docker0 (172.17.0.0/16)", reserved)
    _rejects("172.31.255.0/24", "tun link", reserved)
    # Docker unreachable and no host report yet: the env values still apply.
    bare = _reserved(docker_text="", status=None)
    _rejects("172.16.53.0/28", "KINE_DNS_SUBNET", bare)
    _rejects("10.100.100.0/24", "KINE_LAN_IP", bare)
    _rejects("10.13.0.0/16", "gluetun tunnel", bare)


def test_overlap_is_checked_only_when_the_subnet_changes():
    state = client_vpn.fresh_state("kine.local")
    blocking = [("pretend", "10.66.0.0/16")]
    kept = client_vpn.apply_settings(
        state, enabled=False, host_port=51820, client_cidr="10.66.66.0/24", reserved=blocking,
    )
    assert kept["client_cidr"] == "10.66.66.0/24"
    try:
        client_vpn.apply_settings(
            state, enabled=False, host_port=51820, client_cidr="10.66.67.0/24", reserved=blocking,
        )
    except ValueError as exc:
        assert "overlaps pretend (10.66.0.0/16)" in str(exc)
    else:
        raise AssertionError("a new subnet is checked")


def test_readdressing_keeps_keys_and_flags_each_device():
    state = client_vpn.fresh_state("couttsnet.com")
    state["endpoint_host"] = "vpn.example.net"
    state["enabled"] = True
    mac = client_vpn.add_peer(state, "steven macbook")
    phone = client_vpn.add_peer(state, "Phone")
    client_vpn.remove_peer(state, mac["id"])
    state["peers"].insert(0, mac)
    before = {p["id"]: dict(p) for p in state["peers"]}
    updated = client_vpn.apply_settings(
        state, enabled=True, host_port=51820, client_cidr="10.88.1.0/28", reserved=_reserved(),
    )
    assert [p["name"] for p in updated["peers"]] == ["steven macbook", "Phone"]
    assert [p["address"] for p in updated["peers"]] == ["10.88.1.2", "10.88.1.3"]
    for peer in updated["peers"]:
        old = before[peer["id"]]
        for key in ("private_key", "public_key", "preshared_key", "name"):
            assert peer[key] == old[key]
        assert peer["needs_config"] is True
    assert updated["server_private"] == state["server_private"]
    assert state["peers"][0]["address"] == "10.66.66.2"  # input is not mutated
    assert client_vpn.needs_compose(state, updated) == "recreate"
    public = client_vpn.summary(updated, None)
    assert all(p["needs_config"] for p in public["peers"])
    assert phone["private_key"] not in repr(public)
    assert client_vpn.config_fetched(updated, mac["id"]) is True
    assert client_vpn.config_fetched(updated, mac["id"]) is False
    assert [p.get("needs_config", False) for p in updated["peers"]] == [False, True]
    assert "Address = 10.88.1.2/32" in client_vpn.client_config(updated, updated["peers"][0])


def test_subnet_change_only_recreates_wg_access_and_only_when_on():
    state = client_vpn.fresh_state("kine.local")
    state["endpoint_host"] = "vpn.example.net"
    off = client_vpn.apply_settings(state, enabled=False, host_port=51820, client_cidr="10.70.0.0/24")
    assert client_vpn.needs_compose(state, off) is None
    on = dict(state, enabled=True)
    moved = client_vpn.apply_settings(on, enabled=True, host_port=51820, client_cidr="10.70.0.0/24")
    assert client_vpn.needs_compose(on, moved) == "recreate"
    failed = client_vpn.failed_start_state(on, moved)
    assert failed["enabled"] is False
    assert failed["client_cidr"] == "10.70.0.0/24"
    assert client_vpn.needs_compose(failed, dict(failed, enabled=True)) == "up"
    http = (ROOT / "helm" / "backend" / "app" / "client_vpn_http.py").read_text()
    recreate = http.split('if action == "recreate":', 1)[1].split('if action == "stop":', 1)[0]
    assert "COMPOSE_STOP" in recreate and "COMPOSE_RM" in recreate and "COMPOSE_UP" in recreate
    assert "gluetun" not in http


def test_host_networks_keep_bridges_and_skip_loopback():
    text = (
        "1: lo    inet 127.0.0.1/8 scope host lo\n"
        "4: br0    inet 10.100.100.34/24 brd 10.100.100.255 scope global br0\\ valid_lft forever\n"
        "4: br0    inet 10.100.100.4/32 scope global br0\\ valid_lft forever\n"
        "8: br-859e532cb396    inet 192.168.144.1/20 scope global br-859e532cb396\n"
    )
    assert client_vpn.host_networks_from_addrs(text) == [
        {"name": "br0", "cidr": "10.100.100.0/24"},
        {"name": "br0", "cidr": "10.100.100.4/32"},
        {"name": "br-859e532cb396", "cidr": "192.168.144.0/20"},
    ]


def test_entrypoint_reads_the_saved_subnet():
    entry = (ROOT / "wg-access" / "entrypoint.py").read_text()
    assert "SERVER_ADDRESS" not in entry
    assert "CLIENT_CIDR" not in entry
    assert "/24" not in entry
    assert "client_vpn.server_address(state)" in entry
    assert "client_vpn.client_network(state)" in entry
    assert "host_networks" in entry
    assert "Laptop" not in entry
