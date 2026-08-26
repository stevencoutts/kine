"""Heal apps still pinned to a dead Gluetun network namespace.

Every tunnelled service uses ``network_mode: service:gluetun`` (or
``service:gluetun_<shortId>`` for secondary tunnels). Docker binds that
to the Gluetun *container ID* at create time. Recreating Gluetun without
recreating those peers leaves them healthy-looking but unreachable
(Traefik 502). This module finds and recreates orphans per tunnel.
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable

from .compose import REPO, compose_env
from .vpn_routing import container_name_for_tunnel_service

RunFn = Callable[..., subprocess.CompletedProcess]


def container_id(network_mode: str | None) -> str | None:
    mode = (network_mode or "").strip()
    if not mode.startswith("container:"):
        return None
    return mode.split(":", 1)[1] or None


def container_to_service(container_name: str) -> str | None:
    """Map a running kine-gluetun* container to its compose service name."""
    if container_name == "kine-gluetun":
        return "gluetun"
    prefix = "kine-gluetun-"
    if container_name.startswith(prefix):
        return f"gluetun_{container_name.removeprefix(prefix)}"
    return None


def orphan_services(
    *,
    expected_id: str | None,
    network_modes: dict[str, str],
    peers: set[str],
) -> list[str]:
    """Return peers pinned to a non-current Gluetun container for one tunnel."""
    if not expected_id:
        return []
    orphans: list[str] = []
    for name in sorted(peers):
        pinned = container_id(network_modes.get(name))
        if pinned and pinned != expected_id:
            orphans.append(name)
    return orphans


def _run(
    cmd: list[str],
    *,
    runner: RunFn | None = None,
) -> subprocess.CompletedProcess:
    if runner is not None:
        return runner(cmd)
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=compose_env(),
    )


def inspect_id(name: str, *, runner: RunFn | None = None) -> str | None:
    result = _run(
        ["docker", "inspect", "--format", "{{.Id}}", name],
        runner=runner,
    )
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def inspect_network_mode(name: str, *, runner: RunFn | None = None) -> str:
    result = _run(
        ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", name],
        runner=runner,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def discover_gluetun_services(*, runner: RunFn | None = None) -> list[str]:
    """Compose service names for running kine-gluetun* containers."""
    result = _run(
        ["docker", "ps", "--filter", "name=kine-gluetun", "--format", "{{.Names}}"],
        runner=runner,
    )
    if result.returncode != 0:
        return []
    services: list[str] = []
    for line in (result.stdout or "").splitlines():
        name = line.strip()
        if not name:
            continue
        svc = container_to_service(name)
        if svc:
            services.append(svc)
    return sorted(set(services))


def _active_compose_profiles() -> set[str]:
    import os

    active = {
        p.strip()
        for p in os.environ.get("COMPOSE_PROFILES", "").split(",")
        if p.strip()
    }
    if not active:
        try:
            env_path = REPO / ".env"
            if env_path.is_file():
                for line in env_path.read_text().splitlines():
                    if line.startswith("COMPOSE_PROFILES="):
                        active = {
                            p.strip()
                            for p in line.split("=", 1)[1].split(",")
                            if p.strip()
                        }
                        break
        except OSError:
            pass
    return active


def _service_enabled(meta: dict, active: set[str]) -> bool:
    svc_profiles = {p for p in (meta.get("profiles") or []) if p}
    if svc_profiles and active and not (svc_profiles & active):
        return False
    return True


def tunnel_peers_by_service(*, runner: RunFn | None = None) -> dict[str, set[str]]:
    """Map each Gluetun compose service to enabled tunnelled peer names."""
    result = _run(
        ["docker", "compose", "config", "--format", "json"],
        runner=runner,
    )
    if result.returncode != 0:
        return {}
    try:
        cfg = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    active = _active_compose_profiles()
    out: dict[str, set[str]] = {}
    for name, meta in (cfg.get("services") or {}).items():
        meta = meta or {}
        mode = (meta.get("network_mode") or "").strip()
        if not mode.startswith("service:gluetun"):
            continue
        if not _service_enabled(meta, active):
            continue
        tunnel = mode.split(":", 1)[1]
        out.setdefault(tunnel, set()).add(name)
    return out


def find_orphans_for_tunnels(
    tunnels: dict[str, set[str]],
    *,
    runner: RunFn | None = None,
) -> list[str]:
    """Return orphan peers across the given tunnel → peer map."""
    orphans: list[str] = []
    for service, peers in sorted(tunnels.items()):
        if not peers:
            continue
        expected_id = inspect_id(
            container_name_for_tunnel_service(service),
            runner=runner,
        )
        if not expected_id:
            continue
        modes = {
            name: inspect_network_mode(f"kine-{name}", runner=runner)
            for name in peers
        }
        orphans.extend(
            orphan_services(
                expected_id=expected_id,
                network_modes=modes,
                peers=peers,
            )
        )
    return orphans


def find_orphans(*, runner: RunFn | None = None) -> list[str]:
    """Discover running Gluetun tunnels and return all orphan peers."""
    running = set(discover_gluetun_services(runner=runner))
    if not running:
        return []
    by_service = tunnel_peers_by_service(runner=runner)
    tunnels = {
        svc: peers
        for svc, peers in by_service.items()
        if svc in running
    }
    return find_orphans_for_tunnels(tunnels, runner=runner)


def heal_all(
    tunnels: dict[str, set[str]],
    *,
    runner: RunFn | None = None,
) -> dict:
    """Force-recreate orphan peers for each running Gluetun tunnel."""
    orphans = find_orphans_for_tunnels(tunnels, runner=runner)
    if not orphans:
        return {"ok": True, "healed": [], "log": "no tunnel orphans"}
    result = _run(
        ["docker", "compose", "up", "-d", "--force-recreate", "--no-deps", *orphans],
        runner=runner,
    )
    log = ((result.stdout or "") + (result.stderr or "")).strip()
    return {
        "ok": result.returncode == 0,
        "healed": orphans,
        "log": log or f"recreated {', '.join(orphans)}",
    }


def heal_orphans(*, runner: RunFn | None = None) -> dict:
    """Force-recreate any tunnelled peers still on a dead Gluetun ID."""
    running = set(discover_gluetun_services(runner=runner))
    if not running:
        return {"ok": True, "healed": [], "log": "no tunnel orphans"}
    by_service = tunnel_peers_by_service(runner=runner)
    tunnels = {
        svc: peers
        for svc, peers in by_service.items()
        if svc in running
    }
    return heal_all(tunnels, runner=runner)


def main() -> int:
    result = heal_orphans()
    healed = result.get("healed") or []
    if healed:
        print(f"healed tunnel orphans: {', '.join(healed)}")
    else:
        print("no tunnel orphans")
    if result.get("log") and healed:
        print(result["log"])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
