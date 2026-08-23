# Grafana Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Metrics tier — cAdvisor, node-exporter, Prometheus and Grafana — with app-level stats exported by Helm, dashboards provisioned from the repo, and the best graphs embedded in the Helm GUI.

**Architecture:** cAdvisor and node-exporter cover machine resources. Helm grows a `/api/metrics` endpoint that renders a cache filled by a 60s background collector querying apps it already holds keys for. Prometheus scrapes all three; Grafana renders four provisioned dashboards; Helm's new Stats tab embeds panels from them and draws per-card sparklines from Prometheus range queries.

**Tech Stack:** Docker Compose, Prometheus, Grafana, cAdvisor, node-exporter, FastAPI, httpx, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-08-23-grafana-metrics-design.md`

## Global Constraints

- Every compose fragment declares `profiles: ["<service>"]` and `container_name: kine-<service>`, and is listed in `docker-compose.yml` `include:`.
- Every image tag comes from `${VAR}` and every such `VAR` is declared in `.env.example`.
- `COMPOSE_PROFILES` in `.env.example` stays `mdns`.
- Prometheus retention: `--storage.tsdb.retention.time=30d` and `--storage.tsdb.retention.size=2GB`.
- Grafana datasource UID is exactly `kine-prom`. Dashboard UIDs are exactly `kine-overview`, `kine-containers`, `kine-host`, `kine-media`.
- Helm listens on container port 8600. Prometheus scrape target is `http://helm:8600/api/metrics`.
- Metric names and labels are exactly as specced; the dashboard test enforces this.
- Run tests with `python -m pytest tests -q` from the repo root.
- Commit after every task.

---

### Task 1: Transitive dependency resolution

`resolve_deps` walks one level of `requires`, so `grafana -> prometheus -> cadvisor` stops at Prometheus and the exporters never start.

**Files:**
- Modify: `helm/backend/app/catalogue.py:39-43`
- Test: `tests/test_catalogue.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `resolve_deps(app_id: str, cat: dict, wanted: list[str]) -> list[str]` — unchanged signature, now transitive.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_catalogue.py`:

```python
def test_resolve_deps_follows_multi_hop_chains():
    cat = {
        "grafana": {"requires": ["prometheus"]},
        "prometheus": {"requires": ["cadvisor", "node-exporter"]},
        "cadvisor": {},
        "node-exporter": {},
    }
    wanted = catalogue.resolve_deps("grafana", cat, ["grafana"])
    assert set(wanted) == {"grafana", "prometheus", "cadvisor", "node-exporter"}


def test_resolve_deps_survives_a_dependency_cycle():
    cat = {"a": {"requires": ["b"]}, "b": {"requires": ["a"]}}
    assert set(catalogue.resolve_deps("a", cat, ["a"])) == {"a", "b"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_catalogue.py -q`
Expected: FAIL — `cadvisor` and `node-exporter` missing from the resolved set.

- [ ] **Step 3: Write minimal implementation**

Replace `resolve_deps` in `helm/backend/app/catalogue.py`:

```python
def resolve_deps(app_id: str, cat: dict, wanted: list[str]) -> list[str]:
    """Pull in requires, and their requires, until nothing new appears.

    One level is not enough: Grafana needs Prometheus, which needs the
    exporters, and stopping halfway starts a dashboard with no data.
    """
    queue = [app_id]
    seen = set()
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for dep in cat.get(current, {}).get("requires", []):
            if dep not in wanted:
                wanted.append(dep)
            queue.append(dep)
    return wanted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_catalogue.py tests/test_helm.py -q`
Expected: PASS, including the existing `test_resolve_deps_pulls_in_gluetun`.

- [ ] **Step 5: Commit**

```bash
git add helm/backend/app/catalogue.py tests/test_catalogue.py
git commit -m "Resolve requires transitively so dependency chains start whole."
```

---

### Task 2: Extract app key resolution

The exporter needs the *arr key helpers currently buried in a 540-line module.

**Files:**
- Create: `helm/backend/app/appkeys.py`
- Modify: `helm/backend/app/library_rescan.py:41-89`
- Test: `tests/test_appkeys.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `appkeys.arr_key(app: str) -> str | None` — on-disk `config.xml` ApiKey, else key derived from `KINE_SECRET`, else `None`.
  - `appkeys.bazarr_key() -> str | None`
  - `appkeys.key_for(app: str) -> str | None` — dispatches bazarr vs *arr.
  - `appkeys.STACK: pathlib.Path`

- [ ] **Step 1: Write the failing test**

Create `tests/test_appkeys.py`:

```python
"""Key resolution prefers what is on disk over what is derived."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import appkeys  # noqa: E402


def test_arr_key_reads_the_key_on_disk(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "sonarr"
    cfg.mkdir(parents=True)
    (cfg / "config.xml").write_text(
        '<?xml version="1.0"?><Config><ApiKey>ondisk123</ApiKey></Config>'
    )
    monkeypatch.setattr(appkeys, "STACK", tmp_path)
    assert appkeys.arr_key("sonarr") == "ondisk123"


def test_arr_key_derives_from_secret_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(appkeys, "STACK", tmp_path)
    monkeypatch.setenv("KINE_SECRET", "s3cret")
    key = appkeys.arr_key("sonarr")
    assert key and len(key) == 32


def test_bazarr_key_reads_nested_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "bazarr" / "config"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text("auth:\n  apikey: bzr456\n")
    monkeypatch.setattr(appkeys, "STACK", tmp_path)
    assert appkeys.bazarr_key() == "bzr456"


def test_key_for_dispatches_by_app(tmp_path, monkeypatch):
    monkeypatch.setattr(appkeys, "STACK", tmp_path)
    monkeypatch.setenv("KINE_SECRET", "s3cret")
    assert appkeys.key_for("radarr") == appkeys.arr_key("radarr")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_appkeys.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.appkeys'`.

- [ ] **Step 3: Write the module**

Create `helm/backend/app/appkeys.py`:

```python
"""Resolve the API key each app will actually accept.

Keys are derived from KINE_SECRET so they are known before an app has
ever started, but seed adopts any pre-existing key rather than
overwriting it — so on-disk always wins over derived.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import xml.etree.ElementTree as ET

import yaml

from . import config

STACK = pathlib.Path(os.environ.get("KINE_ROOT", "/stack"))


def _secret() -> str:
    secret = os.environ.get("KINE_SECRET") or config.read().get("KINE_SECRET", "")
    if not secret:
        raise RuntimeError("KINE_SECRET is not set")
    return secret


def derived_key(app: str) -> str:
    return hashlib.sha256(f"{_secret()}:{app}".encode()).hexdigest()[:32]


def arr_key(app: str) -> str | None:
    cfg = STACK / "config" / app / "config.xml"
    if cfg.is_file():
        try:
            existing = ET.parse(cfg).getroot().findtext("ApiKey")
            if existing:
                return existing
        except ET.ParseError:
            pass
    try:
        return derived_key(app)
    except RuntimeError:
        return None


def bazarr_key() -> str | None:
    for path in (
        STACK / "config" / "bazarr" / "config" / "config.yaml",
        STACK / "config" / "bazarr" / "config.yaml",
    ):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
            key = (data.get("auth") or {}).get("apikey")
            if key:
                return str(key)
        except (OSError, yaml.YAMLError):
            continue
    return None


def key_for(app: str) -> str | None:
    return bazarr_key() if app == "bazarr" else arr_key(app)
```

- [ ] **Step 4: Point library_rescan at it**

In `helm/backend/app/library_rescan.py`, add `appkeys` to the existing `from . import catalogue, config` line, then delete the local `_secret`, `_derived_key`, `_arr_key` and `_bazarr_key` definitions and replace them with thin aliases so the rest of the 540-line module keeps working unchanged:

```python
_arr_key = appkeys.arr_key
_bazarr_key = appkeys.bazarr_key
```

Leave `STACK` in `library_rescan.py` alone — other code there uses it for media paths, not keys.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests -q`
Expected: PASS, including `tests/test_library_rescan.py` unchanged.

- [ ] **Step 6: Commit**

```bash
git add helm/backend/app/appkeys.py helm/backend/app/library_rescan.py tests/test_appkeys.py
git commit -m "Lift API key resolution out of library_rescan so metrics can share it."
```

---

### Task 3: Prometheus text rendering

**Files:**
- Create: `helm/backend/app/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `metrics.Sample` — `NamedTuple(name: str, labels: dict[str, str], value: float)`
  - `metrics.METRIC_TYPES: dict[str, str]` — metric name to `"gauge"` or `"counter"`; the single source of truth the dashboard test checks panel expressions against.
  - `metrics.render(samples: list[Sample]) -> str` — Prometheus text exposition format.

- [ ] **Step 1: Write the failing test**

Create `tests/test_metrics.py`:

```python
"""Exporter rendering and parsing, with no network anywhere."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import metrics  # noqa: E402


def test_render_emits_help_and_type_once_per_metric():
    out = metrics.render([
        metrics.Sample("kine_app_up", {"app": "sonarr"}, 1),
        metrics.Sample("kine_app_up", {"app": "radarr"}, 0),
    ])
    assert out.count("# TYPE kine_app_up gauge") == 1
    assert out.count("# HELP kine_app_up") == 1
    assert 'kine_app_up{app="sonarr"} 1' in out
    assert 'kine_app_up{app="radarr"} 0' in out
    assert out.endswith("\n")


def test_render_escapes_label_values():
    out = metrics.render([
        metrics.Sample("kine_streams_active", {"user": 'a"b\\c'}, 1),
    ])
    assert 'user="a\\"b\\\\c"' in out


def test_render_writes_bare_name_when_unlabelled():
    out = metrics.render([metrics.Sample("kine_app_up", {}, 1)])
    assert "kine_app_up 1" in out


def test_render_marks_counters_as_counters():
    out = metrics.render([
        metrics.Sample("kine_collect_errors_total", {"app": "sonarr"}, 3),
    ])
    assert "# TYPE kine_collect_errors_total counter" in out


def test_every_rendered_metric_is_declared():
    for sample_name in metrics.METRIC_TYPES:
        assert sample_name.startswith("kine_")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: FAIL — `No module named 'app.metrics'`.

- [ ] **Step 3: Write the renderer**

Create `helm/backend/app/metrics.py`:

```python
"""Kine's own Prometheus exporter.

Collection is deliberately decoupled from scraping: a background task
fills a cache every 60s and /api/metrics renders whatever is in it. A
hung Sonarr must never stall a scrape or punch a hole in the graphs.
"""
from __future__ import annotations

from typing import NamedTuple


class Sample(NamedTuple):
    name: str
    labels: dict[str, str]
    value: float


METRIC_TYPES = {
    "kine_streams_active": "gauge",
    "kine_app_up": "gauge",
    "kine_update_pending": "gauge",
    "kine_library_items": "gauge",
    "kine_library_missing": "gauge",
    "kine_library_bytes": "gauge",
    "kine_queue_items": "gauge",
    "kine_subtitles_wanted": "gauge",
    "kine_indexers_enabled": "gauge",
    "kine_download_rate_bytes": "gauge",
    "kine_torrents": "gauge",
    "kine_collect_duration_seconds": "gauge",
    "kine_indexer_queries_total": "counter",
    "kine_indexer_grabs_total": "counter",
    "kine_collect_errors_total": "counter",
}

METRIC_HELP = {
    "kine_streams_active": "Sessions currently open on a media server",
    "kine_app_up": "1 when the app answered its API, 0 when it did not",
    "kine_update_pending": "1 when a newer image digest is available",
    "kine_library_items": "Items known to the PVR",
    "kine_library_missing": "Monitored items with no file on disk",
    "kine_library_bytes": "Bytes on disk according to the PVR",
    "kine_queue_items": "Items in the PVR download queue",
    "kine_subtitles_wanted": "Episodes or films missing wanted subtitles",
    "kine_indexers_enabled": "Indexers currently enabled",
    "kine_download_rate_bytes": "Current transfer rate in bytes per second",
    "kine_torrents": "Torrents by state",
    "kine_collect_duration_seconds": "Time the last collection of an app took",
    "kine_indexer_queries_total": "Indexer queries since the indexer was added",
    "kine_indexer_grabs_total": "Indexer grabs since the indexer was added",
    "kine_collect_errors_total": "Failed collections since Helm started",
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def render(samples: list[Sample]) -> str:
    lines: list[str] = []
    for name in METRIC_TYPES:
        matching = [s for s in samples if s.name == name]
        if not matching:
            continue
        lines.append(f"# HELP {name} {METRIC_HELP[name]}")
        lines.append(f"# TYPE {name} {METRIC_TYPES[name]}")
        for sample in matching:
            if sample.labels:
                pairs = ",".join(
                    f'{k}="{_escape(str(v))}"' for k, v in sorted(sample.labels.items())
                )
                lines.append(f"{name}{{{pairs}}} {_format_value(sample.value)}")
            else:
                lines.append(f"{name} {_format_value(sample.value)}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add helm/backend/app/metrics.py tests/test_metrics.py
git commit -m "Render Prometheus exposition text without a client library."
```

---

### Task 4: Per-app sample parsers

Pure functions from API payloads to samples. No network — that is Task 5's job.

**Files:**
- Modify: `helm/backend/app/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `metrics.Sample` from Task 3.
- Produces:
  - `parse_arr(app: str, *, counts: dict, queue: dict, missing: dict) -> list[Sample]`
  - `parse_bazarr(series: dict, movies: dict) -> list[Sample]`
  - `parse_prowlarr(indexers: list, stats: dict) -> list[Sample]`
  - `parse_transmission(session_stats: dict) -> list[Sample]`
  - `parse_streams(snapshot: dict) -> list[Sample]`
  - `parse_updates(payload: dict) -> list[Sample]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_metrics.py`:

```python
SONARR_COUNTS = {"episodeCount": 4200, "episodeFileCount": 4100, "sizeOnDisk": 9_000_000_000}
SONARR_SERIES = [
    {"statistics": {"episodeCount": 20, "episodeFileCount": 18, "sizeOnDisk": 500}},
    {"statistics": {"episodeCount": 10, "episodeFileCount": 10, "sizeOnDisk": 300}},
]


def test_parse_arr_counts_series_episodes_and_gaps():
    out = metrics.parse_arr(
        "sonarr",
        counts={"items": SONARR_SERIES},
        queue={"totalRecords": 3},
        missing={"totalRecords": 7},
    )
    by = {(s.name, s.labels.get("kind")): s.value for s in out}
    assert by[("kine_library_items", "series")] == 2
    assert by[("kine_library_items", "episodes")] == 30
    assert by[("kine_library_missing", "episodes")] == 7
    assert by[("kine_queue_items", None)] == 3
    assert next(s.value for s in out if s.name == "kine_library_bytes") == 800
    assert all(s.labels.get("app") == "sonarr" for s in out)


def test_parse_arr_handles_radarr_movie_shape():
    movies = {"items": [
        {"hasFile": True, "sizeOnDisk": 1000},
        {"hasFile": False, "sizeOnDisk": 0},
    ]}
    out = metrics.parse_arr("radarr", counts=movies, queue={"totalRecords": 0}, missing={"totalRecords": 1})
    by = {(s.name, s.labels.get("kind")): s.value for s in out}
    assert by[("kine_library_items", "movies")] == 2
    assert by[("kine_library_missing", "movies")] == 1


def test_parse_bazarr_splits_series_and_movies():
    out = metrics.parse_bazarr({"data": [{}, {}]}, {"data": [{}]})
    by = {s.labels["kind"]: s.value for s in out}
    assert by == {"series": 2, "movies": 1}
    assert all(s.name == "kine_subtitles_wanted" for s in out)


def test_parse_prowlarr_counts_enabled_and_totals():
    indexers = [{"enable": True}, {"enable": True}, {"enable": False}]
    stats = {"indexers": [
        {"numberOfQueries": 10, "numberOfGrabs": 2},
        {"numberOfQueries": 5, "numberOfGrabs": 1},
    ]}
    out = metrics.parse_prowlarr(indexers, stats)
    by = {s.name: s.value for s in out}
    assert by["kine_indexers_enabled"] == 2
    assert by["kine_indexer_queries_total"] == 15
    assert by["kine_indexer_grabs_total"] == 3


def test_parse_transmission_reads_rates_and_states():
    out = metrics.parse_transmission({
        "arguments": {
            "downloadSpeed": 1200,
            "uploadSpeed": 300,
            "activeTorrentCount": 4,
            "pausedTorrentCount": 2,
            "torrentCount": 6,
        }
    })
    rates = {s.labels["direction"]: s.value for s in out if s.name == "kine_download_rate_bytes"}
    states = {s.labels["state"]: s.value for s in out if s.name == "kine_torrents"}
    assert rates == {"down": 1200, "up": 300}
    assert states == {"active": 4, "paused": 2, "total": 6}


def test_parse_streams_groups_by_server_state_and_user():
    snapshot = {"sessions": [
        {"server": "plex", "state": "playing", "user": "steve"},
        {"server": "plex", "state": "playing", "user": "steve"},
        {"server": "emby", "state": "paused", "user": "kate"},
    ]}
    out = metrics.parse_streams(snapshot)
    by = {(s.labels["server"], s.labels["state"], s.labels["user"]): s.value for s in out}
    assert by[("plex", "playing", "steve")] == 2
    assert by[("emby", "paused", "kate")] == 1


def test_parse_streams_reports_zero_when_nothing_is_playing():
    out = metrics.parse_streams({"sessions": []})
    assert out == []


def test_parse_updates_flags_pending_containers():
    payload = {"containers": [
        {"app": "sonarr", "status": "update"},
        {"app": "radarr", "status": "current"},
    ]}
    by = {s.labels["app"]: s.value for s in metrics.parse_updates(payload)}
    assert by == {"sonarr": 1, "radarr": 0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: FAIL — `module 'app.metrics' has no attribute 'parse_arr'`.

- [ ] **Step 3: Implement the parsers**

Append to `helm/backend/app/metrics.py`:

```python
ARR_ITEM_KIND = {"sonarr": "series", "radarr": "movies"}


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_arr(app: str, *, counts: dict, queue: dict, missing: dict) -> list[Sample]:
    items = counts.get("items") or []
    kind = ARR_ITEM_KIND.get(app, "items")
    out = [Sample("kine_library_items", {"app": app, "kind": kind}, len(items))]

    size = sum(
        _num((item.get("statistics") or {}).get("sizeOnDisk", item.get("sizeOnDisk")))
        for item in items
    )
    out.append(Sample("kine_library_bytes", {"app": app}, size))

    if kind == "series":
        episodes = sum(_num((i.get("statistics") or {}).get("episodeCount")) for i in items)
        out.append(Sample("kine_library_items", {"app": app, "kind": "episodes"}, episodes))
        gap_kind = "episodes"
    else:
        gap_kind = "movies"
    out.append(
        Sample("kine_library_missing", {"app": app, "kind": gap_kind},
               _num(missing.get("totalRecords")))
    )
    out.append(Sample("kine_queue_items", {"app": app}, _num(queue.get("totalRecords"))))
    return out


def parse_bazarr(series: dict, movies: dict) -> list[Sample]:
    return [
        Sample("kine_subtitles_wanted", {"kind": "series"}, len(series.get("data") or [])),
        Sample("kine_subtitles_wanted", {"kind": "movies"}, len(movies.get("data") or [])),
    ]


def parse_prowlarr(indexers: list, stats: dict) -> list[Sample]:
    rows = stats.get("indexers") or []
    return [
        Sample("kine_indexers_enabled", {"app": "prowlarr"},
               sum(1 for i in indexers if i.get("enable"))),
        Sample("kine_indexer_queries_total", {"app": "prowlarr"},
               sum(_num(r.get("numberOfQueries")) for r in rows)),
        Sample("kine_indexer_grabs_total", {"app": "prowlarr"},
               sum(_num(r.get("numberOfGrabs")) for r in rows)),
    ]


def parse_transmission(session_stats: dict) -> list[Sample]:
    args = session_stats.get("arguments") or {}
    client = {"client": "transmission"}
    return [
        Sample("kine_download_rate_bytes", {**client, "direction": "down"},
               _num(args.get("downloadSpeed"))),
        Sample("kine_download_rate_bytes", {**client, "direction": "up"},
               _num(args.get("uploadSpeed"))),
        Sample("kine_torrents", {**client, "state": "active"},
               _num(args.get("activeTorrentCount"))),
        Sample("kine_torrents", {**client, "state": "paused"},
               _num(args.get("pausedTorrentCount"))),
        Sample("kine_torrents", {**client, "state": "total"},
               _num(args.get("torrentCount"))),
    ]


def parse_streams(snapshot: dict) -> list[Sample]:
    counts: dict[tuple[str, str, str], int] = {}
    for session in snapshot.get("sessions") or []:
        key = (
            session.get("server") or "unknown",
            session.get("state") or "playing",
            session.get("user") or "unknown",
        )
        counts[key] = counts.get(key, 0) + 1
    return [
        Sample("kine_streams_active", {"server": s, "state": st, "user": u}, n)
        for (s, st, u), n in sorted(counts.items())
    ]


def parse_updates(payload: dict) -> list[Sample]:
    out = []
    for row in payload.get("containers") or []:
        app = row.get("app") or row.get("name")
        if not app:
            continue
        out.append(
            Sample("kine_update_pending", {"app": app}, 1 if row.get("status") == "update" else 0)
        )
    return out
```

- [ ] **Step 4: Verify the updates payload shape matches reality**

Read `helm/backend/app/updates_info.py` and confirm each container dict uses the keys `app` (or `name`) and `status` with the value `"update"` for a pending update. If the real keys differ, fix `parse_updates` and its test to match the real payload — do not change `updates_info.py`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add helm/backend/app/metrics.py tests/test_metrics.py
git commit -m "Turn app API payloads into metric samples."
```

---

### Task 5: Collector, cache and the /api/metrics endpoint

**Files:**
- Modify: `helm/backend/app/metrics.py`
- Modify: `helm/backend/app/scheduler.py:159-168`
- Modify: `helm/backend/app/main.py:17` and the updates section around `main.py:571`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: parsers from Task 4, `appkeys.key_for` from Task 2, `watching.snapshot`, `updates_info.fetch`, `catalogue.load`, `config.profiles`.
- Produces:
  - `metrics.collect_once() -> None` — async; refreshes the module cache.
  - `metrics.export() -> str` — renders the cache.
  - `metrics.CACHE: list[Sample]`
  - `metrics.collector_loop() -> None` — async; forever, every 60s.
  - `GET /api/metrics` returning `text/plain; version=0.0.4`, no auth dependency.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_metrics.py`:

```python
import asyncio


def test_export_renders_whatever_is_cached(monkeypatch):
    monkeypatch.setattr(metrics, "CACHE", [metrics.Sample("kine_app_up", {"app": "x"}, 1)])
    assert 'kine_app_up{app="x"} 1' in metrics.export()


def test_export_never_raises_on_an_empty_cache(monkeypatch):
    monkeypatch.setattr(metrics, "CACHE", [])
    assert metrics.export() == "\n"


def test_a_failing_app_does_not_lose_the_other_samples(monkeypatch):
    async def ok(_app):
        return [metrics.Sample("kine_app_up", {"app": "good"}, 1)]

    async def boom(_app):
        raise RuntimeError("sonarr is wedged")

    monkeypatch.setattr(metrics, "COLLECTORS", {"good": ok, "bad": boom})
    monkeypatch.setattr(metrics, "_enabled_apps", lambda: ["good", "bad"])
    monkeypatch.setattr(metrics, "CACHE", [])
    asyncio.run(metrics.collect_once())
    names = {(s.name, tuple(sorted(s.labels.items()))) for s in metrics.CACHE}
    assert ("kine_app_up", (("app", "good"),)) in names
    assert any(
        s.name == "kine_collect_errors_total" and s.labels["app"] == "bad"
        for s in metrics.CACHE
    )


def test_collection_records_its_own_duration(monkeypatch):
    async def ok(_app):
        return []

    monkeypatch.setattr(metrics, "COLLECTORS", {"good": ok})
    monkeypatch.setattr(metrics, "_enabled_apps", lambda: ["good"])
    monkeypatch.setattr(metrics, "CACHE", [])
    asyncio.run(metrics.collect_once())
    assert any(s.name == "kine_collect_duration_seconds" for s in metrics.CACHE)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: FAIL — `module 'app.metrics' has no attribute 'export'`.

- [ ] **Step 3: Implement collection**

Append to `helm/backend/app/metrics.py` (add `import asyncio`, `import time`, `import httpx`, and `from . import appkeys, catalogue, compose, config, updates_info, watching` at the top of the file):

```python
CACHE: list[Sample] = []
COLLECT_INTERVAL = 60.0
ERRORS: dict[str, int] = {}
ARR_API = {"sonarr": "v3", "radarr": "v3", "prowlarr": "v1"}


def _enabled_apps() -> list[str]:
    return list(config.profiles())


async def _get_json(url: str, headers: dict, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json() if response.content else {}


def _base(app: str) -> str:
    return (catalogue.load().get(app, {}).get("internal") or "").rstrip("/")


async def _collect_arr(app: str) -> list[Sample]:
    key, base = appkeys.key_for(app), _base(app)
    if not key or not base:
        return []
    api = ARR_API[app]
    headers = {"X-Api-Key": key}
    resource = "series" if app == "sonarr" else "movie"
    items = await _get_json(f"{base}/api/{api}/{resource}", headers)
    queue = await _get_json(f"{base}/api/{api}/queue", headers, {"pageSize": 1})
    missing = await _get_json(
        f"{base}/api/{api}/wanted/missing", headers, {"pageSize": 1}
    )
    counts = {"items": items if isinstance(items, list) else []}
    return parse_arr(app, counts=counts, queue=queue or {}, missing=missing or {})


async def _collect_bazarr(_app: str) -> list[Sample]:
    key, base = appkeys.bazarr_key(), _base("bazarr")
    if not key or not base:
        return []
    headers = {"X-API-KEY": key}
    series = await _get_json(f"{base}/api/episodes/wanted", headers)
    movies = await _get_json(f"{base}/api/movies/wanted", headers)
    return parse_bazarr(series or {}, movies or {})


async def _collect_prowlarr(_app: str) -> list[Sample]:
    key, base = appkeys.arr_key("prowlarr"), _base("prowlarr")
    if not key or not base:
        return []
    headers = {"X-Api-Key": key}
    indexers = await _get_json(f"{base}/api/v1/indexer", headers)
    stats = await _get_json(f"{base}/api/v1/indexerstats", headers)
    return parse_prowlarr(indexers if isinstance(indexers, list) else [], stats or {})


async def _collect_transmission(_app: str) -> list[Sample]:
    base = _base("transmission")
    if not base:
        return []
    url = f"{base}/transmission/rpc"
    body = {"method": "session-stats"}
    async with httpx.AsyncClient(timeout=8.0) as client:
        # Transmission answers the first call with 409 and the session id
        # it wants echoed back. There is no way to skip the handshake.
        first = await client.post(url, json=body)
        if first.status_code == 409:
            token = first.headers.get("X-Transmission-Session-Id", "")
            first = await client.post(url, json=body,
                                      headers={"X-Transmission-Session-Id": token})
        first.raise_for_status()
        return parse_transmission(first.json())


async def _collect_streams(_app: str) -> list[Sample]:
    return parse_streams(await watching.snapshot())


async def _collect_updates(_app: str) -> list[Sample]:
    return parse_updates(await updates_info.fetch(compose, refresh=False))


async def _probe(app: str) -> list[Sample]:
    base = _base(app)
    if not base:
        return []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(base, follow_redirects=True)
        up = 1 if response.status_code < 500 else 0
    except httpx.HTTPError:
        up = 0
    return [Sample("kine_app_up", {"app": app}, up)]


COLLECTORS = {
    "sonarr": _collect_arr,
    "radarr": _collect_arr,
    "prowlarr": _collect_prowlarr,
    "bazarr": _collect_bazarr,
    "transmission": _collect_transmission,
}

# Always run, regardless of which apps are enabled.
GLOBAL_COLLECTORS = {"streams": _collect_streams, "updates": _collect_updates}


async def collect_once() -> None:
    started = time.monotonic()
    samples: list[Sample] = []
    enabled = _enabled_apps()

    jobs = [(name, fn, name) for name, fn in GLOBAL_COLLECTORS.items()]
    jobs += [(app, COLLECTORS[app], app) for app in enabled if app in COLLECTORS]

    for label, fn, arg in jobs:
        app_start = time.monotonic()
        try:
            samples.extend(await fn(arg))
        except Exception:  # noqa: BLE001 — one bad app must not lose the rest
            ERRORS[label] = ERRORS.get(label, 0) + 1
        samples.append(
            Sample("kine_collect_duration_seconds", {"app": label},
                   round(time.monotonic() - app_start, 3))
        )

    for app in enabled:
        try:
            samples.extend(await _probe(app))
        except Exception:  # noqa: BLE001
            ERRORS[app] = ERRORS.get(app, 0) + 1

    samples.extend(
        Sample("kine_collect_errors_total", {"app": app}, count)
        for app, count in ERRORS.items()
    )
    samples.append(
        Sample("kine_collect_duration_seconds", {"app": "total"},
               round(time.monotonic() - started, 3))
    )
    CACHE[:] = samples


def export() -> str:
    return render(CACHE)


async def collector_loop() -> None:
    while True:
        try:
            await collect_once()
        except Exception:  # noqa: BLE001 — the loop must outlive any failure
            pass
        await asyncio.sleep(COLLECT_INTERVAL)
```

- [ ] **Step 4: Start the loop**

In `helm/backend/app/scheduler.py`, add `metrics` to the `from . import ...` line and append one task inside `start`:

```python
        asyncio.create_task(metrics.collector_loop()),
```

- [ ] **Step 5: Add the endpoint**

In `helm/backend/app/main.py`, add `metrics` to the `from . import ...` line on line 17, then add this beside the updates routes. Note the deliberate absence of `Depends(require_user)`:

```python
@app.get("/api/metrics")
async def prometheus_metrics():
    """Scraped by Prometheus, which cannot log in.

    Reachable only from inside the stack: no Traefik router points here,
    and it renders a cache, so a wedged app cannot stall the scrape.
    """
    return Response(content=metrics.export(),
                    media_type="text/plain; version=0.0.4; charset=utf-8")
```

- [ ] **Step 6: Run the suite**

Run: `python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add helm/backend/app/metrics.py helm/backend/app/scheduler.py helm/backend/app/main.py tests/test_metrics.py
git commit -m "Collect app metrics on a timer and serve them to Prometheus."
```

---

### Task 6: Compose fragments, catalogue and environment

**Files:**
- Create: `compose/metrics.prometheus.yml`, `compose/metrics.cadvisor.yml`, `compose/metrics.node-exporter.yml`, `compose/metrics.grafana.yml`
- Modify: `docker-compose.yml` (`include:`), `catalogue.yml`, `.env.example`, `helm/backend/app/catalogue.py:8-14`
- Test: `tests/test_stack.py`

**Interfaces:**
- Consumes: transitive `resolve_deps` from Task 1.
- Produces: services `prometheus`, `cadvisor`, `node-exporter`, `grafana`; catalogue tier `metrics`; env vars `PROMETHEUS_TAG`, `CADVISOR_TAG`, `NODE_EXPORTER_TAG`, `GRAFANA_TAG` with matching `_DIGEST`, plus `GRAFANA_ADMIN_PASSWORD`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stack.py`, matching that file's existing helpers for loading compose fragments and the catalogue:

```python
def test_prometheus_stays_off_the_edge_network():
    svc = service("prometheus")
    assert "kine_edge" not in svc.get("networks", [])
    assert not [l for l in svc.get("labels", []) if "traefik" in l]


def test_prometheus_retention_is_capped_by_time_and_size():
    command = " ".join(service("prometheus").get("command", []))
    assert "--storage.tsdb.retention.time=30d" in command
    assert "--storage.tsdb.retention.size=2GB" in command


def test_cadvisor_never_mounts_the_raw_docker_socket():
    volumes = " ".join(service("cadvisor").get("volumes", []))
    assert "docker.sock" not in volumes


def test_grafana_allows_anonymous_viewing_and_embedding():
    env = service("grafana")["environment"]
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "true"
    assert env["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Viewer"
    assert env["GF_SECURITY_ALLOW_EMBEDDING"] == "true"


def test_metrics_tier_is_labelled_in_helm():
    from app import catalogue as helm_catalogue
    assert helm_catalogue.TIER_LABELS["metrics"] == "Metrics"


def test_only_grafana_is_visible_in_the_metrics_tier():
    cat = load_catalogue()
    visible = [k for k, v in cat.items() if v.get("tier") == "metrics" and not v.get("hidden")]
    assert visible == ["grafana"]
```

If `service()` and `load_catalogue()` are not the helper names already used in `tests/test_stack.py`, use whatever that file already defines rather than adding new helpers.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_stack.py -q`
Expected: FAIL — no `prometheus` service.

- [ ] **Step 3: Write the fragments**

`compose/metrics.prometheus.yml`:

```yaml
# Metrics store. Internal only: Grafana and Helm are its sole clients,
# so it needs no certificate and no edge exposure. Retention is capped
# on both axes so it cannot quietly fill the disk the media lives on.
services:
  prometheus:
    image: prom/prometheus:${PROMETHEUS_TAG}
    container_name: kine-prometheus
    profiles: ["prometheus"]
    restart: unless-stopped
    user: "${PUID}:${PGID}"
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=30d
      - --storage.tsdb.retention.size=2GB
      - --web.enable-lifecycle
    environment:
      TZ: ${KINE_TIMEZONE}
    volumes:
      - ${STACK_ROOT}/config/prometheus:/etc/prometheus
      - ${STACK_ROOT}/config/prometheus/data:/prometheus
    networks: [kine_internal]
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:9090/-/healthy || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 30s
```

`compose/metrics.cadvisor.yml`:

```yaml
# Per-container CPU, memory, network and disk. Talks to Docker through
# the same socket proxy Traefik and Helm use, so nothing here holds the
# raw socket. The default housekeeping interval is wasteful on a box
# that also transcodes, hence the flags.
services:
  cadvisor:
    image: gcr.io/cadvisor/cadvisor:${CADVISOR_TAG}
    container_name: kine-cadvisor
    profiles: ["cadvisor"]
    restart: unless-stopped
    command:
      - --docker=tcp://dockerproxy:2375
      - --docker_only=true
      - --housekeeping_interval=15s
      - --store_container_labels=false
    environment:
      TZ: ${KINE_TIMEZONE}
    volumes:
      - /:/rootfs:ro
      - /sys:/sys:ro
      - /var/lib/docker/:/var/lib/docker:ro
    devices:
      - /dev/kmsg:/dev/kmsg
    privileged: true
    networks: [kine_internal, kine_ctrl]
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:8080/healthz || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 30s
```

`compose/metrics.node-exporter.yml`:

```yaml
# Host CPU, memory, filesystems, network and sensors. Needs the host PID
# namespace and read-only host filesystems; it changes nothing.
services:
  node-exporter:
    image: prom/node-exporter:${NODE_EXPORTER_TAG}
    container_name: kine-node-exporter
    profiles: ["node-exporter"]
    restart: unless-stopped
    pid: host
    command:
      - --path.procfs=/host/proc
      - --path.sysfs=/host/sys
      - --path.rootfs=/rootfs
      - --collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc|rootfs/var/lib/docker)($$|/)
    environment:
      TZ: ${KINE_TIMEZONE}
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/rootfs:ro
    networks: [kine_internal]
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:9100/metrics >/dev/null || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 20s
```

`compose/metrics.grafana.yml`:

```yaml
# Dashboards. Anonymous Viewer access is deliberate: it is what lets the
# Helm GUI embed panels without a login dance. Editing still needs the
# admin account. Provisioning is read-only and comes from the repo, so
# dashboards live in git rather than inside grafana.db.
services:
  grafana:
    image: grafana/grafana:${GRAFANA_TAG}
    container_name: kine-grafana
    profiles: ["grafana"]
    restart: unless-stopped
    depends_on:
      prometheus:
        condition: service_healthy
    environment:
      TZ: ${KINE_TIMEZONE}
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: "Viewer"
      GF_SECURITY_ALLOW_EMBEDDING: "true"
      GF_USERS_DEFAULT_THEME: "dark"
      GF_SERVER_ROOT_URL: "https://grafana.${KINE_DOMAIN}"
      GF_ANALYTICS_REPORTING_ENABLED: "false"
      GF_ANALYTICS_CHECK_FOR_UPDATES: "false"
      GF_PATHS_PROVISIONING: /etc/grafana/provisioning
    volumes:
      - ${STACK_ROOT}/config/grafana/data:/var/lib/grafana
      - ${STACK_ROOT}/config/grafana/provisioning:/etc/grafana/provisioning:ro
      - ${STACK_ROOT}/config/grafana/dashboards:/etc/grafana/dashboards:ro
    networks: [kine_internal, kine_edge]
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:3000/api/health || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 30s
    labels:
      - traefik.enable=true
      - traefik.http.routers.grafana.rule=Host(`grafana.${KINE_DOMAIN}`) || Host(`grafana.${KINE_LOCAL_DOMAIN}`)
      - traefik.http.services.grafana.loadbalancer.server.port=3000
```

- [ ] **Step 4: Register everything**

Add the four fragments to `include:` in `docker-compose.yml`, keeping the file's existing ordering convention.

Add `"metrics": "Metrics"` to `TIER_LABELS` in `helm/backend/app/catalogue.py`.

Append to `catalogue.yml`:

```yaml
  grafana:
    name: Grafana
    tier: metrics
    summary: Dashboards for stack and app statistics. Anonymous read access so Helm can embed panels.
    url: https://grafana.com/docs/grafana/latest/
    releases: https://github.com/grafana/grafana/releases
    internal: http://grafana:3000
    subdomain: grafana
    requires: [prometheus]
    default: true

  prometheus:
    name: Prometheus
    tier: metrics
    summary: Metrics store scraped from cAdvisor, node-exporter and Helm.
    releases: https://github.com/prometheus/prometheus/releases
    internal: http://prometheus:9090
    requires: [cadvisor, node-exporter]
    default: true
    hidden: true

  cadvisor:
    name: cAdvisor
    tier: metrics
    summary: Per-container resource statistics.
    releases: https://github.com/google/cadvisor/releases
    internal: http://cadvisor:8080
    default: true
    hidden: true

  node-exporter:
    name: Node Exporter
    tier: metrics
    summary: Host CPU, memory, disk and network statistics.
    releases: https://github.com/prometheus/node_exporter/releases
    internal: http://node-exporter:9100
    default: true
    hidden: true
```

Add to the image pins section of `.env.example`:

```
PROMETHEUS_TAG=latest
PROMETHEUS_DIGEST=
CADVISOR_TAG=latest
CADVISOR_DIGEST=
NODE_EXPORTER_TAG=latest
NODE_EXPORTER_DIGEST=
GRAFANA_TAG=latest
GRAFANA_DIGEST=
```

And near the other credentials in `.env.example`:

```
# Grafana's admin login. Dashboards are readable anonymously; this is
# only needed to edit them.
GRAFANA_ADMIN_PASSWORD=kine
```

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests -q`
Expected: PASS, including `test_top_level_includes_every_fragment`, `test_every_catalogued_app_has_a_service`, `test_service_declares_its_own_profile`, `test_env_example_defines_every_tag_referenced`, `test_untunnelled_web_apps_have_routers` and `test_web_apps_have_local_traefik_aliases`.

If `test_service_declares_its_own_profile` has an exception list, do not add these services to it — they each declare their own profile and should pass unaided.

- [ ] **Step 6: Verify compose can parse it**

Run: `docker compose --profile grafana --profile prometheus --profile cadvisor --profile node-exporter config >/dev/null && echo OK`
Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add compose/metrics.*.yml docker-compose.yml catalogue.yml .env.example helm/backend/app/catalogue.py tests/test_stack.py
git commit -m "Add a Metrics tier of Prometheus, cAdvisor, node-exporter and Grafana."
```

---

### Task 7: Seed Prometheus and Grafana configuration

**Files:**
- Create: `provision/recipes/metrics.py`
- Modify: `provision/seed.py` (`seed_all`), `provision/provision.py`
- Test: `tests/test_provision.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `metrics.seed(stack: pathlib.Path, enabled: set[str], log=print) -> None`, writing `config/prometheus/prometheus.yml`, `config/grafana/provisioning/datasources/prometheus.yml`, `config/grafana/provisioning/dashboards/kine.yml`, and copying `provision/assets/grafana/dashboards/*.json` into `config/grafana/dashboards/`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provision.py`:

```python
def test_metrics_seed_writes_scrape_targets(tmp_path):
    from recipes import metrics as metrics_recipe
    metrics_recipe.seed(tmp_path, {"grafana", "prometheus"}, log=lambda *_: None)
    cfg = yaml.safe_load((tmp_path / "config" / "prometheus" / "prometheus.yml").read_text())
    targets = {j["job_name"]: j for j in cfg["scrape_configs"]}
    assert "cadvisor:8080" in targets["cadvisor"]["static_configs"][0]["targets"]
    assert "node-exporter:9100" in targets["node"]["static_configs"][0]["targets"]
    assert "helm:8600" in targets["kine"]["static_configs"][0]["targets"]
    assert targets["kine"]["metrics_path"] == "/api/metrics"


def test_metrics_seed_pins_the_datasource_uid(tmp_path):
    from recipes import metrics as metrics_recipe
    metrics_recipe.seed(tmp_path, {"grafana"}, log=lambda *_: None)
    ds = yaml.safe_load(
        (tmp_path / "config" / "grafana" / "provisioning" / "datasources"
         / "prometheus.yml").read_text()
    )
    assert ds["datasources"][0]["uid"] == "kine-prom"
    assert ds["datasources"][0]["url"] == "http://prometheus:9090"


def test_metrics_seed_copies_dashboards(tmp_path):
    from recipes import metrics as metrics_recipe
    metrics_recipe.seed(tmp_path, {"grafana"}, log=lambda *_: None)
    copied = list((tmp_path / "config" / "grafana" / "dashboards").glob("*.json"))
    assert {p.name for p in copied} >= {"kine-overview.json"}


def test_metrics_seed_is_idempotent(tmp_path):
    from recipes import metrics as metrics_recipe
    metrics_recipe.seed(tmp_path, {"grafana"}, log=lambda *_: None)
    first = (tmp_path / "config" / "prometheus" / "prometheus.yml").read_text()
    metrics_recipe.seed(tmp_path, {"grafana"}, log=lambda *_: None)
    assert (tmp_path / "config" / "prometheus" / "prometheus.yml").read_text() == first
```

Match this file's existing import style for reaching `provision/` — if it inserts `provision` on `sys.path` at module level, reuse that rather than importing inside each test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_provision.py -q`
Expected: FAIL — `No module named 'recipes.metrics'`.

- [ ] **Step 3: Write the recipe**

Create `provision/recipes/metrics.py`:

```python
"""Seed Prometheus scrape config and Grafana provisioning.

Dashboards are kept in the repo and copied in, not created through
Grafana's API, so they stay reviewable in git instead of living inside
grafana.db. Grafana runs as uid 472 rather than the stack PUID, so the
directories it writes to are chowned explicitly.
"""
from __future__ import annotations

import os
import pathlib
import shutil

import yaml

GRAFANA_UID = 472
GRAFANA_GID = 472
ASSETS = pathlib.Path(__file__).resolve().parents[1] / "assets" / "grafana"

PROMETHEUS_CONFIG = {
    "global": {"scrape_interval": "15s", "evaluation_interval": "15s"},
    "scrape_configs": [
        {
            "job_name": "prometheus",
            "static_configs": [{"targets": ["localhost:9090"]}],
        },
        {
            "job_name": "cadvisor",
            "static_configs": [{"targets": ["cadvisor:8080"]}],
        },
        {
            "job_name": "node",
            "static_configs": [{"targets": ["node-exporter:9100"]}],
        },
        {
            # Matches the collector's own 60s tick; scraping faster only
            # re-reads the same cache.
            "job_name": "kine",
            "scrape_interval": "60s",
            "metrics_path": "/api/metrics",
            "static_configs": [{"targets": ["helm:8600"]}],
        },
    ],
}

DATASOURCE = {
    "apiVersion": 1,
    "datasources": [
        {
            "name": "Prometheus",
            "type": "prometheus",
            "access": "proxy",
            "url": "http://prometheus:9090",
            # Fixed: dashboards reference this uid, and provisioning
            # fails opaquely if it drifts.
            "uid": "kine-prom",
            "isDefault": True,
            "editable": False,
        }
    ],
}

DASHBOARD_PROVIDER = {
    "apiVersion": 1,
    "providers": [
        {
            "name": "kine",
            "orgId": 1,
            "folder": "Kine",
            "type": "file",
            "disableDeletion": False,
            "updateIntervalSeconds": 30,
            "allowUiUpdates": False,
            "options": {"path": "/etc/grafana/dashboards", "foldersFromFilesStructure": False},
        }
    ],
}


def _write_yaml(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _chown(path: pathlib.Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid)
    except (OSError, AttributeError):
        # Non-root test runs and Docker Desktop cannot chown; Grafana
        # only needs this on a real appliance.
        pass


def seed(stack: pathlib.Path, enabled: set[str], log=print) -> None:
    if not {"grafana", "prometheus"} & set(enabled):
        return

    prom_dir = stack / "config" / "prometheus"
    (prom_dir / "data").mkdir(parents=True, exist_ok=True)
    _write_yaml(prom_dir / "prometheus.yml", PROMETHEUS_CONFIG)
    log("  prometheus: wrote scrape config")

    grafana = stack / "config" / "grafana"
    _write_yaml(grafana / "provisioning" / "datasources" / "prometheus.yml", DATASOURCE)
    _write_yaml(grafana / "provisioning" / "dashboards" / "kine.yml", DASHBOARD_PROVIDER)

    dashboards = grafana / "dashboards"
    dashboards.mkdir(parents=True, exist_ok=True)
    count = 0
    for src in sorted((ASSETS / "dashboards").glob("*.json")):
        shutil.copyfile(src, dashboards / src.name)
        count += 1
    log(f"  grafana: provisioned datasource and {count} dashboards")

    (grafana / "data").mkdir(parents=True, exist_ok=True)
    for path in (grafana, grafana / "data", grafana / "dashboards",
                 grafana / "provisioning"):
        _chown(path, GRAFANA_UID, GRAFANA_GID)
```

- [ ] **Step 4: Hook it into seeding**

In `provision/seed.py`, import the recipe and call it from `seed_all` alongside the other seeders, passing the module-level `STACK` path and the enabled set. In `provision/provision.py`, add `metrics` to the `from recipes import ...` line so the module is importable in both modes even though only seed uses it.

- [ ] **Step 5: Create the assets directory**

```bash
mkdir -p provision/assets/grafana/dashboards
```

Task 8 fills it. Until then `test_metrics_seed_copies_dashboards` fails, which is expected and is why that test lands with Task 8's commit if you are running strictly red-green. If you prefer each task green, run Task 8 before re-running that one test.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_provision.py -q`
Expected: PASS except `test_metrics_seed_copies_dashboards` until Task 8 adds dashboards.

- [ ] **Step 7: Commit**

```bash
git add provision/recipes/metrics.py provision/seed.py provision/provision.py tests/test_provision.py
git commit -m "Seed Prometheus scrape config and Grafana provisioning."
```

---

### Task 8: Dashboards

Four dashboards as repo JSON, plus a test that keeps their PromQL honest.

**Files:**
- Create: `provision/assets/grafana/dashboards/kine-overview.json`, `kine-containers.json`, `kine-host.json`, `kine-media.json`
- Test: `tests/test_dashboards.py`

**Interfaces:**
- Consumes: `metrics.METRIC_TYPES` from Task 3 (the test cross-checks against it).
- Produces: dashboard files with UIDs `kine-overview`, `kine-containers`, `kine-host`, `kine-media`, and stable numeric `panelId`s on the overview dashboard that Task 10 embeds.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dashboards.py`:

```python
"""Dashboards must parse, be uniquely identified, and only ask for
metrics that something actually exports."""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import metrics  # noqa: E402

DASHBOARDS = sorted((ROOT / "provision" / "assets" / "grafana" / "dashboards").glob("*.json"))
EXPECTED_UIDS = {"kine-overview", "kine-containers", "kine-host", "kine-media"}
# Everything cAdvisor and node-exporter provide is named by them, not us.
EXTERNAL_PREFIXES = ("container_", "node_", "machine_", "up", "scrape_")


def _panels(dashboard: dict):
    for panel in dashboard.get("panels", []):
        yield panel
        yield from panel.get("panels", [])


def _expressions(dashboard: dict):
    for panel in _panels(dashboard):
        for target in panel.get("targets", []):
            if target.get("expr"):
                yield target["expr"]


def test_there_are_four_dashboards():
    assert len(DASHBOARDS) == 4


def test_every_dashboard_parses_and_is_uniquely_identified():
    uids = set()
    for path in DASHBOARDS:
        data = json.loads(path.read_text())
        assert data["uid"] not in uids, f"duplicate uid in {path.name}"
        uids.add(data["uid"])
        assert data["title"]
    assert uids == EXPECTED_UIDS


def test_every_panel_uses_the_pinned_datasource():
    for path in DASHBOARDS:
        data = json.loads(path.read_text())
        for panel in _panels(data):
            ds = panel.get("datasource")
            if ds is None:
                continue
            assert ds.get("uid") == "kine-prom", f"{path.name}: {panel.get('title')}"


def test_every_panel_has_an_id_and_a_title():
    for path in DASHBOARDS:
        data = json.loads(path.read_text())
        ids = [p["id"] for p in _panels(data)]
        assert len(ids) == len(set(ids)), f"duplicate panel id in {path.name}"
        assert all(p.get("title") is not None for p in _panels(data))


def test_dashboards_only_reference_metrics_that_exist():
    known = set(metrics.METRIC_TYPES)
    for path in DASHBOARDS:
        data = json.loads(path.read_text())
        for expr in _expressions(data):
            for name in re.findall(r"\b[a-z][a-z0-9_]*_[a-z0-9_]+\b", expr):
                if name.startswith(EXTERNAL_PREFIXES) or not name.startswith("kine_"):
                    continue
                assert name in known, f"{path.name} references unknown metric {name}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dashboards.py -q`
Expected: FAIL — `assert 0 == 4`.

- [ ] **Step 3: Build the overview dashboard**

Create `provision/assets/grafana/dashboards/kine-overview.json`. Use `schemaVersion: 39`, `"editable": false`, `"refresh": "30s"`, `"time": {"from": "now-24h", "to": "now"}`, `"style": "dark"`, `"uid": "kine-overview"`, `"title": "Kine Overview"`, `"tags": ["kine"]`, and `"graphTooltip": 1` for a shared crosshair.

Every panel carries `"datasource": {"type": "prometheus", "uid": "kine-prom"}`. Panel ids are fixed because Task 10 embeds them by id.

Stat row across the top, `gridPos` height 4, width 4 each, at `y: 0`. Each uses `"type": "stat"` with `"options": {"graphMode": "area", "colorMode": "value", "textMode": "auto"}`:

| id | Title | Expression | Unit |
|---|---|---|---|
| 1 | Streaming Now | `sum(kine_streams_active)` | `short` |
| 2 | Apps Up | `sum(kine_app_up)` | `short` |
| 3 | Host CPU | `100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)` | `percent` |
| 4 | Memory Used | `100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)` | `percent` |
| 5 | Library Size | `sum(kine_library_bytes)` | `bytes` |
| 6 | Updates Pending | `sum(kine_update_pending)` | `short` |

Panels 3, 4 and 6 get thresholds: green base, yellow at 70, red at 90 for the percentages; green base and yellow at 1 for updates.

Graph row at `y: 4`, height 8:

- id 7, `timeseries`, "Streams", width 12, `sum by(server) (kine_streams_active)`, legend `{{server}}`, `fillOpacity: 30`, `gradientMode: "opacity"`, `lineInterpolation: "smooth"`, `stacking: {"mode": "normal"}`, unit `short`.
- id 8, `timeseries`, "Download Rate", width 12, `sum by(direction) (kine_download_rate_bytes)`, legend `{{direction}}`, unit `Bps`, `fillOpacity: 25`, `gradientMode: "opacity"`, `lineInterpolation: "smooth"`.

Row at `y: 12`, height 8:

- id 9, `bargauge`, "Container CPU", width 12, `sort_desc(sum by(name) (rate(container_cpu_usage_seconds_total{name=~"kine-.*"}[5m])) * 100)`, legend `{{name}}`, unit `percent`, `"options": {"displayMode": "gradient", "orientation": "horizontal"}`.
- id 10, `gauge`, "Filesystems", width 12, `100 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"} * 100)`, legend `{{mountpoint}}`, unit `percent`, thresholds green / yellow 75 / red 90.

Row at `y: 20`, height 8:

- id 11, `timeseries`, "Download Queue", width 12, `kine_queue_items`, legend `{{app}}`, unit `short`.
- id 12, `table`, "App Status", width 12, two targets both with `"instant": true, "format": "table"`: `kine_app_up` and `kine_update_pending`. Add a `"transformations"` entry of type `"merge"` so the two join on `app`.

- [ ] **Step 4: Build the containers dashboard**

Create `kine-containers.json` with `"uid": "kine-containers"`, `"title": "Kine Containers"`, and a `templating` list holding one variable:

```json
{
  "name": "container",
  "type": "query",
  "datasource": {"type": "prometheus", "uid": "kine-prom"},
  "query": "label_values(container_cpu_usage_seconds_total{name=~\"kine-.*\"}, name)",
  "multi": true,
  "includeAll": true,
  "current": {"text": "All", "value": "$__all"},
  "refresh": 2
}
```

Four `timeseries` panels, ids 1-4, each 12 wide and 8 high, all with `fillOpacity: 25`, `gradientMode: "opacity"`, `lineInterpolation: "smooth"` and legend `{{name}}`:

| id | Title | Expression | Unit |
|---|---|---|---|
| 1 | CPU | `sum by(name) (rate(container_cpu_usage_seconds_total{name=~"$container"}[5m])) * 100` | `percent` |
| 2 | Memory | `container_memory_working_set_bytes{name=~"$container"}` | `bytes` |
| 3 | Network | `sum by(name) (rate(container_network_receive_bytes_total{name=~"$container"}[5m]))` | `Bps` |
| 4 | Disk IO | `sum by(name) (rate(container_fs_writes_bytes_total{name=~"$container"}[5m]))` | `Bps` |

Then id 5, a `table` titled "Top Consumers", 24 wide, instant query `topk(10, sum by(name) (rate(container_cpu_usage_seconds_total{name=~"kine-.*"}[5m])) * 100)`, unit `percent`.

- [ ] **Step 5: Build the host dashboard**

Create `kine-host.json` with `"uid": "kine-host"`, `"title": "Kine Host"`:

| id | Type | Title | Expression | Unit |
|---|---|---|---|---|
| 1 | timeseries | CPU by Core | `100 - (rate(node_cpu_seconds_total{mode="idle"}[5m]) * 100)`, legend `core {{cpu}}` | `percent` |
| 2 | timeseries | Load Average | `node_load1`, `node_load5`, `node_load15` | `short` |
| 3 | timeseries | Memory | `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes` and `node_memory_Cached_bytes` | `bytes` |
| 4 | gauge | Filesystems | `100 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"} * 100)`, legend `{{mountpoint}}` | `percent` |
| 5 | timeseries | Network | `rate(node_network_receive_bytes_total{device!~"lo\|veth.*\|br-.*\|docker0"}[5m])` and the transmit equivalent, legend `{{device}}` | `Bps` |
| 6 | timeseries | Drive Temperature | `node_hwmon_temp_celsius`, legend `{{chip}}` | `celsius` |

Set panel 6's description to note that it stays empty on hosts without hwmon sensors, so an empty panel does not read as a fault.

- [ ] **Step 6: Build the media dashboard**

Create `kine-media.json` with `"uid": "kine-media"`, `"title": "Kine Media"`:

| id | Type | Title | Expression | Unit |
|---|---|---|---|---|
| 1 | timeseries | Library Size | `sum by(app) (kine_library_bytes)`, legend `{{app}}`, smooth gradient fill | `bytes` |
| 2 | timeseries | Library Items | `kine_library_items`, legend `{{app}} {{kind}}` | `short` |
| 3 | timeseries | Missing | `kine_library_missing`, legend `{{app}} {{kind}}` | `short` |
| 4 | timeseries | Subtitles Wanted | `kine_subtitles_wanted`, legend `{{kind}}` | `short` |
| 5 | timeseries | Indexer Grabs | `rate(kine_indexer_grabs_total[1h]) * 3600`, legend `grabs/hour` | `short` |
| 6 | bargauge | Streams by User | `sum by(user) (kine_streams_active)`, legend `{{user}}`, gradient display | `short` |
| 7 | timeseries | Torrents | `kine_torrents{state!="total"}`, legend `{{state}}`, stacked | `short` |

- [ ] **Step 7: Run the dashboard test**

Run: `python -m pytest tests/test_dashboards.py tests/test_provision.py -q`
Expected: PASS, including `test_metrics_seed_copies_dashboards` from Task 7.

- [ ] **Step 8: Commit**

```bash
git add provision/assets/grafana/dashboards tests/test_dashboards.py
git commit -m "Add provisioned dashboards for the stack, containers, host and media."
```

---

### Task 9: Sparkline data endpoint

**Files:**
- Create: `helm/backend/app/promquery.py`
- Modify: `helm/backend/app/main.py`
- Test: `tests/test_promquery.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `promquery.parse_range(payload: dict) -> dict[str, list[float]]` — Prometheus `query_range` response to app id to values, stripping the `kine-` container prefix.
  - `promquery.card_series() -> dict` — async; `{"apps": {"sonarr": {"cpu": [...], "mem": [...]}}}`, `{"apps": {}}` when Prometheus is off or unreachable.
  - `GET /api/stats/cards` returning that dict.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_promquery.py`:

```python
"""Prometheus range responses become per-app sparkline series."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import promquery  # noqa: E402

RANGE_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"name": "kine-sonarr"},
                "values": [[1700000000, "1.5"], [1700000300, "2.25"]],
            },
            {
                "metric": {"name": "kine-radarr"},
                "values": [[1700000000, "0"], [1700000300, "0.5"]],
            },
        ],
    },
}


def test_parse_range_strips_the_container_prefix():
    assert set(promquery.parse_range(RANGE_RESPONSE)) == {"sonarr", "radarr"}


def test_parse_range_returns_floats_in_order():
    assert promquery.parse_range(RANGE_RESPONSE)["sonarr"] == [1.5, 2.25]


def test_parse_range_survives_an_error_response():
    assert promquery.parse_range({"status": "error"}) == {}


def test_parse_range_skips_unparseable_points():
    payload = {
        "status": "success",
        "data": {"result": [
            {"metric": {"name": "kine-x"}, "values": [[1, "NaN"], [2, "3"]]}
        ]},
    }
    assert promquery.parse_range(payload)["x"] == [3.0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_promquery.py -q`
Expected: FAIL — `No module named 'app.promquery'`.

- [ ] **Step 3: Write the module**

Create `helm/backend/app/promquery.py`:

```python
"""Small read-only client for the sparklines on the Apps page.

One range query covers every container, because twenty per-card requests
would be twenty round trips for a graph two centimetres wide.
"""
from __future__ import annotations

import math
import time

import httpx

from . import config

PROMETHEUS = "http://prometheus:9090"
WINDOW_SECONDS = 3 * 60 * 60
STEP_SECONDS = 300
CACHE_TTL = 30.0
_cache: dict[str, object] = {"at": 0.0, "data": {"apps": {}}}

CPU_QUERY = 'sum by(name) (rate(container_cpu_usage_seconds_total{name=~"kine-.*"}[5m])) * 100'
MEM_QUERY = 'container_memory_working_set_bytes{name=~"kine-.*"}'


def parse_range(payload: dict) -> dict[str, list[float]]:
    if payload.get("status") != "success":
        return {}
    out: dict[str, list[float]] = {}
    for series in (payload.get("data") or {}).get("result") or []:
        name = (series.get("metric") or {}).get("name") or ""
        app = name[5:] if name.startswith("kine-") else name
        if not app:
            continue
        values = []
        for point in series.get("values") or []:
            try:
                value = float(point[1])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isnan(value) or math.isinf(value):
                continue
            values.append(value)
        out[app] = values
    return out


async def _range(client: httpx.AsyncClient, query: str) -> dict[str, list[float]]:
    now = int(time.time())
    response = await client.get(
        f"{PROMETHEUS}/api/v1/query_range",
        params={"query": query, "start": now - WINDOW_SECONDS, "end": now,
                "step": STEP_SECONDS},
    )
    response.raise_for_status()
    return parse_range(response.json())


async def card_series() -> dict:
    if time.monotonic() - float(_cache["at"]) < CACHE_TTL:
        return _cache["data"]  # type: ignore[return-value]
    if "prometheus" not in config.profiles():
        return {"apps": {}}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            cpu = await _range(client, CPU_QUERY)
            mem = await _range(client, MEM_QUERY)
    except httpx.HTTPError:
        # The Apps page must render with or without metrics.
        return {"apps": {}}
    apps = {
        app: {"cpu": cpu.get(app, []), "mem": mem.get(app, [])}
        for app in set(cpu) | set(mem)
    }
    _cache["at"] = time.monotonic()
    _cache["data"] = {"apps": apps}
    return _cache["data"]  # type: ignore[return-value]
```

- [ ] **Step 4: Add the endpoint**

In `helm/backend/app/main.py`, add `promquery` to the `from . import ...` line and add beside the other read endpoints:

```python
@app.get("/api/stats/cards")
async def stats_cards(user: str = Depends(require_user)):
    """Sparkline series for the Apps page. Empty when metrics are off."""
    return await promquery.card_series()
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add helm/backend/app/promquery.py helm/backend/app/main.py tests/test_promquery.py
git commit -m "Serve per-app sparkline series from one Prometheus range query."
```

---

### Task 10: Stats tab and card sparklines

**Files:**
- Modify: `helm/frontend/index.html`
- Test: `tests/test_frontend.py`

**Interfaces:**
- Consumes: `GET /api/stats/cards` from Task 9; dashboard UID `kine-overview` and panel ids 1, 2, 7, 8, 9, 10 from Task 8.
- Produces: `render.stats`, `sparkline(values, width, height)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_frontend.py`, matching how that file already loads `index.html`:

```python
def test_stats_tab_is_in_the_nav():
    html = INDEX.read_text()
    assert "'stats'" in html
    assert "render.stats" in html


def test_stats_embeds_solo_panels_from_the_overview_dashboard():
    html = INDEX.read_text()
    assert "/d-solo/kine-overview" in html
    assert "kiosk" in html


def test_apps_page_asks_for_sparkline_data():
    assert "/stats/cards" in INDEX.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_frontend.py -q`
Expected: FAIL — `'stats'` not found.

- [ ] **Step 3: Add the tab**

In `helm/frontend/index.html`, add `stats: 'Stats'` to `TAB_LABELS` and `'stats'` to the nav array in `render.shell`, placing it after `'apps'`:

```javascript
        ${['apps','stats','updates','vpn','status','settings'].map(t =>
```

- [ ] **Step 4: Add the CSS**

Beside the existing card styles:

```css
.panel-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:.75rem}
.panel-grid iframe{width:100%;height:220px;border:0;border-radius:10px;
  background:var(--panel)}
.panel-grid .tall{grid-column:span 2}
.spark{display:block;width:100%;height:26px;margin-top:.4rem;opacity:.85}
.spark path{fill:none;stroke:var(--accent);stroke-width:1.5}
.spark .fill{fill:color-mix(in srgb, var(--accent) 22%, transparent);stroke:none}
```

- [ ] **Step 5: Render the page**

Add `render.stats`. It reads the Grafana host from the apps payload the same way app launch URLs are built, so it works locally and remotely:

```javascript
render.stats = async () => {
  render.shell('<div class="card"><h3>Stats</h3><p>Loading&hellip;</p></div>');
  const data = await api('/apps');
  const grafana = (data.apps || []).find(a => a.id === 'grafana');
  if (!grafana || !grafana.enabled) {
    render.shell(`
      <div class="card">
        <h3>Stats</h3>
        <p>The Metrics section is off, so there is nothing to graph yet.
           Turning it on starts Prometheus, cAdvisor, node-exporter and
           Grafana, and history begins accumulating from that moment.</p>
        <button class="act primary" id="enable-metrics">Enable Metrics</button>
      </div>`);
    $('#enable-metrics').onclick = async () => {
      await api('/tiers/metrics/enable', {method: 'POST'});
      render.stats();
    };
    return;
  }
  const base = (grafana.url || '').replace(/\/$/, '');
  const panel = (id, cls = '') =>
    `<iframe class="${cls}" loading="lazy" title="panel ${id}" src="${base}` +
    `/d-solo/kine-overview?panelId=${id}&theme=dark&kiosk&from=now-24h&to=now&refresh=30s"></iframe>`;
  render.shell(`
    <div class="card">
      <div class="row" style="justify-content:space-between;align-items:center">
        <h3 style="margin:0">Stats</h3>
        <a class="link" href="${base}/d/kine-overview" target="_blank"
           rel="noopener noreferrer">Open Grafana</a>
      </div>
      <div class="panel-grid" style="margin-top:.75rem">
        ${panel(1)}${panel(2)}${panel(3)}${panel(4)}
        ${panel(7, 'tall')}${panel(8, 'tall')}
        ${panel(9, 'tall')}${panel(10, 'tall')}
      </div>
    </div>`);
};
```

- [ ] **Step 6: Draw the sparklines**

Add the helper near the other formatting helpers:

```javascript
const sparkline = (values, w = 100, h = 26) => {
  if (!values || values.length < 2) return '';
  const max = Math.max(...values, 0.0001);
  const step = w / (values.length - 1);
  const points = values.map((v, i) => [i * step, h - (v / max) * (h - 2)]);
  const line = points.map(([x, y], i) =>
    `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">` +
    `<path class="fill" d="${line} L${w},${h} L0,${h} Z"></path>` +
    `<path d="${line}"></path></svg>`;
};
```

In `render.apps`, after the cards are in the DOM, fetch the series and inject them without blocking the page:

```javascript
  api('/stats/cards').then(stats => {
    Object.entries(stats.apps || {}).forEach(([id, series]) => {
      const host = document.querySelector(`[data-spark="${id}"]`);
      if (host) host.innerHTML = sparkline(series.cpu);
    });
  }).catch(() => {});
```

Add `<div data-spark="${a.id}"></div>` inside each app card's body, below the existing status line, so the injection has a target.

- [ ] **Step 7: Run the tests**

Run: `python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add helm/frontend/index.html tests/test_frontend.py
git commit -m "Add a Stats page and per-app sparklines to Helm."
```

---

### Task 11: Local verification and documentation

**Files:**
- Create: `/tmp/kine-metrics-check/` (throwaway, not committed)
- Modify: `README.md`

- [ ] **Step 1: Stand up Grafana and Prometheus against a stub exporter**

Write a throwaway compose file in `/tmp/kine-metrics-check/` with Grafana and Prometheus using the same environment and provisioning mounts as the real fragments, pointed at `provision/assets/grafana/dashboards` and a stub `/metrics` served by `python -m http.server` from a file containing a handful of `kine_*` samples. Bring it up on a spare port.

- [ ] **Step 2: Confirm the dashboards load clean**

Open each dashboard and check Grafana's log for provisioning errors:

Run: `docker compose -f /tmp/kine-metrics-check/compose.yml logs grafana | grep -i "error\|failed" || echo CLEAN`
Expected: `CLEAN`.

- [ ] **Step 3: Screenshot the overview dashboard**

Load `http://localhost:<port>/d/kine-overview` in a browser and capture it, so the panel layout can be judged before it reaches the appliance. Fix any panel that renders as "No data" for a metric the stub does export — that means the expression is wrong, not the data.

- [ ] **Step 4: Tear the stub down**

```bash
docker compose -f /tmp/kine-metrics-check/compose.yml down -v
rm -rf /tmp/kine-metrics-check
```

- [ ] **Step 5: Document it**

Add to `README.md`, in the same voice as the surrounding sections: a Metrics tier entry describing what the four containers do and that it is off by default; a note that Grafana is anonymously readable so Helm can embed panels; and a line about the Stats page and app-card sparklines.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "Document the Metrics tier and Stats page."
```

---

### Task 12: Deploy to osiris

- [ ] **Step 1: Push**

```bash
git push origin master
```

- [ ] **Step 2: Pull on the appliance**

Run over ssh on osiris in the kine checkout: `git pull`, resolving any locally-rsynced files with `git stash push -u` first if the pull refuses.

- [ ] **Step 3: Seed the metrics config**

Run: `docker compose run --rm provision seed`
Expected: log lines `prometheus: wrote scrape config` and `grafana: provisioned datasource and 4 dashboards`.

- [ ] **Step 4: Enable the tier from Helm**

Turn on Metrics in the Helm UI, then confirm all four containers are healthy:

Run: `docker compose ps --format json | grep -c healthy`

- [ ] **Step 5: Confirm the scrape targets are up**

Run: `docker compose exec prometheus wget -qO- 'http://localhost:9090/api/v1/targets?state=active' | grep -o '"health":"[a-z]*"' | sort | uniq -c`
Expected: every target `"health":"up"`. If `kine` is down, check `docker compose exec prometheus wget -qO- http://helm:8600/api/metrics` returns text.

- [ ] **Step 6: Check cAdvisor's own cost**

Run: `docker stats --no-stream kine-cadvisor kine-prometheus`
Expected: cAdvisor comfortably under one core. If not, raise `--housekeeping_interval` to 30s and add `--disable_metrics=percpu,sched,tcp,udp,process`, then commit that change.

- [ ] **Step 7: Confirm the GUI**

Open Helm, check the Stats tab renders panels with data and that app cards show sparklines. Screenshot for the record.

---

## Self-Review

**Spec coverage:** Metrics tier and four containers — Task 6. Transitive deps — Task 1. Helm exporter with the full metric contract — Tasks 2-5. Auth exemption — Task 5 (per-route dependency, so simply omitted). Seeding of Prometheus and Grafana config — Task 7. Four dashboards with fixed UIDs — Task 8. Stats page and sparklines — Tasks 9-10. Testing — folded into every task, with the dashboard metric cross-check in Task 8. Risks: cAdvisor cost is measured in Task 12 Step 6; the socket-proxy fallback is Task 6 Step 6 and Task 12 Step 4; hwmon emptiness is documented on the panel in Task 8 Step 5. Docs — Task 11.

**Known ordering wrinkle:** `test_metrics_seed_copies_dashboards` (Task 7) passes only once Task 8 has written the dashboards. Called out in Task 7 Step 5 rather than left to surprise the implementer.

**Type consistency:** `Sample` is used identically in Tasks 3-5. `METRIC_TYPES` is defined in Task 3 and consumed by the Task 8 test. `appkeys.key_for` / `arr_key` / `bazarr_key` are defined in Task 2 and used with those exact names in Task 5. `parse_range` and `card_series` are defined and consumed consistently in Task 9. Panel ids 1, 2, 7, 8, 9, 10 embedded in Task 10 all exist in the Task 8 overview panel table.
