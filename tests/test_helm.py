"""Helm onboarding: parsing a client WireGuard .conf."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

from wireguard import parse_conf, proton_clears, write_gluetun_conf  # noqa: E402

SAMPLE_PROTON = """\
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


def test_named_provider_keeps_key_and_address_only():
    found = parse_conf(SAMPLE_PROTON)
    assert found == {
        "WIREGUARD_PRIVATE_KEY": "cHJpdmF0ZS1rZXktdmFsdWU=",
        "WIREGUARD_ADDRESSES": "10.2.0.2/32",
    }
    assert "VPN_SERVICE_PROVIDER" not in found


def test_ip_endpoint_selects_custom_provider():
    found = parse_conf(SAMPLE_CUSTOM)
    assert found["VPN_SERVICE_PROVIDER"] == "custom"
    assert found["VPN_PORT_FORWARDING"] == "off"
    assert found["WIREGUARD_ENDPOINT_IP"] == "198.167.192.9"
    assert found["WIREGUARD_ENDPOINT_PORT"] == "51820"
    assert found["WIREGUARD_PUBLIC_KEY"] == "c2VydmVyLXB1Yi1rZXktYWJjZGVm="
    assert found["WIREGUARD_PRESHARED_KEY"] == "cHNrLXZhbHVlLWFiY2RlZmdoaWpr="
    assert found["WIREGUARD_ADDRESSES"] == "10.8.0.2/32"
    assert proton_clears() == {
        "VPN_SERVER_COUNTRIES": "",
        "VPN_PORT_FORWARDING_PROVIDER": "",
    }


def test_write_gluetun_conf_creates_and_removes(tmp_path):
    stack = tmp_path / "stack"
    write_gluetun_conf(SAMPLE_CUSTOM, str(stack), custom=True)
    conf = stack / "config" / "gluetun" / "wireguard" / "wg0.conf"
    assert conf.is_file()
    assert "198.167.192.9" in conf.read_text()
    write_gluetun_conf(SAMPLE_CUSTOM, str(stack), custom=False)
    assert not conf.exists()


def test_hostname_endpoint_uses_named_provider():
    found = parse_conf(SAMPLE_PROTON)
    assert "VPN_SERVICE_PROVIDER" not in found
    assert found["WIREGUARD_PRIVATE_KEY"] == "cHJpdmF0ZS1rZXktdmFsdWU="


def test_peer_only_is_not_enough():
    found = parse_conf("not a line\n[Peer]\nEndpoint = 1.2.3.4:51820\n")
    assert found == {}


def test_empty_input():
    assert parse_conf("") == {}
