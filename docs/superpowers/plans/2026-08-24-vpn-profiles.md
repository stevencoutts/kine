# VPN Multi-Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Helm-managed WireGuard VPN profiles with Watching-style UI — add/edit/delete/activate, auto-migrate existing `wg0.conf` to `"Default"`, materialize active profile into gluetun, keep leak test and restart.

**Architecture:** `vpn_profiles.py` owns JSON CRUD + migration + activate/materialize; `main.py` exposes REST; frontend `render.vpn` becomes card UI; OpenVPN is type-only until a later plan.

**Tech Stack:** FastAPI, existing `wireguard.py` / `compose` tunnel-group recreate, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-vpn-profiles-design.md`

## Global Constraints

- Profiles file: `${STACK_ROOT}/config/helm/vpn-profiles.json` (in container: `/stack/config/helm/vpn-profiles.json`).
- Activate materializes via existing `write_gluetun_conf` + `parse_conf` + `config.write` VPN keys.
- Tunnelled apps stay global (`VPN_TUNNELLED_APPS`).
- Never log private keys or full conf bodies.
- OpenVPN `type` allowed in storage/UI but activate returns 400 in MVP.
- Run pytest from repo root; commit after every task.

## File map

| File | Responsibility |
|---|---|
| `helm/backend/app/vpn_profiles.py` | Load/save, migrate, CRUD, activate, redact |
| `helm/backend/app/main.py` | Routes |
| `helm/backend/app/wireguard.py` | Unchanged parser (reuse) |
| `helm/frontend/index.html` | VPN tab UI |
| `tests/test_vpn_profiles.py` | Unit tests |

---

### Task 1: Profiles store + migration

**Files:**
- Create: `helm/backend/app/vpn_profiles.py`
- Test: `tests/test_vpn_profiles.py`

**Interfaces:**
- Produces:
  - `profiles_path(stack_root: str) -> Path`
  - `load(stack_root: str) -> dict`  # `{active_id, profiles}`
  - `save(stack_root: str, data: dict) -> None`
  - `migrate_from_wg0(stack_root: str) -> dict`  # idempotent
  - `redact_conf(conf: str) -> str`
  - `summary(data: dict) -> list[dict]`  # no conf field

- [ ] **Step 1: Failing tests**

```python
import pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import vpn_profiles  # noqa: E402


def test_migrate_imports_wg0_as_default(tmp_path):
    wg = tmp_path / "config" / "gluetun" / "wireguard"
    wg.mkdir(parents=True)
    conf = "[Interface]\nPrivateKey = abc\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = xyz\nEndpoint = 1.2.3.4:51820\n"
    (wg / "wg0.conf").write_text(conf)
    data = vpn_profiles.migrate_from_wg0(str(tmp_path))
    assert data["active_id"]
    assert len(data["profiles"]) == 1
    assert data["profiles"][0]["name"] == "Default"
    assert data["profiles"][0]["type"] == "wireguard"
    assert "PrivateKey = abc" in data["profiles"][0]["conf"]
    # second call no duplicate
    data2 = vpn_profiles.migrate_from_wg0(str(tmp_path))
    assert len(data2["profiles"]) == 1


def test_redact_conf_strips_private_key():
    raw = "[Interface]\nPrivateKey = SECRET\nAddress = 10.0.0.2/32\n"
    out = vpn_profiles.redact_conf(raw)
    assert "SECRET" not in out
    assert "PrivateKey" in out
    assert "Address = 10.0.0.2/32" in out


def test_summary_omits_conf(tmp_path):
    data = {
        "active_id": "1",
        "profiles": [{"id": "1", "name": "Default", "type": "wireguard", "conf": "x", "updated_at": "t"}],
    }
    rows = vpn_profiles.summary(data)
    assert rows[0]["name"] == "Default"
    assert rows[0]["active"] is True
    assert "conf" not in rows[0]
```

- [ ] **Step 2: FAIL then implement**

`migrate_from_wg0`: if profiles file exists, `load` and return. Else if `wg0.conf` exists, create Default profile with uuid4 id, save, return. Else return empty `{active_id: None, profiles: []}`.

- [ ] **Step 3: PASS + commit**

```bash
git commit -am "Add VPN profiles store and wg0 migration."
```

---

### Task 2: CRUD + activate helpers

**Files:**
- Modify: `helm/backend/app/vpn_profiles.py`
- Test: `tests/test_vpn_profiles.py`

**Interfaces:**
- Produces:
  - `add_profile(stack_root, name, conf, type="wireguard") -> dict`
  - `update_profile(stack_root, profile_id, *, name=None, conf=None) -> dict`
  - `delete_profile(stack_root, profile_id) -> None`  # raises ValueError if active
  - `activate_profile(stack_root, profile_id, write_env: callable, write_conf: callable) -> dict`  
    Or activate returns parsed env fields + conf text and `main.py` performs IO — prefer testable pure core:

```python
def prepare_activate(stack_root: str, profile_id: str) -> tuple[str, dict[str, str]]:
    """Return (conf_text, vpn_env_fields) after validation; updates active_id on disk."""
```

Use `wireguard.parse_conf(conf)`; raise `ValueError` on failure; reject `type != wireguard`.

- [ ] **Step 1: Tests for add / delete active refused / prepare_activate**

Use minimal valid WireGuard conf fixture (same shape as `tests` already use for onboarding if any — else inline PrivateKey/Address/Peer PublicKey/Endpoint IP).

- [ ] **Step 2: Implement**

- [ ] **Step 3: Commit**

```bash
git commit -am "Add VPN profile CRUD and activate preparation."
```

---

### Task 3: HTTP routes

**Files:**
- Modify: `helm/backend/app/main.py`
- Test: string/route asserts in `tests/test_vpn_profiles.py` or frontend/backend hybrid test

**Routes:**

```python
@app.get("/api/vpn")
# existing status fields + "profiles": vpn_profiles.summary(migrate_from_wg0(...))

@app.post("/api/vpn/profiles")
# body: {name, conf, type?}

@app.put("/api/vpn/profiles/{profile_id}")
@app.delete("/api/vpn/profiles/{profile_id}")
@app.post("/api/vpn/profiles/{profile_id}/activate")
# prepare_activate → write_gluetun_conf → config.write({VPN_ENABLED: true, **fields}) → recreate tunnel group (reuse vpn_restart group list)

@app.post("/api/vpn/disable")
# optional but recommended: empty_vpn_env, remove conf, VPN_ENABLED=false, active_id=null (keep profiles), recreate/stop gluetun group carefully — match onboarding disable behaviour
```

Stack root: `config.read()["STACK_ROOT"]` mapped to `/stack` inside container (same as elsewhere).

- [ ] **Step 1: Assert routes exist in main.py tests**

- [ ] **Step 2: Implement handlers with HTTPException 400 on ValueError**

- [ ] **Step 3: Commit**

```bash
git commit -am "Expose VPN profile API endpoints."
```

---

### Task 4: Watching-style VPN UI

**Files:**
- Modify: `helm/frontend/index.html` — CSS + `render.vpn`
- Test: `tests/test_frontend.py` asserts for `vpn-card`, `vpn-profiles`, activate button hooks

**UI structure:**

1. Hero card (`.vpn-card.active-hero`) — connection_type, IP, port, tunnelled, Leak Test, Restart, Disable if API exists.
2. Section “Profiles” — `.vpn-cards` list; each `.vpn-card` with name, type pill, ACTIVE badge, Activate / Edit / Delete.
3. Add Profile button → modal (reuse existing modal patterns if any) with name + textarea + file input.
4. Edit → same modal prefilled; on save PUT; if active, confirm rematerialize via activate.

Wire events to new API paths; refresh `render.vpn()` after mutations; show errors in `#out` or banner.

CSS: accent bar, pills (`watch-badge`-like), avoid flat single grey card.

- [ ] **Step 1: Frontend string tests**

- [ ] **Step 2: Implement CSS + render.vpn**

- [ ] **Step 3: Commit**

```bash
git commit -am "Redesign VPN tab with multi-profile cards."
```

---

### Task 5: Onboarding coexistence + README

**Files:**
- Modify: onboarding path only if needed so first-run still writes `wg0.conf` then migration picks it up on first `GET /api/vpn`.
- Modify: `README.md` — VPN profiles paragraph.

Ensure first `GET /api/vpn` always calls `migrate_from_wg0`.

- [ ] **Step 1: README**
- [ ] **Step 2: Commit**

```bash
git commit -am "Document Helm VPN profiles and Default migration."
```

---

## Spec coverage check

| Spec item | Task |
|---|---|
| JSON profiles store | 1 |
| Migrate Default | 1 |
| CRUD + activate | 2, 3 |
| Redact / no keys in list | 1, 3 |
| Materialize wg0 + env | 2, 3 |
| Global tunnelled apps | 3 (unchanged) |
| Hero + profile cards UI | 4 |
| OpenVPN type only | 2 (reject activate) |
| Leak/restart kept | 4 |

## Execution order note

Implement **after** Dispatcharr wire plan (`docs/superpowers/plans/2026-08-24-dispatcharr-wire.md`) is complete, unless parallelized on a separate branch.

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-08-24-vpn-profiles.md`.
