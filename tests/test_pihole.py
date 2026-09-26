"""Pi-hole DNS gate, compose fragment, and recreate set."""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import pihole_dns  # noqa: E402

FRAGMENT = yaml.safe_load((ROOT / "compose" / "core.pihole.yml").read_text())
ENV_EXAMPLE = (ROOT / ".env.example").read_text()


def test_default_firewall_contains_the_dns_subnet():
    assert pihole_dns.firewall_allows(
        "192.168.0.0/16,172.16.0.0/12,10.0.0.0/8",
        "172.30.53.0/29",
    )


def test_firewall_rejects_a_list_that_misses_the_subnet():
    assert not pihole_dns.firewall_allows("192.168.1.0/24", "172.30.53.0/29")
    assert pihole_dns.rejection(
        {"FIREWALL_OUTBOUND_SUBNETS": "10.0.0.0/8", "KINE_DNS_SUBNET": "172.30.53.0/29"},
        "",
    ) == "FIREWALL_OUTBOUND_SUBNETS must include 172.30.53.0/29"


def test_address_error_rejects_the_gateway_and_an_address_outside_the_subnet():
    base = {
        "KINE_DNS_SUBNET": "172.16.53.0/29",
        "KINE_DNS_GLUETUN": "172.16.53.2",
        "KINE_DNS_PIHOLE": "172.16.53.3",
    }
    assert pihole_dns.address_error(base) is None
    gateway = dict(base)
    gateway["KINE_DNS_GLUETUN"] = "172.16.53.1"
    assert "gateway" in (pihole_dns.address_error(gateway) or "")
    outside = dict(base)
    outside["KINE_DNS_PIHOLE"] = "172.30.53.53"
    assert "outside" in (pihole_dns.address_error(outside) or "")


def test_password_update_replaces_sentinel_and_keeps_a_real_value():
    assert pihole_dns.password_update("") is not None
    assert pihole_dns.password_update("change-me") not in ("", "change-me")
    assert len(pihole_dns.password_update("change-me")) == 32
    assert pihole_dns.password_update("already-set") is None


def test_specific_listener_leaves_the_lan_address_free():
    ss = (
        'udp UNCONN 0 0 192.168.122.1:53 0.0.0.0:* '
        'users:(("dnsmasq",pid=1,fd=5))'
    )
    addrs = "\n".join([
        "5: virbr0    inet 192.168.122.1/24 brd 192.168.122.255 scope global virbr0",
        "4: br0    inet 10.100.100.34/24 brd 10.100.100.255 scope global br0",
        "10: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0",
        "771: br-5fd6d55dffe0    inet 172.24.0.1/16 brd 172.24.255.255 scope global br-5fd6d55dffe0",
    ])
    assert pihole_dns.bind_addresses(ss, addrs) == ["10.100.100.34"]
    env = {
        "FIREWALL_OUTBOUND_SUBNETS": "172.16.0.0/12",
        "KINE_DNS_SUBNET": "172.30.53.0/29",
        "PIHOLE_WEBPASSWORD": "secret",
    }
    assert pihole_dns.gate(env, ss, addrs) == {"KINE_DNS_BIND": "10.100.100.34"}


def test_wildcard_listener_still_blocks_every_address():
    ss = 'tcp LISTEN 0 32 0.0.0.0:53 0.0.0.0:* users:(("named",pid=1,fd=6))'
    addrs = "4: br0    inet 10.100.100.34/24 brd 10.100.100.255 scope global br0"
    try:
        pihole_dns.bind_addresses(ss, addrs)
    except pihole_dns.PiholeRejected as exc:
        assert "named" in str(exc)
    else:
        raise AssertionError("expected PiholeRejected")


def test_port53_error_names_the_listener():
    assert pihole_dns.port53_error("") is None
    assert pihole_dns.port53_error("   \n") is None
    text = 'udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:(("systemd-resolve",pid=1,fd=1))'
    assert pihole_dns.port53_error(text) == (
        "TCP or UDP port 53 is already in use (systemd-resolve)"
    )
    assert pihole_dns.port53_error("tcp LISTEN 0 0 0.0.0.0:53 0.0.0.0:*") == (
        "TCP or UDP port 53 is already in use"
    )


def test_gate_returns_a_password_write_or_rejects():
    env = {
        "FIREWALL_OUTBOUND_SUBNETS": "172.16.0.0/12",
        "KINE_DNS_SUBNET": "172.30.53.0/29",
        "PIHOLE_WEBPASSWORD": "change-me",
    }
    updates = pihole_dns.gate(env, "")
    assert "PIHOLE_WEBPASSWORD" in updates
    assert updates["PIHOLE_WEBPASSWORD"] != "change-me"
    kept = dict(env)
    kept["PIHOLE_WEBPASSWORD"] = "secret"
    assert pihole_dns.gate(kept, "") == {}
    try:
        pihole_dns.gate(env, 'udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:(("named",pid=1,fd=1))')
    except pihole_dns.PiholeRejected as exc:
        assert "named" in str(exc)
    else:
        raise AssertionError("expected PiholeRejected")


def test_patches_point_untunnelled_apps_at_pihole_and_gluetun_at_the_bridge():
    doc = pihole_dns.patches()
    assert doc["gluetun"]["networks"]["kine_dns"]["ipv4_address"] == "${KINE_DNS_GLUETUN}"
    assert set(doc["gluetun"]["networks"]) == {"kine_internal", "kine_edge", "kine_dns"}
    assert doc["traefik"]["dns"] == ["${KINE_DNS_PIHOLE}"]
    assert "kine_ctrl" in doc["traefik"]["networks"]
    assert "kine_dns" in doc["traefik"]["networks"]
    assert "dockerproxy" not in doc
    assert "mdns" not in doc
    assert "sonarr" not in doc
    assert "ports" not in doc.get("pihole", {})


def test_patches_publish_on_the_recorded_lan_address(monkeypatch):
    monkeypatch.setattr(
        pihole_dns.config, "read", lambda: {"KINE_DNS_BIND": "10.100.100.34"},
    )
    doc = pihole_dns.patches()
    assert doc["pihole"]["ports"] == [
        "10.100.100.34:53:53/tcp",
        "10.100.100.34:53:53/udp",
    ]


def test_services_to_recreate_skips_the_tunnel_when_vpn_is_off():
    names = pihole_dns.services_to_recreate({
        "COMPOSE_PROFILES": "pihole,emby,sonarr",
        "VPN_ENABLED": "false",
    })
    assert names == ["pihole", "traefik", "helm", "provision", "emby"]


def test_services_to_recreate_includes_the_tunnel_group():
    env = {
        "COMPOSE_PROFILES": "pihole,gluetun,sonarr,emby",
        "VPN_ENABLED": "true",
        "VPN_TUNNELLED_APPS": "sonarr",
    }
    names = pihole_dns.services_to_recreate(env, store={"primary_id": None, "profiles": []})
    assert "pihole" in names
    assert "emby" in names
    assert "gluetun" in names
    assert "sonarr" in names
    assert "traefik" in names
    assert "helm" in names
    assert "provision" in names


def test_compose_fragment_matches_the_spec():
    svc = FRAGMENT["services"]["pihole"]
    assert "network_mode" not in svc
    assert svc["profiles"] == ["pihole"]
    assert svc["image"] == "pihole/pihole:${PIHOLE_TAG}"
    assert "53:53/tcp" in svc["ports"]
    assert "53:53/udp" in svc["ports"]
    assert svc["environment"]["FTLCONF_dns_upstreams"] == "${KINE_DNS_GLUETUN}#53"
    assert svc["environment"]["FTLCONF_dhcp_active"] == "false"
    assert svc["dns"] == ["${KINE_DNS_GLUETUN}"]
    assert svc["networks"]["kine_dns"]["ipv4_address"] == "${KINE_DNS_PIHOLE}"
    labels = " ".join(svc["labels"])
    assert "Host(`pihole.${KINE_DOMAIN}`)" in labels
    assert "Host(`pihole.${KINE_LOCAL_DOMAIN}`)" in labels
    assert FRAGMENT["networks"]["kine_dns"]["ipam"]["config"][0]["subnet"] == "${KINE_DNS_SUBNET}"


def test_env_example_declares_pihole_variables():
    for key in (
        "PIHOLE_TAG",
        "PIHOLE_DIGEST",
        "PIHOLE_WEBPASSWORD",
        "KINE_DNS_SUBNET",
        "KINE_DNS_POOL",
        "KINE_DNS_GLUETUN",
        "KINE_DNS_PIHOLE",
        "KINE_DNS_BIND",
    ):
        assert f"{key}=" in ENV_EXAMPLE
    assert "PIHOLE_WEBPASSWORD=change-me" in ENV_EXAMPLE


def test_install_and_cli_hook_the_gate():
    install = (ROOT / "install.sh").read_text()
    cli = (ROOT / "kine").read_text()
    assert "PIHOLE_WEBPASSWORD" in install
    assert "change-me" in install
    assert "pihole_gate" in cli
    assert "pihole_sync" in cli
    assert "profiles_add gluetun" in cli
