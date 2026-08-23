# Grafana metrics for kine

Date: 2026-08-23
Status: approved, ready for implementation planning

## Goal

Give kine a monitoring tier: Prometheus-backed history for both machine
resources and application activity, rendered as Grafana dashboards, with
the best of those graphs embedded directly in the Helm GUI.

Two things are being measured, and they come from different places:

- **Infrastructure** — per-container CPU, memory, network and disk, plus
  host CPU, memory, filesystems, network and drive temperatures. Off the
  shelf, from cAdvisor and node-exporter.
- **Applications** — streams playing, library sizes, download queues,
  subtitles wanted, indexer activity, pending image updates. Exported by
  Helm itself, because Helm already resolves every app's API key and
  already talks to Plex and Emby.

## Decisions

| Question | Decision |
|---|---|
| What is graphed | Infrastructure and application stats. Logs are out of scope. |
| Enablement | New `metrics` tier, off by default, toggled in Helm. |
| Grafana auth | Anonymous Viewer; admin account for editing. |
| Embedding | Stats page in Helm with embedded panels, plus sparklines on app cards. |
| App stats source | Helm `/api/metrics`, not Exportarr sidecars. |

Anonymous access was chosen because it is what makes iframe embedding
work without a login dance. The consequence is that anyone who can reach
`grafana.${KINE_DOMAIN}` sees the dashboards — the same exposure as every
other app behind Traefik, but a deliberate choice rather than an
accident.

Exportarr was rejected because it would add four to six containers, each
needing an API key already known to Helm, and would still not cover Plex
or Emby. It remains a later option if per-indexer *arr internals are ever
wanted.

## Architecture

Four new containers in a new `metrics` tier.

```
cadvisor ──┐
           ├──> prometheus <── grafana ──> browser (grafana.DOMAIN)
node-exp ──┤                      ^
           │                      └────── Helm Stats page (iframes)
helm ──────┘                              Helm app cards (sparklines,
  /api/metrics                             via Helm -> Prometheus API)
```

### Compose fragments

Named by tier prefix, matching the existing `acq.` / `media.` / `live.`
convention. All four are added to `include:` in `docker-compose.yml`, and
each declares `profiles: ["<name>"]` and `container_name: kine-<name>`.

**`compose/metrics.prometheus.yml`** — `prom/prometheus:${PROMETHEUS_TAG}`
on `kine_internal` only. No Traefik router and no `kine_edge`: Grafana and
Helm are its only clients, so it needs no certificate and no exposure.
Retention is capped on both axes, `--storage.tsdb.retention.time=30d` and
`--storage.tsdb.retention.size=2GB`, so it cannot quietly fill the media
disk. Config at `${STACK_ROOT}/config/prometheus/prometheus.yml`, TSDB at
`${STACK_ROOT}/config/prometheus/data`. Healthcheck hits `/-/healthy`.

**`compose/metrics.cadvisor.yml`** — `gcr.io/cadvisor/cadvisor:${CADVISOR_TAG}`
on `kine_internal` and `kine_ctrl`. Read-only `/sys`, `/rootfs` and
`/var/lib/docker`, and it reaches Docker through the existing socket
proxy with `--docker=tcp://dockerproxy:2375` rather than mounting the
socket, preserving the rule the stack already enforces for Traefik and
Helm. `dockerproxy` already permits `CONTAINERS`, `IMAGES`, `INFO` and
`EVENTS`, which is what cAdvisor needs. Run with `--docker_only=true`,
`--housekeeping_interval=15s` and `--store_container_labels=false`,
because cAdvisor's defaults are expensive on a box that also transcodes.

*Fallback:* if cAdvisor's Docker client cannot negotiate through the
proxy, mount `/var/run/docker.sock:ro` instead and record why in the
fragment's comments. Verify this early — it gates the fragment's shape.

**`compose/metrics.node-exporter.yml`** — `prom/node-exporter:${NODE_EXPORTER_TAG}`
with `pid: host` and read-only `/proc`, `/sys`, `/rootfs`, on
`kine_internal`.

**`compose/metrics.grafana.yml`** — `grafana/grafana:${GRAFANA_TAG}` on
`kine_internal` and `kine_edge`, routed at `grafana.${KINE_DOMAIN}` and
`grafana.${KINE_LOCAL_DOMAIN}`, service port 3000. Environment:
`GF_AUTH_ANONYMOUS_ENABLED=true`, `GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer`,
`GF_SECURITY_ALLOW_EMBEDDING=true`, `GF_USERS_DEFAULT_THEME=dark`,
`GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}`, and
`GF_SERVER_ROOT_URL` built from `KINE_DOMAIN`.

### Catalogue and tier

`TIER_LABELS` gains `"metrics": "Metrics"`. Catalogue entries:

- `grafana` — visible, `default: true`, `subdomain: grafana`,
  `requires: [prometheus]`.
- `prometheus` — `hidden: true`, `requires: [cadvisor, node-exporter]`.
- `cadvisor`, `node-exporter` — `hidden: true`.

Hidden apps are excluded from `tier_default_apps`, so the tier toggle
enables Grafana and dependency resolution must pull in the rest.

**`resolve_deps` must become transitive.** It currently walks one level
of `requires` and is called once per default app, so `grafana ->
prometheus -> cadvisor` would stop at Prometheus and the exporters would
never start. Fix it to iterate until the wanted set stops growing, and
add a regression test for a two-hop chain. This is a real latent bug, not
just a blocker for this feature; the alternative — flattening Grafana's
`requires` to list all three — hides it instead of fixing it.

### Environment

`.env.example` gains `PROMETHEUS_TAG`/`_DIGEST`, `CADVISOR_TAG`/`_DIGEST`,
`NODE_EXPORTER_TAG`/`_DIGEST`, `GRAFANA_TAG`/`_DIGEST` so the existing
updater manages them like every other app, plus `GRAFANA_ADMIN_PASSWORD`.
`COMPOSE_PROFILES` stays `mdns` on a fresh install.

## Application metrics

New module `helm/backend/app/metrics.py`.

**Refactor first:** `library_rescan.py` (~540 lines) owns `_arr_key`,
`_bazarr_key` and the `_arr_get` helpers. The exporter is a second
consumer, so lift key resolution into a small `helm/backend/app/appkeys.py`
and have both import it. No behaviour change.

**Collection is decoupled from scraping.** A collector runs on the
existing scheduler tick every 60 seconds, queries each *enabled* app, and
writes into a module-level cache. `GET /api/metrics` renders the cache and
returns immediately, so a hung Sonarr cannot stall a scrape or punch a gap
in the graphs. This mirrors how the update checker already works. Every
per-app collection is individually guarded so one failure does not lose
the whole sample set.

Output is Prometheus text format written by hand — roughly twenty lines of
string formatting, avoiding a `prometheus_client` dependency.

### Metric contract

Gauges:

| Metric | Labels | Source |
|---|---|---|
| `kine_streams_active` | `server`, `state`, `user` | `watching.snapshot()` |
| `kine_app_up` | `app` | collector probe of the catalogue `internal` URL |
| `kine_update_pending` | `app` | `updates_info` cache |
| `kine_library_items` | `app`, `kind` | Sonarr, Radarr |
| `kine_library_missing` | `app`, `kind` | Sonarr, Radarr |
| `kine_library_bytes` | `app` | Sonarr, Radarr |
| `kine_queue_items` | `app` | Sonarr, Radarr |
| `kine_subtitles_wanted` | `kind` | Bazarr |
| `kine_indexers_enabled` | `app` | Prowlarr |
| `kine_download_rate_bytes` | `client`, `direction` | Transmission RPC |
| `kine_torrents` | `client`, `state` | Transmission RPC |
| `kine_collect_duration_seconds` | `app` | collector |

Counters: `kine_indexer_queries_total{app}`,
`kine_indexer_grabs_total{app}`, `kine_collect_errors_total{app}`.

`kind` is `series`, `episodes` or `movies`. `state` on streams is
`playing` or `paused`; on torrents it is Transmission's status name.
`direction` is `down` or `up`.

The `user` label on streams is what makes the per-user breakdown on the
media dashboard possible. Its cardinality is bounded by the household, so
the usual warning about per-user labels does not bite here; panels that
do not want it aggregate with `sum by(server)`.

`kine_app_up` is the collector issuing a short-timeout HTTP request to
each enabled app's catalogue `internal` URL, not a re-read of Docker
health status, so it reflects whether the app is actually answering.

Transmission needs the 409 `X-Transmission-Session-Id` handshake before
`session-stats` will answer. It is a few lines and worth it: download rate
is the best live graph in the set.

### Auth exemption

Helm's API sits behind `auth.py`, and Prometheus cannot log in, so
`/api/metrics` is exempt from the session check. It is not routed through
Traefik, so it is reachable only from inside the stack. Helm listens on
8600, so the scrape target is `http://helm:8600/api/metrics`.

## Provisioning

New `provision/recipes/metrics.py`, run in seed mode alongside the other
seeders, writing:

- `config/prometheus/prometheus.yml` — scrape jobs for `cadvisor:8080`
  and `node-exporter:9100` at 15s, and `helm:8600/api/metrics` at 60s to
  match the collector tick.
- `config/grafana/provisioning/datasources/prometheus.yml` — default
  datasource with the fixed UID `kine-prom`. The UID must be fixed because
  dashboards reference it and provisioning fails opaquely otherwise.
- `config/grafana/provisioning/dashboards/kine.yml` — file provider
  pointing at `/etc/grafana/dashboards`.

Dashboard JSON lives in the repo at `provision/assets/grafana/dashboards/`
and is copied to `config/grafana/dashboards`, mounted read-only at
`/etc/grafana/dashboards`. Keeping the JSON in git makes dashboards
reviewable and diffable instead of trapped in `grafana.db`.

Grafana's data directory is a separate mount, `config/grafana/data` ->
`/var/lib/grafana`, so provisioned files never collide with its database.
Grafana runs as uid 472 rather than the stack PUID, so seed sets ownership
explicitly, as it already does for Seerr.

## Dashboards

Four, each with a fixed UID so Helm's embed URLs stay valid.

**`kine-overview`** — the showpiece. A top row of large stat panels with
sparkline area backgrounds: active streams, apps up, host CPU, memory,
total library size, updates pending. Below: streams over time as a stacked
gradient area by server, download rate as a smooth gradient area, the
hungriest containers as a gradient bar gauge, filesystem gauges with
threshold colours, queue depth over time, and a colour-coded table of app
health and pending updates.

**`kine-containers`** — a multi-select container variable driving CPU,
memory working set, network and disk IO, plus a top-N table.

**`kine-host`** — per-core CPU, load, memory breakdown, filesystem gauges,
network throughput, drive temperatures. Temperatures come from
node-exporter's hwmon collector and depend on the host exposing sensors;
if osiris does not, that panel stays empty and the dashboard is still
useful, so it is not worth gating on.

**`kine-media`** — library growth over time, wanted-missing and
subtitles-wanted trends, indexer grab rates, streams by user.

House style throughout: dark theme, gradient fills, smooth interpolation,
shared crosshair, correct units so bytes read as TB and rates as MB/s, and
threshold colours rather than default blue everywhere.

## Helm GUI

**Stats tab** — a fourth entry in `TAB_LABELS` beside Apps, Settings and
Updates. Renders a responsive grid of about six panels embedded from
`kine-overview` via `/d-solo/` in kiosk mode, dark theme, 30s refresh,
with a link out to full Grafana. The iframe hostname reuses the domain
logic `render.apps` already uses for app links, so it behaves the same
locally and remotely. When the Metrics tier is off, the tab explains what
it does and offers a button to enable it rather than showing six broken
frames.

**App card sparklines** — twenty iframes on the Apps page would be
miserable, so `GET /api/stats/cards` makes a single Prometheus range query
for container CPU and memory across all containers (three hours at a
five-minute step) and returns a short series per app. The frontend draws
inline SVG polylines in each card. Containers are named `kine-<app>`, so
mapping series to cards is prefix-stripping; tunnelled apps still report
individually despite sharing gluetun's network namespace. Cached 30s
server-side. If Prometheus is absent or the tier is off, the endpoint
returns nothing and cards render exactly as they do today.

## Testing

- `tests/test_metrics.py` — per-app parsers and the text renderer as pure
  functions over fixture JSON, plus cache behaviour when an app errors.
  No network, mirroring `test_watching.py`.
- `tests/test_dashboards.py` — every dashboard parses, UIDs are unique,
  only `kine-prom` is referenced, and every metric name used in a panel
  expression is one the exporter actually emits. That last check catches
  `kine_stream_active` typos before they become empty panels.
- `tests/test_stack.py` — additions for what the generic invariants miss:
  Prometheus stays off `kine_edge`, cAdvisor does not mount the raw
  socket, Grafana has anonymous access and embedding enabled.
- `tests/test_catalogue.py` — transitive `resolve_deps` regression test.
- `tests/test_frontend.py` — Stats tab present.
- Manual: run Grafana and Prometheus locally against a stub exporter,
  confirm dashboards load without errors, screenshot them.

## Risks

**cAdvisor cost.** Not free on a transcoding box. The `--docker_only` and
15s housekeeping flags keep it modest; measure its own CPU once running
and dial back if needed.

**cAdvisor through the socket proxy.** May not negotiate; fallback is a
read-only socket mount. Verify before building the rest of the fragment.

**Anonymous Grafana.** Accepted trade-off for embedding, stated above.

**Hand-written dashboard JSON.** Error-prone; mitigated by the dashboard
test and local verification.

## Out of scope

Logs and Loki. Exportarr sidecars. Alerting and notifications. Long-term
storage beyond 30 days. Metrics for apps outside the kine stack.
