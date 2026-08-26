# Multi-Gluetun Egress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let operators assign forced-tunnel apps to different VPN profiles so multiple Gluetun tunnels can run concurrently, with leftovers on an explicit primary.

**Architecture:** Extend `vpn-profiles.json` with `primary_id` + per-profile `apps[]`. Helm regenerates `compose/vpn-routing.override.yml` (secondaries, `network_mode` overrides, Traefik labels). Primary stays `gluetun`; secondaries are `gluetun_<shortId>`. Provision and heal use `tunnel_service(app)`.

**Tech Stack:** FastAPI/Helm, Docker Compose includes, Gluetun, vanilla JS, pytest, PyYAML (already available via compose stack tooling — prefer stdlib where enough; use `yaml` only if already a Helm dependency, else emit YAML with careful string templates).

**Spec:** `docs/superpowers/specs/2026-08-26-multi-gluetun-egress-design.md`

## Global Constraints

- Profiles file: `${STACK_ROOT}/config/helm/vpn-profiles.json`.
- Generated routing: repo path `compose/vpn-routing.override.yml` (always included; committed stub `services: {}`; Helm overwrites).
- Primary service name is always `gluetun` / `kine-gluetun`.
- Secondary: `gluetun_<shortId>` / `kine-gluetun-<shortId>` where `shortId` = first 8 hex chars of profile UUID (lowercase).
- Secondary conf dir: `${STACK_ROOT}/config/gluetun-<shortId>/`.
- Soft UI warn when concurrent running tunnels > 3; do not hard-block.
- Live-TV affinity: `dispatcharr`, `ecm`, `teamarr` must share the same tunnel (same profile or all leftovers/primary). Reject saves that split them.
- Never log PrivateKey / full conf bodies.
- OpenVPN profiles remain non-runnable.
- TDD: failing test → implement → pass → commit per task.
- Run pytest from repo root with `.venv/bin/pytest`.

## File map

| File | Responsibility |
|---|---|
| `helm/backend/app/vpn_profiles.py` | Schema migrate, primary/apps CRUD, `tunnel_service`, exclusivity, affinity |
| `helm/backend/app/vpn_routing.py` | Generate override YAML; Traefik label map; secondary service defs |
| `helm/backend/app/wireguard.py` | Reuse `parse_conf` / write helpers (extend write path for secondary dirs) |
| `helm/backend/app/tunnel_heal.py` | Heal per tunnel service name |
| `helm/backend/app/main.py` | VPN API: set primary, set apps, apply routing; status shows per-tunnel |
| `helm/backend/app/compose.py` | Unchanged `run`; callers pass regenerated file via include |
| `compose/vpn-routing.override.yml` | Stub + generated content |
| `compose/vpn.gluetun.yml` | Remove static Traefik app routers (labels move to generator) |
| `docker-compose.yml` | Include override; update “no generation” comment |
| `provision/` recipes + shared helper | Resolve `http://{tunnel}:port` |
| `helm/frontend/index.html` | Primary, apps checklist, per-tunnel detail |
| `docs/port-map.md` | Document multi-tunnel addressing |
| `tests/test_vpn_profiles.py` | Extend |
| `tests/test_vpn_routing.py` | New |
| `tests/test_tunnel_heal.py` | Extend if present / add |

---

### Task 1: Schema migration + `tunnel_service`

**Files:**
- Modify: `helm/backend/app/vpn_profiles.py`
- Modify: `tests/test_vpn_profiles.py`

**Interfaces:**
- Produces:
  - `short_id(profile_id: str) -> str`  # 8 hex chars
  - `migrate_schema(data: dict) -> dict`  # active_id→primary_id, apps defaults
  - `load` / `migrate_from_wg0` return `{primary_id, profiles}` (no `active_id`)
  - `tunnel_service(data: dict, app_id: str) -> str`  # `gluetun` or `gluetun_<short>`
  - `summary` exposes `primary: bool`, `apps: list[str]` (not conf)

- [ ] **Step 1: Write failing tests**

```python
def test_migrate_schema_active_id_to_primary():
    raw = {
        "active_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "profiles": [{
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "name": "Default",
            "type": "wireguard",
            "conf": "x",
            "updated_at": "t",
        }],
    }
    data = vpn_profiles.migrate_schema(raw)
    assert data["primary_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert "active_id" not in data
    assert data["profiles"][0]["apps"] == []


def test_tunnel_service_leftovers_use_primary():
    data = {
        "primary_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "profiles": [
            {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "apps": []},
            {"id": "11111111-2222-3333-4444-555555555555", "apps": ["dispatcharr"]},
        ],
    }
    assert vpn_profiles.tunnel_service(data, "sonarr") == "gluetun"
    assert vpn_profiles.tunnel_service(data, "dispatcharr") == "gluetun_11111111"


def test_short_id():
    assert vpn_profiles.short_id("11111111-2222-3333-4444-555555555555") == "11111111"
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `.venv/bin/pytest tests/test_vpn_profiles.py::test_migrate_schema_active_id_to_primary tests/test_vpn_profiles.py::test_tunnel_service_leftovers_use_primary tests/test_vpn_profiles.py::test_short_id -v`

- [ ] **Step 3: Implement**

In `vpn_profiles.py`:

- `short_id`: strip UUID hyphens, take first 8 lowercase hex chars (if non-uuid, sanitize to `[a-z0-9]` and pad/trim to 8).
- `migrate_schema`: copy dict; if `primary_id` missing and `active_id` set, copy; pop `active_id`; ensure each profile has `apps: list`; if `primary_id` still missing and profiles non-empty, set to `profiles[0]["id"]`.
- Call `migrate_schema` at end of `load` and `migrate_from_wg0`.
- Update `empty()`, `summary()`, `add_profile` (set `apps: []`, use `primary_id` instead of `active_id`), `delete_profile` (block deleting primary), `prepare_activate` → rename conceptually later; for now keep writing `primary_id` when rematerializing primary only.
- `tunnel_service(data, app_id)`: find profile whose `apps` contains `app_id`; if none → `gluetun`; if that profile id == `primary_id` → `gluetun`; else → `gluetun_{short_id(id)}`.

- [ ] **Step 4: Update existing tests** that assert `active_id` to use `primary_id` / `apps`.

- [ ] **Step 5: PASS + commit**

```bash
.venv/bin/pytest tests/test_vpn_profiles.py -q
git add helm/backend/app/vpn_profiles.py tests/test_vpn_profiles.py
git commit -m "Add VPN profile primary_id, apps list, and tunnel_service helper."
```

---

### Task 2: Set primary, set apps, affinity validation

**Files:**
- Modify: `helm/backend/app/vpn_profiles.py`
- Modify: `tests/test_vpn_profiles.py`

**Interfaces:**
- Produces:
  - `LIVE_TV_AFFINITY = ("dispatcharr", "ecm", "teamarr")`
  - `set_primary(stack_root, profile_id) -> dict`
  - `set_profile_apps(stack_root, profile_id, apps: list[str], *, forced: set[str]) -> dict`
  - Raises `ValueError` on affinity split or unknown app

- [ ] **Step 1: Failing tests**

```python
def test_set_profile_apps_moves_exclusively(tmp_path):
    # create two profiles via add_profile; set apps on B including sonarr
    # assert sonarr only on B; not on primary apps


def test_set_profile_apps_rejects_live_tv_split(tmp_path):
    # assign dispatcharr to secondary without ecm/teamarr (or with them on primary)
    # expect ValueError matching /affinity|live.?tv|together/i
```

- [ ] **Step 2: FAIL then implement**

`set_profile_apps`:

1. Normalize apps to unique list intersecting `forced`.
2. Build proposed assignment map; ensure `LIVE_TV_AFFINITY` apps that appear in `forced` ∩ enabled sense all map to the same tunnel owner (same profile id, or all unassigned→primary). Simplest rule: the set of affinity apps included in the new global assignment must all be on one profile’s list or all absent from every list.
3. Remove those apps from every other profile; set on target; save.

`set_primary`: update `primary_id`; save; cannot point at missing id.

- [ ] **Step 3: PASS + commit**

```bash
.venv/bin/pytest tests/test_vpn_profiles.py -q
git add helm/backend/app/vpn_profiles.py tests/test_vpn_profiles.py
git commit -m "Validate VPN app assignments and support set_primary/set_apps."
```

---

### Task 3: Routing YAML generator

**Files:**
- Create: `helm/backend/app/vpn_routing.py`
- Create: `tests/test_vpn_routing.py`
- Create: `compose/vpn-routing.override.yml` stub (`services: {}`)

**Interfaces:**
- Consumes: `vpn_profiles.tunnel_service`, `short_id`, profile conf via `wireguard.parse_conf`
- Produces:
  - `ROUTING_REL = pathlib.Path("compose/vpn-routing.override.yml")`  # under KINE_REPO
  - `APP_PORTS: dict[str, int]`  # sonarr 8989, radarr 7878, … (from port-map)
  - `APP_TRAEFIK_HOST: dict[str, str]`  # sonarr → `sonarr`, dispatcharr → `tv`, …
  - `render_override(data, *, enabled_apps: set[str], stack_root: str, domain_keys) -> str`
  - `write_override(repo: Path, text: str) -> Path`

`domain_keys` = values needed for Traefik Host rules: `KINE_DOMAIN`, `KINE_LOCAL_DOMAIN` from env/config.

- [ ] **Step 1: Failing tests**

```python
def test_render_override_secondary_and_network_mode():
    data = {
        "primary_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "profiles": [
            {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "apps": [], "conf": VALID_WG, "type": "wireguard"},
            {"id": "11111111-2222-3333-4444-555555555555", "apps": ["dispatcharr"], "conf": VALID_WG, "type": "wireguard"},
        ],
    }
    text = vpn_routing.render_override(
        data,
        enabled_apps={"dispatcharr", "sonarr", "gluetun"},
        stack_root="/srv/kine",
        kine_domain="example.com",
        kine_local_domain="kine.local",
    )
    assert "gluetun_11111111:" in text
    assert "network_mode: service:gluetun_11111111" in text
    assert "dispatcharr" in text
    assert "service:gluetun\n" in text or "service:gluetun\"" in text or 'service:gluetun' in text
    # sonarr leftover on primary
    assert "sonarr:" in text
    assert text.index("sonarr:") < text.index("network_mode:") or "sonarr" in text
```

Prefer asserting structured fragments:

```python
assert "kine-gluetun-11111111" in text
assert "traefik.http.routers.dispatcharr" in text
assert "traefik.http.routers.sonarr" in text
# dispatcharr labels appear under secondary service block — check order
sec = text.split("gluetun_11111111:", 1)[1].split("\n  sonarr:", 1)[0]
assert "traefik.http.routers.dispatcharr" in sec
prim_labels_owner = "gluetun:"  # primary service may only be referenced via app overrides; 
# Traefik for sonarr must be on primary gluetun service extension
```

Design the rendered YAML so:

```yaml
services:
  gluetun:
    labels:
      - traefik.http.routers.sonarr.rule=...
      ...
  gluetun_11111111:
    image: qmcgaw/gluetun:${GLUETUN_TAG}
    ...
    labels: [dispatcharr routers]
  sonarr:
    network_mode: service:gluetun
    depends_on:
      gluetun:
        condition: service_healthy
  dispatcharr:
    network_mode: service:gluetun_11111111
    depends_on:
      gluetun_11111111:
        condition: service_healthy
```

Primary `gluetun:` entry in the override **only** carries dynamic Traefik labels (merge with base service). Secondary is a full service definition (image, env from `parse_conf`, volume, networks, healthcheck, labels).

- [ ] **Step 2: Implement `vpn_routing.py`** using string templates or PyYAML if `yaml` is already importable in Helm image (`python -c "import yaml"`). Prefer PyYAML if present in `helm/backend/requirements.txt`; otherwise add it or emit YAML manually carefully.

Secondary env must set `VPN_SERVICE_PROVIDER=custom`, `VPN_TYPE=wireguard`, and `WIREGUARD_*` from `parse_conf` — **do not** rely on primary `.env` keys.

- [ ] **Step 3: PASS + commit stub + module**

```bash
.venv/bin/pytest tests/test_vpn_routing.py -q
git add helm/backend/app/vpn_routing.py tests/test_vpn_routing.py compose/vpn-routing.override.yml
git commit -m "Generate compose override for secondary Gluetun tunnels."
```

---

### Task 4: Include override; move Traefik labels off static gluetun

**Files:**
- Modify: `docker-compose.yml`
- Modify: `compose/vpn.gluetun.yml` (remove app router labels lines 66–105; leave comment pointing at generator)
- Modify: `tests/test_stack.py` (assert include + no static sonarr router on gluetun file)

- [ ] **Step 1: Failing tests**

```python
def test_compose_includes_vpn_routing_override():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "compose/vpn-routing.override.yml" in text


def test_static_gluetun_has_no_app_traefik_routers():
    text = (ROOT / "compose" / "vpn.gluetun.yml").read_text()
    assert "traefik.http.routers.sonarr" not in text
    assert "vpn-routing.override" in text or "generated" in text.lower()
```

- [ ] **Step 2: Implement** — add include after `compose/vpn.gluetun.yml`; strip labels; update top comment in `docker-compose.yml` to note routing override is generated by Helm from VPN profiles.

- [ ] **Step 3: Ensure boot still works when override is stub** — generator must be invoked on Helm startup / VPN GET so labels exist before Traefik needs them. Add a note for Task 5 to call `ensure_routing` on VPN migrate.

- [ ] **Step 4: PASS + commit**

```bash
.venv/bin/pytest tests/test_stack.py -k vpn_routing -q
git add docker-compose.yml compose/vpn.gluetun.yml tests/test_stack.py
git commit -m "Include generated VPN routing override and drop static Traefik app labels."
```

---

### Task 5: Apply orchestration (write confs + override + recreate groups)

**Files:**
- Modify: `helm/backend/app/wireguard.py` — add `write_gluetun_conf_at(text, conf_dir: Path)` or `write_secondary_conf(stack_root, short_id, text)`
- Create helpers in `vpn_routing.py` or `main.py`: `async def apply_vpn_routing(...)`
- Modify: `tests/test_vpn_routing.py` / new `tests/test_vpn_apply.py` for pure functions; mock compose in unit tests

**Interfaces:**
- Produces:
  - `running_secondaries(data) -> list[tuple[profile, service_name]]`
  - `peers_for(data, service: str, enabled: set[str]) -> list[str]`
  - `apply_routing_sync(stack_root, repo, data, enabled) -> None`  # filesystem only
  - API/main calls compose recreate after

- [ ] **Step 1: Tests for peers_for and write paths**

```python
def test_peers_for_secondary():
    data = {... dispatcharr on secondary ...}
    assert vpn_routing.peers_for(data, "gluetun_11111111", {"dispatcharr", "sonarr"}) == ["dispatcharr"]
    assert "sonarr" in vpn_routing.peers_for(data, "gluetun", {"dispatcharr", "sonarr"})
```

- [ ] **Step 2: Implement filesystem apply** — write primary wg0 via existing helper; for each secondary with apps, mkdir config dir, write wg0, render+write override.

- [ ] **Step 3: Wire `apply_vpn_routing` in `main.py`** after set_apps / set_primary / rematerialize / enable VPN:

```python
await asyncio.to_thread(vpn_routing.apply_filesystem, ...)
# recreate each affected group
for svc in affected_services:
    peers = vpn_routing.peers_for(...)
    await compose.run("up", "-d", "--force-recreate", svc, *peers, timeout=300)
```

Also call filesystem apply + ensure override on `migrate_from_wg0` path during `GET /api/vpn` so Traefik labels exist after Task 4.

- [ ] **Step 4: PASS + commit**

```bash
.venv/bin/pytest tests/test_vpn_routing.py tests/test_vpn_profiles.py -q
git add helm/backend/app/wireguard.py helm/backend/app/vpn_routing.py helm/backend/app/main.py tests/
git commit -m "Apply VPN routing override and recreate per-tunnel groups."
```

---

### Task 6: VPN API + status payload for multi-tunnel

**Files:**
- Modify: `helm/backend/app/main.py`
- Modify: `tests/test_vpn_profiles.py` (route string asserts)

**Interfaces:**
- `GET /api/vpn` returns profiles with `primary`, `apps`, and optional `tunnel: {service, public_ip, forwarded_port, enabled}` per running tunnel
- `POST /api/vpn/profiles/{id}/primary`
- `PUT /api/vpn/profiles/{id}/apps` body `{"apps": ["sonarr", ...]}`
- Keep rematerialize endpoint (former activate) writing conf for that profile’s slot then `apply_vpn_routing`
- Leak test / restart accept optional `?tunnel=` or body `service` defaulting to primary `gluetun`

- [ ] **Step 1: Failing route presence tests**

```python
def test_vpn_apps_and_primary_routes_exist():
    main = (ROOT / "helm/backend/app/main.py").read_text()
    assert '/profiles/{profile_id}/apps' in main
    assert '/profiles/{profile_id}/primary' in main
```

- [ ] **Step 2: Implement routes**; on apps/primary success run apply + return `{ok, log}`.

- [ ] **Step 3: PASS + commit**

```bash
.venv/bin/pytest tests/test_vpn_profiles.py -q
git add helm/backend/app/main.py tests/test_vpn_profiles.py
git commit -m "Expose VPN primary and per-profile app assignment APIs."
```

---

### Task 7: Multi-tunnel heal

**Files:**
- Modify: `helm/backend/app/tunnel_heal.py`
- Modify/create: `tests/test_tunnel_heal.py`

**Interfaces:**
- Change `orphan_services` to accept `expected_id` + peers for one tunnel; add `heal_all(tunnels: dict[str, set[str]])` iterating running gluetun* services.

- [ ] **Step 1: Failing test** — two tunnels, peer pinned to wrong id for secondary only → only that peer listed.

- [ ] **Step 2: Implement** — discover containers matching `kine-gluetun` and `kine-gluetun-*`; map to service names; heal each.

- [ ] **Step 3: PASS + commit**

```bash
.venv/bin/pytest tests/test_tunnel_heal.py -q
git add helm/backend/app/tunnel_heal.py tests/test_tunnel_heal.py
git commit -m "Heal orphaned peers per Gluetun tunnel service."
```

---

### Task 8: Provision / internal URL helper

**Files:**
- Create: `provision/tunnel_hosts.py` (or under `helm` and share — prefer `provision/tunnel_hosts.py` duplicated thin wrapper importing JSON load logic, OR put shared module under `provision/` and have Helm duplicate call via HTTP only). Simplest: implement `tunnel_service` resolution in provision by reading the same JSON path `/stack/config/helm/vpn-profiles.json`.
- Modify recipes that hardcode `http://gluetun:` — at minimum: Seerr wire, dispatcharr HDHR constants used from Helm (`dispatcharr_sources`, `scheduler` Emby URL), library_rescan catalogue internals if read from YAML.

**Catalogue note:** `catalogue.yml` `internal:` URLs stay as documentation defaults; runtime code that needs the live host must call the helper.

- [ ] **Step 1: Failing test** in `tests/test_tunnel_hosts.py`

```python
def test_internal_base_uses_secondary():
    data = {...}
    assert tunnel_hosts.internal_base(data, "dispatcharr", 9191) == "http://gluetun_11111111:9191"
```

- [ ] **Step 2: Implement + update Helm call sites** that use `http://gluetun:9191` for Emby/Dispatcharr wiring to resolve dynamically at call time.

- [ ] **Step 3: Update provision recipes** similarly (`provision/recipes/dispatcharr.py`, seerr wiring, etc.).

- [ ] **Step 4: Document Live-TV loopback** — ECM/Teamarr keep `http://127.0.0.1:9191` **only** because affinity keeps them on the same namespace as Dispatcharr.

- [ ] **Step 5: PASS + commit**

```bash
.venv/bin/pytest tests/test_tunnel_hosts.py tests/test_vpn_profiles.py -q
git add provision/ helm/backend/app/ tests/
git commit -m "Resolve tunnelled app base URLs from VPN profile assignment."
```

---

### Task 9: Frontend — primary, apps checklist, per-tunnel actions

**Files:**
- Modify: `helm/frontend/index.html` (`render.vpn`)
- Modify: `tests/test_vpn_profiles.py` / `tests/test_frontend.py` string asserts

- [ ] **Step 1: Failing UI tests**

```python
def test_vpn_ui_has_apps_checklist_and_primary():
    fe = (ROOT / "helm/frontend/index.html").read_text()
    assert "data-vpn-primary" in fe or "/primary" in fe
    assert "data-vpn-apps" in fe or "/apps" in fe
    assert "vpn-app-check" in fe or "forcedTunnelApps" in fe
```

- [ ] **Step 2: Implement UI**

- Show **Primary** badge; button **Set as Primary** on others.
- On each profile: multi-select checkboxes of forced-tunnel apps (from `/api/vpn` include `assignable_apps: [{id,name}]`).
- Save apps → `PUT .../apps` then refresh.
- Soft banner if `tunnels_running > 3`.
- Leak/Restart use that card’s `tunnel.service`.
- Rematerialize replaces Activate label when already primary/secondary running.

- [ ] **Step 3: PASS + commit**

```bash
.venv/bin/pytest tests/test_vpn_profiles.py tests/test_frontend.py -k vpn -q
git add helm/frontend/index.html tests/
git commit -m "Add VPN profile app checklist and primary controls to Helm UI."
```

---

### Task 10: Docs + acceptance checklist

**Files:**
- Modify: `docs/port-map.md`
- Modify: `README.md` VPN section (short paragraph on multi-tunnel)
- Modify: spec status line to “approved / implemented” only after manual verify (leave “in progress” until done)

- [ ] **Step 1: Update port-map** — explain per-tunnel `gluetun_<id>:<port>` and that Traefik labels follow the owning tunnel.

- [ ] **Step 2: Commit docs**

```bash
git add docs/port-map.md README.md
git commit -m "Document multi-Gluetun addressing for tunnelled apps."
```

- [ ] **Step 3: Manual acceptance on a test host** (osiris or worktree deploy)

1. Migrate: existing profiles show one Primary; all apps leftovers.
2. Assign Dispatcharr+ECM+Teamarr to Proton secondary; save; confirm secondary container up; Traefik `tv.` still works; leak test IPs differ.
3. Split Live TV affinity → UI/API error.
4. Disable VPN → secondaries stop; override regenerates safely.
5. Re-enable → primary up; secondaries return with apps.

---

## Spec coverage check

| Spec requirement | Task |
|---|---|
| `primary_id` + `apps[]` | 1–2 |
| Leftovers on primary | 1 |
| Dynamic override secondaries | 3–5 |
| Traefik labels on owning tunnel | 3–4 |
| `tunnel_service` helper | 1, 8 |
| Helm checklist UX | 9 |
| Migration active_id | 1 |
| Heal per tunnel | 7 |
| Provision URLs | 8 |
| Soft warn >3 | 9 |
| Live-TV co-location (loopback safety) | 2, 8 |
| Regenerate routing (not backup source of truth) | 5 |

## Placeholder scan

None intentional. If PyYAML is missing from Helm requirements, Task 3 must either add `PyYAML` to `helm/backend/requirements.txt` or emit YAML via templates — decide in Task 3 Step 2 by checking the requirements file before coding.
