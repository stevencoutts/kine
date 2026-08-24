# VPN multi-profile UI (WireGuard MVP)

Date: 2026-08-24
Status: draft for review (design approved in chat; awaiting written-spec review)

## Goal

Replace the plain single-card VPN page with a Watching/Downloads-style surface that can:

- Show live tunnel status (public IP, forwarded port, tunnelled apps).
- Hold multiple VPN profiles in Helm-managed state.
- Add, edit, delete, and activate profiles.
- Reconfigure the active tunnel without re-running onboarding.
- Keep Leak Test and Restart Tunnel Group.

OpenVPN is reserved in the data model and UI chrome only; activation in this MVP is WireGuard-only.

## Decisions

| Question | Decision |
|---|---|
| Profile storage | Helm/UI state — `config/helm/vpn-profiles.json` (not one-file-per-profile as source of truth) |
| Protocol MVP | WireGuard now; OpenVPN later (schema `type` + disabled UI affordance) |
| Tunnelled apps | Global (`VPN_TUNNELLED_APPS`) for MVP; per-profile later |
| Migration | Auto-import existing `wg0.conf` as profile `"Default"` and mark active |
| Activate path | Materialize active conf to existing gluetun `wg0.conf` + env keys, then recreate tunnel group |
| Approach | Profiles JSON + materialize active |

## Data model

File: `${STACK_ROOT}/config/helm/vpn-profiles.json`

```json
{
  "active_id": "uuid-or-null",
  "profiles": [
    {
      "id": "uuid",
      "name": "Default",
      "type": "wireguard",
      "conf": "[Interface]\nPrivateKey = …\n…",
      "updated_at": "2026-08-24T19:00:00Z"
    }
  ]
}
```

Rules:

- `type` is `wireguard` | `openvpn`. Only `wireguard` may be activated in MVP.
- `conf` stores the full client config text for WireGuard.
- List/summary API responses never include `PrivateKey` lines; edit/detail may return conf only to the authenticated admin session over TLS (same trust as Settings secrets today).
- Tunnelled apps remain in `.env` as `VPN_TUNNELLED_APPS` (global).

### Materialization

On activate (and on first migrate):

1. Validate with existing `wireguard.parse_conf`.
2. `write_gluetun_conf` → `${STACK_ROOT}/config/gluetun/wireguard/wg0.conf`.
3. Write derived gluetun env keys (`VPN_*`, `WIREGUARD_*`) via `config.write`.
4. Set `VPN_ENABLED=true` and `active_id`.
5. Recreate the tunnel group (`gluetun`, `vpn-portsync`, tunnelled apps) — same discipline as today’s restart helper.

Disable VPN (optional endpoint): clear active materialization / set `VPN_ENABLED=false` using existing `empty_vpn_env` + `remove_gluetun_conf`, without deleting saved profiles.

### Migration

If `vpn-profiles.json` is missing and `wg0.conf` exists (or onboarding already wrote WireGuard env):

1. Create profiles file with one profile `name=Default`, `type=wireguard`, `conf=<file contents>`.
2. Set `active_id` to that profile.
3. Do not recreate containers solely for migration if gluetun is already healthy with that conf.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/vpn` | Status (enabled, connection_type, public_ip, forwarded_port, tunnelled) + profiles summary (`id`, `name`, `type`, `active`, `updated_at`) |
| `POST` | `/api/vpn/profiles` | Create profile `{name, type?, conf}` — WireGuard validated |
| `PUT` | `/api/vpn/profiles/{id}` | Rename and/or replace conf |
| `DELETE` | `/api/vpn/profiles/{id}` | Delete; refuse deleting the active profile until another is activated (or VPN disabled) |
| `POST` | `/api/vpn/profiles/{id}/activate` | Materialize + recreate tunnel group |
| `POST` | `/api/vpn/restart` | Existing |
| `POST` | `/api/vpn/leaktest` | Existing |
| `POST` | `/api/vpn/disable` | Optional: disconnect without deleting profiles |

`POST /api/vpn/settings` either shrinks to tunnelled-apps/global knobs only, or is superseded by profile endpoints for conf changes.

## UI

Tab **VPN** visual language aligned with Watching/Downloads:

1. **Hero / active card** — accent bar; connection type pill; Active state pill; public IP; forwarded port; tunnelled app chips or truncated list; primary actions Leak Test + Restart; secondary Disable if shipped.
2. **Profile cards** — one per profile; name; `WIREGUARD` / `OPENVPN` (OpenVPN muted/disabled); `ACTIVE` badge on current; actions Activate, Edit, Delete.
3. **Add profile** — modal or inline panel: name, paste textarea and/or `.conf` file upload; save validates before persist.
4. **Edit** — same editor prefilled; saving an active profile re-materializes (confirm if conf changed).

Empty state: short copy + Add Profile when VPN never configured.

No dashboard clutter: one job per section (status, then profiles).

## Backend modules

| Path | Role |
|---|---|
| `helm/backend/app/vpn_profiles.py` | Load/save JSON, migrate, CRUD, activate, redact helpers |
| `helm/backend/app/wireguard.py` | Unchanged parser; still used for validation/materialize |
| `helm/backend/app/main.py` | New routes; extend `GET /api/vpn` |
| `helm/frontend/index.html` | Redesigned `render.vpn` |
| `tests/test_vpn_profiles.py` | Migration, CRUD rules, activate materialize (tmpdir), redaction |

## Safety

- Never log private keys or full conf bodies.
- Validate before write/activate; return clear 400s on bad conf.
- Activate is the only path that rewrites live gluetun conf (plus edit-of-active with explicit rematerialize).
- Authenticated Helm session only (existing cookie auth).

## Out of scope (MVP)

- Activating OpenVPN profiles.
- Per-profile tunnelled app sets.
- Named-provider wizards (Mullvad, Proton, …) — stay on pasted client conf / custom gluetun.
- Split-tunnelling UI beyond the existing global tunnelled list.

## Success criteria

1. Existing install opens VPN tab and sees a `"Default"` profile matching today’s tunnel without manual re-paste.
2. User can add a second WireGuard profile and Activate it; public IP/leak test reflect the new egress after recreate.
3. Profile list/cards look intentional (accent, pills, hierarchy), not a single flat grey card.
4. Deleting/activating never leaves gluetun with an empty or unvalidated conf.

## Follow-ups (explicitly later)

- OpenVPN conf parse + activate.
- Per-profile tunnelled apps.
- Optional provider presets.
