# Live TV apps on VPN (Dispatcharr, ECM, Teamarr)

Date: 2026-08-24  
Status: approved in chat

## Goal

Route Dispatcharr, Enhanced Channel Manager, and Teamarr through gluetun so IPTV fetch/proxy and related live-TV tooling egress via the VPN kill switch.

## Decision

Join gluetun’s network namespace (`network_mode: service:gluetun`), same pattern as the acquisition tier. Emby stays untunnelled and reaches the HDHomeRun endpoint as `http://gluetun:9191/hdhr`.

## Port map (tunnel)

| Port | App |
|---|---|
| 9191 | Dispatcharr |
| 6100 | ECM (`ECM_PORT=6100`) |
| 9195 | Teamarr |

Inside the namespace: `DISPATCHARR_URL=http://127.0.0.1:9191`.  
From `kine_internal`: `http://gluetun:9191`.

## Constraints

- Traefik routers for these apps live on the gluetun service.
- Catalogue: `tunnelled: forced`, `requires: [gluetun]`, internals via `gluetun:…`.
- `VPN_TUNNELLED_APPS` includes `dispatcharr,ecm,teamarr`.
- Dispatcharr still omits `PUID`/`PGID` (nologin Postgres init failure).
- VPN outage takes live TV offline with acquisition — intentional.

## Out of scope

- Separate gluetun for live TV
- Tunnelling Emby
