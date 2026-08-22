"""Reading and writing .env.

Helm's whole relationship with the stack goes through this file plus
`docker compose`. It deliberately never holds its own copy of what is
running: .env is the source of truth, so the repo and the GUI can never
disagree about what the appliance is.
"""
import os
import pathlib
import re
import threading

REPO = pathlib.Path(os.environ.get("MC_REPO", "/repo"))
ENV = REPO / ".env"
_lock = threading.Lock()


def read() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV.exists():
        return out
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def write(updates: dict[str, str]) -> None:
    """Rewrite in place, preserving comments and ordering."""
    with _lock:
        lines = ENV.read_text().splitlines()
        seen = set()
        for i, line in enumerate(lines):
            m = re.match(r"^([A-Z0-9_]+)=", line)
            if m and m.group(1) in updates:
                key = m.group(1)
                lines[i] = f"{key}={updates[key]}"
                seen.add(key)
        for key, value in updates.items():
            if key not in seen:
                lines.append(f"{key}={value}")
        ENV.write_text("\n".join(lines) + "\n")


def profiles() -> list[str]:
    raw = read().get("COMPOSE_PROFILES", "")
    return [p for p in (x.strip() for x in raw.split(",")) if p]


def set_profiles(items: list[str]) -> None:
    write({"COMPOSE_PROFILES": ",".join(dict.fromkeys(items))})
