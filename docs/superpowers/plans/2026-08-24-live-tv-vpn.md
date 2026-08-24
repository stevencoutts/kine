# Live TV VPN Tunnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put Dispatcharr, ECM, and Teamarr inside gluetun’s namespace with correct ports and Emby/provision URLs.

**Architecture:** Same tunnel pattern as acquisition apps; Traefik on gluetun; loopback URL between live apps; `gluetun:9191` for Emby/Helm.

**Tech Stack:** Docker Compose, catalogue.yml, provision recipes, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-live-tv-vpn-design.md`

## Global Constraints

- Ports: Dispatcharr 9191, ECM 6100, Teamarr 9195.
- No PUID/PGID on Dispatcharr.
- Update `.env.example` and deployed `.env` `VPN_TUNNELLED_APPS`.

## File map

| File | Change |
|---|---|
| `compose/live.dispatcharr.yml` | Join gluetun; drop networks/labels |
| `compose/live.ecm.yml` | Join gluetun; ECM_PORT; loopback URL |
| `compose/live.teamarr.yml` | Join gluetun; loopback URL |
| `compose/vpn.gluetun.yml` | Traefik routers for tv/channels/sports |
| `catalogue.yml` | tunnelled + internals + requires gluetun |
| `.env.example` | VPN_TUNNELLED_APPS |
| `docs/port-map.md` | Move live apps into tunnel table |
| `provision/recipes/dispatcharr.py` | gluetun URLs |
| `provision/recipes/envfiles.py` | `127.0.0.1:9191` |
| `helm/backend/app/scheduler.py` | Emby want URL |
| `tests/test_dispatcharr.py` | URL asserts |

---

### Task 1: Compose + catalogue + port map

- [ ] Update three live fragments + gluetun Traefik labels + catalogue + port-map + `.env.example`
- [ ] `pytest tests/test_stack.py -q` (tunnelled parametrized tests)
- [ ] Commit

### Task 2: Provision / scheduler URLs

- [ ] Point HDHR/base/env merge at gluetun / 127.0.0.1 as appropriate
- [ ] Update `tests/test_dispatcharr.py`
- [ ] Commit

### Task 3: Deploy osiris

- [ ] Push; pull; extend `VPN_TUNNELLED_APPS`; recreate tunnel group; enable live profiles
