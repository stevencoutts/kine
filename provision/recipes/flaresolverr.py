"""Wire FlareSolverr into Prowlarr as its Cloudflare solver.

1337x and KickassTorrents sit behind Cloudflare's "Just a moment..."
check, so without a solver Prowlarr gets a challenge page, backs them off
and never searches them. FlareSolverr shares gluetun's namespace with
Prowlarr, so Prowlarr reaches it on loopback.

Prowlarr only sends an indexer through a proxy when they share a tag, so
this creates the tag, the proxy carrying it, and adds the tag to the
Cloudflare-protected indexers that provision seeds. Every step checks
what is already there first, so re-running provision changes nothing.
"""
TAG = "flaresolverr"
PROXY_IMPLEMENTATION = "FlareSolverr"
# Prowlarr and FlareSolverr share gluetun's network namespace.
HOST = "http://localhost:8191/"
# KickassTorrents' challenge takes about 70 s to solve; Prowlarr's 60 s
# default times out before FlareSolverr finishes.
REQUEST_TIMEOUT = 110
# Definitions seeded by prowlarr_indexers.py that are behind Cloudflare.
CLOUDFLARE_INDEXERS = ("1337x", "kickasstorrents-ws")


def ensure_tag(client) -> int:
    for tag in client.get("tag"):
        if tag.get("label") == TAG:
            return tag["id"]
    return client.post("tag", {"label": TAG})["id"]


def _overrides() -> dict:
    return {"host": HOST, "requestTimeout": REQUEST_TIMEOUT}


def proxy_payload(template: dict, tag_id: int) -> dict:
    overrides = _overrides()
    fields = [
        {"name": f["name"], "value": overrides.get(f["name"], f.get("value"))}
        for f in template.get("fields", [])
    ]
    return {
        "name": "FlareSolverr",
        "implementation": template["implementation"],
        "configContract": template["configContract"],
        "fields": fields,
        "tags": [tag_id],
    }


def _proxy_matches(existing: dict, tag_id: int) -> bool:
    values = {f["name"]: f.get("value") for f in existing.get("fields", [])}
    return (
        all(values.get(k) == v for k, v in _overrides().items())
        and tag_id in (existing.get("tags") or [])
    )


def ensure_proxy(client, tag_id: int, log) -> None:
    current = next(
        (p for p in client.get("indexerproxy") if p.get("implementation") == PROXY_IMPLEMENTATION),
        None,
    )
    if current and _proxy_matches(current, tag_id):
        return
    template = next(
        item for item in client.get("indexerproxy/schema")
        if item.get("implementation") == PROXY_IMPLEMENTATION
    )
    payload = proxy_payload(template, tag_id)
    # forceSave: Prowlarr probes the proxy on save, and a slow first
    # Chromium start must not stop the wiring.
    if current:
        payload["id"] = current["id"]
        payload["tags"] = sorted(set(current.get("tags") or []) | {tag_id})
        client.put(f"indexerproxy/{current['id']}?forceSave=true", payload)
        log("prowlarr: updated FlareSolverr proxy")
    else:
        client.post("indexerproxy?forceSave=true", payload)
        log("prowlarr: added FlareSolverr proxy")


def tag_cloudflare_indexers(client, tag_id: int, log) -> None:
    for item in client.get("indexer"):
        if item.get("definitionName") not in CLOUDFLARE_INDEXERS:
            continue
        if tag_id in (item.get("tags") or []):
            continue
        full = client.get(f"indexer/{item['id']}")
        full["tags"] = sorted(set(full.get("tags") or []) | {tag_id})
        # forceSave: Prowlarr tests an indexer on save, and that test is
        # exactly what Cloudflare fails until the tag is in place.
        client.put(f"indexer/{item['id']}?forceSave=true", full)
        log(f"prowlarr: {item['definitionName']} now uses FlareSolverr")


def configure(client, log) -> None:
    tag_id = ensure_tag(client)
    ensure_proxy(client, tag_id, log)
    tag_cloudflare_indexers(client, tag_id, log)
