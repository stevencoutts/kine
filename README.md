# Kine

Kine is a self-hosted media appliance built from Docker Compose. It combines
Emby, the Sonarr/Radarr/Prowlarr/Jackett acquisition stack, VPN-routed download tools,
and IPTV/sports services behind one Traefik HTTPS entry point.

The provisioner does more than start containers:

- Sonarr and Radarr receive their root folders and enabled download clients.
- Prowlarr is linked to Sonarr and Radarr, seeds three public indexers
  (KickassTorrents, The Pirate Bay, and 1337x), and registers Transmission
  as its download client.
- Jackett (optional) receives a derived API key and the same three public
  indexers when enabled; Torznab feeds into Sonarr/Radarr stay manual
  because Prowlarr is the wired indexer proxy.
- Emby's first-run wizard is completed with Movies, TV, and Sports libraries.
- Seerr (optional) is linked to Sonarr and Radarr over `gluetun` once its
  wizard Sign In has created the admin user; re-run provision after that.
- Transmission runs in gluetun's WireGuard namespace; ProtonVPN port changes
  are copied into Transmission by the `vpn-portsync` sidecar.

Kine targets a dedicated Linux host for production. Docker Desktop on macOS is
useful for development and integration testing, with limitations described
below.

## Production requirements

- Linux x86_64 with Bash, Python 3, PyYAML, OpenSSL, and standard GNU user and
  filesystem tools
- Docker Engine and Docker Compose **2.20+** (`include:` is required)
- free TCP ports for Traefik (defaults **8080** / **8443**; `install.sh`
  picks the next free ports automatically when those are taken)
- `/dev/net/tun` for gluetun
- one filesystem mounted at `DATA_ROOT` for both media and downloads if you
  want hardlinks and atomic imports
- enough space for application state and images (preflight warns below 20 GB
  free on `/`)
- optional `/dev/dri/renderD128` for Intel QuickSync; the installer detects
  its render-group GID

For host-managed NFS mounts, also install the distribution's NFS client,
`util-linux` (`mountpoint` and `findmnt`), and use a systemd-compatible
`/etc/fstab`.

Kine is designed for a trusted LAN. Do not expose Helm or the Docker socket
proxy directly to the internet.

## Linux installation

```bash
git clone <repository-url> kine
cd kine
sudo install -d /srv/kine/config/{ecm,teamarr,unpackerr}
sudo touch /srv/kine/config/{ecm/ecm.env,teamarr/teamarr.env,unpackerr/unpackerr.env}
sudo ./install.sh
```

The empty environment files are currently required because Compose validates
each `env_file` before the provisioner can populate it.

The installer is idempotent. On its first run it:

1. creates `.env` from `.env.example` (or merges any missing keys into an
   existing one), generates secrets, and picks free Traefik HTTP/HTTPS ports
   when the defaults are already in use;
2. checks Compose, storage, TUN, and optional GPU support;
3. creates the `kine` service user and the data/config directories;
4. prepares TLS, seeds application configuration, starts the enabled profiles,
   and runs the provisioner.

Open the URL printed at the end, normally:

- admin: `https://kine-admin.kine.local:8443` (or `:443` if you set that in `.env`)
- Emby: `https://emby.kine.local:8443`
- recovery access to Helm: `http://<host-lan-ip>:8600`

The first-run admin password must be at least 12 characters. If VPN is enabled
in onboarding, paste a valid WireGuard client configuration. Prowlarr ships
with three public indexers. For Live TV: enable the Live TV section, finish
Dispatcharr’s first login, create an API token in its profile, then paste that
token under Helm → Settings → Live TV. Helm registers Dispatcharr as Emby’s
HDHomeRun tuner and writes the token into ECM and Teamarr.

The default `internal` TLS mode uses Traefik's generated self-signed
certificate, so browsers warn. `custom` mode reads `fullchain.pem` and
`privkey.pem` from `${STACK_ROOT}/config/traefik/certs/`.

`acme-dns` issues a Let's Encrypt wildcard for `*.${KINE_DOMAIN}` via
DNS-01 (default provider: ClouDNS). Set `KINE_ACME_EMAIL` and enter your
ClouDNS API Auth ID / password in Helm → Settings → Appliance (stored in
`${STACK_ROOT}/config/traefik/acme.env`), then Save so Traefik recreates.
No inbound port 80/443 is required for renewal.

## Application defaults

Fresh installs start every visible application section disabled. Core platform
services and mDNS start immediately; onboarding may also start the hidden
Gluetun gateway when VPN is selected.

Enabling a section selects its catalogue defaults:

- media: none (enable Emby individually when wanted)
- acquisition: Sonarr, Radarr, Prowlarr, Transmission, and Recyclarr
- process: Tdarr
- live TV: Dispatcharr, Enhanced Channel Manager (ECM), and Teamarr
- metrics: Grafana, which pulls in Prometheus, cAdvisor and node-exporter

Optional apps remain individually available: Emby, Jackett, Bazarr,
NZBGet, Unpackerr, and Seerr. Prowlarr remains the indexer
proxy wired into Sonarr and Radarr. Jackett ships with the same three
public indexers when enabled; its Torznab feeds are copied manually.

Recyclarr is an acquisition default. When enabled it syncs TRaSH Guide 1080p
quality profiles and custom formats into Sonarr (`WEB-1080p`) and Radarr
(`HD Bluray + WEB`) on a daily cron schedule. Config is seeded under
`${STACK_ROOT}/config/recyclarr/` before first start; `./kine provision`
refreshes API keys from the live *arr configs.

Seerr is not VPN-tunnelled. After enabling it, complete its setup wizard
through Sign In (and media-server setup). `./kine provision` then registers
Sonarr and Radarr at host `gluetun` ports `8989` / `7878` with the Recyclarr
quality profiles (`WEB-1080p` / `HD Bluray + WEB`, falling back to stock
`HD-1080p` if those are missing) and the live API keys. Re-running provision
updates an existing Seerr server entry when its active profile is wrong.
Finish the wizard Configure Services step (or skip it if provision already
filled it) when prompted.

The **Metrics** tier is off by default and records the stack's own history.
Enabling it starts four containers: cAdvisor for per-container CPU, memory,
network and disk; node-exporter for host CPU, memory, filesystems and
sensors; Prometheus to store them for 30 days (capped at 2 GB so it cannot
fill the media disk); and Grafana to draw them. Application statistics come
from Helm itself, which already holds every app's API key — it exposes
library sizes, download queues, subtitles wanted, indexer activity and live
Plex and Emby sessions at `/api/metrics`, refreshed every 60 seconds. Pending
image updates are published there too, but they mirror the nightly update
check (04:00 by default, `HELM_UPDATE_CHECK_CRON`) rather than the 60-second
cycle, because comparing digests means querying every registry in turn and
takes far longer than a minute; the update panels stay empty until that job
has run once. Only Grafana appears in Apps; the other three are hidden
plumbing pulled in as dependencies, and disabling the section stops all four.

Grafana is readable without signing in, which is what lets Helm embed panels;
`GRAFANA_ADMIN_PASSWORD` in `.env` is only needed to edit dashboards. Four
dashboards are provisioned from `provision/assets/grafana/dashboards/` — an
overview, per-container detail, host resources, and media statistics — so they
live in git rather than inside `grafana.db`. History starts when you enable the
tier; the graphs fill in over the following hours.

Helm's **Stats** page embeds the overview panels, and each app card on the Apps
page grows a CPU sparkline drawn from a single Prometheus range query. With the
Metrics tier off, the Stats page offers to turn it on and the app cards render
exactly as before.

Tdarr lives in the **Process** tier (not tunnelled). Enable the Process
section from Apps, then point libraries at `/media/...` in its UI. Transcode
cache is `${DATA_ROOT}/cache/tdarr` on local disk, or the NFS export assigned
as `NFS_CACHE` after `sudo ./scripts/mount-media.sh`.

Important defaults:

- domain: `kine.local`
- timezone: `Europe/London`
- stack state: `/srv/kine`
- media/download data: `/srv/media-data`
- Helm direct port: `0.0.0.0:8600`
- admin username: `admin`
- VPN provider/type: ProtonVPN with WireGuard and port forwarding
- TLS mode: `internal`

`install.sh` creates:

```text
/srv/media-data/
├── media/
│   ├── movies/
│   ├── tv/
│   ├── sports/
│   └── recordings/
└── downloads/
    ├── incomplete/
    └── complete/
```

Sonarr uses `/data/media/tv`, Radarr uses `/data/media/movies`, and completed
downloads use `/data/downloads/complete`. Emby mounts media read-only.

## Docker Desktop on macOS

macOS is a test environment, not the production target. Docker Desktop's Linux
VM provides `/dev/net/tun`, so gluetun can be exercised, but it does not expose
Intel `/dev/dri`; Emby therefore uses software transcoding.

Use repository-local state and the checked-in macOS Compose override:

```bash
cp .env.example .env
mkdir -p .local/stack/config/{ecm,teamarr,unpackerr} .local/data
touch .local/stack/config/{ecm/ecm.env,teamarr/teamarr.env,unpackerr/unpackerr.env}
```

Before starting, edit `.env`:

- set `STACK_ROOT` and `DATA_ROOT` to **absolute** paths under this checkout,
  such as `<repo>/.local/stack` and `<repo>/.local/data`;
- set `PUID=$(id -u)` and `PGID=$(id -g)` to their numeric values;
- generate `KINE_SECRET` and `HELM_SESSION_SECRET` with
  `openssl rand -hex 32`;
- add `COMPOSE_FILE=docker-compose.yml:.local/compose.macos.yml`;
- leave all `NFS_*` values empty;
- provide a valid WireGuard configuration, or remove gluetun and every
  acquisition app from `COMPOSE_PROFILES` for non-VPN UI testing.

Then use the same seed-before-start order as the installer:

```bash
./scripts/tls-setup.sh
docker compose build provision helm
./kine seed
docker compose pull --ignore-buildable
./kine up
./kine provision
```

The override removes Emby's `/dev/dri` device mapping. `.env` and `.local/`
are ignored by Git. mDNS uses host networking, whose LAN behavior differs
under Docker Desktop; `http://localhost:8600` is the dependable Helm URL for
local testing. When Helm is opened on loopback, app launch buttons use
`<app>.127.0.0.1.nip.io`; these names resolve to loopback but still traverse
Traefik rather than exposing application ports. Internal TLS may show the same
certificate warning as the production-domain URLs until its CA is trusted.

## NFS storage

NFS is mounted by the **Linux host**, not by Helm or an application container.
In **Settings → Media Storage**, enter the NFS server and click **Browse**
(or **Browse…** on a role). Exports are listed with readable names (UniFi
`/.data` shares show as `media`, `Downloads`, etc.). Open a folder to list
subdirectories when the NFS server allows this host to mount.

On Docker Desktop, subfolder listing often fails because the Linux VM’s IP is
not on the share allowlist — even when your Mac’s LAN IP is. Run a host browse
agent (needs sudo for mounts), then use Browse in Settings as usual:

```bash
sudo ./kine nfs-agent
```

Leave that terminal open. Alternatively pick the export and use **Append
subfolder** (for example `TV`), then **Use this path**. Values are written to
`.env`:

```dotenv
NFS_SERVER=192.168.1.10
NFS_MEDIA=/exports/media
NFS_DOWNLOADS=/exports/media/downloads
NFS_CACHE=/exports/cache
```

Leave `NFS_TV` / `NFS_MOVIES` empty when those folders already live under
`NFS_MEDIA`. Leave `NFS_CACHE` empty to keep Tdarr's transcode cache on local
disk under `${DATA_ROOT}/cache/tdarr`.

Apply the settings as root:

```bash
sudo ./scripts/mount-media.sh
```

The script manages a `# BEGIN kine-nfs` block in `/etc/fstab` and mounts:

- `NFS_MEDIA` at `${DATA_ROOT}/media`
- `NFS_DOWNLOADS` at `${DATA_ROOT}/downloads`
- `NFS_CACHE` at `${DATA_ROOT}/cache/tdarr` (optional)
- optional `NFS_TV` / `NFS_MOVIES` at `${DATA_ROOT}/media/tv` and `…/movies`

Separate NFS mount points cannot hardlink to one another, even when they are
exports from the same server. For Sonarr/Radarr hardlinks, mount one media
export, set `NFS_DOWNLOADS` to a folder under that export (for example
`…/media/downloads`), and leave nested `NFS_TV` / `NFS_MOVIES` empty.
`mount-media.sh` then **symlinks** `${DATA_ROOT}/downloads` into that media
folder (a second NFS/bind mount of the same folder still cannot hardlink).
Verify:

```bash
stat -c '%d' /srv/media-data/media /srv/media-data/downloads /srv/media-data/media/Movies
ln /srv/media-data/downloads/.probe /srv/media-data/media/Movies/.probe && rm -f /srv/media-data/downloads/.probe /srv/media-data/media/Movies/.probe
```

After changing NFS settings in Helm, run `mount-media.sh` on the host; Helm
only saves the values (and can browse exports). Stop dependent containers
before changing a live mount.

## VPN and TUN behavior

Helm manages multiple WireGuard profiles under `config/helm/vpn-profiles.json`.
The first visit to the VPN tab (or `GET /api/vpn`) imports an existing
`config/gluetun/wireguard/wg0.conf` as profile **Default** when the profiles
file is missing. Activating a profile writes `wg0.conf` and the derived
gluetun env keys, then recreates the tunnel group. Tunnelled apps remain
global via `VPN_TUNNELLED_APPS`. OpenVPN can be stored as a type later; only
WireGuard activates in this release.

Most of the acquisition tier does not merely route through gluetun. Sonarr,
Radarr, Prowlarr, Bazarr, Transmission, NZBGet, and Unpackerr join
gluetun's network namespace with `network_mode: service:gluetun`. Live TV
(Dispatcharr, ECM, Teamarr) joins the same tunnel so IPTV egress uses the
VPN. Seerr stays on `kine_internal` and talks to those apps as
`gluetun:<port>`. Emby stays untunnelled and reaches Dispatcharr HDHomeRun
at `gluetun:9191`.

Consequences:

- the apps have no independent network interface or fallback route;
- a failed tunnel stops the entire acquisition tier instead of leaking;
- web routers for tunnelled apps live on the gluetun service;
- apps communicate inside the namespace over `127.0.0.1`;
- all tunnelled apps share one port space (see
  [the port map](docs/port-map.md));
- restarting gluetun invalidates every dependent namespace, so use
  `./kine vpn restart`, not `./kine restart gluetun`.

`FIREWALL_OUTBOUND_SUBNETS` must include the LAN and Docker bridge ranges that
Traefik and the provisioner need. Check the tunnel rather than assuming it:

`COMPOSE_PROFILES` controls whether gluetun and its dependants run. Changing
`VPN_ENABLED` alone does not change Compose profiles; the onboarding/API code
updates both values together.

```bash
./kine vpn status
./kine vpn leaktest
```

The leak test compares an ordinary container's public IP with a container
sharing gluetun's namespace. Different addresses pass; unavailable addresses
make the result inconclusive.

## Architecture

**Compose profiles are the source of truth.** `docker-compose.yml` includes
one fragment per service from `compose/`. Each optional application has a
same-named profile, and `.env`'s `COMPOSE_PROFILES` selects what runs.

**Dependencies resolve transitively, and both files must agree.** Enabling
Grafana pulls in Prometheus, which pulls in cAdvisor and node-exporter. Any
`depends_on` in a Compose fragment has to be mirrored by a `requires` in
`catalogue.yml`: without it Helm enables the profile but not its dependency's,
and Compose then rejects every command because `depends_on` points at a
service no active profile provides. A test enforces the mirror.

**Image channels are optional and explicit.** Catalogue entries may declare
a `dev_tag` (for example `develop` or `beta`). Membership in
`APP_DEV_CHANNELS` switches that app's `<APP>_TAG` to the develop pin,
remembering the previous value in `<APP>_STABLE_TAG`. Fresh installs leave
`APP_DEV_CHANNELS` empty.

**Provisioning is deterministic.** `KINE_SECRET` derives API keys for
Sonarr, Radarr, Prowlarr, and Jackett before first boot (`config.xml` for the
*arr apps, `ServerConfig.json` for Jackett). The seeder never overwrites an
existing key. The provisioner then performs idempotent API wiring.

**A single `/data` preserves imports.** Sonarr and Radarr receive one
`DATA_ROOT:/data` mount. Splitting downloads and media into separate container
mounts would turn hardlinks and atomic moves into copies.

**Traefik is the HTTPS front door.** Enabled applications are routed by
subdomain. The `mdns` profile advertises `kine.local` and enabled subdomains on
the LAN; the installer also writes loopback entries to `/etc/hosts` on the
host. Only `.local` names are advertised by the mDNS container.

**Helm is an operator UI over the same files and commands.** It edits `.env`
and controls Docker through `tecnativa/docker-socket-proxy`. The proxy narrows
the API surface but still grants powerful container-management access.
Traefik separately mounts the raw Docker socket read-only for discovery.

Helm tabs map to the same operations as the CLI:

- **Apps** — enable/disable sections and individual apps, toggle development
  image channels, restart services. A **Watching** overview button shows
  active Plex and Emby sessions (from Settings credentials) and expands
  into a combined now-playing list.
- **Stats** — embedded Grafana panels for the stack's own history. Offers to
  turn the Metrics tier on when it is off, so nothing else needs configuring.
- **Updates** — split into Apps and Core containers (catalogue `hidden`
  entries and compose-only plumbing). Each section has an Update All that
  runs one container at a time and stops on the first failure. Applying an
  update clears that row in the overnight cache immediately; **Check Now**
  is the only action that re-queries registries. `dockerproxy` is host-only:
  Helm reaches Docker through it, so recreating it from the UI would cut
  that path mid-apply and leave the proxy (and often Traefik) down.
  Updating `gluetun` also force-recreates every tunnelled app onto the new
  gateway, otherwise they stay pinned to the old network namespace.
- **Settings** — domain, TLS, NFS paths, Plex/Emby library notify, and
  OpenSubtitles credentials for Bazarr.

Provisioning (`seed` / `wire`) is single-flight: Helm holds a file lock at
`${STACK_ROOT}/provision.lock` so overlapping saves, enable actions, and
background Seerr auto-wire cannot spawn concurrent `provision-run` containers.
A second wire while one is in progress returns HTTP 409 instead of racing
gluetun and tunnelled apps.

Repository layout:

```text
compose/        Compose fragment per service
provision/      config seeding and API wiring recipes
helm/           FastAPI admin backend and single-file frontend
mdns/           Avahi host generation
scripts/        preflight, TLS, storage, backup, updates, and VPN helpers
tests/          structural and backend invariants
catalogue.yml   shared application metadata and dependencies
```

Runtime state is under `STACK_ROOT`; media and downloads are under
`DATA_ROOT`. Backups include `.env`, Compose/catalogue files, and application
configuration, but never media.

## Common commands

```bash
./kine apps                 # list available and enabled apps
./kine ps                   # show running services
./kine up                   # start enabled profiles
./kine down                 # stop the stack
./kine logs sonarr          # follow the last 200 log lines
./kine enable bazarr        # enable, start, and provision an app
./kine disable bazarr       # stop and remove an app profile
./kine provision            # repeat idempotent application wiring
./kine updates              # compare local and remote image digests (text)
./kine update sonarr        # back up, pull, recreate, and health-check
./kine vpn restart          # restart gluetun and all tunnelled dependants
./kine tls                  # regenerate TLS files after changing mode
./kine backup               # back up config and environment
./kine restore <archive>    # restore a backup and restart
```

`./kine rekey` rotates every derived internal API key. External tools holding
an old key will break, so use it only after reading its confirmation warning.
See [troubleshooting](docs/troubleshooting.md) for common failures.

## Testing

Create an isolated Python environment and run the invariant suite:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install pytest pyyaml httpx
python -m pytest tests -q
```

After installation has created `.env` and the runtime environment files,
validate the effective Compose model and shell syntax:

```bash
docker compose config --quiet
for script in install.sh kine scripts/*.sh; do bash -n "$script"; done
```

On macOS, add
`COMPOSE_FILE=docker-compose.yml:.local/compose.macos.yml` to `.env` before
the Compose check. The tests cover profile/catalogue drift, shared VPN
namespace invariants, port collisions, startup ordering, data mounts, mDNS,
NFS host boundaries, provisioner behavior, and Helm's Docker access.
