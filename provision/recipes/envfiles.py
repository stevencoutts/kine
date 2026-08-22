"""Write the env files consumed by apps that have no REST API to poke.

Unpackerr, ECM and Teamarr are all configured by environment rather
than by API call, so their wiring is a file write plus a restart. The
compose fragments reference these files with env_file, which means they
must exist before the container starts even if they are empty.
"""
import pathlib

from keys import api_key

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


def configure(enabled: set[str], log) -> None:
    if "unpackerr" in enabled:
        _write("unpackerr", {
            "UN_SONARR_0_API_KEY": api_key("sonarr"),
            "UN_RADARR_0_API_KEY": api_key("radarr"),
        }, log)

    for app in ("ecm", "teamarr"):
        if app in enabled:
            _write(app, {
                "DISPATCHARR_URL": "http://dispatcharr:9191",
                # Dispatcharr issues its own token on first login; the
                # user pastes it once in the GUI and Helm writes it back
                # here. We create the file so the container can start.
                "DISPATCHARR_TOKEN": "",
            }, log)
