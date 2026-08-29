"""Small presentation invariants for the single-file Helm UI."""
import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = (ROOT / "helm" / "frontend" / "index.html").read_text()
BACKEND = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()


def test_backup_restore_api_routes_exist():
    assert '@app.get("/api/backups")' in BACKEND
    assert '@app.post("/api/backups/restore")' in BACKEND
    assert '@app.get("/api/backups/{name}/file")' in BACKEND
    assert '@app.delete("/api/backups/{name}")' in BACKEND
    assert "backups.resolve" in BACKEND
    assert "restore.sh" in BACKEND


def test_snapshots_admin_is_a_settings_section():
    assert "{id:'snapshots', label:'Snapshots'}" in FRONTEND
    assert "render.snapshots" in FRONTEND
    assert "id=\"backup-now\"" in FRONTEND
    assert "data-backup-download" in FRONTEND or "/backups/" in FRONTEND and "/file" in FRONTEND
    assert "data-backup-delete" in FRONTEND
    assert "data-backup-restore" in FRONTEND
    snaps = FRONTEND.split("render.snapshots = async", 1)[1].split("render.settings", 1)[0]
    assert "kind" in snaps
    assert "scheduled" in snaps or "update" in snaps


def test_status_page_links_to_snapshots_instead_of_restore_ui():
    status = FRONTEND.split("render.status = async () => {", 1)[1].split("render.snapshots = async", 1)[0]
    assert "snapshots" in status
    assert "id=\"goto-snapshots\"" in status or "data-settings-section=\"snapshots\"" in status
    assert "id=\"backup-now\"" not in status
    assert "id=\"backup-list\"" not in status
    assert "Backup and Restore" not in status


def test_status_page_has_disk_rings_and_glass():
    assert "status-disk-ring" in FRONTEND or "status-ring" in FRONTEND
    assert "status-glass" in FRONTEND
    assert "stroke-dasharray" in FRONTEND
    assert "render.status" in FRONTEND
    status = FRONTEND.split("render.status = async () => {", 1)[1].split("render.settings", 1)[0]
    assert "status-page" in status
    assert "svg" in status.lower() or "stroke-dashoffset" in status
    # Pending updates drive the badge; sticky cron parse noise must not.
    assert "jobs.errors?.updates" not in status
    assert "pending.length" in status


def test_status_api_stats_media_via_data_root_media():
    assert "nfs_media_mountpoint" in BACKEND or 'f"{data_root}/media"' in BACKEND or "/media" in BACKEND.split("@app.get(\"/api/status\")", 1)[1].split("@app.get(", 1)[0]
    status = BACKEND.split('@app.get("/api/status")', 1)[1].split("@app.get(", 1)[0]
    assert "statvfs" in status
    assert "media" in status
    assert 'error": "not mounted"' in status or "not mounted" in status


def test_header_has_logout_button():
    assert 'data-logout>Log Out</button>' in FRONTEND
    assert 'data-nav-toggle' in FRONTEND
    assert 'nav-open' in FRONTEND
    assert '@media (max-width:720px)' in FRONTEND
    assert 'border-radius:999px' in FRONTEND
    assert 'nav button.nav-logout' in FRONTEND
    assert "/api/auth/logout" in FRONTEND
    assert '@app.post("/api/auth/logout")' in BACKEND


def test_footer_has_updates_chip_that_opens_updates():
    assert 'id="updates-chip"' in FRONTEND
    assert "data-footer-updates" in FRONTEND
    footer_css = FRONTEND.split("footer#vpnbar{", 1)[1].split("}", 1)[0]
    assert "space-between" in footer_css
    shell = FRONTEND.split("render.shell = (body) => {", 1)[1].split("updateVpnBar();", 1)[0]
    assert 'id="vpn-status"' in shell
    assert 'id="updates-chip"' in shell
    vpn = FRONTEND.split("const updateVpnBar = async () => {", 1)[1].split("setInterval", 1)[0]
    assert "bar.innerHTML" not in vpn
    assert "vpn-status" in vpn
    assert "updateUpdatesChip" in vpn or "updates-chip" in vpn
    assert "render.updates()" in FRONTEND
    assert "settingsSection = 'updates'" in FRONTEND or 'settingsSection="updates"' in FRONTEND


def test_top_nav_is_dashboard_stats_vpn_settings():
    compact = FRONTEND.replace(" ", "")
    assert "apps:'Dashboard'" in compact
    assert "['apps','stats','vpn','settings']" in compact
    assert "['apps','stats','updates','vpn','status','settings']" not in compact
    assert "{id:'status',label:'Status'}" in compact
    assert "{id:'updates',label:'Updates'}" in compact


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
    assert "activeTier:'watching'" in FRONTEND
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
    assert "await _start_app(app_id, wanted)" in BACKEND
    assert 'compose.run("up", "-d", app_id)' in BACKEND


def test_app_enable_seeds_before_up():
    enable = BACKEND.split("@app.post(\"/api/apps/{app_id}/enable\")", 1)[1]
    enable = enable.split("@app.post(", 1)[0]
    assert 'compose.run("run", "--rm", "provision", "seed")' in enable
    assert enable.index('provision", "seed"') < enable.index("_start_app(app_id, wanted)")


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


def test_apps_panel_splits_enabled_and_disabled():
    assert "appsSectionsHtml" in FRONTEND
    assert "apps-section-title" in FRONTEND
    assert "section('Enabled', enabled)" in FRONTEND
    assert "section('Disabled', disabled)" in FRONTEND
    assert "appsSectionsHtml(activeItems)" in FRONTEND
    assert "appsSectionsHtml(items)" in FRONTEND


def test_teamarr_enable_modal_exists():
    assert "promptTeamarrLeagues" in FRONTEND
    assert "enableTeamarrFlow" in FRONTEND
    assert "Teamarr Soccer Leagues" in FRONTEND
    assert "/teamarr/leagues" in FRONTEND
    assert "/apps/teamarr/enable" in FRONTEND
    assert '@app.get("/api/teamarr/leagues")' in BACKEND
    assert "teamarr-league-row" in FRONTEND
    assert ".teamarr-league-row input[type=checkbox]{width:auto" in FRONTEND
    assert "await render.apps()" in FRONTEND
    assert "create_task(asyncio.to_thread(_apply))" in BACKEND


def test_open_embeds_arr_apps_same_origin():
    assert "openEmbed" in FRONTEND
    assert "embed_url" in FRONTEND
    assert "embed-overlay" in FRONTEND
    # overflow:hidden clips capital glyph side-bearings without a hair of padding
    assert ".embed-bar h2" in FRONTEND and "padding-inline:2px" in FRONTEND
    assert "/view/" in (ROOT / "helm" / "backend" / "app" / "embed_proxy.py").read_text()
    assert "embed_proxy.mount" in BACKEND
    assert "ping_interval=None" in (ROOT / "helm" / "backend" / "app" / "embed_proxy.py").read_text()
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
    assert ".watch-art-fallback[hidden]" in FRONTEND
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
    assert "PLEX_TV_MAP_FROM" in FRONTEND
    assert "PLEX_TV_MAP_TO" in FRONTEND
    assert "PLEX_MOVIES_MAP_FROM" in FRONTEND
    assert "EMBY_TV_MAP_FROM" in FRONTEND
    assert "EMBY_MOVIES_MAP_TO" in FRONTEND
    assert "save-media-servers" in FRONTEND
    assert "EMBY_DEFAULT_HOST" in FRONTEND
    assert "port 443 with SSL" in FRONTEND
    assert "bindMediaPortToggle" in FRONTEND
    assert "mediaPortFor" in FRONTEND
    assert "PLEX_HOST" in BACKEND
    assert "EMBY_API_KEY" in BACKEND
    assert "PLEX_TV_MAP_FROM" in BACKEND
    assert "EMBY_MOVIES_MAP_TO" in BACKEND
    assert "_MEDIA_SERVER_KEYS" in BACKEND
    assert "EMBY_DEFAULT_HOST" in BACKEND
    assert "DISPATCHARR_TOKEN" in FRONTEND
    assert "save-live-tv" in FRONTEND
    assert "_LIVE_TV_KEYS" in BACKEND
    assert 'wiring failed" not in log.lower()' in BACKEND
    assert "notification plex failed" in BACKEND
    assert "notifyFails" in FRONTEND
    assert 'provision", "wire"' in BACKEND


def test_settings_section_nav():
    assert "SETTINGS_SECTIONS" in FRONTEND
    assert "settingsSection" in FRONTEND
    assert "settings-nav" in FRONTEND
    assert "data-settings-section" in FRONTEND
    assert "data-settings-panel" in FRONTEND
    assert "CLOUDNS_AUTH_ID" in FRONTEND
    assert "CLOUDNS_AUTH_PASSWORD" in FRONTEND
    assert "acme.env" in FRONTEND or "ClouDNS Auth" in FRONTEND
    for label in ("Appliance", "Storage", "Media Servers", "Live TV", "Subtitles", "NZBGet", "Status", "Updates"):
        assert label in FRONTEND
    assert "settingsPanel(" in FRONTEND or "settingsPanel =" in FRONTEND
    # One-panel UX: inactive panels use the hidden attribute.
    assert "settings-panel" in FRONTEND
    assert "panel.hidden" in FRONTEND or "p.hidden" in FRONTEND or "hidden'" in FRONTEND


def test_settings_page_uses_side_nav_and_field_groups():
    assert "settings-layout" in FRONTEND
    assert "settings-side" in FRONTEND
    assert "settings-main" in FRONTEND
    assert "settings-glass" in FRONTEND
    assert "settings-group" in FRONTEND
    assert "settings-group-title" in FRONTEND
    # Field groups instead of one undifferentiated card dump.
    for title in ("Domain & TLS", "ACME", "Timezone", "NFS Server", "Share roles", "Plex", "Emby"):
        assert title in FRONTEND
    # Desktop side-nav layout (sticky left column).
    assert "position:sticky" in FRONTEND or "settings-side" in FRONTEND
    css = FRONTEND.split("</style>", 1)[0]
    assert ".settings-layout" in css
    assert "grid-template-columns" in css[css.find(".settings-layout"):css.find(".settings-layout") + 400]


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


def test_settings_prowlarr_newznab_indexers():
    assert "Indexers" in FRONTEND
    assert "save-indexers" in FRONTEND
    assert "prowlarr_newznab_indexers" in FRONTEND
    assert "emptyNewznabIndexer" in FRONTEND
    assert "PROWLARR_NEWZNAB" in BACKEND
    assert "prowlarr_newznab_apply" in BACKEND
    text = (ROOT / ".env.example").read_text()
    assert "PROWLARR_NEWZNAB_INDEXERS=" in text
    provision = (ROOT / "compose" / "core.provision.yml").read_text()
    assert "PROWLARR_NEWZNAB_INDEXERS" in provision
    assert "prowlarr_newznab" in (ROOT / "provision" / "recipes" / "prowlarr.py").read_text()


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
    assert "section('Core containers'" not in FRONTEND
    assert "catalogue_apps" in (ROOT / "helm" / "backend" / "app" / "updates_info.py").read_text()
    assert "is a core container" in BACKEND

    assert "stats:'Stats'" in FRONTEND.replace(" ", "")
    assert "render.stats" in FRONTEND


def test_updates_page_splits_acquisition_live_tv_and_other():
    assert "section('Acquisition'" in FRONTEND
    assert "section('Live TV'" in FRONTEND
    assert "section('Other'" in FRONTEND
    assert "section('Apps', apps, 'apps')" not in FRONTEND
    assert "b.dataset.upAll" in FRONTEND
    assert "const group = apps;" not in FRONTEND


def test_updates_shows_check_and_per_row_progress_bars():
    assert "/updates/progress" in FRONTEND
    assert '@app.get("/api/updates/progress")' in BACKEND
    assert 'id="updates-check-meter"' in FRONTEND
    assert "data-row-meter" in FRONTEND
    assert ".up-meter" in FRONTEND
    assert "$('#refresh').onclick = () => render.updates(true);" not in FRONTEND


def test_updates_table_fits_without_horizontal_scroll():
    wrap = FRONTEND.split(".updates-wrap", 1)[1].split("}", 1)[0]
    assert "overflow:auto" not in wrap
    assert "table-layout:fixed" in FRONTEND
    assert "Snapshotting…" in FRONTEND


def test_updates_all_runs_sections_in_parallel():
    assert "data-up-section" in FRONTEND
    assert "document.querySelectorAll('[data-up], [data-up-all], #refresh')" not in FRONTEND
    assert "updatesBusy" in FRONTEND
    assert "backup.sh \"$svc\"" in (ROOT / "scripts" / "updates.sh").read_text()


def test_stats_embeds_solo_panels_from_the_overview_dashboard():
    assert "/d-solo/kine-overview" in FRONTEND
    assert "kiosk" in FRONTEND
    assert "/stats/overview" in FRONTEND
    assert "stats-hero" in FRONTEND
    assert "stats-glass" in FRONTEND


def test_metrics_tier_is_ordered_with_the_others():
    assert "'metrics'" in FRONTEND


def test_stats_endpoints_exist_in_the_backend():
    assert '"/api/stats/cards"' in BACKEND
    assert '"/api/stats/overview"' in BACKEND
    assert '"/api/metrics"' in BACKEND
