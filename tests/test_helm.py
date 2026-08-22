"""Helm onboarding: parsing a client WireGuard .conf."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

from wireguard import parse_conf  # noqa: E402

SAMPLE = """\
[Interface]
PrivateKey = cHJpdmF0ZS1rZXktdmFsdWU=
Address = 10.2.0.2/32
DNS = 10.2.0.1

[Peer]
PublicKey = cHVibGljLWtleS12YWx1ZQ==
Endpoint = vpn.example.com:51820
AllowedIPs = 0.0.0.0/0
"""


def test_pulls_private_key_and_address():
    found = parse_conf(SAMPLE)
    assert found == {
        "WIREGUARD_PRIVATE_KEY": "cHJpdmF0ZS1rZXktdmFsdWU=",
        "WIREGUARD_ADDRESSES": "10.2.0.2/32",
    }


def test_ignores_peer_and_junk_lines():
    found = parse_conf("not a line\n[Peer]\nEndpoint = 1.2.3.4:51820\n")
    assert found == {}


def test_empty_input():
    assert parse_conf("") == {}
