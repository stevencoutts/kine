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
    assert "pushState" in out


def test_rewrite_url_base_empty_string():
    raw = "window.Radarr = {\n        urlBase: ''\n      };"
    out = embed_proxy._rewrite_url_base(raw, "/view/radarr")
    assert "urlBase: '/view/radarr'" in out or 'urlBase: "/view/radarr"' in out


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


def test_mount_does_not_treat_request_as_query_param():
    """Regression: local Request import + future annotations made Open return
    {"detail":[{"loc":["query","request"],"msg":"field required"...}]}."""
    from fastapi import FastAPI

    app = FastAPI()
    embed_proxy.mount(app, require_user=lambda: "u")
    route = next(r for r in app.routes if getattr(r, "path", "") == "/view/{app_id}")
    assert "request" not in {p.name for p in route.dependant.query_params}
