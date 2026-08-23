"""Image channel switching (stable vs development)."""
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import channels, config  # noqa: E402

CATALOGUE = yaml.safe_load((ROOT / "catalogue.yml").read_text())["apps"]


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "COMPOSE_PROFILES=mdns,sonarr\n"
        "APP_DEV_CHANNELS=\n"
        "SONARR_TAG=latest\n"
        "SONARR_DIGEST=\n"
        "TRANSMISSION_TAG=latest\n"
    )
    monkeypatch.setattr(config, "ENV", path)
    return path


def test_fresh_env_example_has_empty_dev_channels():
    env = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    assert env.get("APP_DEV_CHANNELS", "") == ""


def test_catalogue_dev_tags_are_declared_where_supported():
    assert CATALOGUE["sonarr"]["dev_tag"] == "develop"
    assert CATALOGUE["jackett"]["dev_tag"] == "development"
    assert CATALOGUE["emby"]["dev_tag"] == "beta"
    assert "dev_tag" not in CATALOGUE["transmission"]
    assert "dev_tag" not in CATALOGUE["nzbget"]


def test_enable_dev_swaps_tag_and_remembers_stable(env_file):
    updates = channels.apply("sonarr", CATALOGUE["sonarr"], enabled=True)
    env = config.read()

    assert updates["SONARR_TAG"] == "develop"
    assert env["SONARR_TAG"] == "develop"
    assert env["SONARR_STABLE_TAG"] == "latest"
    assert env["SONARR_DIGEST"] == ""
    assert channels.channels() == ["sonarr"]


def test_reenable_dev_does_not_clobber_stable_pin(env_file):
    channels.apply("sonarr", CATALOGUE["sonarr"], enabled=True)
    # Simulate someone already on develop with a remembered pin.
    config.write({"SONARR_STABLE_TAG": "4.0.10"})
    channels.apply("sonarr", CATALOGUE["sonarr"], enabled=True)

    assert config.read()["SONARR_STABLE_TAG"] == "4.0.10"
    assert config.read()["SONARR_TAG"] == "develop"


def test_disable_dev_restores_stable_pin(env_file):
    channels.apply("sonarr", CATALOGUE["sonarr"], enabled=True)
    channels.apply("sonarr", CATALOGUE["sonarr"], enabled=False)
    env = config.read()

    assert env["SONARR_TAG"] == "latest"
    assert env["SONARR_STABLE_TAG"] == ""
    assert channels.channels() == []


def test_unsupported_app_raises(env_file):
    with pytest.raises(ValueError, match="no development"):
        channels.apply("transmission", CATALOGUE["transmission"], enabled=True)
