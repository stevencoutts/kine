# Teamarr leagues on enable (Select Leagues + 2000s channel blocks)

Date: 2026-08-25
Status: draft for review (design approved in chat; awaiting written-spec review)

## Goal

When enabling Teamarr from Helm, show a league picker dialog, then provision Teamarr with:

1. Soccer mode **Select Leagues** and the chosen leagues subscribed.
2. **Manual** channel numbering with fixed **20-number blocks** starting at **2000**.
3. Last picks remembered so the next Enable dialog defaults to them.
4. Dispatcharr connection seeded so Teamarr can talk to the local tunnel peer.

Out of scope for v1: importing event groups / M3U groups, running Generate, team-channel import, or copying a full kore Teamarr DB.

## Decisions

| Question | Decision |
|---|---|
| Picker | Enable dialog every time Teamarr is enabled |
| Defaults | Last saved selection; first run uses curated UK football set from kore |
| Soccer mode | Select Leagues (not Follow Teams / all soccer) |
| Channel numbers | Manual mode; blocks of 20 from 2000 |
| Extra leagues | Next free 20-block after the curated defaults’ reserved starts |
| Persist | `${STACK_ROOT}/config/teamarr/leagues.json` |
| Apply | Teamarr REST after healthy (not SQLite file writes) |
| Volume | Fix mount to `/app/data` so DB survives recreate |
| Pattern | Mirror NZBGet “enable with options” modal |
| Live TV tier | Same modal once if that enable path starts Teamarr |

## Default league set and channel starts

| Display (UI) | Start |
|---|---|
| EPL | 2000 |
| FA Cup | 2020 |
| Carabao Cup | 2040 |
| UCL | 2060 |
| UCL Qualifying | 2080 |
| UEL | 2100 |
| UECL | 2120 |
| World Cup | 2140 |
| WC Qualifying Playoffs | 2160 |

Canonical league **ids/slugs** must be resolved against Teamarr’s soccer cache (`GET /api/v1/cache/leagues?sport=soccer`) during implementation. The table above is the product mapping; the persisted file stores Teamarr’s ids plus assigned starts.

Any league in the default table uses its **reserved** start whenever it is selected (even if other defaults are unticked). Additional leagues (not in the table) get `next_start = max(all reserved and assigned starts in this selection) + 20`, starting from 2180 if the selection only contains extras beyond the table’s last reserved block.

## Enable UX

1. User clicks **Enable** on Teamarr (Apps → Live TV → Disabled), or Live TV tier enable would start Teamarr.
2. Modal opens: checklist of soccer leagues (static curated list before Teamarr is up; optionally refreshed from cache after).
3. Pre-tick from `leagues.json` if present; else the default set above.
4. **Cancel** → do not enable.
5. **Confirm** with zero leagues → blocked (need ≥1).
6. **Confirm** → save picks → start Teamarr → wait healthy → apply subscriptions + numbering + Dispatcharr settings → Apps UI refreshes.

## Persist format (`leagues.json`)

```json
{
  "soccer_mode": "select_leagues",
  "leagues": [
    {"id": "eng.1", "name": "EPL", "channel_start": 2000},
    {"id": "eng.fa", "name": "FA Cup", "channel_start": 2020}
  ],
  "updated_at": "2026-08-25T14:00:00+00:00"
}
```

`id` values are examples only — implementation resolves real ids/slugs from Teamarr’s soccer cache (or a static map verified against that cache). Helm reads this for dialog defaults. Provision reads it to apply settings. Re-enable overwrites the file and Teamarr settings with the new selection.

## Architecture

```
Helm Apps Enable (teamarr)
        │
        ├─► modal: pick soccer leagues
        │         save leagues.json
        │
        ├─► compose up teamarr (+ gluetun deps)
        │         volume: config/teamarr → /app/data
        │
        └─► recipes/teamarr.py (or Helm helper + provision)
                  │
                  ├─► wait GET /health is_ready
                  ├─► PATCH subscription / soccer select-leagues + ids
                  ├─► PATCH channel-numbering (manual + league_channel_starts)
                  └─► PATCH settings/dispatcharr (loopback URL + creds)
```

Exact subscription endpoint field names are confirmed against running Teamarr OpenAPI (`/docs`) during implementation. Prefer documented `/api/v1/` settings/subscription surfaces over scraping the UI.

## Dispatcharr connection

Teamarr stores Dispatcharr **username/password** in its own settings (not ECM-style API key env). On provision v1:

- Always set URL to `http://127.0.0.1:9191` and enable the Dispatcharr integration when the settings API allows.
- If Teamarr’s running build accepts an API key field, prefer `DISPATCHARR_TOKEN` from stack `.env`.
- Otherwise set username to `HELM_ADMIN_USER` when known; **do not** invent a password. Leave password empty and still apply leagues/numbering. User can finish auth in Teamarr Settings if the connection test fails.

Also keep writing `DISPATCHARR_URL` / `DISPATCHARR_TOKEN` into `teamarr.env` for kine wire bookkeeping (existing behaviour); that alone does not configure Teamarr’s UI connection.

## Volume fix

`compose/live.teamarr.yml` today mounts `${STACK_ROOT}/config/teamarr:/config`. Upstream persists under `/app/data`. Change the data volume to:

```yaml
volumes:
  - ${STACK_ROOT}/config/teamarr:/app/data
```

`env_file` stays a **host** path `${STACK_ROOT}/config/teamarr/teamarr.env` (Compose reads it from the host; it may also appear inside `/app/data` as `teamarr.env`, which is harmless). `leagues.json` lives in the same host directory.

## Live TV tier enable

If enabling the Live TV tier starts Teamarr as a default, show the same modal **once** before Teamarr starts. Other live apps (Dispatcharr, ECM) are unaffected. If Teamarr is already enabled, skip the modal.

## Non-goals (v1)

- Syncing leagues from kore automatically (defaults are a static list inspired by kore).
- Creating Dispatcharr EPG source / event groups / Generate.
- NFL/NBA/etc. non-soccer subscriptions in the first dialog (soccer-only picker).
- Editing leagues from Helm Settings without Disable/Enable (can add later).

## Files (expected)

| Path | Role |
|---|---|
| `helm/frontend/index.html` | Teamarr enable modal |
| `helm/backend/app/main.py` (and/or small module) | Accept league payload on enable; orchestrate apply |
| `provision/recipes/teamarr.py` | Health wait + REST apply |
| `provision/provision.py` | Call teamarr recipe when profile enabled / after wire |
| `compose/live.teamarr.yml` | `/app/data` volume |
| `${STACK_ROOT}/config/teamarr/leagues.json` | Last picks |
| `tests/test_teamarr*.py`, `tests/test_frontend.py` | Block math, payloads, modal invariants |

## Testing

- Unit: channel-start assignment (defaults + extras in steps of 20 from 2000).
- Unit: `leagues.json` round-trip and empty-selection rejection.
- Mocked httpx: subscription + channel-numbering + dispatcharr PATCH payloads.
- Frontend invariant: modal exists; enable path references it for `teamarr`.
- Manual on osiris: Enable → pick leagues → Teamarr Subscriptions shows Select Leagues + ticks; Settings channel numbering shows 2000-block starts.

## Success criteria

1. Enable Teamarr without the dialog is impossible from the Apps UI (must go through modal or cancel).
2. After confirm, Teamarr UI shows Select Leagues with the chosen set.
3. Channel starts match the 20-block map (and extras continue the sequence).
4. Re-enable opens the dialog pre-ticked from last time and overwrites Teamarr on confirm.
5. Teamarr DB survives container recreate (`/app/data`).
