"""Push Teamarr event logos onto Emby Live TV channels.

Emby caches Primary art per tuner channel number. Teamarr reuses 2000 for
the next EPL match and updates Dispatcharr, but Emby keeps yesterday's image.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))

from recipes import emby  # noqa: E402


class _FakeResp:
    def __init__(self, status_code=204, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _FakeEmby:
    def __init__(self, channels=None):
        self.channels = channels or []
        self.posts = []

    def get(self, path, **kwargs):
        if path.rstrip("/").endswith("/LiveTv/Channels") or path.endswith("/LiveTv/Channels"):
            return _FakeResp(200, {"Items": self.channels})
        return _FakeResp(404, {})

    def post(self, path, **kwargs):
        self.posts.append((path, kwargs.get("params") or {}))
        return _FakeResp(204)


def test_channel_number_key_strips_fraction():
    assert emby.channel_number_key("2000.0") == "2000"
    assert emby.channel_number_key(2000) == "2000"
    assert emby.channel_number_key(" 2402 ") == "2402"


def test_sync_event_channel_art_posts_current_thumb_url():
    fake = _FakeEmby(channels=[
        {"Id": "2272890", "Name": "Aston Villa vs Arsenal", "Number": "2000"},
        {"Id": "1", "Name": "BBC 1", "Number": "101"},
    ])
    logs = []
    n = emby.sync_event_channel_art(
        [
            {
                "channel_number": "2000",
                "channel_name": "Aston Villa vs Arsenal",
                "logo_url": "https://thumbs.example/eng.1/AstonVilla/Arsenal/thumb",
            },
        ],
        logs.append,
        emby_http=fake,
    )
    assert n == 1
    path, params = fake.posts[0]
    assert path.endswith("/Items/2272890/Images/Primary/0/Url")
    assert params["Url"] == "https://thumbs.example/eng.1/AstonVilla/Arsenal/thumb"
    assert fake.posts[0][0].count("BBC") == 0


def test_sync_event_channel_art_skips_blank_logo():
    fake = _FakeEmby(channels=[{"Id": "9", "Number": "2000"}])
    assert emby.sync_event_channel_art(
        [{"channel_number": "2000", "logo_url": ""}],
        lambda *_: None,
        emby_http=fake,
    ) == 0
    assert fake.posts == []
