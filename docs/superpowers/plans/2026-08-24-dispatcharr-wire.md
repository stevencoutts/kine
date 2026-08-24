# Dispatcharr Wire Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After Dispatcharr has an API token, automatically register it as Emby’s HDHomeRun Live TV tuner and write that token into ECM/Teamarr env files (recreate those containers only when the token changes).

**Architecture:** Pure helpers in `provision/recipes/dispatcharr.py` for Emby tuner idempotency and env merge; provision `wire` calls them when profiles allow; Helm scheduler polls like Seerr and re-runs provision wire under the provision lock; optional Settings `DISPATCHARR_TOKEN` paste feeds the same path.

**Tech Stack:** Python 3.12, httpx, FastAPI/Helm scheduler, Docker Compose provision container, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-dispatcharr-wire-design.md`

## Global Constraints

- Emby tuner URL is exactly `http://dispatcharr:9191/hdhr`.
- Never delete unrelated Emby tuners.
- Recreate `ecm` / `teamarr` only when `DISPATCHARR_TOKEN` value changes.
- No IPTV/EPG/provider import in this MVP.
- Use provision lock for any `compose run provision wire` from Helm.
- Run tests with `/tmp/kine-test-venv/bin/python -m pytest` (or project venv) from repo root.
- Commit after every task.

## File map

| File | Responsibility |
|---|---|
| `provision/recipes/dispatcharr.py` | Ready/token helpers, Emby tuner link, orchestrate configure |
| `provision/recipes/envfiles.py` | Merge `DISPATCHARR_*` into ecm/teamarr env; report changed |
| `provision/provision.py` | Call `dispatcharr.configure` when profile enabled |
| `helm/backend/app/scheduler.py` | `dispatcharr-wire` poll loop |
| `helm/backend/app/main.py` | Settings key `DISPATCHARR_TOKEN` |
| `helm/frontend/index.html` | Settings paste field for Dispatcharr token |
| `tests/test_dispatcharr.py` | Unit tests |

---

### Task 1: Emby tuner helpers (pure)

**Files:**
- Create: `provision/recipes/dispatcharr.py`
- Test: `tests/test_dispatcharr.py`

**Interfaces:**
- Produces:
  - `DISPATCHARR_HDHR = "http://dispatcharr:9191/hdhr"`
  - `tuner_already_linked(hosts: list) -> bool`
  - `tuner_host_payload() -> dict`

- [ ] **Step 1: Write the failing test**

```python
"""Dispatcharr Emby tuner + token env helpers."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))

from recipes.dispatcharr import (  # noqa: E402
    DISPATCHARR_HDHR,
    tuner_already_linked,
    tuner_host_payload,
)


def test_tuner_host_payload_shape():
    body = tuner_host_payload()
    assert body["Type"] == "hdhomerun"
    assert body["Url"] == DISPATCHARR_HDHR
    assert body["FriendlyName"] == "Dispatcharr"
    assert body["ImportFavoritesOnly"] is False


def test_tuner_already_linked_matches_url():
    assert tuner_already_linked([
        {"Url": "http://other:5004", "Type": "hdhomerun"},
    ]) is False
    assert tuner_already_linked([
        {"Url": DISPATCHARR_HDHR, "Type": "hdhomerun"},
    ]) is True
    assert tuner_already_linked([
        {"Url": DISPATCHARR_HDHR + "/", "Type": "hdhomerun"},
    ]) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dispatcharr.py::test_tuner_host_payload_shape tests/test_dispatcharr.py::test_tuner_already_linked_matches_url -q`
Expected: FAIL — import error / missing module.

- [ ] **Step 3: Write minimal implementation**

In `provision/recipes/dispatcharr.py`:

```python
DISPATCHARR_HDHR = "http://dispatcharr:9191/hdhr"


def tuner_host_payload() -> dict:
    return {
        "Type": "hdhomerun",
        "Url": DISPATCHARR_HDHR,
        "FriendlyName": "Dispatcharr",
        "ImportFavoritesOnly": False,
    }


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def tuner_already_linked(hosts: list) -> bool:
    want = _norm_url(DISPATCHARR_HDHR)
    for host in hosts:
        if not isinstance(host, dict):
            continue
        if _norm_url(str(host.get("Url") or "")) == want:
            return True
    return False
```

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add provision/recipes/dispatcharr.py tests/test_dispatcharr.py
git commit -m "Add Dispatcharr Emby HDHomeRun tuner helpers."
```

---

### Task 2: Env merge for ECM/Teamarr token

**Files:**
- Modify: `provision/recipes/envfiles.py`
- Modify: `tests/test_dispatcharr.py` (or `tests/test_envfiles.py` if preferred)

**Interfaces:**
- Produces: `write_dispatcharr_token(app: str, token: str, log) -> bool`  
  Returns `True` if file content changed.

- [ ] **Step 1: Failing tests**

```python
def test_write_dispatcharr_token_sets_url_and_token(tmp_path, monkeypatch):
    from recipes import envfiles
    monkeypatch.setattr(envfiles, "STACK", tmp_path)
    changed = envfiles.write_dispatcharr_token("ecm", "abc-token", lambda m: None)
    assert changed is True
    text = (tmp_path / "config" / "ecm" / "ecm.env").read_text()
    assert "DISPATCHARR_URL=http://dispatcharr:9191" in text
    assert "DISPATCHARR_TOKEN=abc-token" in text
    changed2 = envfiles.write_dispatcharr_token("ecm", "abc-token", lambda m: None)
    assert changed2 is False


def test_write_dispatcharr_token_preserves_extra_keys(tmp_path, monkeypatch):
    from recipes import envfiles
    monkeypatch.setattr(envfiles, "STACK", tmp_path)
    d = tmp_path / "config" / "ecm"
    d.mkdir(parents=True)
    (d / "ecm.env").write_text("OTHER=1\nDISPATCHARR_TOKEN=\n")
    envfiles.write_dispatcharr_token("ecm", "tok", lambda m: None)
    text = (d / "ecm.env").read_text()
    assert "OTHER=1" in text
    assert "DISPATCHARR_TOKEN=tok" in text
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `write_dispatcharr_token`**

Merge strategy: read existing `KEY=VAL` lines into a dict; set `DISPATCHARR_URL` and `DISPATCHARR_TOKEN`; write stable sorted or original-order keys (prefer: keep existing key order, append missing). Return whether body changed.

Update `configure()` so when `ecm`/`teamarr` enabled it still writes URL; token may stay empty until Task 3 supplies it — either leave current empty write or call `write_dispatcharr_token(app, "", log)` for compatibility.

- [ ] **Step 4: Tests PASS**

- [ ] **Step 5: Commit**

```bash
git commit -am "Merge Dispatcharr token into ECM and Teamarr env files."
```

---

### Task 3: `dispatcharr.configure` orchestration

**Files:**
- Modify: `provision/recipes/dispatcharr.py`
- Modify: `provision/provision.py`
- Test: `tests/test_dispatcharr.py`

**Interfaces:**
- Consumes: `tuner_*`, `envfiles.write_dispatcharr_token`
- Produces: `configure(enabled: set[str], token: str | None, log) -> dict`  
  Return `{"emby_linked": bool, "env_changed": list[str]}` (for tests / logging).

Behaviour:

1. If `dispatcharr` not in `enabled`, return early.
2. If `token` empty, log skip and return (still ok).
3. For each of `ecm`, `teamarr` in `enabled`, `write_dispatcharr_token`; collect apps whose files changed.
4. If `emby` in `enabled` and Emby API key resolvable (`keys.resolve_key("emby")` or Settings path — use whatever provision already uses for Emby; if none, skip Emby with log):
   - `GET http://emby:8096/LiveTv/TunerHosts` with `X-Emby-Token`
   - If not `tuner_already_linked`, `POST` with `tuner_host_payload()`
5. Do **not** recreate containers inside the recipe — return `env_changed` so the caller (scheduler/Helm) can `compose up -d --force-recreate` those apps. Provision wire from CLI may recreate if easy; document in log either way.

- [ ] **Step 1: Failing test with httpx mock / monkeypatch**

Fake Emby client: empty tuner list → POST called once; second configure → POST not called.

- [ ] **Step 2: FAIL then implement**

- [ ] **Step 3: Wire `provision.py`**

```python
if "dispatcharr" in enabled:
    try:
        from recipes import dispatcharr
        token = os.environ.get("DISPATCHARR_TOKEN", "").strip() or None
        # Also try reading from stack helm settings if present later
        dispatcharr.configure(enabled, token, log)
    except Exception as exc:
        log(f"dispatcharr: wiring failed ({exc})")
```

Token resolution inside recipe (preferred):  
`token = token or _token_from_envfiles() or _token_from_helm_env()`  
where empty strings are ignored.

- [ ] **Step 4: Tests PASS + commit**

```bash
git commit -am "Wire Dispatcharr to Emby tuner and dependent env tokens."
```

---

### Task 4: Helm settings paste + provision trigger

**Files:**
- Modify: `helm/backend/app/main.py` — add `DISPATCHARR_TOKEN` to settings allow-list (with media/live keys group)
- Modify: `helm/frontend/index.html` — Settings field near Live TV / media servers
- Test: assert key + label in `tests/test_frontend.py` or small settings test

- [ ] **Step 1: Failing frontend/backend string asserts**

- [ ] **Step 2: Add settings field**  
  Label: `Dispatcharr API token`  
  Help: paste from Dispatcharr profile; used for ECM/Teamarr and Emby tuner wire.

- [ ] **Step 3: On settings save including `DISPATCHARR_TOKEN`, existing `_provision("wire")` path should run (same as media server keys). Ensure the key is included in that trigger set.**

- [ ] **Step 4: After successful wire, if env changed for ecm/teamarr, recreate those services:**  
  `compose up -d --force-recreate ecm teamarr` (only profiles enabled).

  If recreate is cleaner inside scheduler Task 5 only, settings save can rely on scheduler — but then document delay. Prefer immediate recreate on settings save when token changes.

- [ ] **Step 5: Commit**

```bash
git commit -am "Add Dispatcharr API token to Helm Settings."
```

---

### Task 5: Scheduler `dispatcharr-wire` loop

**Files:**
- Modify: `helm/backend/app/scheduler.py`
- Modify: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `_dispatcharr_needs_wire() -> bool`, `wire_dispatcharr_if_ready()`, `_dispatcharr_wire_loop()`

Needs wire when:

- `dispatcharr` in profiles
- Token available (`config.read().get("DISPATCHARR_TOKEN")` or non-empty token already intended)
- Emby enabled and tuner not linked **or** ecm/teamarr enabled with empty token in their env files

Keep the check cheap: if no token in Helm settings, return False (user is the MVP ready signal unless a later task discovers API keys).

On need: `provision_lock` + `compose run --rm provision wire` with env passing `DISPATCHARR_TOKEN`; then recreate changed dependents if recipe logs/state indicates — simplest approach: always `up -d --force-recreate` ecm/teamarr when they are profiled and token non-empty after wire (compose is idempotent if image/config unchanged — actually force-recreate always restarts; better: compare env file mtime or have recipe write `/stack/helm-jobs.json` flag). Minimal approach matching Seerr: just run provision wire; teach recipe to recreate via `subprocess` only if changed — **avoid docker from provision if policy forbids**. Prefer Helm scheduler after wire:

```python
changed = ...  # parse log lines "ecm: wrote" / return code path
if "ecm" in profiles:
    await compose.run("up", "-d", "--force-recreate", "ecm", ...)
```

YAGNI compromise: after successful wire with token present, recreate ecm/teamarr if enabled (once per successful wire when `dispatcharr_wire.ok` flips). Store `token_fingerprint` (hash) in `helm-jobs.json`; only recreate when fingerprint changes.

- [ ] **Step 1: Extend `tests/test_scheduler.py` asserts for new symbols / create_task**

- [ ] **Step 2: Implement loop (sleep 60s, poll 120s like Seerr)**

- [ ] **Step 3: Commit**

```bash
git commit -am "Poll Dispatcharr wire after token is available."
```

---

### Task 6: Deploy verification notes

**Files:**
- Modify: `README.md` — one short Live TV paragraph: enable Live TV → create Dispatcharr admin → paste API token in Helm Settings (or wait if auto) → Emby gains Dispatcharr tuner; ECM/Teamarr pick up token.

- [ ] **Step 1: README blurb**
- [ ] **Step 2: Commit**

```bash
git commit -am "Document Dispatcharr token and Emby tuner wiring."
```

---

## Spec coverage check

| Spec item | Task |
|---|---|
| Emby HDHomeRun POST | 1, 3 |
| Idempotent tuner | 1, 3 |
| ECM/Teamarr token env | 2, 3 |
| Recreate only on token change | 2, 5 |
| Seerr-style scheduler | 5 |
| Settings paste fallback | 4 |
| No IPTV import | — (omitted) |

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-08-24-dispatcharr-wire.md`.
