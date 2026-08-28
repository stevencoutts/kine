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
    assert starts[1]["channel_start"] == 3520


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
        "boxing", "ufc",
    }
    starts = {row["id"]: row["channel_start"] for row in teamarr.DEFAULT_LEAGUES}
    assert starts["boxing"] == 3000
    assert starts["ufc"] == 3500


def test_default_followed_teams():
    names = [t["name"] for t in teamarr.DEFAULT_FOLLOWED_TEAMS]
    assert names == ["Liverpool", "Sunderland", "Arsenal"]
    assert all(t.get("provider") == "espn" and t.get("team_id") for t in teamarr.DEFAULT_FOLLOWED_TEAMS)


def test_default_template_assignments_cover_kore_set():
    names = [a["name"] for a in teamarr.DEFAULT_TEMPLATE_ASSIGNMENTS]
    assert names == ["EPL Default", "Europe", "World Cup", "Boxing", "UFC"]
    epl = next(a for a in teamarr.DEFAULT_TEMPLATE_ASSIGNMENTS if a["name"] == "EPL Default")
    assert epl["leagues"] == ["eng.1", "eng.league_cup", "eng.fa"]
    ufc = next(a for a in teamarr.DEFAULT_TEMPLATE_ASSIGNMENTS if a["name"] == "UFC")
    assert ufc["sports"] == ["mma"]
    assert ufc["leagues"] == ["ufc"]


def test_resolve_stream_profile_id_prefers_stable():
    rows = [
        {"id": 1, "name": "ffmpeg"},
        {"id": 5, "name": "VLC"},
        {"id": 19, "name": "Stable"},
    ]
    assert teamarr.resolve_stream_profile_id(rows) == 19
    assert teamarr.resolve_stream_profile_id([{"id": 1, "name": "ffmpeg"}]) == 1
    assert teamarr.resolve_stream_profile_id([]) is None


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
    def __init__(self, assignments=None, dispatcharr=None, stream_profiles=None):
        self.puts = []
        self.posts = []
        self.deletes = []
        self.healthy = True
        self.assignments = list(assignments or [])
        self.templates = [
            {"id": 6, "name": "Soccer Club Event (Starter)", "template_type": "event"},
            {"id": 4, "name": "Default Event (Starter)", "template_type": "event"},
            {"id": 7, "name": "Combat Event (Starter)", "template_type": "event"},
            {"id": 8, "name": "International Event (Starter)", "template_type": "event"},
        ]
        self.next_template_id = 100
        self.dispatcharr = dict(dispatcharr or {
            "enabled": True,
            "url": "http://127.0.0.1:9191",
            "default_channel_profile_ids": None,
            "default_stream_profile_id": None,
            "default_channel_group_id": None,
            "default_channel_group_mode": "static",
            "cleanup_unused_logos": False,
        })
        self.stream_profiles = list(stream_profiles or [
            {"id": 1, "name": "ffmpeg"},
            {"id": 19, "name": "Stable"},
        ])
        self.subscription = {
            "leagues": [],
            "soccer_mode": "manual",
            "soccer_followed_teams": [],
        }
        self.groups = {"groups": []}
        self.m3u_accounts = []
        self.epg_sources = []
        self.next_group_id = 100

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, path, **kwargs):
        params = kwargs.get("params") or {}
        if params.get("include_disabled") in (True, "true", "1", 1):
            path = f"{path}{'&' if '?' in path else '?'}include_disabled=true"
        if path.endswith("/health") or path == "/health":
            return _FakeResp(200, {
                "status": "healthy" if self.healthy else "starting",
                "startup": {"is_ready": self.healthy, "phase": "READY"},
            })
        if path.endswith("/subscription-templates"):
            return _FakeResp(200, {
                "templates": self.assignments,
                "total": len(self.assignments),
            })
        if path.endswith("/templates"):
            return _FakeResp(200, self.templates)
        if path.endswith("/settings/epg"):
            return _FakeResp(200, {
                "team_schedule_days_ahead": 30,
                "event_match_days_ahead": 3,
                "art_base_url": "",
                "epg_timezone": "America/New_York",
            })
        if path.endswith("/settings/dispatcharr"):
            return _FakeResp(200, dict(self.dispatcharr))
        if path.endswith("/dispatcharr/stream-profiles"):
            return _FakeResp(200, self.stream_profiles)
        if path.endswith("/sports-subscription"):
            return _FakeResp(200, dict(self.subscription))
        if path.endswith("/dispatcharr/m3u-accounts"):
            return _FakeResp(200, list(self.m3u_accounts))
        if path.endswith("/dispatcharr/epg-sources"):
            return _FakeResp(200, {"success": True, "sources": list(self.epg_sources)})
        group_path = path.split("?", 1)[0]
        if group_path in ("/api/v1/groups", "/groups"):
            rows = list(self.groups.get("groups") or [])
            include_disabled = "include_disabled=true" in path.lower()
            if not include_disabled:
                rows = [g for g in rows if g.get("enabled")]
            return _FakeResp(200, {"groups": rows, "total": len(rows)})
        return _FakeResp(200, {})

    def put(self, path, **kwargs):
        body = kwargs.get("json") or {}
        self.puts.append((path, body))
        if path.endswith("/settings/dispatcharr"):
            self.dispatcharr.update(body)
        if path.endswith("/sports-subscription"):
            self.subscription.update(body)
        if "/api/v1/groups/" in path and not path.endswith("/disable") and not path.endswith("/enable"):
            gid = int(path.rstrip("/").split("/")[-1])
            for row in self.groups["groups"]:
                if row.get("id") == gid:
                    row.update(body)
                    return _FakeResp(200, dict(row))
        return _FakeResp(200, body)

    def delete(self, path, **kwargs):
        self.deletes.append(path)
        if "/subscription-templates/" in path:
            aid = int(path.rstrip("/").split("/")[-1])
            self.assignments = [a for a in self.assignments if a.get("id") != aid]
        return _FakeResp(200, {})

    def post(self, path, **kwargs):
        body = kwargs.get("json") or {}
        self.posts.append((path, body))
        if path.endswith("/templates"):
            row = {"id": self.next_template_id, **body}
            self.next_template_id += 1
            self.templates.append(row)
            return _FakeResp(201, row)
        if path.endswith("/subscription-templates"):
            row = {
                "id": len(self.assignments) + 1,
                "template_id": body.get("template_id"),
                "sports": body.get("sports"),
                "leagues": body.get("leagues"),
                "template_name": next(
                    (t["name"] for t in self.templates if t["id"] == body.get("template_id")),
                    None,
                ),
            }
            self.assignments.append(row)
            return _FakeResp(201, row)
        if path.split("?")[0].rstrip("/") in ("/api/v1/groups", "/groups"):
            row = {"id": self.next_group_id, "enabled": True, **body}
            self.next_group_id += 1
            self.groups.setdefault("groups", []).append(row)
            return _FakeResp(201, row)
        if path.endswith("/disable"):
            gid = int(path.rstrip("/").split("/")[-2])
            for row in self.groups["groups"]:
                if row.get("id") == gid:
                    row["enabled"] = False
            return _FakeResp(200, {"success": True})
        if path.endswith("/enable"):
            gid = int(path.rstrip("/").split("/")[-2])
            for row in self.groups["groups"]:
                if row.get("id") == gid:
                    row["enabled"] = True
            return _FakeResp(200, {"success": True})
        return _FakeResp(200, body)


def test_configure_puts_subscription_and_numbering(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(teamarr.httpx, "Client", lambda **kw: fake)
    monkeypatch.setenv("KINE_DOMAIN", "example.test")
    monkeypatch.setenv("KINE_TIMEZONE", "Europe/London")
    monkeypatch.setenv("TRAEFIK_HTTPS_PORT", "8443")
    rows = teamarr.assign_channel_starts([
        {"id": "eng.1", "name": "EPL"},
        {"id": "uefa.champions", "name": "UCL"},
    ])
    logs = []
    out = teamarr.configure(
        rows,
        log=logs.append,
        dispatcharr_token="tok",
        dispatcharr_username="kine",
        dispatcharr_password="secret",
    )
    assert out["ok"] is True
    paths = [p for p, _ in fake.puts]
    assert "/api/v1/sports-subscription" in paths
    assert "/api/v1/settings/channel-numbering" in paths
    assert "/api/v1/settings/dispatcharr" in paths
    assert "/api/v1/settings/epg" in paths
    sub = next(body for p, body in fake.puts if p.endswith("sports-subscription"))
    assert sub["soccer_mode"] == "manual"
    assert sub["leagues"] == ["eng.1", "uefa.champions"]
    assert sub["soccer_followed_teams"] == teamarr.DEFAULT_FOLLOWED_TEAMS
    num = next(body for p, body in fake.puts if p.endswith("channel-numbering"))
    assert num["global_channel_mode"] == "manual"
    assert num["league_channel_starts"] == {"eng.1": 2000, "uefa.champions": 2060}
    disp = next(body for p, body in fake.puts if p.endswith("settings/dispatcharr"))
    assert disp["enabled"] is True
    assert disp["url"] == "http://127.0.0.1:9191"
    assert disp["username"] == "kine"
    assert disp["password"] == "secret"
    assert disp["default_channel_group_mode"] == "{sport} | {league}"
    assert disp["default_channel_profile_ids"] == ["{sport}"]
    assert disp["default_stream_profile_id"] == 19
    epg = next(body for p, body in fake.puts if p.endswith("settings/epg"))
    assert epg["art_base_url"] == "https://thumbs.example.test:8443"
    assert epg["epg_timezone"] == "Europe/London"
    assigned_names = [
        body.get("template_id")
        for p, body in fake.posts
        if p.endswith("/subscription-templates")
    ]
    assert len(assigned_names) == len(teamarr.DEFAULT_TEMPLATE_ASSIGNMENTS)
    created = [body["name"] for p, body in fake.posts if p.endswith("/templates")]
    assert created == [a["name"] for a in teamarr.DEFAULT_TEMPLATE_ASSIGNMENTS]


def test_art_base_url_from_env_prefers_explicit(monkeypatch):
    monkeypatch.setenv("GAME_THUMBS_PUBLIC_URL", "http://thumbs.lan:3000/")
    monkeypatch.setenv("KINE_DOMAIN", "ignored.example")
    assert teamarr.art_base_url_from_env() == "http://thumbs.lan:3000"


def test_art_base_url_from_env_includes_nonstandard_https_port(monkeypatch):
    monkeypatch.delenv("GAME_THUMBS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("KINE_DOMAIN", "example.test")
    monkeypatch.setenv("TRAEFIK_HTTPS_PORT", "8443")
    assert teamarr.art_base_url_from_env() == "https://thumbs.example.test:8443"
    monkeypatch.setenv("TRAEFIK_HTTPS_PORT", "443")
    assert teamarr.art_base_url_from_env() == "https://thumbs.example.test"


def test_epg_timezone_from_env(monkeypatch):
    monkeypatch.setenv("KINE_TIMEZONE", "Europe/London")
    monkeypatch.delenv("TZ", raising=False)
    assert teamarr.epg_timezone_from_env() == "Europe/London"
    monkeypatch.delenv("KINE_TIMEZONE", raising=False)
    monkeypatch.setenv("TZ", "Europe/Paris")
    assert teamarr.epg_timezone_from_env() == "Europe/Paris"
    monkeypatch.delenv("TZ", raising=False)
    assert teamarr.epg_timezone_from_env() == ""


def test_configure_sets_epg_timezone_from_kine(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(teamarr.httpx, "Client", lambda **kw: fake)
    monkeypatch.delenv("KINE_DOMAIN", raising=False)
    monkeypatch.delenv("GAME_THUMBS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("KINE_TIMEZONE", "Europe/London")
    logs = []
    out = teamarr.configure(
        [{"id": "eng.1", "name": "EPL"}],
        log=logs.append,
    )
    assert out["ok"] is True
    epg = next(body for p, body in fake.puts if p.endswith("settings/epg"))
    assert epg["epg_timezone"] == "Europe/London"
    assert "art_base_url" not in epg or epg.get("art_base_url") in ("", None)
    assert any("epg_timezone" in m for m in logs)


def test_configure_skips_channel_output_when_already_customized(monkeypatch):
    fake = _FakeClient(dispatcharr={
        "enabled": True,
        "url": "http://127.0.0.1:9191",
        "default_channel_profile_ids": ["{sport}"],
        "default_stream_profile_id": 19,
        "default_channel_group_id": None,
        "default_channel_group_mode": "{sport} | {league}",
        "cleanup_unused_logos": False,
    })
    monkeypatch.setattr(teamarr.httpx, "Client", lambda **kw: fake)
    monkeypatch.delenv("KINE_DOMAIN", raising=False)
    monkeypatch.delenv("GAME_THUMBS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("KINE_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    out = teamarr.configure(
        [{"id": "eng.1", "name": "EPL"}],
        log=lambda _m: None,
        dispatcharr_username="kine",
        dispatcharr_password="secret",
    )
    assert out["ok"] is True
    disp_puts = [body for p, body in fake.puts if p.endswith("settings/dispatcharr")]
    # Teamarr PUT replaces the whole object — echo customized output fields.
    assert disp_puts[-1]["default_channel_group_mode"] == "{sport} | {league}"
    assert disp_puts[-1]["default_channel_profile_ids"] == ["{sport}"]
    assert disp_puts[-1]["default_stream_profile_id"] == 19
    assert disp_puts[-1]["cleanup_unused_logos"] is False


def test_configure_replaces_placeholder_template_assignments(monkeypatch):
    fake = _FakeClient(assignments=[{
        "id": 1, "template_id": 6, "sports": ["Soccer"], "leagues": None,
        "template_name": "Soccer Club Event (Starter)",
    }])
    monkeypatch.setattr(teamarr.httpx, "Client", lambda **kw: fake)
    monkeypatch.delenv("KINE_DOMAIN", raising=False)
    monkeypatch.delenv("GAME_THUMBS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("KINE_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    out = teamarr.configure(
        [{"id": "eng.1", "name": "EPL"}],
        log=lambda _m: None,
    )
    assert out["ok"] is True
    assert any(path.endswith("/subscription-templates/1") for path in fake.deletes)
    assert len([1 for p, _ in fake.posts if p.endswith("/subscription-templates")]) == 5


def test_configure_skips_templates_when_already_customized(monkeypatch):
    fake = _FakeClient(assignments=[{
        "id": 1, "template_id": 100, "sports": None,
        "leagues": ["eng.1", "eng.league_cup", "eng.fa"],
        "template_name": "EPL Default",
    }])
    monkeypatch.setattr(teamarr.httpx, "Client", lambda **kw: fake)
    monkeypatch.delenv("KINE_DOMAIN", raising=False)
    monkeypatch.delenv("GAME_THUMBS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("KINE_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    out = teamarr.configure(
        [{"id": "eng.1", "name": "EPL"}],
        log=lambda _m: None,
    )
    assert out["ok"] is True
    assert fake.deletes == []
    assert not any(p.endswith("/subscription-templates") for p, _ in fake.posts)
    assert not any(p.endswith("/templates") for p, _ in fake.posts)


def test_warns_when_event_group_bound_to_disabled_m3u():
    fake = _FakeClient()
    fake.groups = {"groups": [
        {
            "id": 2, "name": "Sports | EPL", "enabled": True,
            "m3u_account_id": 3, "m3u_account_name": "Lumen-direct",
        },
        {
            "id": 13, "name": "Strong8K | EPL PPV", "enabled": True,
            "m3u_account_id": 5, "m3u_account_name": "Strong8K",
        },
        {
            "id": 8, "name": "old lumen sky", "enabled": False,
            "m3u_account_id": 3,
        },
    ]}
    fake.m3u_accounts = [
        {"id": 3, "name": "Lumen-direct", "status": "disabled"},
        {"id": 5, "name": "Strong8K", "status": "success"},
    ]
    logs = []
    stale = teamarr.warn_inactive_event_group_m3us(fake, logs.append)
    assert [g["id"] for g in stale] == [2]
    assert any("Sports | EPL" in m and "Lumen-direct" in m for m in logs)


def _active_accounts():
    return [
        {"id": 3, "name": "Lumen-direct", "status": "idle"},
        {"id": 5, "name": "Strong8K", "status": "success"},
        {"id": 1, "name": "custom", "status": "error"},
        {"id": 9, "name": "Dead", "status": "disabled"},
    ]


def test_managed_event_group_name():
    assert teamarr.managed_event_group_name("Strong8K") == "Strong8K | Sports"


def test_ensure_event_groups_creates_pattern_group_per_active_m3u():
    fake = _FakeClient()
    fake.m3u_accounts = _active_accounts()
    logs = []
    created = teamarr.ensure_event_groups(fake, logs.append)
    names = [g["name"] for g in fake.groups["groups"] if g.get("enabled")]
    assert names == ["Lumen-direct | Sports", "Strong8K | Sports"]
    assert created == 2
    for row in fake.groups["groups"]:
        if not row.get("enabled"):
            continue
        assert row["m3u_group_name_pattern_enabled"] is True
        assert row["m3u_group_name_pattern"] == teamarr.SPORTS_GROUP_PATTERN
        assert row["stream_exclude_regex_enabled"] is True
        assert row["duplicate_event_handling"] == "consolidate"
        posts = [body for p, body in fake.posts if p.rstrip("/") in ("/api/v1/groups", "/groups")]
        assert posts and posts[0]["m3u_account_id"] in {3, 5}


def test_ensure_event_groups_skips_disabled_and_error_m3u():
    fake = _FakeClient()
    fake.m3u_accounts = _active_accounts()
    teamarr.ensure_event_groups(fake, lambda _m: None)
    ids = {g["m3u_account_id"] for g in fake.groups["groups"]}
    assert ids == {3, 5}


def test_ensure_event_groups_updates_existing_instead_of_duplicating():
    fake = _FakeClient()
    fake.m3u_accounts = [{"id": 5, "name": "Strong8K", "status": "success"}]
    fake.groups = {"groups": [{
        "id": 13, "name": "Strong8K | Sports", "enabled": True,
        "m3u_account_id": 5, "m3u_group_name_pattern_enabled": True,
        "m3u_group_name_pattern": "old",
    }]}
    fake.next_group_id = 50
    logs = []
    created = teamarr.ensure_event_groups(fake, logs.append)
    assert created == 0
    enabled = [g for g in fake.groups["groups"] if g.get("enabled")]
    assert len(enabled) == 1
    assert enabled[0]["id"] == 13
    assert enabled[0]["m3u_group_name_pattern"] == teamarr.SPORTS_GROUP_PATTERN
    assert not any(p.rstrip("/") in ("/api/v1/groups", "/groups") for p, _ in fake.posts)


def test_ensure_event_groups_disables_sibling_groups_on_same_account():
    fake = _FakeClient()
    fake.m3u_accounts = [{"id": 5, "name": "Strong8K", "status": "success"}]
    fake.groups = {"groups": [{
        "id": 13, "name": "Strong8K | EPL PPV", "enabled": True,
        "m3u_account_id": 5, "m3u_group_name_pattern_enabled": False,
    }]}
    teamarr.ensure_event_groups(fake, lambda _m: None)
    by_id = {g["id"]: g for g in fake.groups["groups"]}
    assert by_id[13]["enabled"] is False
    sports = [g for g in fake.groups["groups"] if g["name"] == "Strong8K | Sports"]
    assert len(sports) == 1 and sports[0]["enabled"] is True


def test_ensure_event_groups_disables_managed_group_when_m3u_disabled():
    fake = _FakeClient()
    fake.m3u_accounts = [{"id": 3, "name": "Lumen-direct", "status": "disabled"}]
    fake.groups = {"groups": [{
        "id": 20, "name": "Lumen-direct | Sports", "enabled": True,
        "m3u_account_id": 3, "m3u_group_name_pattern_enabled": True,
    }]}
    teamarr.ensure_event_groups(fake, lambda _m: None)
    assert fake.groups["groups"][0]["enabled"] is False
    assert any(p.endswith("/disable") for p, _ in fake.posts)


def test_resolve_teamarr_epg_source_id():
    sources = [
        {"id": 8, "name": "Open-EPG"},
        {"id": 7, "name": "Teamarr"},
        {"id": 2, "name": "Jesmann - UK"},
    ]
    assert teamarr.resolve_teamarr_epg_source_id(sources) == 7
    assert teamarr.resolve_teamarr_epg_source_id([]) is None
    assert teamarr.resolve_teamarr_epg_source_id([{"id": 1, "name": "Lumen"}]) is None


def test_configure_sets_dispatcharr_epg_id_from_teamarr_source(monkeypatch):
    fake = _FakeClient()
    fake.epg_sources = [
        {"id": 8, "name": "Open-EPG"},
        {"id": 7, "name": "Teamarr"},
    ]
    monkeypatch.setattr(teamarr.httpx, "Client", lambda **kw: fake)
    monkeypatch.delenv("KINE_DOMAIN", raising=False)
    monkeypatch.delenv("GAME_THUMBS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("KINE_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    out = teamarr.configure(
        [{"id": "eng.1", "name": "EPL"}],
        log=lambda _m: None,
        dispatcharr_username="kine",
        dispatcharr_password="secret",
    )
    assert out["ok"] is True
    disp = next(body for p, body in fake.puts if p.endswith("settings/dispatcharr"))
    assert disp["epg_id"] == 7
