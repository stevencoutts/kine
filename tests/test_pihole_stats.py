"""Pi-hole v6 summary on the Helm Stats payload."""
import asyncio
import pathlib
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import pihole_stats, promquery  # noqa: E402

SUMMARY = {
    "queries": {
        "total": 830,
        "blocked": 82,
        "percent_blocked": 9.879518,
        "unique_domains": 312,
        "forwarded": 500,
        "cached": 248,
    },
    "clients": {"active": 3, "total": 8},
    "gravity": {"domains_being_blocked": 123456, "last_update": 1712345678},
    "took": 0.001,
}


def _reset() -> None:
    pihole_stats._cache["at"] = 0.0
    pihole_stats._cache["data"] = None
    promquery._overview_cache["at"] = 0.0
    promquery._overview_cache["data"] = {}


def test_parse_summary_reads_v6_fields():
    assert pihole_stats.parse_summary(SUMMARY) == {
        "queries": 830,
        "blocked": 82,
        "percent_blocked": 9.9,
        "domains_blocked": 123456,
    }


def test_parse_summary_rejects_a_non_summary():
    assert pihole_stats.parse_summary({}) is None
    assert pihole_stats.parse_summary({"queries": {}}) is None
    assert pihole_stats.parse_summary("nope") is None


def test_parse_summary_omits_domains_when_gravity_is_absent():
    payload = {"queries": {"total": 10, "blocked": 1, "percent_blocked": 10}}
    parsed = pihole_stats.parse_summary(payload)
    assert parsed["queries"] == 10
    assert parsed["blocked"] == 1
    assert parsed["percent_blocked"] == 10.0
    assert "domains_blocked" not in parsed


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://pihole/api/auth")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("bad", request=request, response=response)

    def json(self):
        return self._payload


class _Client:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        return _Response({"session": {"valid": True, "sid": "sid-1"}})

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, headers))
        return _Response(SUMMARY)

    async def delete(self, url, headers=None):
        self.calls.append(("DELETE", url, headers))
        return _Response({})


def test_summary_is_absent_when_pihole_is_disabled(monkeypatch):
    _reset()
    monkeypatch.setattr(pihole_stats.config, "profiles", lambda: ["grafana"])
    called = []
    monkeypatch.setattr(
        pihole_stats.httpx, "AsyncClient", lambda *a, **k: called.append(1))
    assert asyncio.run(pihole_stats.summary()) is None
    assert called == []


def test_summary_skips_the_api_when_the_password_is_unset(monkeypatch):
    _reset()
    monkeypatch.setattr(pihole_stats.config, "profiles", lambda: ["pihole"])
    monkeypatch.setattr(pihole_stats.config, "read", lambda: {})
    called = []
    monkeypatch.setattr(
        pihole_stats.httpx, "AsyncClient", lambda *a, **k: called.append(1))
    assert asyncio.run(pihole_stats.summary()) is None
    assert called == []


def test_summary_reads_the_v6_api_without_returning_the_password(monkeypatch):
    _reset()
    monkeypatch.setattr(pihole_stats.config, "profiles", lambda: ["pihole"])
    monkeypatch.setattr(
        pihole_stats.config, "read", lambda: {"PIHOLE_WEBPASSWORD": "s3cret-pass"})
    client = _Client()
    monkeypatch.setattr(pihole_stats.httpx, "AsyncClient", lambda *a, **k: client)
    got = asyncio.run(pihole_stats.summary())
    assert got == {
        "queries": 830,
        "blocked": 82,
        "percent_blocked": 9.9,
        "domains_blocked": 123456,
    }
    assert "s3cret-pass" not in str(got)
    assert [c[0] for c in client.calls] == ["POST", "GET", "DELETE"]
    assert client.calls[0][1] == "http://pihole/api/auth"
    assert client.calls[0][2] == {"password": "s3cret-pass"}
    assert client.calls[1] == (
        "GET",
        "http://pihole/api/stats/summary",
        {"X-FTL-SID": "sid-1"},
    )
    assert "s3cret-pass" not in client.calls[1][1]
    assert client.calls[2][1] == "http://pihole/api/auth"
    again = asyncio.run(pihole_stats.summary())
    assert again == got
    assert len(client.calls) == 3


def test_summary_is_none_when_auth_fails(monkeypatch):
    _reset()
    monkeypatch.setattr(pihole_stats.config, "profiles", lambda: ["pihole"])
    monkeypatch.setattr(
        pihole_stats.config, "read", lambda: {"PIHOLE_WEBPASSWORD": "nope"})

    class Bad(_Client):
        async def post(self, url, json=None):
            self.calls.append(("POST", url, json))
            return _Response({}, status=401)

    client = Bad()
    monkeypatch.setattr(pihole_stats.httpx, "AsyncClient", lambda *a, **k: client)
    assert asyncio.run(pihole_stats.summary()) is None
    assert [c[0] for c in client.calls] == ["POST"]


def test_overview_omits_pihole_when_summary_is_absent(monkeypatch):
    _reset()
    monkeypatch.setattr(promquery.config, "profiles", lambda: [])

    async def no_pihole():
        return None

    monkeypatch.setattr(promquery.pihole_stats, "summary", no_pihole)
    data = asyncio.run(promquery.overview())
    assert "pihole" not in data
    assert data["streamers"]["unique"] == 0


def test_overview_includes_pihole_when_summary_is_present(monkeypatch):
    _reset()
    monkeypatch.setattr(promquery.config, "profiles", lambda: [])

    async def yes():
        return {
            "queries": 10,
            "blocked": 2,
            "percent_blocked": 20.0,
            "domains_blocked": 5,
        }

    monkeypatch.setattr(promquery.pihole_stats, "summary", yes)
    data = asyncio.run(promquery.overview())
    assert data["pihole"] == {
        "queries": 10,
        "blocked": 2,
        "percent_blocked": 20.0,
        "domains_blocked": 5,
    }
    assert data["streamers"]["unique"] == 0
