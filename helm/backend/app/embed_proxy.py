"""Same-origin reverse proxy so Helm can embed apps without cert prompts.

Apps keep serving at `/` on their own hostnames (and for internal clients).
Helm exposes them under `/view/{app}/` on `admin.*`, rewrites root-absolute
URLs in HTML/CSS, and patches fetch/XHR/WebSocket in a bootstrap script so
SPAs keep talking through the prefix without changing UrlBase.
"""
from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlsplit

import httpx

from . import catalogue, config

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}

_ATTR_ABS = re.compile(
    r"""(?P<attr>\b(?:href|src|action)\s*=\s*)(?P<q>['"])/(?!/)(?P<path>[^'"]*)(?P=q)""",
    re.IGNORECASE,
)


def embed_prefix(app_id: str) -> str:
    return f"/view/{app_id}"


def embeddable(meta: dict | None) -> bool:
    return bool(meta and meta.get("embed") and meta.get("internal") and meta.get("subdomain"))


def upstream_base(app_id: str) -> str:
    from fastapi import HTTPException  # noqa: PLC0415 — keep module importable in unit tests

    cat = catalogue.load()
    meta = cat.get(app_id) or {}
    if not embeddable(meta):
        raise HTTPException(404, f"{app_id} cannot be embedded")
    if app_id not in config.profiles():
        raise HTTPException(409, f"{app_id} is disabled")
    return str(meta["internal"]).rstrip("/")


def _filter_request_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers:
        if key.lower() in _HOP_BY_HOP:
            continue
        out[key] = value
    return out


def _rewrite_location(value: str, prefix: str, upstream: str) -> str:
    if not value:
        return value
    if value.startswith(prefix):
        return value
    if value.startswith("/"):
        return prefix + value
    up = upstream.rstrip("/")
    if value.startswith(up):
        rest = value[len(up):] or "/"
        return prefix + rest
    return value


def _rewrite_set_cookie(value: str, prefix: str) -> str:
    parts = [p.strip() for p in value.split(";")]
    if not parts:
        return value
    out = [parts[0]]
    saw_path = False
    for part in parts[1:]:
        if part.lower().startswith("path="):
            out.append(f"Path={prefix}/")
            saw_path = True
        else:
            out.append(part)
    if not saw_path:
        out.append(f"Path={prefix}/")
    return "; ".join(out)


def _bootstrap_script(prefix: str) -> str:
    return (
        "<script>(function(){"
        f"var P={prefix!r};"
        "function abs(u){if(typeof u!=='string')return u;"
        "if(u.charAt(0)==='/'&&u.indexOf(P)!==0)return P+u;return u;}"
        "var f=window.fetch;window.fetch=function(i,n){"
        "if(typeof i==='string')i=abs(i);"
        "else if(i&&typeof i.url==='string')i=new Request(abs(i.url),i);"
        "return f.call(this,i,n);};"
        "var o=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u){"
        "var a=Array.prototype.slice.call(arguments,2);"
        "return o.apply(this,[m,abs(u)].concat(a));};"
        "var W=window.WebSocket;window.WebSocket=function(u,p){"
        "if(typeof u==='string'){if(u.charAt(0)==='/')u=P+u;"
        "else if(u.indexOf('ws')===0){try{var x=new URL(u);"
        "if(x.pathname.indexOf(P)!==0)x.pathname=P+x.pathname;u=x.toString();}"
        "catch(e){}}}"
        "return p===undefined?new W(u):new W(u,p);};"
        "window.WebSocket.prototype=W.prototype;"
        "window.WebSocket.CONNECTING=W.CONNECTING;window.WebSocket.OPEN=W.OPEN;"
        "window.WebSocket.CLOSING=W.CLOSING;window.WebSocket.CLOSED=W.CLOSED;"
        "})();</script>"
    )


def _rewrite_html(text: str, prefix: str) -> str:
    def repl(match: re.Match[str]) -> str:
        path = match.group("path")
        pref = prefix.lstrip("/") + "/"
        if path.startswith(pref):
            return match.group(0)
        return (
            f'{match.group("attr")}{match.group("q")}{prefix}/{path}{match.group("q")}'
        )

    text = _ATTR_ABS.sub(repl, text)
    boot = _bootstrap_script(prefix)
    lower = text.lower()
    idx = lower.find("<head>")
    if idx >= 0:
        at = idx + len("<head>")
        return text[:at] + boot + text[at:]
    idx = lower.find("<head ")
    if idx >= 0:
        end = lower.find(">", idx)
        if end >= 0:
            return text[: end + 1] + boot + text[end + 1 :]
    return boot + text


def _rewrite_css(text: str, prefix: str) -> str:
    return re.sub(
        r"""url\(\s*(['"]?)/(?!/)""",
        lambda m: f"url({m.group(1)}{prefix}/",
        text,
    )


async def proxy_http(app_id: str, path: str, request) -> object:
    from fastapi import HTTPException  # noqa: PLC0415
    from fastapi.responses import Response  # noqa: PLC0415

    prefix = embed_prefix(app_id)
    upstream = upstream_base(app_id)
    rel = (path or "").lstrip("/")
    target = f"{upstream}/{rel}" if rel else f"{upstream}/"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    headers = _filter_request_headers(request.headers.items())
    headers["host"] = urlsplit(upstream).netloc
    body = await request.body()

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=False,
        ) as client:
            upstream_resp = await client.request(
                request.method,
                target,
                headers=headers,
                content=body or None,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach {app_id}: {exc}") from exc

    content_type = upstream_resp.headers.get("content-type", "")
    media = content_type.split(";")[0].strip().lower()
    raw = upstream_resp.content

    if media.startswith("text/html"):
        try:
            text = raw.decode(upstream_resp.charset_encoding or "utf-8")
        except (LookupError, UnicodeDecodeError):
            text = raw.decode("utf-8", errors="replace")
        raw = _rewrite_html(text, prefix).encode("utf-8")
    elif media == "text/css":
        try:
            text = raw.decode(upstream_resp.charset_encoding or "utf-8")
        except (LookupError, UnicodeDecodeError):
            text = raw.decode("utf-8", errors="replace")
        raw = _rewrite_css(text, prefix).encode("utf-8")

    out_headers: dict[str, str] = {}
    set_cookies: list[str] = []
    for key, value in upstream_resp.headers.multi_items():
        lk = key.lower()
        if lk in _HOP_BY_HOP:
            continue
        if lk == "location":
            out_headers[key] = _rewrite_location(value, prefix, upstream)
        elif lk == "set-cookie":
            set_cookies.append(_rewrite_set_cookie(value, prefix))
        elif lk in {"content-security-policy", "x-frame-options"}:
            continue
        else:
            out_headers[key] = value

    response = Response(
        content=raw,
        status_code=upstream_resp.status_code,
        headers=out_headers,
        media_type=content_type or None,
    )
    for cookie in set_cookies:
        response.headers.append("set-cookie", cookie)
    return response


def mount(app, require_user) -> None:
    """Register authenticated embed routes on the FastAPI app."""
    from fastapi import Depends, Request  # noqa: PLC0415
    from fastapi.responses import Response  # noqa: PLC0415

    @app.api_route(
        "/view/{app_id}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    @app.api_route(
        "/view/{app_id}/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def embed_http(
        app_id: str,
        request: Request,
        path: str = "",
        _user: str = Depends(require_user),
    ) -> Response:
        if path == "" and not str(request.url.path).endswith("/"):
            return Response(
                status_code=307,
                headers={"Location": embed_prefix(app_id) + "/"},
            )
        return await proxy_http(app_id, path, request)
