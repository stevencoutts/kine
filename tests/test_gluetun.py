"""Parsing gluetun's /v1/publicip/ip response, which has changed shape
across gluetun versions."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

from gluetun import parse_public_ip  # noqa: E402


def test_json_shape_public_ip_key():
    assert parse_public_ip('{"public_ip": "1.2.3.4", "country": "NL"}') == "1.2.3.4"


def test_json_shape_ip_key():
    assert parse_public_ip('{"ip": "5.6.7.8"}') == "5.6.7.8"


def test_bare_ip_response():
    assert parse_public_ip("9.9.9.9\n") == "9.9.9.9"


def test_empty_response():
    assert parse_public_ip("") is None
    assert parse_public_ip("   ") is None
