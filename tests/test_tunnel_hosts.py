"""Resolve tunnelled app internal URLs from VPN profile assignment."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))

import tunnel_hosts  # noqa: E402


def test_internal_base_uses_secondary():
    data = {
        "primary_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "profiles": [
            {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "apps": []},
            {"id": "11111111-2222-3333-4444-555555555555", "apps": ["dispatcharr"]},
        ],
    }
    assert (
        tunnel_hosts.internal_base(data, "dispatcharr", 9191)
        == "http://gluetun_11111111:9191"
    )


def test_internal_base_primary_leftover():
    data = {
        "primary_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "profiles": [
            {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "apps": []},
            {"id": "11111111-2222-3333-4444-555555555555", "apps": ["dispatcharr"]},
        ],
    }
    assert tunnel_hosts.internal_base(data, "sonarr", 8989) == "http://gluetun:8989"


def test_internal_base_primary_assigned_app():
    data = {
        "primary_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "profiles": [
            {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "apps": ["sonarr"]},
        ],
    }
    assert tunnel_hosts.internal_base(data, "sonarr", 8989) == "http://gluetun:8989"
