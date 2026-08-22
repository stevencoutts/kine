#!/usr/bin/env python3
"""Media Centre provisioner.

Runs after `docker compose up` and wires the enabled applications to
each other. Idempotent by construction: every write checks for an
existing match first, so running it again after enabling a new app only
adds what is missing.

Modes:
    python provision.py seed      write config files before first start
    python provision.py wire      configure running apps (default)
    python provision.py all       both, in order
"""
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from recipes import arr, emby, envfiles, prowlarr  # noqa: E402
from seed import seed_all  # noqa: E402

STATE = pathlib.Path("/stack/provision.log")


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    line = f"{stamp}  {msg}"
    print(line)
    try:
        with STATE.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def enabled_apps() -> set[str]:
    raw = os.environ.get("COMPOSE_PROFILES", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def ensure_data_tree() -> None:
    for path in (
        "/data/media/movies",
        "/data/media/tv",
        "/data/media/sports",
        "/data/media/recordings",
        "/data/downloads/incomplete",
        "/data/downloads/complete",
    ):
        pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def wire(enabled: set[str]) -> None:
    log(f"Provisioning: {', '.join(sorted(enabled))}")
    ensure_data_tree()

    envfiles.configure(enabled, log)

    for app in ("sonarr", "radarr"):
        if app in enabled:
            arr.configure(app, enabled, log)

    if "prowlarr" in enabled:
        prowlarr.configure(enabled, log)

    if "emby" in enabled:
        emby.configure(
            os.environ.get("HELM_ADMIN_USER", "admin"),
            os.environ.get("MC_SECRET", "")[:16],
            log,
        )

    log("Provisioning complete")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "wire"
    enabled = enabled_apps()

    if mode in ("seed", "all"):
        seed_all(enabled)
    if mode in ("wire", "all"):
        wire(enabled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
