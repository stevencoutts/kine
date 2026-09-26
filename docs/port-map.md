# Host ports (Traefik)

| Port | Service | Notes |
|---|---|---|
| `${TRAEFIK_HTTP_PORT}` (default 8080) | Traefik HTTP | Redirects to HTTPS |
| `${TRAEFIK_HTTPS_PORT}` (default 8443) | Traefik HTTPS | App hostnames |
| `${HELM_PORT}` (default 8600) | Helm admin UI | Also via `kine-admin.${KINE_DOMAIN}` |
| 53/tcp and 53/udp | kine-pihole | LAN DNS, only while the pihole profile is on. Published on each address in `KINE_DNS_BIND`, or on every interface when that value is empty |

Set `TRAEFIK_HTTP_PORT=80` and `TRAEFIK_HTTPS_PORT=443` in `.env` when those
ports are free on the host.

# Port map inside the tunnel

Every tier 2 application shares its tunnel container's network namespace,
which means they share one port space per tunnel. Two apps wanting the
same port on the same tunnel is not a configuration clash to resolve
later; it is a container that will not start. Anything added to tier 2
must claim a free port here first.

With multiple VPN profiles, each running tunnel has its own namespace.
The primary profile uses `gluetun`; other profiles with assigned apps
get `gluetun-<shortId>` (first 8 hex chars of the profile UUID).
Unassigned forced-tunnel apps stay on the primary tunnel.

| Port | Application | Reached from outside as |
|---|---|---|
| 6100 | ECM | `gluetun:6100` |
| 6101 | ECM MCP | `gluetun:6101` |
| 6767 | Bazarr | `gluetun:6767` |
| 6789 | NZBGet | `gluetun:6789` |
| 7878 | Radarr | `gluetun:7878` |
| 8000 | gluetun control server | `gluetun:8000` (internal only) |
| 8686 | Lidarr | `gluetun:8686` |
| 8989 | Sonarr | `gluetun:8989` |
| 9091 | Transmission | `gluetun:9091` |
| 9117 | Jackett | `gluetun:9117` |
| 9191 | Dispatcharr | `gluetun:9191` |
| 9195 | Teamarr | `gluetun:9195` |
| 9696 | Prowlarr | `gluetun:9696` |

Inside a namespace, apps address each other as `127.0.0.1:<port>`.
From `kine_internal` (Traefik, Helm, the provisioner, Emby) reach a
tunnelled app as `<tunnel_service>:<port>` — `gluetun:<port>` for apps
on the primary tunnel, or `gluetun-<shortId>:<port>` when assigned to
another profile. Helm resolves the owning tunnel from
`config/helm/vpn-profiles.json`.

# Direct Live TV

Moving Live TV to Direct on the VPN tab takes Dispatcharr, ECM, ECM MCP,
and Teamarr out of the tunnel. Each then has its own network namespace on
`kine_internal` and `kine_edge`, and the ports above are no longer shared.
They address each other by service name:

| Port | Application | Address |
|---|---|---|
| 6100 | ECM | `ecm:6100` |
| 6101 | ECM MCP | `ecm-mcp:6101` |
| 9191 | Dispatcharr | `dispatcharr:9191` |
| 9195 | Teamarr | `teamarr:9195` |

`127.0.0.1` between those apps only works while they still share a tunnel.
Helm rewrites ECM, Teamarr, and the Dispatcharr Teamarr EPG URL when the
group moves either way. Game Thumbs is never tunnelled.

# Pi-hole DNS bridge

While the pihole profile is on, primary Gluetun and Pi-hole sit on
`kine_dns` (`KINE_DNS_SUBNET`, default `172.16.53.0/27`). Docker keeps
`.1` as the gateway. Gluetun is `KINE_DNS_GLUETUN` (default
`172.16.53.2`) and Pi-hole is `KINE_DNS_PIHOLE` (default `172.16.53.3`).
Other containers on that bridge come from `KINE_DNS_POOL` (default
`172.16.53.16/28`).

Pi-hole's only general upstream is `172.16.53.2#53`. Untunnelled apps
that should use it get `dns: 172.16.53.3`: Traefik, Helm, the provisioner,
Emby, Tdarr, Beets, Seerr, Recyclarr, Game Thumbs, Grafana, Prometheus,
cAdvisor, and node-exporter. Docker's own DNS still answers container
names. Tunnelled apps, secondary Gluetun containers, and vpn-portsync
keep the resolver of the tunnel they are in.

Traefik router/service labels follow the owning tunnel: each app's HTTPS
hostname targets the Gluetun service that currently hosts that app
(primary or secondary), not a fixed `gluetun` service.

Untunnelled, on their own service names as usual:

| Application | Address |
|---|---|
| Emby | `emby:8096` |
| Tdarr | `tdarr:8265` (UI), `tdarr:8266` (server) |
| Seerr | `seerr:5055` |
| Beets | `beets:8337` |

Seerr reaches tunnelled Sonarr/Radarr at each app's assigned tunnel
host (for example `gluetun:8989` / `gluetun:7878` when both are on the
primary). Emby reaches Dispatcharr HDHomeRun at
`<tunnel_service(dispatcharr)>:9191/hdhr`.
