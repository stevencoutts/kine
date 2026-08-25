"""Same-origin embed proxy helpers."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import embed_proxy  # noqa: E402


def test_embed_prefix():
    assert embed_proxy.embed_prefix("sonarr") == "/view/sonarr"


def test_rewrite_html_injects_bootstrap_and_prefixes_assets():
    html = (
        "<html><head><title>x</title>"
        "<script>window.Sonarr={urlBase:''};</script>"
        "</head>"
        '<body><a href="/wanted">Wanted</a>'
        '<script src="/Content/app.js"></script></body></html>'
    )
    out = embed_proxy._rewrite_html(html, "/view/sonarr")
    assert "var P='/view/sonarr'" in out or 'var P="/view/sonarr"' in out
    assert 'href="/view/sonarr/wanted"' in out
    assert 'src="/view/sonarr/Content/app.js"' in out
    assert "urlBase:'/view/sonarr'" in out or 'urlBase:"/view/sonarr"' in out or "urlBase: '/view/sonarr'" in out
    assert "createElement" in out
    assert "forceBase" in out or "urlBase=P" in out
    assert "lockBase" in out
    assert "lockProp" in out
    assert "baseUrl" in out
    assert "pushState" in out
    assert "trapAttr" in out
    assert "setAttribute" in out
    assert "svg-inline--fa" in out
    assert "initialize.json" in out
    assert "fixSheet" in out
    # Must not redefine link.href (Safari drops webpack CSS chunks).
    assert "n==='link'" not in out and "n===\"link\"" not in out
    # Must preserve setAttribute name casing (SVG viewBox ≠ viewbox).
    assert "var ln=String(n).toLowerCase()" in out
    assert "sa.call(this,n,v)" in out
    assert "sa.call(this,ln,v)" not in out
    # Same-origin absolute URLs (Socket.IO) must be rewritten.
    assert "Location.prototype.assign" in out
    assert "Location.prototype.replace" in out
    # Same-origin absolute URLs (Socket.IO) must be rewritten.
    assert "x.origin!==location.origin" in out or "x.origin===location.origin" in out


def test_rewrite_url_base_empty_string():
    raw = "window.Radarr = {\n        urlBase: ''\n      };"
    out = embed_proxy._rewrite_url_base(raw, "/view/radarr")
    assert "urlBase: '/view/radarr'" in out or 'urlBase: "/view/radarr"' in out


def test_rewrite_bazarr_base_url_json():
    raw = (
        'window.Bazarr = JSON.parse(`{"apiKey": "abc", "baseUrl": "", '
        '"canUpdate": false}`);'
    )
    out = embed_proxy._rewrite_url_base(raw, "/view/bazarr")
    assert '"baseUrl": "/view/bazarr"' in out or "\"baseUrl\": \"/view/bazarr\"" in out
    assert '"baseUrl": ""' not in out


def test_rewrite_html_skips_protocol_relative():
    html = '<html><head></head><body><script src="//cdn.example/x.js"></script></body></html>'
    out = embed_proxy._rewrite_html(html, "/view/sonarr")
    assert 'src="//cdn.example/x.js"' in out


def test_rewrite_location_prefixes_absolute_paths():
    assert (
        embed_proxy._rewrite_location("/login", "/view/radarr", "http://gluetun:7878")
        == "/view/radarr/login"
    )
    assert (
        embed_proxy._rewrite_location(
            "http://gluetun:7878/login", "/view/radarr", "http://gluetun:7878"
        )
        == "/view/radarr/login"
    )


def test_rewrite_set_cookie_forces_embed_path():
    assert "Path=/view/sonarr/" in embed_proxy._rewrite_set_cookie(
        "sid=abc; Path=/", "/view/sonarr"
    )


def test_embeddable_requires_flag_and_internal(monkeypatch):
    assert embed_proxy.embeddable({
        "embed": True, "internal": "http://gluetun:8989", "subdomain": "sonarr",
    })
    assert not embed_proxy.embeddable({
        "embed": False, "internal": "http://gluetun:8989", "subdomain": "sonarr",
    })
    assert not embed_proxy.embeddable({"embed": True, "subdomain": "sonarr"})


def test_filter_request_headers_forces_identity_encoding():
    out = embed_proxy._filter_request_headers([
        ("Host", "admin.example"),
        ("Accept-Encoding", "gzip, deflate, br"),
        ("Cookie", "kine_session=abc"),
        ("Connection", "keep-alive"),
    ])
    assert out["accept-encoding"] == "identity"
    assert out["Cookie"] == "kine_session=abc"
    assert "Connection" not in out and "connection" not in out


def test_rewrite_initialize_json_sets_url_base():
    raw = b'{"apiRoot":"/api/v3","urlBase":"","theme":"dark"}'
    out = embed_proxy._rewrite_initialize_json(raw, "/view/radarr")
    data = __import__("json").loads(out)
    assert data["urlBase"] == "/view/radarr"
    assert data["apiRoot"] == "/api/v3"


def test_rewrite_js_injects_react_router_basename():
    js = (
        "function W6({basename:n,children:e,useTransitions:t,window:s}){}"
        "function App(){return jsxs(W6,{children:[jsx(Routes,{})]})}"
    )
    out = embed_proxy._rewrite_js(js, "/view/dispatcharr")
    assert "jsxs(W6,{basename:'/view/dispatcharr',children:" in out


def test_rewrite_js_detects_renamed_browser_router_symbol():
    """Dispatcharr 0.29 minifies BrowserRouter as J6 instead of W6."""
    js = (
        "function J6({basename:n,children:e,useTransitions:t,window:s}){}"
        "function App(){return jsxs(J6,{children:[jsx(fi,{path:'/sources'})]})}"
    )
    out = embed_proxy._rewrite_js(js, "/view/dispatcharr")
    assert "jsxs(J6,{basename:'/view/dispatcharr',children:" in out
    assert "jsxs(W6," not in out


def test_rewrite_js_leaves_unrelated_bundles_unchanged():
    js = "console.log('jsxs(W6,{children:');"
    assert embed_proxy._rewrite_js(js, "/view/dispatcharr") == js


def test_rewrite_js_skips_low_level_router_with_default_basename():
    """R6 is react-router's <Router>, not BrowserRouter — do not patch it."""
    js = (
        'function R6({basename:n="/",children:e=null,location:t}){}'
        "jsxs(R6,{children:[1]})"
    )
    assert embed_proxy._rewrite_js(js, "/view/dispatcharr") == js


def test_mount_does_not_treat_request_as_query_param():
    """Regression: local Request import + future annotations made Open return
    {"detail":[{"loc":["query","request"],"msg":"field required"...}]}."""
    from fastapi import FastAPI

    app = FastAPI()
    embed_proxy.mount(app, require_user=lambda: "u")
    route = next(r for r in app.routes if getattr(r, "path", "") == "/view/{app_id}")
    assert "request" not in {p.name for p in route.dependant.query_params}
    ws_routes = [r for r in app.routes if getattr(r, "path", "") == "/view/{app_id}/{path:path}"]
    assert any(getattr(r, "endpoint", None).__name__ == "embed_ws" for r in ws_routes)


def test_upstream_ws_url_builds_from_internal(monkeypatch):
    monkeypatch.setattr(
        embed_proxy,
        "upstream_base",
        lambda app_id: "http://gluetun:9191",
    )
    url = embed_proxy.upstream_ws_url("dispatcharr", "ws/", "token=abc")
    assert url == "ws://gluetun:9191/ws/?token=abc"
