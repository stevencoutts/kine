# Dispatcharr wire-only (Emby tuner + ECM/Teamarr token)

Date: 2026-08-24
Status: draft for review (design approved in chat; awaiting written-spec review)

## Goal

After the user finishes Dispatcharr’s first login, Helm automatically:

1. Registers Dispatcharr as an Emby Live TV HDHomeRun tuner (internal URL).
2. Writes `DISPATCHARR_TOKEN` into ECM and Teamarr env files so those front ends can talk to Dispatcharr.

No IPTV providers, channels, or EPG sources are imported. Provider setup stays manual in Dispatcharr (same stance as today’s README).

## Decisions

| Question | Decision |
|---|---|
| Scope | Wire only (option A) |
| Trigger | Seerr-style background poller after admin/API key exists |
| Emby link | `POST /LiveTv/TunerHosts` with HDHomeRun URL `http://dispatcharr:9191/hdhr` |
| EPG | Optional XMLTV listing provider if a stable Dispatcharr EPG URL is available; skip cleanly if not |
| ECM / Teamarr | Write `DISPATCHARR_URL` + `DISPATCHARR_TOKEN`; recreate containers only when token changes |
| Fallback | Optional Helm Settings field to paste/replace the Dispatcharr API token |
| Out of scope | Unattended Dispatcharr admin creation; copying an old instance; channel/EPG bootstrap |

## Ready signal

Mirror Seerr’s “wait until wizard/login produced credentials” pattern.

1. `dispatcharr` is in `COMPOSE_PROFILES`.
2. Dispatcharr HTTP responds on `http://dispatcharr:9191`.
3. An API key is available via one of:
   - **Preferred:** discovered or created through Dispatcharr’s accounts/API-key endpoints after an admin user exists.
   - **Fallback:** user-pasted token stored by Helm (Settings) and written to env files.

Until a key exists, the poller no-ops (no errors in the Apps UI). Provision `wire` may call the same recipe; it must also be idempotent when the key is missing.

Exact key discovery path is an implementation detail to confirm against the running Dispatcharr OpenAPI (`/api/swagger/` / `/api/accounts/`). If auto-create is impossible without a password, Settings paste is the supported path and the poller still applies Emby + env once the token is present.

## Architecture

```
Helm scheduler (dispatcharr-wire loop)
        │
        ├─► recipes/dispatcharr.py
        │         │
        │         ├─► Emby  POST /LiveTv/TunerHosts
        │         │         Url: http://dispatcharr:9191/hdhr
        │         │         (+ optional ListingProviders XMLTV)
        │         │
        │         └─► envfiles  ecm.env / teamarr.env
        │                       DISPATCHARR_URL
        │                       DISPATCHARR_TOKEN
        │                             │
        └─► recreate ecm/teamarr ─────┘  (only if token changed)
```

Catalogue already declares:

- `dispatcharr` → `wires: [emby-tuner]`
- `ecm` / `teamarr` → `wires: [dispatcharr-link]`

Those names become real behaviour inside `recipes/dispatcharr.py` (and a thin env helper), not separate scripts.

## Emby tuner (`emby-tuner`)

Preconditions: `emby` profile enabled; Emby API key available (Settings `EMBY_API_KEY` and/or stack Emby after wizard).

1. List existing tuner hosts (Live TV API / livetv config — use whichever Emby build exposes for de-dupe).
2. If a host already points at `http://dispatcharr:9191/hdhr` (or equivalent BaseURL), skip.
3. Otherwise `POST /LiveTv/TunerHosts` roughly:

```json
{
  "Type": "hdhomerun",
  "Url": "http://dispatcharr:9191/hdhr",
  "FriendlyName": "Dispatcharr",
  "ImportFavoritesOnly": false
}
```

4. Optionally register Dispatcharr XMLTV as a listing provider when the EPG URL is known and stable; do not fail the whole wire if this step fails.

Rules:

- Idempotent across restarts.
- Never delete unrelated tuners.
- If Emby is not enabled, skip Emby steps; still write ECM/Teamarr token when possible.

## ECM / Teamarr (`dispatcharr-link`)

Extend the existing `provision/recipes/envfiles.py` pattern (today writes empty `DISPATCHARR_TOKEN`):

- Always set `DISPATCHARR_URL=http://dispatcharr:9191`.
- Set `DISPATCHARR_TOKEN=<api-key>` when known.
- Preserve unrelated keys already present in `ecm.env` / `teamarr.env` if we move to a merge-style writer.
- Recreate `ecm` / `teamarr` containers only when the token value changes (avoid flapping on every poll).

## Scheduler

Add a loop next to Seerr’s wire loop in `helm/backend/app/scheduler.py`:

- Same provision lock discipline (no concurrent provision-run containers).
- Record last success / skip reason in job state for Status page visibility.
- Interval similar to Seerr (tens of seconds while pending; back off when done).

## Settings fallback

If auto key minting is unreliable:

- Settings → Live TV / Dispatcharr: paste API token.
- Save writes env files and kicks one wire attempt (Emby + recreate dependents).

UI stays minimal; this is a credential field, not a new “cool” panel.

## Files (expected)

| Path | Role |
|---|---|
| `provision/recipes/dispatcharr.py` | New: ready check, Emby tuner, token export |
| `provision/recipes/envfiles.py` | Merge token into ecm/teamarr env |
| `provision/provision.py` | Call dispatcharr.configure when profile enabled |
| `helm/backend/app/scheduler.py` | Background `dispatcharr-wire` loop |
| `helm/backend/app/main.py` | Optional settings keys + save hook |
| `tests/test_dispatcharr.py` | New unit tests for payloads / idempotency / env merge |

## Tests

- Parse/build Emby tuner payload; “already linked” detection.
- Env merge: empty token → filled; unchanged token → no recreate flag; changed token → recreate flag.
- Scheduler gate: no key → no provision invoke; key present + not linked → invoke once.
- Frontend/settings assertions only if the paste field ships.

## Out of scope (explicit)

- Creating the first Dispatcharr admin user unattended.
- Importing M3U/Xtream/EPG from another Dispatcharr.
- Plex Live TV tuner registration (Emby-only for this MVP).
- Changing Traefik host (`tv.`) or recordings mounts.

## Success criteria

1. Fresh Live TV enable → user logs into Dispatcharr once → within a short poll window Emby shows a Dispatcharr HDHomeRun source without manual URL paste.
2. ECM and Teamarr start with a non-empty `DISPATCHARR_TOKEN` and can reach Dispatcharr on the internal network.
3. Re-running wire / restarting Helm does not duplicate Emby tuners or thrash ECM/Teamarr restarts.
