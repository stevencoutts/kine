"""FlareSolverr: a headless, tunnelled solver that provision wires into Prowlarr."""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import vpn_profiles, vpn_routing  # noqa: E402
from recipes import flaresolverr  # noqa: E402

SCHEMA = {
    "implementation": "FlareSolverr",
    "configContract": "FlareSolverrSettings",
    "fields": [
        {"name": "host", "value": "http://localhost:8191/"},
        {"name": "requestTimeout", "value": 60},
    ],
}


class FakeProwlarr:
    """In-memory Prowlarr: tags, indexer proxies and indexers."""

    def __init__(self, indexers=None, proxies=None, tags=None):
        self.tags = list(tags or [])
        self.proxies = list(proxies or [])
        self.indexers = list(indexers or [])
        self.writes = []

    def get(self, path):
        if path == "tag":
            return self.tags
        if path == "indexerproxy":
            return self.proxies
        if path == "indexerproxy/schema":
            return [{"implementation": "HttpProxy", "configContract": "x", "fields": []}, SCHEMA]
        if path == "indexer":
            return self.indexers
        if path.startswith("indexer/"):
            wanted = int(path.split("/")[1])
            return dict(next(i for i in self.indexers if i["id"] == wanted))
        raise AssertionError(path)

    def post(self, path, payload):
        self.writes.append(("POST", path, payload))
        if path == "tag":
            row = {"id": len(self.tags) + 1, **payload}
            self.tags.append(row)
            return row
        if path.startswith("indexerproxy"):
            self.proxies.append({"id": len(self.proxies) + 1, **payload})
            return {}
        raise AssertionError(path)

    def put(self, path, payload):
        self.writes.append(("PUT", path, payload))
        base, ident = path.split("?")[0].split("/")
        rows = self.proxies if base == "indexerproxy" else self.indexers
        for i, row in enumerate(rows):
            if row["id"] == int(ident):
                rows[i] = {**row, **payload}
        return {}


def seeded_indexers():
    return [
        {"id": 1, "definitionName": "thepiratebay", "name": "The Pirate Bay", "tags": []},
        {"id": 2, "definitionName": "1337x", "name": "1337x", "tags": []},
        {"id": 3, "definitionName": "kickasstorrents-ws", "name": "kickasstorrents.ws", "tags": []},
    ]


def test_configure_creates_tag_proxy_and_tags_cloudflare_indexers():
    client = FakeProwlarr(indexers=seeded_indexers())
    logs = []
    flaresolverr.configure(client, logs.append)

    tag_id = client.tags[0]["id"]
    assert client.tags[0]["label"] == "flaresolverr"
    proxy = client.proxies[0]
    fields = {f["name"]: f["value"] for f in proxy["fields"]}
    # Loopback: Prowlarr and FlareSolverr share gluetun's namespace.
    assert fields["host"] == "http://localhost:8191/"
    assert fields["requestTimeout"] == 110
    assert proxy["tags"] == [tag_id]
    tagged = {i["definitionName"]: i["tags"] for i in client.indexers}
    assert tagged == {"thepiratebay": [], "1337x": [tag_id], "kickasstorrents-ws": [tag_id]}
    # Prowlarr tests on save, which is what Cloudflare fails: always forceSave.
    assert all("forceSave=true" in path for verb, path, _ in client.writes if path != "tag")
    assert "prowlarr: added FlareSolverr proxy" in logs


def test_configure_is_idempotent():
    client = FakeProwlarr(indexers=seeded_indexers())
    flaresolverr.configure(client, lambda _m: None)
    client.writes.clear()
    flaresolverr.configure(client, lambda _m: None)
    assert client.writes == []


def test_existing_proxy_with_short_timeout_is_updated_and_keeps_its_tags():
    client = FakeProwlarr(
        indexers=seeded_indexers(),
        tags=[{"id": 4, "label": "flaresolverr"}, {"id": 9, "label": "other"}],
        proxies=[{
            "id": 1, "name": "FlareSolverr", "implementation": "FlareSolverr",
            "configContract": "FlareSolverrSettings", "tags": [9],
            "fields": [{"name": "host", "value": "http://localhost:8191/"},
                       {"name": "requestTimeout", "value": 60}],
        }],
    )
    flaresolverr.configure(client, lambda _m: None)
    proxy = client.proxies[0]
    assert {f["name"]: f["value"] for f in proxy["fields"]}["requestTimeout"] == 110
    assert proxy["tags"] == [4, 9]


def test_user_tags_on_indexers_are_kept():
    rows = seeded_indexers()
    rows[1]["tags"] = [7]
    client = FakeProwlarr(indexers=rows)
    flaresolverr.configure(client, lambda _m: None)
    tag_id = client.tags[0]["id"]
    assert sorted(client.indexers[1]["tags"]) == sorted({7, tag_id})


def test_reseeding_an_indexer_keeps_its_tags():
    """prowlarr_indexers re-saves a seeded indexer whose settings drifted.
    That PUT must not clear the FlareSolverr tag."""
    from prowlarr_indexers import ensure_indexers

    puts = []

    class Client:
        def get(self, path):
            if path == "indexer/schema":
                return [{"definitionName": "1337x", "name": "1337x", "implementation": "Cardigann",
                         "configContract": "CardigannSettings",
                         "fields": [{"name": "definitionFile", "value": "1337x", "type": "textbox"},
                                    {"name": "sort", "value": 0, "type": "select"},
                                    {"name": "type", "value": 0, "type": "select"}]}]
            if path == "indexer":
                return [{"id": 2, "definitionName": "1337x", "enable": True, "tags": [1],
                         "fields": [{"name": "sort", "value": 0}, {"name": "type", "value": 0}]}]
            return []

        def put(self, path, payload):
            puts.append(payload)

        def post(self, path, payload):
            raise AssertionError("should update, not create")

    ensure_indexers(Client(), lambda _m: None)
    assert puts and puts[0]["tags"] == [1]


def test_prowlarr_recipe_wires_flaresolverr_only_when_enabled(monkeypatch):
    import recipes.prowlarr as prowlarr

    calls = []

    class Client:
        def __init__(self, *a, **k):
            pass

        def wait(self):
            return True

        def ensure(self, path, payload, match_on="name"):
            return False

        def get(self, path):
            return []

    monkeypatch.setattr(prowlarr, "ArrClient", Client)
    monkeypatch.setattr(prowlarr, "resolve_key", lambda app: "k")
    monkeypatch.setattr(prowlarr.flaresolverr, "configure", lambda client, log: calls.append(1))
    prowlarr.configure({"sonarr"}, lambda _m: None)
    assert calls == []
    prowlarr.configure({"sonarr", "flaresolverr"}, lambda _m: None)
    assert calls == [1]


def test_catalogue_entry_is_a_headless_tunnelled_acquisition_app():
    app = yaml.safe_load((ROOT / "catalogue.yml").read_text())["apps"]["flaresolverr"]
    assert app["tier"] == "acquisition"
    assert app["tunnelled"] == "forced"
    assert set(app["requires"]) == {"gluetun", "prowlarr"}
    assert "subdomain" not in app  # no UI, so no Traefik router
    assert app["default"] is False


def test_compose_fragment_is_capped_and_health_checked():
    svc = yaml.safe_load((ROOT / "compose" / "acq.flaresolverr.yml").read_text())["services"]["flaresolverr"]
    assert svc["image"] == "ghcr.io/flaresolverr/flaresolverr:${FLARESOLVERR_TAG}"
    assert svc["network_mode"] == "service:gluetun"
    assert svc["mem_limit"] and svc["cpus"]
    assert "8191" in " ".join(svc["healthcheck"]["test"])


def test_flaresolverr_stays_on_prowlarrs_tunnel():
    """Prowlarr reaches FlareSolverr on loopback, so they must share a namespace."""
    assert vpn_routing.APP_PORTS["flaresolverr"] == 8191
    assert "flaresolverr" in vpn_profiles.ACQUISITION_AFFINITY
    assert "prowlarr" in vpn_profiles.ACQUISITION_AFFINITY
    html = (ROOT / "helm" / "frontend" / "index.html").read_text()
    block = html.split("const ACQUISITION_AFFINITY = [", 1)[1].split("];", 1)[0]
    assert "'flaresolverr'" in block and "'prowlarr'" in block
