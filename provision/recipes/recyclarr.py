"""Seed and wire Recyclarr TRaSH Guide sync for Sonarr and Radarr.

Recyclarr reads recyclarr.yml plus secrets.yml from /config. Inside the
gluetun namespace Sonarr and Radarr are reached on loopback, the same
convention Unpackerr uses.
"""
import pathlib

import keys

from keys import api_key, resolve_key

# TRaSH Guide profile trash IDs — 1080p-focused defaults.
SONARR_PROFILE = "72dae194fc92bf828f32cde7744e51a1"  # WEB-1080p
RADARR_PROFILE = "d1d67249d3890e49bc12e275d989a7e9"  # HD Bluray + WEB

RECYCLARR_YML = """\
# yaml-language-server: $schema=https://schemas.recyclarr.dev/latest/config-schema.json
# Seeded by Kine provision. TRaSH Guide 1080p profiles for Sonarr and Radarr.

sonarr:
  tv:
    base_url: !secret sonarr_url
    api_key: !secret sonarr_apikey
    delete_old_custom_formats: true
    quality_definition:
      type: series
    quality_profiles:
      - trash_id: {sonarr_profile}  # WEB-1080p
        reset_unmatched_scores:
          enabled: true

radarr:
  movies:
    base_url: !secret radarr_url
    api_key: !secret radarr_apikey
    delete_old_custom_formats: true
    quality_definition:
      type: movie
    quality_profiles:
      - trash_id: {radarr_profile}  # HD Bluray + WEB
        reset_unmatched_scores:
          enabled: true
"""


def _config_dir() -> pathlib.Path:
    return keys.STACK / "config" / "recyclarr"


def _secrets(sonarr_key: str, radarr_key: str) -> dict[str, str]:
    return {
        "sonarr_url": "http://127.0.0.1:8989",
        "sonarr_apikey": sonarr_key,
        "radarr_url": "http://127.0.0.1:7878",
        "radarr_apikey": radarr_key,
    }


def _write_secrets(lines: dict[str, str], log) -> None:
    cfg_dir = _config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    target = cfg_dir / "secrets.yml"
    body = "\n".join(f"{k}: {v}" for k, v in lines.items()) + "\n"
    if target.exists() and target.read_text() == body:
        return
    target.write_text(body)
    log("recyclarr: wrote secrets.yml")


def seed(log=print) -> None:
    cfg_dir = _config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    target = cfg_dir / "recyclarr.yml"
    if not target.exists():
        target.write_text(
            RECYCLARR_YML.format(
                sonarr_profile=SONARR_PROFILE,
                radarr_profile=RADARR_PROFILE,
            )
        )
        log("recyclarr: seeded recyclarr.yml")
    _write_secrets(_secrets(api_key("sonarr"), api_key("radarr")), log)


def configure(log) -> None:
    cfg_dir = _config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    target = cfg_dir / "recyclarr.yml"
    if not target.exists():
        target.write_text(
            RECYCLARR_YML.format(
                sonarr_profile=SONARR_PROFILE,
                radarr_profile=RADARR_PROFILE,
            )
        )
        log("recyclarr: seeded recyclarr.yml")
    _write_secrets(_secrets(resolve_key("sonarr"), resolve_key("radarr")), log)
