"""Mocked compose lifecycle tests for VPN routing apply."""
from __future__ import annotations

import asyncio
import pathlib
import sys
from unittest.mock import AsyncMock, patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import config, main, vpn_profiles, vpn_routing, wireguard  # noqa: E402

VALID_WG = """[Interface]
PrivateKey = YJqK8nV3mP0sL2wQ9eR5tY7uI1oP3aS4dF6gH8jK0lM=
Address = 10.2.0.2/32
[Peer]
PublicKey = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx=
Endpoint = 1.2.3.4:51820
"""

PRIMARY_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECONDARY_ID = "11111111-2222-3333-4444-555555555555"


def _sample_store():
    return {
        "primary_id": PRIMARY_ID,
        "profiles": [
            {
                "id": PRIMARY_ID,
                "name": "Primary",
                "apps": ["sonarr"],
                "conf": VALID_WG,
                "type": "wireguard",
            },
            {
                "id": SECONDARY_ID,
                "name": "Secondary",
                "apps": ["dispatcharr"],
                "conf": VALID_WG,
                "type": "wireguard",
            },
        ],
    }


@pytest.fixture
def vpn_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    stack = tmp_path / "stack"
    stack.mkdir()
    env_file = repo / ".env"
    env_file.write_text(
        "VPN_ENABLED=true\n"
        "STACK_ROOT=" + str(stack) + "\n"
        "KINE_DOMAIN=example.com\n"
        "KINE_LOCAL_DOMAIN=kine.local\n"
        "VPN_TUNNELLED_APPS=sonarr,dispatcharr,transmission\n"
        "COMPOSE_PROFILES=gluetun,sonarr,dispatcharr\n"
    )
    monkeypatch.setattr(config, "REPO", repo)
    monkeypatch.setattr(config, "ENV", env_file)
    monkeypatch.setattr(main, "_REPO", repo)
    monkeypatch.setenv("KINE_REPO", str(repo))
    profiles_path = stack / "config" / "helm" / "vpn-profiles.json"
    profiles_path.parent.mkdir(parents=True)
    import json

    profiles_path.write_text(json.dumps(_sample_store()))
    return repo, stack


def _compose_calls(mock_run):
    return [tuple(call.args) for call in mock_run.await_args_list]


@pytest.mark.asyncio
async def test_disable_stops_all_tunnel_groups(vpn_env):
    repo, stack = vpn_env
    store = _sample_store()
    calls: list[tuple] = []

    async def fake_run(*args, timeout=600):
        calls.append(args)
        return 0, ""

    with patch.object(main.compose, "run", new=AsyncMock(side_effect=fake_run)):
        with patch.object(main.config, "read", return_value={
            **config.read(),
            "VPN_ENABLED": "false",
        }):
            code, _, stopped = await main.apply_vpn_routing(store, recreate=True)

    assert code == 0
    assert any(args[0] == "stop" for args in calls)
    assert any(args[0] == "rm" for args in calls)
    assert "gluetun" in stopped
    assert "gluetun-11111111" in stopped
    assert not any(args[0] == "up" for args in calls)
    override = repo / vpn_routing.ROUTING_GENERATED_REL
    assert override.is_file()
    assert override.read_text().strip() == "services: {}"


@pytest.mark.asyncio
async def test_empty_apps_removes_secondary(vpn_env):
    repo, _stack = vpn_env
    store = _sample_store()
    store["profiles"][1]["apps"] = []
    calls: list[tuple] = []

    async def fake_run(*args, timeout=600):
        calls.append(args)
        return 0, ""

    with patch.object(main.compose, "run", new=AsyncMock(side_effect=fake_run)):
        code, _, recreated = await main.apply_vpn_routing(store, recreate=True)

    assert code == 0
    stop_rm = [args for args in calls if args[0] in ("stop", "rm")]
    flat = [svc for args in stop_rm for svc in args[1:]]
    assert "gluetun-11111111" in flat
    assert "gluetun" in recreated
    assert "gluetun-11111111" not in recreated


@pytest.mark.asyncio
async def test_set_primary_writes_env_keys(tmp_path, monkeypatch):
    stack = tmp_path / "stack"
    stack.mkdir()
    other_wg = VALID_WG.replace("10.2.0.2", "10.3.0.3")
    primary = vpn_profiles.add_profile(stack, "Primary", VALID_WG)
    secondary = vpn_profiles.add_profile(stack, "Secondary", other_wg)
    repo = tmp_path / "repo"
    repo.mkdir()
    env_file = repo / ".env"
    env_file.write_text(
        f"STACK_ROOT={stack}\n"
        "WIREGUARD_ADDRESSES=10.9.9.9/32\n"
    )
    monkeypatch.setattr(config, "REPO", repo)
    monkeypatch.setattr(config, "ENV", env_file)

    store, fields = vpn_profiles.set_primary(str(stack), secondary["id"])
    assert fields["WIREGUARD_ADDRESSES"] == "10.3.0.3/32"
    assert fields["WIREGUARD_PRIVATE_KEY"]
    config.write(fields)
    env = config.read()
    assert env["WIREGUARD_ADDRESSES"] == "10.3.0.3/32"
    wg0 = stack / "config" / "gluetun" / "wireguard" / "wg0.conf"
    assert wg0.is_file()
    assert "10.3.0.3" in wg0.read_text()
    assert store["primary_id"] == secondary["id"]


@pytest.mark.asyncio
async def test_boot_ensure_recreates_when_vpn_enabled(vpn_env, monkeypatch):
    store = _sample_store()
    calls: list[tuple] = []

    async def fake_run(*args, timeout=600):
        calls.append(args)
        return 0, ""

    with patch.object(main.compose, "run", new=AsyncMock(side_effect=fake_run)):
        with patch.object(
            main.vpn_profiles,
            "migrate_from_wg0",
            return_value=store,
        ):
            await main._vpn_boot_ensure()

    assert any(args[0] == "up" and "gluetun" in args for args in calls)


def test_apply_filesystem_skips_wg0_when_disabled(tmp_path):
    repo = tmp_path / "repo"
    stack = tmp_path / "stack"
    stack.mkdir()
    wg0 = stack / "config" / "gluetun" / "wireguard" / "wg0.conf"
    wg0.parent.mkdir(parents=True)
    wg0.write_text("# removed on disable\n")
    data = _sample_store()
    vpn_routing.apply_filesystem(
        str(stack),
        repo,
        data,
        {"sonarr", "dispatcharr"},
        kine_domain="example.com",
        kine_local_domain="kine.local",
        vpn_enabled=False,
    )
    assert wg0.read_text() == "# removed on disable\n"
    assert (repo / vpn_routing.ROUTING_GENERATED_REL).read_text().strip() == "services: {}"
