"""Same-origin reverse proxy so Helm can embed apps without cert prompts.

Apps keep serving at `/` on their own hostnames (and for internal clients).
Helm exposes them under `/view/{app}/` on `admin.*`, rewrites root-absolute
URLs in HTML/CSS, and patches fetch/XHR/WebSocket in a bootstrap script so
SPAs keep talking through the prefix without changing UrlBase.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Iterable
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response

from . import catalogue, config

if TYPE_CHECKING:
    from fastapi import FastAPI

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
        # Never forward Accept-Encoding: httpx may not decode brotli/zstd, and we
        # strip Content-Encoding below — that leaves compressed bytes labeled as
        # text/html (Safari then shows mojibake in the embed iframe).
        if key.lower() == "accept-encoding":
            continue
        out[key] = value
    # Ask for encodings httpx always decompresses.
    out["accept-encoding"] = "identity"
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
    # Injected into every HTML response. Goals:
    # 1) Lock window.*.urlBase/baseUrl to the embed prefix (*arr/Bazarr may clear it).
    # 2) Keep History API + script/img tags under /view/{app}/.
    # 3) Prefix fetch/XHR/WebSocket absolute paths (incl. same-origin full URLs).
    return (
        "<script>(function(){"
        f"var P={prefix!r};"
        # Prefix root-absolute paths. Also rewrite same-origin absolute URLs
        # (Socket.IO builds https://host/api/socket.io — those must get P too).
        "function abs(u){if(typeof u!=='string'||!u)return u;"
        "if(u.indexOf('://')!==-1||u.indexOf('//')===0){try{"
        "var x=new URL(u,location.href);if(x.origin!==location.origin)return u;"
        "if(x.pathname.indexOf(P)!==0&&x.pathname.charAt(0)==='/')x.pathname=P+x.pathname;"
        "return x.toString();}catch(e){return u;}}"
        "if(u.charAt(0)!=='/'||u.indexOf(P)===0)return u;return P+u;}"
        "function fixNav(u){if(typeof u!=='string'||u==='')return u;"
        "if(u.charAt(0)==='?'||u.charAt(0)==='#')return u;"
        "if(u.indexOf('://')!==-1){try{var x=new URL(u);"
        "if(x.origin===location.origin){x.pathname=abs(x.pathname);"
        "return x.pathname+x.search+x.hash;}"
        "return u;}catch(e){return u;}}"
        "if(u.charAt(0)!=='/')return u;return abs(u);}"
        # Lock urlBase (Sonarr/Radarr) and baseUrl (Bazarr) so empty values cannot win.
        "function lockProp(obj,key){try{Object.defineProperty(obj,key,{configurable:true,"
        "enumerable:true,get:function(){return P;},set:function(){}});}catch(e){obj[key]=P;}}"
        "function lockBase(obj){if(!obj||typeof obj!=='object')return obj;"
        "lockProp(obj,'urlBase');lockProp(obj,'baseUrl');return obj;}"
        "function forceBase(name){try{var cur=window[name];"
        "if(cur&&typeof cur==='object')cur=lockBase(cur);"
        "Object.defineProperty(window,name,{configurable:true,enumerable:true,"
        "get:function(){return cur;},"
        "set:function(v){cur=lockBase(Object.assign({},v||{}));}});}catch(e){}}"
        "forceBase('Sonarr');forceBase('Radarr');forceBase('Prowlarr');forceBase('Bazarr');"
        "var f=window.fetch;window.fetch=function(i,n){"
        "var u=typeof i==='string'?i:(i&&i.url);"
        "if(typeof i==='string')i=abs(i);"
        "else if(i&&typeof i.url==='string')i=new Request(abs(i.url),i);"
        "var p=f.call(this,i,n);"
        # Client-side belt-and-suspenders: force urlBase in initialize.json.
        "if(typeof u==='string'&&u.indexOf('initialize.json')!==-1)"
        "{return p.then(function(res){return res.clone().json().then(function(d){"
        "if(d&&typeof d==='object')d.urlBase=P;"
        "return new Response(JSON.stringify(d),{status:res.status,statusText:res.statusText,"
        "headers:{'content-type':'application/json'}});},function(){return res;});});}"
        "return p;};"
        "var o=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u){"
        "var a=Array.prototype.slice.call(arguments,2);"
        "return o.apply(this,[m,abs(u)].concat(a));};"
        "var W=window.WebSocket;window.WebSocket=function(u,p){"
        "if(typeof u==='string'){if(u.charAt(0)==='/')u=abs(u);"
        "else if(u.indexOf('ws')===0){try{var x=new URL(u);"
        "if(x.pathname.indexOf(P)!==0)x.pathname=P+x.pathname;u=x.toString();}"
        "catch(e){}}}"
        "return p===undefined?new W(u):new W(u,p);};"
        "window.WebSocket.prototype=W.prototype;"
        "window.WebSocket.CONNECTING=W.CONNECTING;window.WebSocket.OPEN=W.OPEN;"
        "window.WebSocket.CLOSING=W.CLOSING;window.WebSocket.CLOSED=W.CLOSED;"
        "var ps=history.pushState.bind(history);"
        "history.pushState=function(s,t,u){return ps(s,t,u===undefined?u:fixNav(u));};"
        "var rs=history.replaceState.bind(history);"
        "history.replaceState=function(s,t,u){return rs(s,t,u===undefined?u:fixNav(u));};"
        "function trapAttr(el,attr){var d=Object.getOwnPropertyDescriptor(el.__proto__,attr);"
        "if(!(d&&d.set))return;"
        "Object.defineProperty(el,attr,{configurable:true,enumerable:true,"
        "get:function(){return d.get.call(this);},"
        "set:function(v){d.set.call(this,abs(v));}});}"
        "var ce=Document.prototype.createElement;"
        "Document.prototype.createElement=function(t,opt){"
        "var el=ce.call(this,t,opt);var n=String(t).toLowerCase();"
        # Trap script/img src. Do NOT redefine link.href — Safari then fails to
        # apply webpack CSS chunks, so FA icons render at full SVG size.
        "if(n==='script'||n==='img'||n==='image'||n==='source'||n==='video'||n==='audio')"
        "{trapAttr(el,'src');}"
        "return el;};"
        # Catch React setAttribute for src/href (including <link href> CSS chunks).
        # Keep original attribute name casing — lowercasing breaks SVG viewBox
        # (becomes "viewbox"), so FA paths paint at 512px with overflow:visible.
        "var sa=Element.prototype.setAttribute;"
        "Element.prototype.setAttribute=function(n,v){"
        "var ln=String(n).toLowerCase();"
        "if((ln==='src'||ln==='href')&&typeof v==='string')v=abs(v);"
        "return sa.call(this,n,v);};"
        # Fix stylesheet URLs when webpack publicPath was still '/' (append time).
        "function fixSheet(n){if(!n||!n.tagName||n.tagName.toLowerCase()!=='link')return;"
        "var h=n.getAttribute('href');if(h)n.setAttribute('href',abs(h));}"
        "var ap=Node.prototype.appendChild;"
        "Node.prototype.appendChild=function(c){fixSheet(c);return ap.call(this,c);};"
        "var ib=Node.prototype.insertBefore;"
        "Node.prototype.insertBefore=function(c,r){fixSheet(c);return ib.call(this,c,r);};"
        # Safety net if FA CSS is late: keep icon SVGs at 1em.
        "var st=document.createElement('style');st.textContent="
        "'.svg-inline--fa{display:inline-block;height:1em;width:1em;overflow:visible;"
        "vertical-align:-.125em}'"
        ";document.documentElement.appendChild(st);"
        # Keep the iframe on the embed prefix if something navigates to /.
        "if(location.pathname!==P&&location.pathname.indexOf(P+'/')!==0)"
        "{location.replace(P+'/'+location.pathname.replace(/^\\//,'')+location.search+location.hash);}"
        "else if(location.pathname===P){history.replaceState(null,'',P+'/'+location.search+location.hash);}"
        "})();</script>"
    )


def _rewrite_url_base(text: str, prefix: str) -> str:
    """Fill empty urlBase/baseUrl so assets and Socket.IO stay under /view/{app}/.

    Sonarr/Radarr/Prowlarr use urlBase; Bazarr uses baseUrl (JSON in a script tag).
    """
    text = re.sub(
        r"(urlBase\s*:\s*)(['\"])\2",
        rf"\1\2{prefix}\2",
        text,
    )
    text = re.sub(
        r'(["\']baseUrl["\']\s*:\s*)(["\'])\2',
        rf"\1\2{prefix}\2",
        text,
    )
    return text


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
    text = _rewrite_url_base(text, prefix)
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


def _rewrite_initialize_json(raw: bytes, prefix: str) -> bytes:
    """*arr initialize.json has urlBase:'' — webpack then loads CSS from /Content/… on admin."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        return raw
    if not isinstance(data, dict):
        return raw
    data["urlBase"] = prefix
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


async def proxy_http(app_id: str, path: str, request: Request) -> Response:
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
    elif media == "application/json" and rel.split("?", 1)[0].endswith("initialize.json"):
        raw = _rewrite_initialize_json(raw, prefix)

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


def mount(app: FastAPI, require_user) -> None:
    """Register authenticated embed routes on the FastAPI app.

    Request must live in this module's globals so FastAPI can resolve the
    annotation under ``from __future__ import annotations``. A local import
    inside mount() made ``request`` look like a required query field.
    """

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
