"""Heal apps still pinned to a dead Gluetun network namespace.

Every tunnelled service uses ``network_mode: service:gluetun``. Docker
binds that to the Gluetun *container ID* at create time. Recreating
Gluetun without recreating those peers leaves them healthy-looking but
unreachable (Traefik 502). This module finds and recreates orphans.
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable

from .compose import REPO, compose_env

RunFn = Callable[..., subprocess.CompletedProcess]


def container_id(network_mode: str | None) -> str | None:
    mode = (network_mode or "").strip()
    if not mode.startswith("container:"):
        return None
    return mode.split(":", 1)[1] or None


def orphan_services(
    *,
    gluetun_id: str | None,
    network_modes: dict[str, str],
    tunnelled: set[str],
) -> list[str]:
    """Return enabled tunnelled services pinned to a non-current Gluetun."""
    if not gluetun_id:
        return []
    orphans: list[str] = []
    for name in sorted(tunnelled):
        pinned = container_id(network_modes.get(name))
        if pinned and pinned != gluetun_id:
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


def tunnelled_services(*, runner: RunFn | None = None) -> set[str]:
    """Compose services that join Gluetun's network namespace (enabled profiles only)."""
    import os

    result = _run(
        ["docker", "compose", "config", "--format", "json"],
        runner=runner,
    )
    if result.returncode != 0:
        return set()
    try:
        cfg = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return set()
    # Prefer compose's resolved set, but never trust a named service that is
    # outside COMPOSE_PROFILES — `compose up <svc>` bypasses profiles.
    active = {p.strip() for p in os.environ.get("COMPOSE_PROFILES", "").split(",") if p.strip()}
    # compose_env drops .env keys from the process env so Compose reads the
    # file; load profiles from that file when the process env is empty.
    if not active:
        try:
            from .compose import REPO

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
    out: set[str] = set()
    for name, meta in (cfg.get("services") or {}).items():
        if (meta or {}).get("network_mode") != "service:gluetun":
            continue
        svc_profiles = {p for p in ((meta or {}).get("profiles") or []) if p}
        if svc_profiles and active and not (svc_profiles & active):
            continue
        out.add(name)
    return out


def find_orphans(*, runner: RunFn | None = None) -> list[str]:
    gluetun_id = inspect_id("kine-gluetun", runner=runner)
    if not gluetun_id:
        return []
    tunnelled = tunnelled_services(runner=runner)
    modes = {
        name: inspect_network_mode(f"kine-{name}", runner=runner)
        for name in tunnelled
    }
    return orphan_services(
        gluetun_id=gluetun_id,
        network_modes=modes,
        tunnelled=tunnelled,
    )


def heal_orphans(*, runner: RunFn | None = None) -> dict:
    """Force-recreate any tunnelled peers still on a dead Gluetun ID."""
    orphans = find_orphans(runner=runner)
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
