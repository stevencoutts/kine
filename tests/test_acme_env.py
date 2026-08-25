"""Unit tests for Traefik acme.env ClouDNS credential helpers."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import acme_env  # noqa: E402


def test_read_cloudns_missing_file(tmp_path):
    path = tmp_path / "acme.env"
    assert acme_env.read_cloudns(path) == {"auth_id": "", "password_set": False}


def test_read_cloudns_parses_values(tmp_path):
    path = tmp_path / "acme.env"
    path.write_text(
        "# comment\n"
        "CLOUDNS_AUTH_ID=12345\n"
        "CLOUDNS_AUTH_PASSWORD=s3cret\n"
    )
    assert acme_env.read_cloudns(path) == {"auth_id": "12345", "password_set": True}


def test_write_cloudns_creates_file(tmp_path):
    path = tmp_path / "acme.env"
    changed = acme_env.write_cloudns(path, auth_id="99", password="pw")
    assert changed is True
    text = path.read_text()
    assert "CLOUDNS_AUTH_ID=99" in text
    assert "CLOUDNS_AUTH_PASSWORD=pw" in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_cloudns_preserves_password_when_blank(tmp_path):
    path = tmp_path / "acme.env"
    path.write_text("CLOUDNS_AUTH_ID=1\nCLOUDNS_AUTH_PASSWORD=keepme\n")
    changed = acme_env.write_cloudns(path, auth_id="2", password="")
    assert changed is True
    text = path.read_text()
    assert "CLOUDNS_AUTH_ID=2" in text
    assert "CLOUDNS_AUTH_PASSWORD=keepme" in text


def test_write_cloudns_noop_when_unchanged(tmp_path):
    path = tmp_path / "acme.env"
    path.write_text("CLOUDNS_AUTH_ID=1\nCLOUDNS_AUTH_PASSWORD=pw\n")
    changed = acme_env.write_cloudns(path, auth_id="1", password="")
    assert changed is False


def test_write_cloudns_keeps_unrelated_keys(tmp_path):
    path = tmp_path / "acme.env"
    path.write_text("OTHER=1\nCLOUDNS_AUTH_ID=1\nCLOUDNS_AUTH_PASSWORD=pw\n")
    acme_env.write_cloudns(path, auth_id="9", password="x")
    text = path.read_text()
    assert "OTHER=1" in text
    assert "CLOUDNS_AUTH_ID=9" in text
