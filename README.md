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
- free TCP ports for Traefik (defaults **8080** / **8443**; set
  `TRAEFIK_HTTP_PORT` / `TRAEFIK_HTTPS_PORT` in `.env`, or use `80` / `443`
  when those are free)
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

1. checks Compose, ports, storage, TUN, and optional GPU support;
2. creates `.env` from `.env.example` and generates the master/session secrets;
3. creates the `kine` service user and the data/config directories;
4. prepares TLS, seeds application configuration, starts the enabled profiles,
   and runs the provisioner.

Open the URL printed at the end, normally:

- admin: `https://admin.kine.local`
- Emby: `https://emby.kine.local`
- recovery access to Helm: `http://<host-lan-ip>:8600`

The first-run admin password must be at least 12 characters. If VPN is enabled
in onboarding, paste a valid WireGuard client configuration. Prowlarr ships
with three public indexers; add IPTV provider details in Dispatcharr.

The default `internal` TLS mode uses Traefik's generated self-signed
certificate, so browsers warn. `custom` mode reads `fullchain.pem` and
`privkey.pem` from `${STACK_ROOT}/config/traefik/certs/`. Although `acme-dns`
settings exist in the UI and environment, the current Compose configuration
does not attach the generated resolver arguments or provider environment to
Traefik; do not rely on that mode for production yet.

## Application defaults

Fresh installs start every visible application section disabled. Core platform
services and mDNS start immediately; onboarding may also start the hidden
Gluetun gateway when VPN is selected.

Enabling a section selects its catalogue defaults:

- media: Emby
- acquisition: Sonarr, Radarr, Prowlarr, and Transmission
- live TV: Dispatcharr, Enhanced Channel Manager (ECM), and Teamarr

Optional acquisition apps remain individually available: Jackett, Bazarr,
NZBGet, Unpackerr, Recyclarr, and Seerr. Prowlarr remains the indexer
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
NFS_TV=/exports/media/tv
NFS_MOVIES=/exports/media/movies
NFS_DOWNLOADS=/exports/downloads
NFS_CACHE=/exports/cache
```

Leave `NFS_CACHE` empty to keep Tdarr's transcode cache on local disk under
`${DATA_ROOT}/cache/tdarr`.

Apply the settings as root:

```bash
sudo ./scripts/mount-media.sh
```

The script manages a `# BEGIN kine-nfs` block in `/etc/fstab` and mounts:

- `NFS_TV` at `${DATA_ROOT}/media/tv`
- `NFS_MOVIES` at `${DATA_ROOT}/media/movies`
- `NFS_DOWNLOADS` at `${DATA_ROOT}/downloads`
- `NFS_CACHE` at `${DATA_ROOT}/cache/tdarr` (optional)

Separate NFS mount points cannot hardlink to one another, even when they are
exports from the same server. If hardlinks matter, export and mount one common
parent directly at `DATA_ROOT`, keep the per-path `NFS_*` settings empty, and
verify that media and downloads report the same device ID:

```bash
stat -c '%d' /srv/media-data/media /srv/media-data/downloads
```

After changing NFS settings in Helm, run `mount-media.sh` on the host; Helm
only saves the values (and can browse exports). Stop dependent containers
before changing a live mount.

## VPN and TUN behavior

Most of the acquisition tier does not merely route through gluetun. Sonarr,
Radarr, Prowlarr, Bazarr, Transmission, NZBGet, Unpackerr, and Recyclarr join
gluetun's network namespace with `network_mode: service:gluetun`. Seerr stays
on `kine_internal` and talks to those apps as `gluetun:<port>`.

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
./kine updates              # compare local and remote images
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
