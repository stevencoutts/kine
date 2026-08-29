"""Compare a local image to the registry index digest.

The Updates page used to sha256 the pretty-printed `docker manifest inspect`
JSON for "latest" and sha256 the RepoDigest *string* for "current". Those
two hashes are never equal, so every Check Now looked like an update even
when the running image already was that tag's index (Lidarr/Beets after apply).
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_SHA = re.compile(r"sha256:([0-9a-f]{12,})", re.I)
_HEX = re.compile(r"^[0-9a-f]{12,}$", re.I)
_ACCEPT = (
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)


def short_digest(raw: str | None) -> str:
    """First 12 hex chars of a Docker sha256 digest, not a hash of `raw`."""
    if raw is None:
        return "none"
    text = str(raw).strip()
    if not text:
        return "none"
    if text in ("?", "none"):
        return text
    match = _SHA.search(text)
    if match:
        return match.group(1)[:12].lower()
    if _HEX.match(text):
        return text[:12].lower()
    return "none"


def update_available(local_raw: str | None, remote_raw: str | None) -> bool:
    local_d = short_digest(local_raw)
    remote_d = short_digest(remote_raw)
    if remote_d in ("?", "none") or local_d == "none":
        return False
    return local_d != remote_d


def parse_image_ref(image: str) -> tuple[str, str, str]:
    """Split `registry/repo:tag` into (registry, repository, tag)."""
    ref = (image or "").strip()
    if "@" in ref:
        ref = ref.split("@", 1)[0]
    if not ref:
        raise ValueError("empty image")
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
    else:
        name, tag = ref, "latest"
    parts = name.split("/")
    first = parts[0]
    dockerhub = (
        len(parts) == 1
        or (
            len(parts) == 2
            and "." not in first
            and ":" not in first
            and first != "localhost"
        )
    )
    if dockerhub:
        repo = name if "/" in name else f"library/{name}"
        return "registry-1.docker.io", repo, tag
    return first, "/".join(parts[1:]), tag


def _bearer_token(www_auth: str, repo: str, opener=urlopen) -> str:
    if not (www_auth or "").lower().startswith("bearer "):
        return ""
    fields: dict[str, str] = {}
    for part in www_auth[7:].split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip().strip('"')
    realm = fields.get("realm")
    if not realm:
        return ""
    params = {
        "scope": fields.get("scope") or f"repository:{repo}:pull",
    }
    if fields.get("service"):
        params["service"] = fields["service"]
    sep = "&" if "?" in realm else "?"
    req = Request(realm + sep + urlencode(params), headers={"Accept": "application/json"})
    with opener(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return data.get("token") or data.get("access_token") or ""


def remote_index_digest(image: str, opener=urlopen) -> str:
    """Registry content digest of this tag (the value RepoDigests stores)."""
    registry, repo, tag = parse_image_ref(image)
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    headers = {"Accept": _ACCEPT}

    def _get(extra: dict | None = None):
        req = Request(url, headers={**headers, **(extra or {})})
        return opener(req, timeout=30)

    try:
        resp = _get()
    except HTTPError as err:
        if err.code != 401:
            raise
        token = _bearer_token(err.headers.get("WWW-Authenticate") or "", repo, opener=opener)
        if not token:
            raise
        resp = _get({"Authorization": f"Bearer {token}"})
    with resp:
        digest = resp.headers.get("Docker-Content-Digest") or ""
        body = resp.read()
    if digest:
        return digest
    return "sha256:" + hashlib.sha256(body).hexdigest()
