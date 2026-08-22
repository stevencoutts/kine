"""Small presentation invariants for the single-file Helm UI."""
import pathlib


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


def test_app_sections_start_collapsed():
    assert "collapsed:{media:true,acquisition:true,live:true,platform:true}" in FRONTEND


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


def test_open_button_uses_safe_new_window_and_inline_error():
    assert "window.open(app.url, '_blank', 'noopener')" in FRONTEND
    assert "opened.opener = null" in FRONTEND
    assert "Could not open" in FRONTEND
