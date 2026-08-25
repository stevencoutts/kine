"""Dispatcharr API token auto-provisioning."""
import asyncio
import pathlib
import sys
from unittest.mock import AsyncMock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import dispatcharr_token  # noqa: E402


def test_ensure_token_returns_existing_env(monkeypatch):
    monkeypatch.setattr(dispatcharr_token.config, "profiles", lambda: {"dispatcharr"})
    monkeypatch.setattr(
        dispatcharr_token.config, "read",
        lambda: {"DISPATCHARR_TOKEN": "already-set"},
    )
    run = AsyncMock()
    monkeypatch.setattr(dispatcharr_token.compose, "run", run)
    token = asyncio.run(dispatcharr_token.ensure_token())
    assert token == "already-set"
    run.assert_not_called()


def test_ensure_token_generates_and_writes(monkeypatch):
    monkeypatch.setattr(dispatcharr_token.config, "profiles", lambda: {"dispatcharr"})
    state = {}
    monkeypatch.setattr(dispatcharr_token.config, "read", lambda: dict(state))
    monkeypatch.setattr(
        dispatcharr_token.config, "write",
        lambda data: state.update(data),
    )
    monkeypatch.setattr(
        dispatcharr_token.compose, "run",
        AsyncMock(return_value=(0, "INFO django\nnew-key\n")),
    )
    token = asyncio.run(dispatcharr_token.ensure_token())
    assert token == "new-key"
    assert state["DISPATCHARR_TOKEN"] == "new-key"


def test_ensure_token_no_superuser(monkeypatch):
    monkeypatch.setattr(dispatcharr_token.config, "profiles", lambda: {"dispatcharr"})
    monkeypatch.setattr(dispatcharr_token.config, "read", lambda: {})
    monkeypatch.setattr(
        dispatcharr_token.compose, "run",
        AsyncMock(return_value=(2, "")),
    )
    assert asyncio.run(dispatcharr_token.ensure_token()) is None


def test_ensure_token_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(dispatcharr_token.config, "profiles", lambda: set())
    monkeypatch.setattr(dispatcharr_token.config, "read", lambda: {})
    assert asyncio.run(dispatcharr_token.ensure_token()) is None
