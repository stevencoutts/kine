"""Write the env files consumed by apps that have no REST API to poke.

Unpackerr, ECM and Teamarr are all configured by environment rather
than by API call, so their wiring is a file write plus a restart. The
compose fragments reference these files with env_file, which means they
must exist before the container starts even if they are empty.
"""
import pathlib

from keys import resolve_key

STACK = pathlib.Path("/stack")


def _write(app: str, lines: dict[str, str], log) -> None:
    d = STACK / "config" / app
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{app}.env"
    body = "\n".join(f"{k}={v}" for k, v in lines.items()) + "\n"
    if target.exists() and target.read_text() == body:
        return
    target.write_text(body)
    log(f"{app}: wrote {target.name}")


def _read_env(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value
    return out


def write_dispatcharr_token(app: str, token: str, log) -> bool:
    """Merge DISPATCHARR_URL/TOKEN into app.env. Return True if content changed."""
    d = STACK / "config" / app
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{app}.env"
    existing = _read_env(target)
    order = list(existing.keys())
    for key in ("DISPATCHARR_URL", "DISPATCHARR_TOKEN"):
        if key not in order:
            order.append(key)
    existing["DISPATCHARR_URL"] = "http://dispatcharr:9191"
    existing["DISPATCHARR_TOKEN"] = token or ""
    body = "\n".join(f"{k}={existing[k]}" for k in order) + "\n"
    if target.exists() and target.read_text() == body:
        return False
    target.write_text(body)
    log(f"{app}: wrote {target.name}")
    return True


def configure(enabled: set[str], log) -> None:
    if "unpackerr" in enabled:
        _write("unpackerr", {
            "UN_SONARR_0_API_KEY": resolve_key("sonarr"),
            "UN_RADARR_0_API_KEY": resolve_key("radarr"),
        }, log)

    for app in ("ecm", "teamarr"):
        if app in enabled:
            # Token may be filled later by dispatcharr.configure / Helm Settings.
            write_dispatcharr_token(app, _read_env(STACK / "config" / app / f"{app}.env").get(
                "DISPATCHARR_TOKEN", ""
            ), log)
