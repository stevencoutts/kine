# Pi-hole DNS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional Pi-hole that answers DNS for the LAN and for untunnelled Kine containers, with its only upstream on the primary Gluetun tunnel.

**Architecture:** Pi-hole is its own Compose service on a fixed `kine_dns` address. While the profile is on, the generated compose override attaches primary Gluetun at `172.16.53.2` and points untunnelled services at Pi-hole (`172.16.53.3`). Tunnelled apps stay on Gluetun’s built-in resolver. Enabling or disabling Pi-hole regenerates that override and recreates the tunnel group together with the DNS consumers. Shipped defaults are `KINE_DNS_SUBNET=172.16.53.0/27` and `KINE_DNS_POOL=172.16.53.16/28`. `.1` is the Docker gateway. When another program already holds port 53 on one host address, Pi-hole is published on the remaining addresses via `KINE_DNS_BIND`.

**Tech Stack:** Docker Compose, Pi-hole v6 (`FTLCONF_*`), existing Helm override writer, pytest.

**Spec:** `docs/superpowers/specs/2026-09-26-pihole-dns-design.md`

## Global Constraints

- Pi-hole is off by default, tier `network`, `requires: [gluetun]`, not `tunnelled: forced`.
- Upstream is only `${KINE_DNS_GLUETUN}#53`. DHCP is off. No second public resolver.
- Defaults: `KINE_DNS_SUBNET=172.16.53.0/27`, `KINE_DNS_POOL=172.16.53.16/28`, `KINE_DNS_GLUETUN=172.16.53.2`, `KINE_DNS_PIHOLE=172.16.53.3`.
- `PIHOLE_WEBPASSWORD` sentinel is `change-me`. Empty or sentinel is replaced with 16 random bytes as hex. An existing value is kept.
- Enable refuses to start when port 53 is bound on every interface, or when `FIREWALL_OUTBOUND_SUBNETS` does not contain `KINE_DNS_SUBNET`. A listener on one address is recorded in `KINE_DNS_BIND` and Pi-hole is published on the other host addresses. Do not rewrite the firewall list or stop `systemd-resolved`.
- Untunnelled DNS targets: `traefik`, `helm`, `provision`, `emby`, `tdarr`, `beets`, `seerr`, `recyclarr`, `game-thumbs`, `grafana`, `prometheus`, `cadvisor`, `node-exporter`.
- Not modified: `dockerproxy`, `mdns`, `nfs-browse-agent`, `network_mode: service:gluetun` apps, `vpn-portsync`, secondary Gluetun services.
- Recreate the tunnel group with its peers when Gluetun’s networks change. Do not restart Gluetun alone.
- ACME DNS-01 stays on `1.1.1.1` and `8.8.8.8`.

## File map

| File | Responsibility |
|---|---|
| `helm/backend/app/pihole_dns.py` | Firewall check, password fill, port 53 parse, override patches, recreate list |
| `helm/backend/app/vpn_routing.py` | Merge patches into `render_override` when `pihole_enabled` |
| `compose/core.pihole.yml` | Pi-hole service and `kine_dns` network |
| `catalogue.yml` | Network-tier entry |
| `kine`, `install.sh`, `helm/backend/app/main.py` | Gate, override regen, recreate |
| `docs/port-map.md`, `README.md`, `docs/troubleshooting.md` | Operator facts |

---

### Task 1: Catalogue and Helm section

**Files:**
- Modify: `catalogue.yml`, `helm/backend/app/catalogue.py`, `helm/frontend/index.html`
- Test: `tests/test_catalogue.py`, `tests/test_frontend.py`

- [ ] Add `pihole` catalogue entry and `TIER_LABELS["network"]`.
- [ ] Add `'network'` to frontend `TIER_ORDER` before `platform`.
- [ ] Tests: not a default, requires gluetun, tier network, not tunnelled forced, label is Network, frontend order contains `'network'`.

### Task 2: Pure Pi-hole helpers

**Files:**
- Create: `helm/backend/app/pihole_dns.py`
- Test: `tests/test_pihole.py`

- [ ] `firewall_allows`, `password_update`, `port53_error`, `gate`, `patches`, `services_to_recreate`.
- [ ] Tests for the spec’s firewall, password, port 53, and patch shapes.

### Task 3: Compose service, env, docs

**Files:**
- Create: `compose/core.pihole.yml`
- Modify: `docker-compose.yml`, `.env.example`, `install.sh`, `docs/port-map.md`, `README.md`, `docs/troubleshooting.md`
- Test: `tests/test_pihole.py`

- [ ] Fragment matches the spec (no `network_mode`, ports 53, single upstream, DHCP false, Traefik host).
- [ ] `.env.example` declares every interpolated variable plus `PIHOLE_DIGEST`.
- [ ] `install.sh` replaces empty/`change-me` passwords and creates the config directory.

### Task 4: Override merge

**Files:**
- Modify: `helm/backend/app/vpn_routing.py`, `helm/backend/app/main.py` (`_vpn_ensure_routing_fs`)
- Test: `tests/test_vpn_routing.py`

- [ ] `render_override(..., pihole_enabled: bool = False)` merges patches with `!override` networks.
- [ ] Disabled flag leaves existing override tests unchanged.
- [ ] Secondary Gluetun block does not gain `kine_dns`.

### Task 5: Enable and disable

**Files:**
- Modify: `kine`, `helm/backend/app/main.py`
- Test: `tests/test_pihole.py`

- [ ] CLI and Helm run `gate` before adding the profile, add `gluetun`, regenerate the override, and force-recreate the tunnel group plus enabled DNS consumers.
- [ ] Disable regenerates the override without Pi-hole, then recreates that same set so consumers return to the host resolver.
