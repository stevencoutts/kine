"""Helm onboarding: parsing a client WireGuard .conf."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

from wireguard import (  # noqa: E402
    empty_vpn_env,
    parse_conf,
    remove_gluetun_conf,
    sanitize_conf_for_gluetun,
    write_gluetun_conf,
)

SAMPLE_HOSTNAME = """\
[Interface]
PrivateKey = cHJpdmF0ZS1rZXktdmFsdWU=
Address = 10.2.0.2/32
DNS = 10.2.0.1

[Peer]
PublicKey = cHVibGljLWtleS12YWx1ZQ==
Endpoint = vpn.example.com:51820
AllowedIPs = 0.0.0.0/0
"""

SAMPLE_CUSTOM = """\
[Interface]
PrivateKey = cHJpdmF0ZS1rZXktdmFsdWU=
Address = 10.8.0.2/32
DNS = 1.1.1.1

[Peer]
PublicKey = c2VydmVyLXB1Yi1rZXktYWJjZGVm=
PresharedKey = cHNrLXZhbHVlLWFiY2RlZmdoaWpr=
Endpoint = 198.167.192.9:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""


def test_ip_endpoint_uses_custom_provider():
    found = parse_conf(SAMPLE_CUSTOM)
    assert found["VPN_SERVICE_PROVIDER"] == "custom"
    assert found["VPN_TYPE"] == "wireguard"
    assert found["VPN_PORT_FORWARDING"] == "off"
    assert found["VPN_SERVER_COUNTRIES"] == ""
    assert found["VPN_PORT_FORWARDING_PROVIDER"] == ""
    assert found["WIREGUARD_ENDPOINT_IP"] == "198.167.192.9"
    assert found["WIREGUARD_ENDPOINT_PORT"] == "51820"
    assert found["WIREGUARD_PUBLIC_KEY"] == "c2VydmVyLXB1Yi1rZXktYWJjZGVm="
    assert found["WIREGUARD_PRESHARED_KEY"] == "cHNrLXZhbHVlLWFiY2RlZmdoaWpr="
    assert found["WIREGUARD_ADDRESSES"] == "10.8.0.2/32"


def test_hostname_endpoint_still_uses_custom():
    found = parse_conf(SAMPLE_HOSTNAME)
    assert found["VPN_SERVICE_PROVIDER"] == "custom"
    assert found["WIREGUARD_ENDPOINT_IP"] == ""
    assert found["WIREGUARD_ENDPOINT_PORT"] == ""
    assert found["WIREGUARD_PUBLIC_KEY"] == "cHVibGljLWtleS12YWx1ZQ=="


def test_write_gluetun_conf_creates_file(tmp_path):
    stack = tmp_path / "stack"
    write_gluetun_conf(SAMPLE_CUSTOM, str(stack))
    conf = stack / "config" / "gluetun" / "wireguard" / "wg0.conf"
    assert conf.is_file()
    assert "198.167.192.9" in conf.read_text()


def test_remove_gluetun_conf_deletes_file(tmp_path):
    stack = tmp_path / "stack"
    write_gluetun_conf(SAMPLE_CUSTOM, str(stack))
    conf = stack / "config" / "gluetun" / "wireguard" / "wg0.conf"
    remove_gluetun_conf(str(stack))
    assert not conf.exists()


def test_empty_vpn_env_blanks_all_keys():
    cleared = empty_vpn_env()
    assert cleared["VPN_SERVICE_PROVIDER"] == ""
    assert cleared["WIREGUARD_PRIVATE_KEY"] == ""
    assert cleared["FIREWALL_VPN_INPUT_PORTS"] == ""


def test_missing_peer_raises():
    with pytest.raises(ValueError, match="PublicKey and Endpoint"):
        parse_conf("[Interface]\nPrivateKey = abc=\nAddress = 10.0.0.2/32\n")


def test_peer_only_is_not_enough():
    found = parse_conf("not a line\n[Peer]\nEndpoint = 1.2.3.4:51820\n")
    assert found == {}


def test_empty_input():
    assert parse_conf("") == {}


SAMPLE_PROTON_IPV6 = """\
[Interface]
PrivateKey = cHJpdmF0ZS1rZXktdmFsdWU=
Address = 10.2.0.2/32, 2a07:b944::2:2/128
DNS = 10.2.0.1

[Peer]
PublicKey = cHVibGljLWtleS12YWx1ZQ==
Endpoint = 169.150.208.246:51820
AllowedIPs = 0.0.0.0/0, ::/0
"""


def test_parse_conf_keeps_ipv4_when_proton_lists_ipv6():
    found = parse_conf(SAMPLE_PROTON_IPV6)
    assert found["WIREGUARD_ADDRESSES"] == "10.2.0.2/32"


def test_write_gluetun_conf_strips_ipv6_so_gluetun_can_start(tmp_path):
    cleaned = sanitize_conf_for_gluetun(SAMPLE_PROTON_IPV6)
    assert "2a07:b944::2:2/128" not in cleaned
    assert "::/0" not in cleaned
    assert "Address = 10.2.0.2/32" in cleaned
    assert "AllowedIPs = 0.0.0.0/0" in cleaned

    stack = tmp_path / "stack"
    write_gluetun_conf(SAMPLE_PROTON_IPV6, str(stack))
    text = (stack / "config" / "gluetun" / "wireguard" / "wg0.conf").read_text()
    assert "2a07:b944::2:2/128" not in text
    assert "Address = 10.2.0.2/32" in text
