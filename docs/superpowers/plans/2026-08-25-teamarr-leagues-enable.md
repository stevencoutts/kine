# Teamarr Leagues-on-Enable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Helm Enable for Teamarr opens a soccer league picker, then provisions Select Leagues + manual channel blocks from 2000 (step 20).

**Architecture:** Persist picks in `leagues.json`; after Teamarr is healthy, PUT `/api/v1/sports-subscription` (`soccer_mode=manual`) and PUT `/api/v1/settings/channel-numbering`; fix volume to `/app/data`.

**Tech Stack:** FastAPI Helm, Teamarr REST, provision recipes, vanilla JS modal (NZBGet pattern).

**Spec:** `docs/superpowers/specs/2026-08-25-teamarr-leagues-enable-design.md`

## Global Constraints

- `soccer_mode` value is `manual` (Teamarr Select Leagues).
- Default slugs: eng.1, eng.fa, eng.league_cup, uefa.champions, uefa.champions_qual, uefa.europa, uefa.europa.conf, fifa.world, fifa.wcq.ply.
- Channel starts: 2000 + 20×index for defaults; extras after 2160.
- Volume: `${STACK_ROOT}/config/teamarr:/app/data`.

---

## Task 1: Channel assignment + leagues.json helpers

- [ ] Tests for `assign_channel_starts` and load/save
- [ ] Implement in `provision/recipes/teamarr.py`

## Task 2: Teamarr REST apply

- [ ] Mocked httpx tests for subscription + numbering + dispatcharr URL
- [ ] `configure()` waits on `/health`, applies settings

## Task 3: Compose volume + enable wiring

- [ ] Fix `live.teamarr.yml` volume
- [ ] Helm enable accepts `leagues`, saves json, applies after start
- [ ] GET last leagues for modal defaults

## Task 4: Frontend modal

- [ ] Teamarr enable dialog (checkbox list, last picks default)
- [ ] Frontend tests

## Task 5: Deploy smoke

- [ ] Rebuild provision/helm on osiris; enable path ready
