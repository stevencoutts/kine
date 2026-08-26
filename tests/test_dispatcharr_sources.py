"""Dispatcharr M3U/EPG proxy helpers."""
import pathlib
import sys
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import dispatcharr_sources  # noqa: E402


def test_list_m3u_unwraps_paginated_results(monkeypatch):
    monkeypatch.setattr(dispatcharr_sources, "enabled", lambda: True)
    monkeypatch.setattr(dispatcharr_sources, "configured", lambda: True)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"results":[{"id":2,"name":"Lumen","account_type":"XC","status":"success","is_active":true}]}'
    mock_resp.json.return_value = {
        "results": [{
            "id": 2,
            "name": "Lumen",
            "account_type": "XC",
            "server_url": "https://my.xc4.net",
            "status": "success",
            "is_active": True,
            "last_message": "ok",
            "password": "secret",
        }],
    }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, headers=None, json=None):
            assert method == "GET"
            assert url.endswith("/api/m3u/accounts/")
            assert headers["X-API-Key"] == "tok"
            return mock_resp

    monkeypatch.setattr(dispatcharr_sources.config, "read", lambda: {"DISPATCHARR_TOKEN": "tok"})
    monkeypatch.setattr(dispatcharr_sources.httpx, "Client", FakeClient)
    rows = dispatcharr_sources.list_m3u()
    assert rows == [{
        "id": 2,
        "name": "Lumen",
        "server_url": "https://my.xc4.net",
        "account_type": "XC",
        "status": "success",
        "is_active": True,
        "last_message": "ok",
        "max_streams": None,
    }]


def test_create_m3u_requires_fields(monkeypatch):
    monkeypatch.setattr(dispatcharr_sources, "enabled", lambda: True)
    monkeypatch.setattr(dispatcharr_sources.config, "read", lambda: {"DISPATCHARR_TOKEN": "tok"})
    try:
        dispatcharr_sources.create_m3u({"name": "x"})
    except ValueError as exc:
        assert "Server URL" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_create_m3u_posts_payload(monkeypatch):
    monkeypatch.setattr(dispatcharr_sources, "enabled", lambda: True)
    monkeypatch.setattr(dispatcharr_sources.config, "read", lambda: {"DISPATCHARR_TOKEN": "tok"})
    seen = {}

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.content = b"{}"
    mock_resp.json.return_value = {
        "id": 3,
        "name": "Lumen",
        "server_url": "https://my.xc4.net",
        "account_type": "XC",
        "status": "fetching",
        "is_active": True,
        "last_message": "",
    }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, headers=None, json=None):
            seen["method"] = method
            seen["json"] = json
            return mock_resp

    monkeypatch.setattr(dispatcharr_sources.httpx, "Client", FakeClient)
    row = dispatcharr_sources.create_m3u({
        "name": "Lumen",
        "account_type": "XC",
        "server_url": "https://my.xc4.net",
        "username": "user",
        "password": "pass",
        "max_streams": 3,
    })
    assert seen["method"] == "POST"
    assert seen["json"]["account_type"] == "XC"
    assert seen["json"]["username"] == "user"
    assert seen["json"]["max_streams"] == 3
    assert row["id"] == 3


def test_create_m3u_rejects_negative_max_streams(monkeypatch):
    monkeypatch.setattr(dispatcharr_sources, "enabled", lambda: True)
    monkeypatch.setattr(dispatcharr_sources.config, "read", lambda: {"DISPATCHARR_TOKEN": "tok"})
    try:
        dispatcharr_sources.create_m3u({
            "name": "Lumen",
            "server_url": "https://my.xc4.net",
            "max_streams": -1,
        })
    except ValueError as exc:
        assert "max_streams" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_refresh_epg_posts_import(monkeypatch):
    monkeypatch.setattr(dispatcharr_sources, "enabled", lambda: True)
    monkeypatch.setattr(dispatcharr_sources.config, "read", lambda: {"DISPATCHARR_TOKEN": "tok"})
    seen = {}

    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.content = b'{"success":true}'
    mock_resp.json.return_value = {"success": True}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, headers=None, json=None):
            seen["method"] = method
            seen["url"] = url
            seen["json"] = json
            return mock_resp

    monkeypatch.setattr(dispatcharr_sources.httpx, "Client", FakeClient)
    dispatcharr_sources.refresh_epg(9)
    assert seen == {
        "method": "POST",
        "url": f"{dispatcharr_sources.dispatcharr_base()}/api/epg/import/",
        "json": {"id": 9},
    }
