"""ECM first-admin setup from Helm credentials."""
import pathlib
import sys
from unittest.mock import MagicMock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import ecm_setup  # noqa: E402


def test_ensure_admin_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(ecm_setup, "enabled", lambda: False)
    assert ecm_setup.ensure_admin("admin", "SecretPass123!")["status"] == "skipped"


def test_ensure_admin_rejects_password_containing_username(monkeypatch):
    monkeypatch.setattr(ecm_setup, "enabled", lambda: True)
    out = ecm_setup.ensure_admin("admin", "admin-SecretPass123!")
    assert out["ok"] is False
    assert "username" in out["reason"]


def test_ensure_admin_creates_when_required(monkeypatch):
    monkeypatch.setattr(ecm_setup, "enabled", lambda: True)
    monkeypatch.setattr(ecm_setup.config, "read", lambda: {"KINE_DOMAIN": "example.com"})
    seen = {}

    class FakeResp:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.content = b"x" if payload is not None else b""
            self.text = text

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            assert url.endswith("/api/auth/setup-required")
            return FakeResp(200, {"required": True})

        def post(self, url, json=None):
            seen["url"] = url
            seen["json"] = json
            return FakeResp(201, {"message": "Setup complete"})

    monkeypatch.setattr(ecm_setup.httpx, "Client", FakeClient)
    out = ecm_setup.ensure_admin("admin", "SecretPass123!")
    assert out == {"ok": True, "status": "created", "username": "admin"}
    assert seen["json"]["email"] == "admin@example.com"
    assert seen["json"]["password"] == "SecretPass123!"


def test_ensure_admin_skips_when_already_configured(monkeypatch):
    monkeypatch.setattr(ecm_setup, "enabled", lambda: True)

    class FakeResp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload
            self.content = b"x"

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return FakeResp(200, {"required": False})

        def post(self, *a, **k):
            raise AssertionError("should not post")

    monkeypatch.setattr(ecm_setup.httpx, "Client", FakeClient)
    out = ecm_setup.ensure_admin("admin", "SecretPass123!")
    assert out["status"] == "skipped"
