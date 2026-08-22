"""Parsing gluetun's /v1/publicip/ip response, which has changed shape
across gluetun versions."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

import gluetun  # noqa: E402

parse_public_ip = gluetun.parse_public_ip


def test_json_shape_public_ip_key():
    assert parse_public_ip('{"public_ip": "1.2.3.4", "country": "NL"}') == "1.2.3.4"


def test_json_shape_ip_key():
    assert parse_public_ip('{"ip": "5.6.7.8"}') == "5.6.7.8"


def test_bare_ip_response():
    assert parse_public_ip("9.9.9.9\n") == "9.9.9.9"


def test_empty_response():
    assert parse_public_ip("") is None
    assert parse_public_ip("   ") is None


def test_public_ip_ignores_compose_warnings():
    raw = 'warning: variable is not set\n{"public_ip": "1.2.3.4"}\n'
    assert parse_public_ip(raw) == "1.2.3.4"


def test_forwarded_port_ignores_compose_warnings():
    assert hasattr(gluetun, "parse_forwarded_port")
    raw = 'warning: variable is not set\n{"port": 54321, "ports": [54321]}\n'
    assert gluetun.parse_forwarded_port(raw) == 54321


def test_forwarded_port_rejects_zero_and_invalid_responses():
    assert gluetun.parse_forwarded_port('{"port": 0, "ports": []}') is None
    assert gluetun.parse_forwarded_port("warning only") is None


def test_connection_label_uses_configured_tunnel_type():
    assert gluetun.connection_label("wireguard") == "WireGuard"
    assert gluetun.connection_label("openvpn") == "OpenVPN"


def test_connection_label_has_generic_fallback():
    assert gluetun.connection_label("") == "VPN"
