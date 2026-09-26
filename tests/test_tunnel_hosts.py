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
        == "http://gluetun-11111111:9191"
    )


def test_sibling_base_is_loopback_until_direct():
    tunnelled = {"live_tv_direct": False, "profiles": []}
    direct = {"live_tv_direct": True, "profiles": []}
    assert tunnel_hosts.sibling_base(tunnelled, "dispatcharr", 9191) == "http://127.0.0.1:9191"
    assert tunnel_hosts.sibling_base(direct, "teamarr", 9195) == "http://teamarr:9195"
    assert (
        tunnel_hosts.align_peer_url(
            "http://127.0.0.1:9195/api/v1/epg/xmltv", direct, "teamarr", 9195,
        )
        == "http://teamarr:9195/api/v1/epg/xmltv"
    )
    assert (
        tunnel_hosts.align_peer_url(
            "http://teamarr:9195/api/v1/epg/xmltv", tunnelled, "teamarr", 9195,
        )
        == "http://127.0.0.1:9195/api/v1/epg/xmltv"
    )
    assert tunnel_hosts.align_peer_url(
        "http://127.0.0.1:9195/api/v1/epg/xmltv", tunnelled, "teamarr", 9195,
    ) is None
    assert tunnel_hosts.align_peer_url(
        "http://example.test/guide.xml", direct, "teamarr", 9195,
    ) is None


def test_internal_base_direct_live_tv():
    data = {
        "primary_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "live_tv_direct": True,
        "profiles": [
            {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "apps": ["sonarr"]},
        ],
    }
    assert (
        tunnel_hosts.internal_base(data, "dispatcharr", 9191)
        == "http://dispatcharr:9191"
    )
    assert tunnel_hosts.internal_base(data, "sonarr", 8989) == "http://gluetun:8989"


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
