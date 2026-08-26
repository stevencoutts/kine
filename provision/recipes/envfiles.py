"""Write the env files consumed by apps that have no REST API to poke.

Unpackerr, ECM and Teamarr are all configured by environment rather
than by API call, so their wiring is a file write plus a restart. The
compose fragments reference these files with env_file, which means they
must exist before the container starts even if they are empty.

ECM is special: the UI reads Dispatcharr connection from
``settings.json`` (url / auth_method / dispatcharr_api_key), not from
``DISPATCHARR_*`` env vars. Env is still written for compose parity.
"""
import json
import pathlib

from keys import resolve_key

STACK = pathlib.Path("/stack")
# Live-TV affinity keeps dispatcharr/ecm/teamarr in one gluetun namespace;
# same-container callers reach Dispatcharr on loopback, not kine_internal DNS.
DISPATCHARR_LOOPBACK = "http://127.0.0.1:9191"


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


def write_ecm_dispatcharr_settings(token: str, log) -> bool:
    """Seed ECM settings.json with API-key auth to Dispatcharr.

    Only runs when ``token`` is non-empty so an empty wire pass cannot
    wipe a working connection. Returns True when the file changed.
    """
    token = (token or "").strip()
    if not token:
        return False
    path = STACK / "config" / "ecm" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text() or "{}")
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}
    updates = {
        "url": DISPATCHARR_LOOPBACK,
        "auth_method": "api_key",
        "dispatcharr_api_key": token,
        "api_key": token,  # legacy alias ECM still mirrors
    }
    if all(data.get(k) == v for k, v in updates.items()):
        return False
    data.update(updates)
    path.write_text(json.dumps(data, indent=2) + "\n")
    log("ecm: wrote settings.json Dispatcharr connection")
    return True


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
    # ECM/Teamarr share Dispatcharr's tunnel namespace (live-TV affinity).
    existing["DISPATCHARR_URL"] = DISPATCHARR_LOOPBACK
    existing["DISPATCHARR_TOKEN"] = token or ""
    body = "\n".join(f"{k}={existing[k]}" for k in order) + "\n"
    env_changed = not (target.exists() and target.read_text() == body)
    if env_changed:
        target.write_text(body)
        log(f"{app}: wrote {target.name}")
    settings_changed = False
    if app == "ecm":
        settings_changed = write_ecm_dispatcharr_settings(token, log)
    return env_changed or settings_changed


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
