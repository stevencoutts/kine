# Multi-Gluetun egress (per-profile app assignment)

Date: 2026-08-26  
Status: draft for review (design approved in chat; awaiting written-spec review)  
Branch: `feature/multi-gluetun-egress`

## Goal

Allow different forced-tunnel apps to egress through different VPN profiles at the same time, while keeping Gluetun’s kill-switch model (`network_mode: service:<tunnel>`).

Today Kine has a single Gluetun namespace: every tunnelled app shares one WireGuard egress, and “Activate profile” only swaps that tunnel’s config. This design adds concurrent secondary Gluetun instances driven by per-profile app checklists.

## Decisions

| Question | Decision |
|---|---|
| Split model | Flexible: each profile has an app checklist; multiple tunnels can run concurrently |
| Unassigned forced-tunnel apps | Stay on the **primary** tunnel |
| Primary selection | Explicit `primary_id` on one profile |
| Implementation approach | **Dynamic compose override** for secondaries (not a fixed 3-slot pool) |
| Soft concurrency warning | Warn in UI above 3 concurrent tunnels; still allow |
| OpenVPN | Unchanged: schema/UI only; not runnable |
| Generated routing | Regenerate from `vpn-profiles.json` (do not treat generated YAML as source of truth in backups) |

## Non-goals (this change)

- Policy routing / split tunneling inside a single Gluetun
- Host-network WireGuard without `network_mode: service:…`
- Per-app dedicated tunnels by default (operators may assign one app per profile, but the product shape is checklists)
- Changing which catalogue apps are `tunnelled: forced`

## Data model

File: `${STACK_ROOT}/config/helm/vpn-profiles.json`

```json
{
  "primary_id": "uuid-or-null",
  "profiles": [
    {
      "id": "uuid",
      "name": "Njalla",
      "type": "wireguard",
      "conf": "[Interface]\n…",
      "apps": ["sonarr", "radarr", "prowlarr"],
      "updated_at": "2026-08-26T18:00:00Z"
    },
    {
      "id": "uuid-2",
      "name": "Proton SE#320",
      "type": "wireguard",
      "conf": "[Interface]\n…",
      "apps": ["dispatcharr", "ecm", "teamarr"],
      "updated_at": "2026-08-26T18:05:00Z"
    }
  ]
}
```

### Rules

- Exactly one `primary_id` when VPN is enabled (required). When VPN is disabled, `primary_id` may still point at a profile for the next enable.
- `apps` is a list of catalogue app ids. Only apps with `tunnelled: forced` are assignable.
- An app id appears in **at most one** profile’s `apps` list. Saving a checklist removes that app from every other profile.
- Forced-tunnel apps **not** listed on any profile are treated as primary leftovers.
- A **secondary** tunnel (non-primary profile) runs only when VPN is enabled **and** that profile’s `apps` is non-empty.
- The **primary** tunnel runs whenever VPN is enabled (even if its own `apps` is empty), so leftovers always have a kill-switched egress.
- Deleting the primary profile is rejected until another profile is set primary.
- WireGuard-only activation for running tunnels (same as today).

### Migration from current schema

On Helm start / first VPN load:

1. If `primary_id` missing and `active_id` present → `primary_id = active_id`; drop `active_id`.
2. If neither set and profiles exist → `primary_id = profiles[0].id`.
3. Ensure every profile has `apps` (default `[]`).
4. Regenerate routing compose so behavior matches today: everything on primary until the operator assigns apps.

## Tunnel identity helper

Single source of truth used by compose generation, provision URLs, heal, VPN API, and Updates recreate:

```text
tunnel_service(app_id) → "gluetun" | "gluetun_<shortId>"
```

- `<shortId>` is a stable, compose-safe abbreviation of the profile id (e.g. first 8 hex chars of the UUID, lower-case, `[a-z0-9_]` only).
- Primary profile always maps to service name `gluetun` (existing container `kine-gluetun`).
- Non-primary profile with apps → `gluetun_<shortId>` / `kine-gluetun-<shortId>`.

`tunnel_peers(service)` → enabled forced-tunnel apps whose `tunnel_service` equals that service, plus `vpn-portsync` when Transmission is among those peers.

## Compose / network / Traefik

### Generated override

Helm writes a generated compose fragment at:

`${STACK_ROOT}/config/helm/vpn-routing.override.yml`

Included by the stack’s compose invocation the same way other overlays are included today (extend `compose` helper / `./kine` so Helm and CLI share one include list).

Contents:

1. **Secondary Gluetun services** for each non-primary profile with `apps.length >= 1`:
   - Same image/env pattern as primary (`vpn.gluetun.yml`)
   - Volume: `${STACK_ROOT}/config/gluetun-<shortId>:/gluetun`
   - Networks: `kine_internal`, `kine_edge`
   - Healthcheck identical to primary
   - Profile: `gluetun` (rides existing VPN enablement)

2. **Per-app overrides** for every enabled forced-tunnel app:
   - `network_mode: service:<tunnel_service(app)>`
   - `depends_on: <tunnel_service>: condition: service_healthy`

3. **Traefik labels** for tunnelled app routers:
   - Must **not** remain solely on primary once apps can move.
   - `vpn.gluetun.yml` removes the hard-coded per-app router/service label block (or leaves none).
   - Generated file attaches each app’s Traefik router/service labels to the Gluetun service that currently hosts that app.

Static `acq.*.yml` / `live.*.yml` keep `network_mode: service:gluetun` as the default; the generated override wins when present.

### Primary materialization

Primary continues to use `${STACK_ROOT}/config/gluetun/wireguard/wg0.conf` and existing `.env` `WIREGUARD_*` / `VPN_*` keys for the primary profile (compatible with current gluetun custom mode).

Each secondary writes its own conf under `${STACK_ROOT}/config/gluetun-<shortId>/wireguard/wg0.conf` and sets that service’s environment from its profile conf (container-local env via compose override, **not** by overwriting primary `.env` keys).

### vpn-portsync

`vpn-portsync` uses `network_mode: service:<tunnel that hosts transmission>`. If Transmission is not enabled, omit or leave stopped.

### Internal DNS

Anything on `kine_internal` that reached apps as `gluetun:<port>` must use `tunnel_service(app):<port>` instead (Seerr → Sonarr/Radarr, provision clients, ECM/Teamarr → Dispatcharr, Emby HDHomeRun URL, etc.).

Ports remain unique **within** a single tunnel namespace (unchanged assignment table). Separate tunnels do not share a network namespace, so the same host port numbers on different Gluetuns do not collide.

## Helm UX

- VPN page lists profiles; live detail (IP, forwarded port, chips, leak/restart/disable) lives on cards that have a running tunnel (primary always when VPN on; secondaries when assigned).
- **Set as Primary** on non-primary profiles (moves leftovers target; may recreate groups).
- Each profile: **Apps** checklist of forced-tunnel catalogue apps; save enforces exclusivity and triggers apply.
- Rematerialize / apply writes that profile’s WireGuard conf into its tunnel slot and recreates that tunnel group.
- Soft warning banner if concurrent running tunnels > 3.
- Disable VPN stops primary and all secondaries and clears/stops generated secondary services.

## Apply flow

On primary change, app checklist save, profile rematerialize, or VPN enable:

1. Persist `vpn-profiles.json` (validated).
2. Write WireGuard conf(s) for primary and each secondary that should run.
3. Regenerate `vpn-routing.override.yml`.
4. `docker compose up -d --force-recreate` each **affected** tunnel group (`tunnel_service` + `tunnel_peers`).
5. Re-run provision wire for apps whose tunnel host changed (or a conservative full wire of enabled tunnelled apps).

When a secondary’s `apps` becomes empty or VPN is disabled: stop/remove that secondary container and regenerate without it.

## Tunnel heal / Updates / restart

- Parameterize today’s heal: for each running Gluetun service name, detect peers still pinned to a stale container id for that service; force-recreate those peers only.
- `Restart Tunnel Group` on a card restarts that card’s group only.
- Enabling a tunnelled app recreates **its** tunnel group (not necessarily every tunnel).

## Provision / wiring

Replace hard-coded `http://gluetun:<port>` bases with `http://{tunnel_service(app)}:<port>` wherever provision or Helm talks to tunnelled apps across `kine_internal`.

Path maps and NFS layout are unchanged.

## Backups / restore

- Source of truth remains `vpn-profiles.json` (+ per-tunnel conf dirs under stack config).
- On restore (or Helm boot after restore): migrate schema if needed, then **regenerate** the routing override from JSON — do not require the override file to be present in the tarball.

## Testing (acceptance)

- Unit: migration `active_id` → `primary_id`; exclusivity of `apps`; `tunnel_service` leftovers → `gluetun`.
- Unit: generated override includes secondary service + correct `network_mode` + Traefik labels on the owning Gluetun.
- Unit/integration: provision URL helper uses secondary host when assigned.
- Frontend: checklist UI; Set as Primary; soft warning >3 tunnels.
- Manual on osiris: assign Live TV apps to Proton, Acquisition leftovers on Njalla primary; confirm distinct public IPs via per-tunnel leak test; Traefik hosts still open; Seerr still reaches Sonarr.

## Risks

- Compose include path must be shared by Helm and `./kine` or secondaries silently never start.
- Moving Traefik labels off a static file is a sharp edge — empty generated file must not brick routers; generate a full label set whenever VPN is enabled.
- Cross-app HTTP that assumed shared loopback (`127.0.0.1`) between apps now on **different** tunnels will break; those pairs must use `kine_internal` + `tunnel_service` DNS instead. Audit provision and Live TV wiring for `127.0.0.1` assumptions across app boundaries.
