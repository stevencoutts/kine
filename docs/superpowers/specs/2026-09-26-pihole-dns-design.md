# Pi-hole DNS beside the VPN

Date: 2026-09-26
Status: approved

## Goal

Add Pi-hole as an optional Kine app, off by default. It answers DNS for LAN devices pointed at the Kine host, and for Kine containers that are not inside a Gluetun namespace. Its only upstream is the primary Gluetun resolver, so those queries leave through the primary WireGuard tunnel and fail when that tunnel is down.

Apps that already share a Gluetun namespace stay on that Gluetun’s built-in resolver. They do not pass through Pi-hole.

## Decisions

| Question | Decision |
|---|---|
| Who is served | LAN clients on host port 53, plus containers with their own network namespace |
| Who is not served | Containers on `network_mode: service:gluetun`, and containers on `network_mode: host` (`mdns`, `nfs-browse-agent`) |
| Where Pi-hole runs | Own service on `kine_internal`, `kine_edge`, and `kine_dns`. It does not join Gluetun’s namespace |
| Upstream | Primary Gluetun only, at a fixed address on `kine_dns`. No second resolver |
| VPN down | Queries fail. Pi-hole does not fall back to a public resolver. The web UI stays up |
| DHCP | Off. The operator points the router or each device at the Kine host |
| Placement in Helm | New **Network** section. Not part of any other section’s defaults |
| Blocklists | Pi-hole defaults. No provision recipe for lists |

## Addresses

`kine_internal` is left alone so an existing install is not renumbered.

A new bridge, `kine_dns`, is declared in `compose/core.pihole.yml`:

| Name | Default |
|---|---|
| `KINE_DNS_SUBNET` | `172.16.53.0/29` |
| `KINE_DNS_GLUETUN` | `172.16.53.2` |
| `KINE_DNS_PIHOLE` | `172.16.53.3` |

`172.16.53.0/29` sits inside the default `FIREWALL_OUTBOUND_SUBNETS` entry `172.16.0.0/12`, which is what already lets Docker-bridge clients reach Gluetun. `.1` is left for the Docker gateway. Enabling Pi-hole checks that the configured `FIREWALL_OUTBOUND_SUBNETS` contains `KINE_DNS_SUBNET`, and that the two addresses are distinct hosts inside it. It does not rewrite the firewall list. A subnet that overlaps an existing Docker network is refused; the operator changes the three keys together.

If compose refuses the subnet because it overlaps another Docker network, the operator changes the three keys together and enables again.

## Data flow

```text
LAN device ──host :53──► Pi-hole ──172.16.53.2:53──► primary Gluetun ──wg0──► upstream
untunnelled container ──embedded DNS──► Pi-hole ──same path──►
tunnelled app ──that Gluetun’s resolver──► that tunnel’s wg0
```

Docker’s embedded DNS on a user-defined network still answers container names. `dns:` is only the forwarder for names it does not know. Each untunnelled service joins `kine_dns` so `KINE_DNS_PIHOLE` is reachable, and gets `dns: [${KINE_DNS_PIHOLE}]`.

Pi-hole’s own resolver is `dns: [${KINE_DNS_GLUETUN}]`, so blocklist downloads use the tunnel too. It does not need Docker DNS: the upstream is a numeric address.

Gluetun’s built-in DNS stays on. `DOT` stays `off`. `DNS_ADDRESS` is not pointed at Pi-hole. Secondary Gluetun services are not attached to `kine_dns`.

Let’s Encrypt DNS-01 stays on the resolvers written by `scripts/tls-setup.sh` (`1.1.1.1`, `8.8.8.8`). Those lookups do not go through Pi-hole.

`recyclarr` is untunnelled today so it can run while the tunnel is down. With Pi-hole enabled, its name lookups follow this kill switch along with the other untunnelled apps.

Host-network services keep the host resolver. Kine does not edit the host’s `resolv.conf`. Pointing the host at itself is the operator’s choice and is what makes `mdns` and the NFS browse agent use Pi-hole.

## Compose

`compose/core.pihole.yml`, included from `docker-compose.yml`. Same rules as an untunnelled app (`docs/adding-an-app.md`):

- `profiles: ["pihole"]`, `container_name: kine-pihole`
- `image: pihole/pihole:${PIHOLE_TAG}`
- config at `${STACK_ROOT}/config/pihole` mounted on `/etc/pihole`
- `ports: ["53:53/tcp", "53:53/udp"]` — web UI is Traefik-only, not a published host port
- networks: `kine_internal`, `kine_edge`, and `kine_dns` with `ipv4_address: ${KINE_DNS_PIHOLE}`
- `dns: [${KINE_DNS_GLUETUN}]`
- `depends_on: gluetun` with `condition: service_healthy`
- healthcheck queries `127.0.0.1` for `pi.hole`, so health does not depend on the upstream answering
- Traefik labels for `pihole.${KINE_DOMAIN}` and `pihole.${KINE_LOCAL_DOMAIN}`, load balancer port `80`
- no `cap_add` (DHCP and NTP are not used)

Environment:

| Variable | Value |
|---|---|
| `TZ` | `${KINE_TIMEZONE}` |
| `FTLCONF_dns_upstreams` | `${KINE_DNS_GLUETUN}#53` only |
| `FTLCONF_dns_listeningMode` | `ALL` |
| `FTLCONF_dhcp_active` | `false` |
| `FTLCONF_webserver_api_password` | `${PIHOLE_WEBPASSWORD}` |
| `FTLCONF_webserver_port` | `80` |

`FTLCONF_dns_upstreams` is set from the environment, so the web UI cannot add another upstream. That is what keeps a public resolver from appearing beside Gluetun.

## Routing override

`helm/backend/app/vpn_routing.py` already writes `docker-compose.override.yml`. When `pihole` is in the enabled profile set, that document also:

1. Attaches primary `gluetun` to `kine_dns` at `${KINE_DNS_GLUETUN}`. The override replaces Gluetun’s network list with `kine_internal`, `kine_edge`, and `kine_dns`, so a sequence merge cannot drop the bridges it already has. `docker compose config` must still show all three.
2. For each service below, adds `kine_dns` and `dns: [${KINE_DNS_PIHOLE}]`, restating that service’s existing networks in the replacement list: `traefik`, `helm`, `provision`, `emby`, `tdarr`, `beets`, `seerr`, `recyclarr`, `game-thumbs`, `grafana`, `prometheus`, `cadvisor`, `node-exporter`.

Not modified: `dockerproxy`, `mdns`, `nfs-browse-agent`, anything with `network_mode: service:gluetun`, `vpn-portsync`, and secondary Gluetun services.

When Pi-hole is not enabled, the override omits `kine_dns` and those `dns` lines. Containers return to the host resolver.

`./kine enable pihole` and the Helm app toggle both regenerate this override before `docker compose up`. `./kine disable pihole` regenerates it after the profile is removed.

## Catalogue and Helm

```yaml
pihole:
  name: Pi-hole
  tier: network
  summary: DNS for the LAN and for apps outside the VPN. Upstream queries leave through the primary tunnel.
  url: https://pi-hole.net
  releases: https://github.com/pi-hole/pi-hole/releases
  internal: http://pihole:80
  subdomain: pihole
  requires: [gluetun]
  default: false
```

`TIER_LABELS` gains `"network": "Network"`. No `dev_tag`. No `tunnelled: forced`. Enabling Pi-hole pulls Gluetun in through the existing `requires` walk. It is not mandatory and not hidden.

## Secrets and port 53

`.env.example` carries `PIHOLE_TAG=latest`, `PIHOLE_WEBPASSWORD`, and the three `KINE_DNS_*` keys. `PIHOLE_WEBPASSWORD` in the example is the sentinel `change-me`. `install.sh`, `./kine enable pihole`, and the Helm enable path replace an empty value or `change-me` with 16 random bytes written as hex before the container starts. A real password already in `.env` is left alone.

Before compose starts Pi-hole, enable checks that host TCP 53 and UDP 53 are free. If either is taken, enable stops. It names the process when the host can show who holds the socket, and otherwise reports that the port is in use. It does not stop `systemd-resolved` or anything else bound there.

## Docs

- `docs/port-map.md`: host TCP/UDP 53 published by `kine-pihole`. Port 53 is not added to the in-tunnel table.
- `README.md`: Pi-hole in the optional-apps list, plus the two operator facts: point LAN DNS at the host, and external DNS stops while the primary tunnel is down.
- `docs/troubleshooting.md`: port 53 already in use; DNS failure while the tunnel is down.

## Tests

- Catalogue: `pihole` is not a default, `requires` is `[gluetun]`, tier is `network`, and it is not `tunnelled: forced`.
- Compose fragment: no `network_mode`, publishes 53/tcp and 53/udp, upstream env is only `${KINE_DNS_GLUETUN}#53`, DHCP env is false, Traefik host is `pihole.${KINE_DOMAIN}`.
- Override renderer: with `pihole` enabled, primary Gluetun’s merged networks are `kine_internal`, `kine_edge`, and `kine_dns` at `KINE_DNS_GLUETUN`, and each listed untunnelled service has `dns` set to `KINE_DNS_PIHOLE`. With `pihole` disabled, those keys are absent. A secondary Gluetun service is unchanged.
- Firewall check: a list that does not contain `KINE_DNS_SUBNET` is rejected; the default `172.16.0.0/12` is accepted.
- Password fill: `change-me` and empty are replaced; an existing value is kept.
- `.env.example` declares every new variable the fragment interpolates.

No live VPN or container test in CI.

## Out of scope

- DHCP, either on Pi-hole or on the host
- Editing the host resolver
- Sending tunnelled apps’ DNS through Pi-hole
- Custom blocklists or a provision recipe
- Moving ACME DNS-01 off `1.1.1.1` / `8.8.8.8`
- A Helm control for the DNS subnet
