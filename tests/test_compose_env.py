"""Helm must not let its baked process env override the project .env."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import compose  # noqa: E402


def test_compose_env_drops_keys_present_in_dotenv(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text(
        "KINE_DOMAIN=couttsnet.com\n"
        "COMPOSE_PROFILES=mdns,gluetun\n"
        "# comment\n"
        "HELM_PORT=8600\n"
    )
    monkeypatch.setattr(compose, "REPO", repo)
    env = compose.compose_env(
        {
            "PATH": "/usr/bin",
            "KINE_DOMAIN": "kine.local",
            "COMPOSE_PROFILES": "mdns",
            "HELM_PORT": "9999",
            "UNRELATED": "keep",
        }
    )
    assert "KINE_DOMAIN" not in env
    assert "COMPOSE_PROFILES" not in env
    assert "HELM_PORT" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["UNRELATED"] == "keep"


def test_compose_env_tolerates_missing_dotenv(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(compose, "REPO", repo)
    env = compose.compose_env({"KINE_DOMAIN": "kine.local", "PATH": "/bin"})
    assert env["KINE_DOMAIN"] == "kine.local"
