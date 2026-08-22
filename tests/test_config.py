"""Compose-safe .env persistence used by Helm."""
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

import config  # noqa: E402


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("PLAIN=value\nHELM_ADMIN_HASH=\n")
    monkeypatch.setattr(config, "ENV", path)
    return path


def test_dollar_value_round_trips_in_compose_literal_quotes(env_file):
    value = "$argon2id$v=19$m=65536,t=3,p=4$not-a-real-hash"
    config.write({"HELM_ADMIN_HASH": value})

    assert config.read()["HELM_ADMIN_HASH"] == value
    assert "HELM_ADMIN_HASH='$argon2id$" in env_file.read_text()


def test_normalize_migrates_existing_unquoted_dollar_values(env_file):
    value = "$argon2id$v=19$m=65536,t=3,p=4$not-a-real-hash"
    env_file.write_text(f"HELM_ADMIN_HASH={value}\n")

    config.normalize()

    assert config.read()["HELM_ADMIN_HASH"] == value
    assert env_file.read_text() == f"HELM_ADMIN_HASH='{value}'\n"


def test_compose_emits_no_warning_for_written_dollar_value(env_file, tmp_path):
    if not shutil.which("docker"):
        pytest.skip("docker compose unavailable")
    config.write({"HELM_ADMIN_HASH": "$argon2id$v=19$m=65536$example"})
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        "services:\n"
        "  check:\n"
        "    image: alpine:3.20\n"
        "    environment:\n"
        "      VALUE: ${HELM_ADMIN_HASH}\n"
    )
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(compose_file), "config"],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "variable is not set" not in result.stderr
