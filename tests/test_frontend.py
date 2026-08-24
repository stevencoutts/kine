"""Small presentation invariants for the single-file Helm UI."""
import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = (ROOT / "helm" / "frontend" / "index.html").read_text()
BACKEND = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()


def test_visible_control_labels_use_title_case():
    lower_case_labels = (
        ">First run<",
        ">Sign in<",
        ">Finish setup<",
        ">Check now<",
        ">Run leak test<",
        ">Restart tunnel group<",
        ">Update check<",
        ">Config backup<",
    )
    for label in lower_case_labels:
        assert label not in FRONTEND


def test_vpn_views_display_connection_type_not_provider():
    assert FRONTEND.count("v.connection_type") == 3
    assert "v.provider" not in FRONTEND


def test_vpn_api_derives_display_from_tunnel_type():
    assert '"connection_type": _connection_label(env.get("VPN_TYPE", ""))' in BACKEND


def test_app_overview_buttons_show_running_counts():
    assert "overview-bar" in FRONTEND
    assert "overview-btn" in FRONTEND
    assert "overview-stat" in FRONTEND
    assert "activeTier" in FRONTEND
    assert "tierStats" in FRONTEND
    assert "/ ${stats.total} running" in FRONTEND
    assert "tier-switch" in FRONTEND
    assert "manualCollapse" not in FRONTEND
    assert "section-head" not in FRONTEND


def test_tier_section_enabled_when_any_app_on():
    assert "any(app in enabled for app in visible)" in BACKEND
    assert "all(app in enabled for app in defaults)" not in BACKEND


def test_tier_enable_starts_only_requested_defaults():
    assert 'compose.run("up", "-d", *defaults)' in BACKEND


def test_tier_enable_seeds_before_up():
    """Without seed-before-up, *arr apps mint random API keys and wire
    cannot register download clients."""
    tier = BACKEND.split("@app.post(\"/api/tiers/{tier}/enable\")", 1)[1]
    tier = tier.split("@app.post(", 1)[0]
    assert 'compose.run("run", "--rm", "provision", "seed")' in tier
    assert tier.index('provision", "seed"') < tier.index('up", "-d", *defaults')


def test_app_enable_starts_only_requested_app():
    assert 'compose.run("up", "-d", app_id)' in BACKEND


def test_app_enable_seeds_before_up():
    enable = BACKEND.split("@app.post(\"/api/apps/{app_id}/enable\")", 1)[1]
    enable = enable.split("@app.post(", 1)[0]
    assert 'compose.run("run", "--rm", "provision", "seed")' in enable
    assert enable.index('provision", "seed"') < enable.index('up", "-d", app_id')


def test_tier_errors_render_inline_without_blocking_alert():
    assert 'id="tier-msg"' in FRONTEND
    assert "alert(e.message" not in FRONTEND
    assert "tierMsg.textContent = e.message" in FRONTEND


def test_tier_enable_does_not_return_raw_compose_output():
    assert 'raise HTTPException(500, out[-2000:])' not in BACKEND
    assert "Could not start {label} apps" in BACKEND


def test_enabled_apps_use_real_open_buttons():
    assert '<button class="act" data-open="${a.id}">Open</button>' in FRONTEND
    assert '<a class="link" href="${a.url}"' not in FRONTEND


def test_open_embeds_arr_apps_same_origin():
    assert "openEmbed" in FRONTEND
    assert "embed_url" in FRONTEND
    assert "embed-overlay" in FRONTEND
    assert "/view/" in (ROOT / "helm" / "backend" / "app" / "embed_proxy.py").read_text()
    assert "embed_proxy.mount" in BACKEND
    cat = (ROOT / "catalogue.yml").read_text()
    assert "embed: true" in cat
    # Transmission and NZBGet use the same same-origin embed path as the *arr apps.
    assert re.search(r"(?m)^  transmission:\n(?:    .*\n)*?    embed: true", cat)
    assert re.search(r"(?m)^  nzbget:\n(?:    .*\n)*?    embed: true", cat)
    assert re.search(r"(?m)^  dispatcharr:\n(?:    .*\n)*?    embed: true", cat)
    assert re.search(r"(?m)^  ecm:\n(?:    .*\n)*?    embed: true", cat)
    assert re.search(r"(?m)^  teamarr:\n(?:    .*\n)*?    embed: true", cat)


def test_media_overview_uses_settings_servers():
    assert "/media-servers" in FRONTEND
    assert "mediaServerCard" in FRONTEND
    assert "mediaPanelHtml" in FRONTEND
    assert "mediaStatHtml" in FRONTEND
    assert "data-media-url" in FRONTEND
    assert "settings-configured" in FRONTEND
    assert "watch-card" in FRONTEND
    assert "watch-art" in FRONTEND
    assert "formatClass" in FRONTEND
    assert "format-video" in FRONTEND
    assert "watch-art-fallback" in FRONTEND
    assert "photo/:/transcode" in (ROOT / "helm" / "backend" / "app" / "watching.py").read_text()
    assert 'image="Logo"' in (ROOT / "helm" / "backend" / "app" / "watching.py").read_text()
    assert "s.formats" in FRONTEND
    assert "s.art_url" in FRONTEND
    assert "position_label" in (ROOT / "helm" / "backend" / "app" / "watching.py").read_text()
    assert '@app.get("/api/watching/art/{server}")' in BACKEND
    assert "art_proxy_path" in (ROOT / "helm" / "backend" / "app" / "watching.py").read_text()


def test_dev_version_checkbox_only_when_supported():
    assert "a.dev_supported" in FRONTEND
    assert 'data-dev="${a.id}"' in FRONTEND
    assert "Dev Version" in FRONTEND
    assert "/apps/${el.dataset.dev}/dev/" in FRONTEND
    assert "tier-switch" in FRONTEND
    assert FRONTEND.count("tier-switch-ui") >= 2


def test_settings_media_servers_section():
    assert "Media Servers" in FRONTEND
    assert "PLEX_HOST" in FRONTEND
    assert "PLEX_TOKEN" in FRONTEND
    assert "EMBY_HOST" in FRONTEND
    assert "EMBY_API_KEY" in FRONTEND
    assert "save-media-servers" in FRONTEND
    assert "EMBY_DEFAULT_HOST" in FRONTEND
    assert "port 443 with SSL" in FRONTEND
    assert "bindMediaPortToggle" in FRONTEND
    assert "mediaPortFor" in FRONTEND
    assert "PLEX_HOST" in BACKEND
    assert "EMBY_API_KEY" in BACKEND
    assert "_MEDIA_SERVER_KEYS" in BACKEND
    assert "EMBY_DEFAULT_HOST" in BACKEND
    assert "DISPATCHARR_TOKEN" in FRONTEND
    assert "save-live-tv" in FRONTEND
    assert "_LIVE_TV_KEYS" in BACKEND
    assert 'wiring failed" not in log.lower()' in BACKEND
    assert "notification plex failed" in BACKEND
    assert "notifyFails" in FRONTEND
    assert 'provision", "wire"' in BACKEND


def test_settings_subtitles_opensubtitles():
    assert "OPENSUBTITLES_USERNAME" in FRONTEND
    assert "OPENSUBTITLES_PASSWORD" in FRONTEND
    assert "save-subtitles" in FRONTEND
    assert "en:forced" in FRONTEND
    assert "_SUBTITLE_KEYS" in BACKEND
    assert "OPENSUBTITLES_USERNAME" in BACKEND
    text = (ROOT / ".env.example").read_text()
    assert "OPENSUBTITLES_USERNAME=" in text
    assert "OPENSUBTITLES_PASSWORD=" in text
    provision = (ROOT / "compose" / "core.provision.yml").read_text()
    assert "OPENSUBTITLES_USERNAME" in provision


def test_settings_nzbget_news_servers():
    assert "nzbget_news_servers" in FRONTEND
    assert "save-nzbget" in FRONTEND
    assert "enableNzbgetFlow" in FRONTEND
    assert "Add a news server host, or choose Skip for Now." in FRONTEND
    assert "Extended Unpacker" in FRONTEND
    assert "Fake Detector" in FRONTEND
    assert "Remove Samples" in FRONTEND
    assert "NZBGET_NEWS_SERVERS" in BACKEND
    assert "news_servers" in BACKEND
    assert "nzbget_news" in BACKEND
    # Saving the form with an empty password field must not wipe .env creds.
    assert "Blank password in the form must not wipe" in BACKEND
    assert 'prev.get("password")' in BACKEND
    text = (ROOT / ".env.example").read_text()
    assert "NZBGET_NEWS_SERVERS=" in text
    provision = (ROOT / "compose" / "core.provision.yml").read_text()
    assert "NZBGET_NEWS_SERVERS" in provision
    recipe = (ROOT / "provision" / "recipes" / "nzbget.py").read_text()
    assert "ExtendedUnpacker" in recipe
    assert "FakeDetector" in recipe
    assert "RemoveSamples" in recipe


def test_enable_tunnelled_app_recreates_gluetun_group():
    assert "async def _start_app(" in BACKEND
    assert '"up", "-d", "--force-recreate"' in BACKEND
    assert "await _start_app(app_id, wanted)" in BACKEND
    assert 'await compose.run("up", "-d", app_id)' in BACKEND
    # Lone up of a tunnelled peer is what orphaned Sonarr when NZBGet enabled.
    assert BACKEND.count('await compose.run("up", "-d", app_id)') == 1


def test_dev_channel_api_recreates_when_profile_enabled():
    assert '@app.post("/api/apps/{app_id}/dev/enable")' in BACKEND
    assert '@app.post("/api/apps/{app_id}/dev/disable")' in BACKEND
    assert 'compose.run("pull", app_id)' in BACKEND
    assert 'compose.run("up", "-d", "--force-recreate", app_id)' in BACKEND


def test_open_button_uses_safe_new_window_and_inline_error():
    assert "const launchApp = (app) =>" in FRONTEND
    assert "window.open(app.url, '_blank', 'noopener')" not in FRONTEND
    assert "w.opener = null" in FRONTEND
    assert "window.location.assign(app.url)" in FRONTEND
    assert "launchApp(state.apps.find" in FRONTEND
    assert "Could not open" in FRONTEND
    assert 'target="_blank" rel="noopener noreferrer"' in FRONTEND


def test_helm_uses_grey_brand_and_favicon():
    assert "--accent:#8a8f98" in FRONTEND
    assert "#ff6a3d" not in FRONTEND
    assert 'href="/assets/favicon.svg"' in FRONTEND
    assert 'class="brand-mark"' in FRONTEND
    assert (ROOT / "helm" / "frontend" / "favicon.svg").is_file()


# ── stats page ──────────────────────────────────────────────────
def test_updates_page_lists_apps_not_core_containers():
    assert "Core containers" not in FRONTEND
    assert "section('Apps', apps, 'apps')" in FRONTEND
    assert "section('Core containers'" not in FRONTEND
    assert "catalogue_apps" in (ROOT / "helm" / "backend" / "app" / "updates_info.py").read_text()
    assert "is a core container" in BACKEND

    assert "stats:'Stats'" in FRONTEND.replace(" ", "")
    assert "render.stats" in FRONTEND


def test_stats_embeds_solo_panels_from_the_overview_dashboard():
    assert "/d-solo/kine-overview" in FRONTEND
    assert "kiosk" in FRONTEND


def test_apps_page_asks_for_sparkline_data():
    assert "/stats/cards" in FRONTEND


def test_metrics_tier_is_ordered_with_the_others():
    assert "'metrics'" in FRONTEND


def test_stats_endpoints_exist_in_the_backend():
    assert '"/api/stats/cards"' in BACKEND
    assert '"/api/metrics"' in BACKEND
