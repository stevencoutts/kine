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

REPO = pathlib.Path(os.environ.get("KINE_REPO", "/repo"))
ENV = REPO / ".env"
_lock = threading.Lock()


def _decode(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        value = re.sub(r"\\(['\\])", r"\1", value[1:-1])
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = re.sub(r'\\(["\\])', r"\1", value[1:-1])
    return value


def _encode(value: str) -> str:
    # Quote anything Compose's unquoted parser would split or interpolate:
    # dollars, spaces, quotes, braces (JSON lists like NZBGET_NEWS_SERVERS).
    if not re.search(r"""[\s"'${}]""", value):
        return value
    literal = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{literal}'"


def read() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV.exists():
        return out
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = _decode(v.strip())
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
                lines[i] = f"{key}={_encode(updates[key])}"
                seen.add(key)
        for key, value in updates.items():
            if key not in seen:
                lines.append(f"{key}={_encode(value)}")
        ENV.write_text("\n".join(lines) + "\n")


def normalize() -> None:
    """Rewrite existing values using Compose-safe literal quoting."""
    write(read())


def profiles() -> list[str]:
    raw = read().get("COMPOSE_PROFILES", "")
    return [p for p in (x.strip() for x in raw.split(",")) if p]


def set_profiles(items: list[str]) -> None:
    write({"COMPOSE_PROFILES": ",".join(dict.fromkeys(items))})
