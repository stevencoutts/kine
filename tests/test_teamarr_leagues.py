"""Teamarr league selection + channel block helpers."""
import json
import pathlib
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))

from recipes import teamarr  # noqa: E402


def test_default_league_reserved_starts():
    starts = teamarr.assign_channel_starts([
        {"id": "eng.1", "name": "EPL"},
        {"id": "eng.fa", "name": "FA Cup"},
    ])
    assert starts == [
        {"id": "eng.1", "name": "EPL", "channel_start": 2000},
        {"id": "eng.fa", "name": "FA Cup", "channel_start": 2020},
    ]


def test_extra_leagues_continue_after_reserved_block():
    starts = teamarr.assign_channel_starts([
        {"id": "eng.1", "name": "EPL"},
        {"id": "esp.1", "name": "La Liga"},
    ])
    assert starts[0]["channel_start"] == 2000
    assert starts[1]["channel_start"] == 2180


def test_assign_rejects_empty():
    try:
        teamarr.assign_channel_starts([])
    except ValueError as exc:
        assert "league" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError")


def test_leagues_json_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(teamarr, "STACK", tmp_path)
    rows = teamarr.assign_channel_starts([
        {"id": "eng.1", "name": "EPL"},
        {"id": "uefa.champions", "name": "UCL"},
    ])
    path = teamarr.save_leagues(rows)
    assert path.is_file()
    loaded = teamarr.load_leagues()
    assert loaded["soccer_mode"] == "manual"
    assert [x["id"] for x in loaded["leagues"]] == ["eng.1", "uefa.champions"]
    assert loaded["leagues"][1]["channel_start"] == 2060


def test_default_catalog_covers_uk_set():
    ids = {row["id"] for row in teamarr.DEFAULT_LEAGUES}
    assert ids == {
        "eng.1", "eng.fa", "eng.league_cup",
        "uefa.champions", "uefa.champions_qual",
        "uefa.europa", "uefa.europa.conf",
        "fifa.world", "fifa.wcq.ply",
    }


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"x"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self):
        self.puts = []
        self.healthy = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, path, **kwargs):
        if path.endswith("/health") or path == "/health":
            return _FakeResp(200, {
                "status": "healthy" if self.healthy else "starting",
                "startup": {"is_ready": self.healthy, "phase": "READY"},
            })
        return _FakeResp(200, {})

    def put(self, path, **kwargs):
        self.puts.append((path, kwargs.get("json")))
        return _FakeResp(200, kwargs.get("json") or {})


def test_configure_puts_subscription_and_numbering(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(teamarr.httpx, "Client", lambda **kw: fake)
    rows = teamarr.assign_channel_starts([
        {"id": "eng.1", "name": "EPL"},
        {"id": "uefa.champions", "name": "UCL"},
    ])
    logs = []
    out = teamarr.configure(rows, log=logs.append, dispatcharr_token="tok")
    assert out["ok"] is True
    paths = [p for p, _ in fake.puts]
    assert "/api/v1/sports-subscription" in paths
    assert "/api/v1/settings/channel-numbering" in paths
    assert "/api/v1/settings/dispatcharr" in paths
    sub = next(body for p, body in fake.puts if p.endswith("sports-subscription"))
    assert sub["soccer_mode"] == "manual"
    assert sub["leagues"] == ["eng.1", "uefa.champions"]
    num = next(body for p, body in fake.puts if p.endswith("channel-numbering"))
    assert num["global_channel_mode"] == "manual"
    assert num["league_channel_starts"] == {"eng.1": 2000, "uefa.champions": 2060}
    disp = next(body for p, body in fake.puts if p.endswith("settings/dispatcharr"))
    assert disp["enabled"] is True
    assert disp["url"] == "http://127.0.0.1:9191"
