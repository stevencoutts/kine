#!/usr/bin/env python3
"""Kine provisioner.

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

from recipes import arr, bazarr, dispatcharr, emby, envfiles, jackett, metrics, nzbget, prowlarr, recyclarr, seerr, teamarr, transmission  # noqa: E402
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
        "/data/media/music",
        "/data/media/sports",
        "/data/media/recordings",
        "/data/downloads/incomplete",
        "/data/downloads/complete",
        "/data/downloads/complete/tv-sonarr",
        "/data/downloads/complete/radarr",
        "/data/downloads/complete/lidarr",
    ):
        pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def wire(enabled: set[str]) -> None:
    log(f"Provisioning: {', '.join(sorted(enabled))}")
    ensure_data_tree()

    envfiles.configure(enabled, log)

    for app in ("sonarr", "radarr", "lidarr"):
        if app in enabled:
            try:
                arr.configure(app, enabled, log)
            except Exception as exc:  # noqa: BLE001 — one app must not abort the rest
                log(f"{app}: wiring failed ({exc})")

    if "transmission" in enabled:
        try:
            transmission.configure(log)
        except Exception as exc:  # noqa: BLE001
            log(f"transmission: wiring failed ({exc})")

    if "prowlarr" in enabled:
        try:
            prowlarr.configure(enabled, log)
        except Exception as exc:  # noqa: BLE001
            log(f"prowlarr: wiring failed ({exc})")

    if "jackett" in enabled:
        try:
            jackett.configure(log)
        except Exception as exc:  # noqa: BLE001
            log(f"jackett: wiring failed ({exc})")

    if "recyclarr" in enabled:
        try:
            recyclarr.configure(log)
        except Exception as exc:  # noqa: BLE001
            log(f"recyclarr: wiring failed ({exc})")

    if "seerr" in enabled:
        try:
            seerr.configure(enabled, log)
        except Exception as exc:  # noqa: BLE001
            log(f"seerr: wiring failed ({exc})")

    if "emby" in enabled:
        emby.configure(
            os.environ.get("HELM_ADMIN_USER", "kine-admin"),
            os.environ.get("KINE_SECRET", "")[:16],
            log,
        )

    if "bazarr" in enabled:
        try:
            bazarr.configure(enabled, log)
        except Exception as exc:  # noqa: BLE001
            log(f"bazarr: wiring failed ({exc})")

    if "nzbget" in enabled:
        try:
            nzbget.configure(enabled, log)
        except Exception as exc:  # noqa: BLE001
            log(f"nzbget: wiring failed ({exc})")

    if "dispatcharr" in enabled:
        try:
            token = os.environ.get("DISPATCHARR_TOKEN", "").strip() or None
            result = dispatcharr.configure(enabled, token, log)
            changed = result.get("env_changed") or []
            if changed:
                log(f"dispatcharr: env updated for {', '.join(changed)} (recreate from Helm)")
        except Exception as exc:  # noqa: BLE001
            log(f"dispatcharr: wiring failed ({exc})")

    if "teamarr" in enabled:
        try:
            token = os.environ.get("DISPATCHARR_TOKEN", "").strip()
            user = (
                os.environ.get("DISPATCHARR_LOGIN_USER", "").strip()
                or "kine"
            )
            password = os.environ.get("DISPATCHARR_LOGIN_PASSWORD", "").strip()
            # Only apply when leagues.json exists (written by Helm enable modal).
            if (teamarr.STACK / "config" / "teamarr" / "leagues.json").is_file():
                teamarr.configure(
                    None,
                    log,
                    dispatcharr_token=token,
                    dispatcharr_username=user,
                    dispatcharr_password=password,
                )
            else:
                log("teamarr: no leagues.json yet, skipping subscription seed")
        except Exception as exc:  # noqa: BLE001
            log(f"teamarr: wiring failed ({exc})")

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
