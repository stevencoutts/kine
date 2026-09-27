# Scratch install test

Operator procedure for proving a Kine install from an empty checkout.
Run it on a spare host. It leaves a successful install running.
Teardown is the last section; do not run it as part of a passing test.

Do not copy another host's `.env`, WireGuard config, or app config onto
the machine under test. Do not reboot the host. Do not change LAN DNS.
Do not add a keepalived VIP (including `10.100.100.4`) and do not start
keepalived.

Each check is **pass**, **fail**, or **blocked**. **Blocked** means the
host cannot prove that step (usually: no WireGuard client config, so
anything that waits on a healthy tunnel cannot start). A product defect
is **fail**. Record the command and a trimmed result next to the check.

## What “provisioned” means

`./kine provision` (`provision.py wire`) is idempotent. On a full stack
it must leave:

| Piece | Expected |
|---|---|
| Sonarr / Radarr / Lidarr | Root folder `/data/media/tv`, `/data/media/movies`, `/data/media/music`. Download client Transmission (category `tv-sonarr`, `radarr`, `lidarr`) when Transmission is enabled. NZBGet client when NZBGet is enabled. |
| Prowlarr | Applications Sonarr, Radarr, and Lidarr when those apps are enabled. Download client Transmission. |
| Recyclarr | `${STACK_ROOT}/config/recyclarr/recyclarr.yml` and `secrets.yml`. Sync creates TRaSH profiles `WEB-1080p` (Sonarr) and `HD Bluray + WEB` (Radarr). |
| Bazarr | Linked to Sonarr and Radarr. |
| Unpackerr | `${STACK_ROOT}/config/unpackerr/unpackerr.env` contains `UN_SONARR_0_API_KEY` and `UN_RADARR_0_API_KEY`, plus Lidarr when Lidarr is enabled. |
| Seerr | After wizard Sign In has created user id 1, Sonarr and Radarr are registered at the tunnel host (`gluetun:8989` / `gluetun:7878` on the primary tunnel). Before Sign In, wire logs `seerr: admin user not ready` and that is expected, not a failure. |
| Emby | When Emby is enabled, wizard completed and libraries Movies, TV, Music, Sports at `/data/media/{movies,tv,music,sports}`. |
| Live TV token | Until a Dispatcharr API token exists, wire logs `dispatcharr: no API token yet`. Peer URLs in ECM and Teamarr env files are `http://127.0.0.1:9191` while those apps share a tunnel, and `http://dispatcharr:9191` after Direct Live TV. |
| Jackett | Public indexers present (wire logs `jackett: configured …`). |
| NZBGet | Control password and paths set. News servers stay empty until Settings has them. |

Tunnelled apps share Gluetun's network namespace and do not start until
`kine-gluetun` is healthy. Gluetun does not become healthy without a
real WireGuard client config. Do not invent one.

## 0. Names used below

Set these in the shell and reuse them. Change only the host-specific
values.

```bash
CLONE="${CLONE:-$HOME/Docker/kine-scratch}"
# Filled in after disk inventory. Defaults match install.sh.
STACK_ROOT="${STACK_ROOT:-/srv/kine}"
DATA_ROOT="${DATA_ROOT:-/srv/media-data}"
ADMIN_FILE="${ADMIN_FILE:-$HOME/.kine-scratch-admin}"
GIT_URL="${GIT_URL:-git@github.com:stevencoutts/kine.git}"
```

Helm's admin user is `kine-admin` (from `.env.example`). Generate the
password on the host; do not reuse another machine's password.

```bash
umask 077
openssl rand -base64 24 > "$ADMIN_FILE"
# Must be at least 12 characters. rand -base64 24 is.
```

## 1. Preconditions

Run as the operator user. `sudo -n` must work.

```bash
set -euo pipefail
id
sudo -n true && echo sudo_ok
command -v docker git python3 openssl bash
docker version --format 'server {{.Server.Version}}'
docker compose version --short
python3 -c 'import yaml; print("pyyaml", yaml.__version__)'
test -e /dev/net/tun && echo tun_ok || echo tun_MISSING
df -hP / /srv /home "$HOME" 2>/dev/null || df -hP /
ss -H -lntup 'sport = :53' || true
ss -H -lnt 'sport = :8080 or sport = :8443 or sport = :8600' || true
ip -4 addr
getent passwd kine || echo 'no kine user yet'
```

| Check | Pass |
|---|---|
| P1 Docker Engine | `docker version` prints a server version |
| P2 Compose | version ≥ 2.20 (`include:` landed in 2.20) |
| P3 Tools | `git`, `python3`, PyYAML, `openssl`, bash |
| P4 TUN | `/dev/net/tun` exists (warn in preflight if missing; VPN cannot start) |
| P5 sudo | `sudo -n true` exits 0 |
| P6 Disk | filesystem chosen for `DATA_ROOT` has room for images (tens of GB). Preflight only **warns** below 20 GB on `/`; treat under 20 GB free on the data filesystem as fail before install |
| P7 Ports | 8080 and 8443 preferred. If taken, install rewrites `TRAEFIK_HTTP_PORT` / `TRAEFIK_HTTPS_PORT`. 8600 is Helm. 53 matters only when Pi-hole is enabled |
| P8 Port 53 | Note every listener. A wildcard listener blocks Pi-hole enable. A listener on one address (libvirt `dnsmasq` on `virbr0`) does not; enable then sets `KINE_DNS_BIND` to the host's other global IPv4s |

Do not install a second Docker if one is already running. If Compose is
older than 2.20, stop and upgrade Compose; do not continue.

Optional GPU: `/dev/dri/renderD128`. Missing is a warning, not a fail.
Emby then software-transcodes.

## 2. Inventory and leftover media

```bash
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
echo '--- mounts ---'
docker ps -aq | while read -r id; do
  docker inspect -f '{{.Name}} {{range .Mounts}}{{.Source}}->{{.Destination}} {{end}}' "$id"
done
ls -la "$HOME/Docker" 2>/dev/null || true
```

Stop and remove leftover **media** containers (Radarr, Sonarr, and the
rest of an old media compose such as `$HOME/Docker/install-media-system.yml`)
only when they are not in active use:

- No published port that something else on the LAN is clearly using
  right now (a website, DNS, or a database with live clients).
- No mount you would miss. Deleted Pi-hole data does not count.
- If unsure, leave the container and record it. Do not delete unrelated
  home directories, disks, or non-media stacks.

```bash
# Example only after the inspection above says the container is leftover media.
# docker stop <name> && docker rm <name>
```

Confirm afterwards:

```bash
docker ps -a --format '{{.Names}} {{.Status}}' | grep -Ei 'pihole|keepalived|radarr|sonarr' || echo 'no leftover media names'
ip -4 addr | grep -F '10.100.100.4' && echo 'VIP PRESENT — stop' || echo 'no 10.100.100.4'
systemctl is-active keepalived 2>/dev/null || echo 'keepalived not active'
```

| Check | Pass |
|---|---|
| H1 | No `10.100.100.4` on the host. Do not add it |
| H2 | keepalived is not started by this test |
| H3 | Old Pi-hole container is absent (do not recreate the pre-Kine one) |
| H4 | Leftover media containers removed, or explicitly skipped with a reason |

## 3. Choose directories

`install.sh` defaults are `STACK_ROOT=/srv/kine` and
`DATA_ROOT=/srv/media-data`. Media and downloads must be the **same
filesystem** (hardlinks). Prefer a disk with room; both paths must sit
on it.

```bash
df -hP
findmnt -T /srv 2>/dev/null || true
findmnt -T /home 2>/dev/null || true
```

If `/srv` is large enough, keep the defaults. If not, pick one
filesystem and export both roots there before creating `.env`, for
example `STACK_ROOT=/mnt/data/kine` and `DATA_ROOT=/mnt/data/media-data`.
The git clone stays at `$CLONE` either way. `KINE_CHECKOUT` is set to
that clone by the installer.

| Check | Pass |
|---|---|
| D1 | `DATA_ROOT` and `STACK_ROOT` are on one filesystem with ≥ 20 GB free |

## 4. Fresh clone and install.sh

```bash
rm -rf "$CLONE"
git clone --branch master "$GIT_URL" "$CLONE"
cd "$CLONE"
git rev-parse --short HEAD
git status -sb
```

`HEAD` must be `origin/master` with a clean work tree. Do not copy a
working tree from another machine.

Compose validates `env_file` paths before the provisioner can fill
them. Create the empty files on `STACK_ROOT` first (the README
requirement). `install.sh` also creates the directories; the files
must exist before `docker compose` runs.

```bash
sudo install -d \
  "$STACK_ROOT/config/ecm" \
  "$STACK_ROOT/config/teamarr" \
  "$STACK_ROOT/config/unpackerr"
sudo touch \
  "$STACK_ROOT/config/ecm/ecm.env" \
  "$STACK_ROOT/config/teamarr/teamarr.env" \
  "$STACK_ROOT/config/unpackerr/unpackerr.env"
```

If you are **not** using the default roots, write `.env` before
`install.sh`. The installer generates `KINE_SECRET` and
`HELM_SESSION_SECRET` only when `.env` is absent, so a hand-made `.env`
must already contain fresh secrets. Leave `PIHOLE_WEBPASSWORD=change-me`
so the installer replaces it.

```bash
# Skip this whole block when the defaults /srv/kine and /srv/media-data are correct.
cp .env.example .env
umask 077
sed -i "s|^KINE_SECRET=.*|KINE_SECRET=$(openssl rand -hex 32)|" .env
sed -i "s|^HELM_SESSION_SECRET=.*|HELM_SESSION_SECRET=$(openssl rand -hex 32)|" .env
sed -i "s|^STACK_ROOT=.*|STACK_ROOT=${STACK_ROOT}|" .env
sed -i "s|^DATA_ROOT=.*|DATA_ROOT=${DATA_ROOT}|" .env
```

Install:

```bash
cd "$CLONE"
sudo ./install.sh
```

This builds Helm, the provisioner, and mDNS, pulls the enabled images,
and can take a long time. Let it finish. Do not reboot.

### 4.1 Secrets, ports, directories, profiles

```bash
cd "$CLONE"
sudo grep -E '^(KINE_SECRET|HELM_SESSION_SECRET|PIHOLE_WEBPASSWORD|COMPOSE_PROFILES|TRAEFIK_HTTP_PORT|TRAEFIK_HTTPS_PORT|STACK_ROOT|DATA_ROOT|VPN_ENABLED|KINE_DNS_BIND|PUID|PGID)=' .env \
  | sed -E 's/(SECRET|PASSWORD)=.*/\1=<redacted>/'
# Secrets must be non-empty and not the example placeholders.
sudo awk -F= '
  $1=="KINE_SECRET" || $1=="HELM_SESSION_SECRET" { ok = (length($2)>=32 && $2 !~ /change-me/); print $1, (ok?"pass":"FAIL") }
  $1=="PIHOLE_WEBPASSWORD" { ok = ($2!="" && $2!="change-me"); print $1, (ok?"pass":"FAIL") }
  $1=="COMPOSE_PROFILES" { print $1, $2 }
  $1=="VPN_ENABLED" { print $1, $2 }
' .env
getent passwd kine
sudo test -d "$DATA_ROOT/media/movies" && echo movies_ok
sudo test -d "$DATA_ROOT/downloads/complete" && echo downloads_ok
sudo test -d "$STACK_ROOT/config/traefik/certs" && echo certs_ok
grep -n 'BEGIN kine' /etc/hosts
```

| Check | Pass |
|---|---|
| I1 | `install.sh` exits 0 |
| I2 | `KINE_SECRET` and `HELM_SESSION_SECRET` are fresh hex (64 chars), not example values |
| I3 | `PIHOLE_WEBPASSWORD` is not empty and not `change-me` |
| I4 | `COMPOSE_PROFILES` is `mdns` only. `VPN_ENABLED=false` |
| I5 | `kine` user exists. `PUID`/`PGID` match `id kine` |
| I6 | Traefik ports recorded. Installer message is either “ports free” or “ports busy; using X/Y” |
| I7 | Directory tree exists under `STACK_ROOT` and `DATA_ROOT` (media movies/tv/sports/recordings, downloads incomplete/complete) |
| I8 | `/etc/hosts` has a `# BEGIN kine` block for `kine.local` |

### 4.2 Containers after a default install

Always-on (no profile): `kine-traefik`, `kine-helm`, `kine-dockerproxy`,
`kine-nfs-browse-agent`. Profile `mdns`: `kine-mdns`. The provisioner
container exits after seed/wire (`restart: no`); it should not stay up.

These must **not** be running yet: gluetun, any *arr, Emby, Pi-hole,
Grafana, Dispatcharr.

```bash
cd "$CLONE"
sudo docker compose ps -a
sudo docker inspect -f '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
  kine-traefik kine-helm kine-dockerproxy kine-mdns kine-nfs-browse-agent
curl -fsS "http://127.0.0.1:$(grep ^HELM_PORT= .env | cut -d= -f2)/api/health"; echo
```

| Check | Pass |
|---|---|
| C0 | The five containers above are running; Helm and Traefik are `healthy` |
| C1 | `/api/health` returns ok |
| C2 | No catalogue app container is running |

## 5. WireGuard on this host only

```bash
sudo ls -la /etc/wireguard 2>/dev/null || echo 'no /etc/wireguard'
sudo find "$STACK_ROOT" /etc/wireguard "$HOME" -name 'wg*.conf' -o -name '*wireguard*' 2>/dev/null | head -50
```

Ignore configs that are clearly another product's leftover if you are
not willing to use them. **Do not** copy a config from another Kine
host.

- **Found a client config you will use:** save its path as `WG_CONF`.
  Onboarding below sets `vpn_enabled` true and sends that file.
  Acquisition can be proven.
- **Not found:** set `WG_CONF=""`. Complete onboarding with VPN off.
  Record **blocked** for every check that needs a healthy tunnel
  (acquisition, tunnelled Live TV, Pi-hole health, Recyclarr start).
  Continue with everything that does not wait on Gluetun. Do not
  fabricate keys.

```bash
# Onboarding. VPN off:
cd "$CLONE"
HP=$(grep ^HELM_PORT= .env | cut -d= -f2)
curl -sS -D - -o /tmp/kine-setup.json -X POST "http://127.0.0.1:${HP}/api/setup" \
  -H 'Content-Type: application/json' \
  -d "{\"password\":$(python3 -c 'import json,pathlib; print(json.dumps(pathlib.Path("'"$ADMIN_FILE"'").read_text().strip()))'),\"vpn_enabled\":false}"
```

With a real config, POST the same URL with `vpn_enabled: true` and
`wireguard_conf` set to the file contents (JSON-encoded). Expect HTTP
200 and `{"ok": true}`. HTTP 400 on VPN means the config was rejected;
do not replace it with a made-up one.

```bash
curl -sS -c /tmp/kine.cookies -X POST "http://127.0.0.1:${HP}/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"kine-admin\",\"password\":$(python3 -c 'import json,pathlib; print(json.dumps(pathlib.Path("'"$ADMIN_FILE"'").read_text().strip()))')}"
curl -fsS -b /tmp/kine.cookies "http://127.0.0.1:${HP}/api/apps" | python3 -m json.tool | head -40
grep ^COMPOSE_PROFILES= .env
grep ^VPN_ENABLED= .env
```

| Check | Pass |
|---|---|
| V0 | WireGuard decision recorded: path used, or explicit gap |
| V1 | `/api/setup` returns 200. Second call returns 409 already configured |
| V2 | Login sets cookie `kine_session`. `/api/apps` returns tiers |
| V3 | With VPN off, `VPN_ENABLED=false` and `COMPOSE_PROFILES` does not gain tunnelled apps during setup |

Setup with `vpn_enabled: false` **removes** tunnelled profiles. Do
onboarding before enabling sections.

## 6. Enable catalogue sections

The Dashboard calls `POST /api/tiers/<tier>/enable`, which pulls
`requires` (Gluetun, Prometheus, and so on). `./kine enable <app>` does
**not** pull dependencies. Use the API for sections. Use `./kine enable`
for an optional app only after `gluetun` is already in
`COMPOSE_PROFILES` (or the app does not need it).

Cookie file from section 5. Sections and their defaults:

| Section | API | Defaults started | Also pulled in |
|---|---|---|---|
| Media | none (404 `no default apps in this section`) | — | enable `emby` alone |
| Acquisition | `acquisition` | sonarr, radarr, prowlarr, transmission, recyclarr | gluetun, vpn-portsync |
| Process | `process` | tdarr | — |
| Live TV | `live` | dispatcharr, ecm, teamarr, game-thumbs | gluetun |
| Metrics | `metrics` | grafana | prometheus, cadvisor, node-exporter |
| Network | none (404) | — | enable `pihole` alone |

```bash
enable_tier() {
  local tier="$1"
  curl -sS -D /tmp/kine-tier.hdr -o /tmp/kine-tier.json -b /tmp/kine.cookies \
    -X POST "http://127.0.0.1:${HP}/api/tiers/${tier}/enable" \
    -H 'Content-Type: application/json' -d '{}'
  echo "--- $tier ---"; head -n 1 /tmp/kine-tier.hdr; python3 -m json.tool /tmp/kine-tier.json 2>/dev/null | head -20
}
```

Order when a tunnel is healthy: metrics, process, acquisition, live.
Media and network have no defaults; confirm the 404, then enable the
optional apps in section 7.

Order when **no** tunnel: metrics, process, then `emby`, `beets`,
`seerr`, `game-thumbs`. Attempt acquisition, live, and pihole once so
the failure is recorded, then mark those checks **blocked** if the
cause is Gluetun never becoming healthy. Do not loop.

```bash
enable_tier metrics
enable_tier process
# Acquisition and Live TV only become healthy with a tunnel:
enable_tier acquisition || true
enable_tier live || true
enable_tier media || true    # expect 404
enable_tier network || true  # expect 404
```

Wait for health. Image pulls of a full stack take a long time; a
20-minute budget per wave is normal. Do not reboot.

```bash
wait_healthy() {
  local name="$1" tries="${2:-40}"
  local i=0 st
  while (( i < tries )); do
    st=$(sudo docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || echo missing)
    echo "$name $st"
    case "$st" in
      "running healthy"|*" running healthy") return 0 ;;
      missing) return 2 ;;
    esac
    sleep 15; i=$((i+1))
  done
  return 1
}
```

Containers that do **not** need a healthy Gluetun:

| App | Container | Health |
|---|---|---|
| Tdarr | `kine-tdarr` | healthy, UI port 8265 inside the container |
| Emby | `kine-emby` | healthy after enable |
| Beets | `kine-beets` | healthy after enable |
| Seerr | `kine-seerr` | healthy after enable |
| Game Thumbs | `kine-game-thumbs` | healthy, `/health` contains `"status":"ok"` |
| Grafana | `kine-grafana` | healthy |
| Prometheus | `kine-prometheus` | healthy |
| cAdvisor | `kine-cadvisor` | healthy |
| node-exporter | `kine-node-exporter` | healthy |

Containers that **do** need `kine-gluetun` healthy (otherwise **blocked**):

`kine-gluetun`, `kine-vpn-portsync`, `kine-sonarr`, `kine-radarr`,
`kine-lidarr`, `kine-prowlarr`, `kine-jackett`, `kine-bazarr`,
`kine-transmission`, `kine-nzbget`, `kine-unpackerr`, `kine-recyclarr`,
`kine-dispatcharr`, `kine-ecm`, `kine-ecm-mcp`, `kine-teamarr`,
`kine-pihole`.

Recyclarr and Pi-hole are not inside the namespace, but their Compose
`depends_on` waits for Gluetun to be healthy, so they stay blocked
with the tunnel.

```bash
sudo docker logs kine-gluetun --tail 30
```

Pass for a blocked tunnel: the log shows a missing or invalid
WireGuard config, not a crash-loop from a bad compose file. Fail if
Gluetun exits because of a Kine defect (bad env, missing device when
`/dev/net/tun` exists, port collision Kine introduced).

### Traefik routes

Untunnelled apps publish their own router labels. Tunnelled apps get
routers from Helm's VPN routing file
(`${STACK_ROOT}/config/traefik/dynamic` plus `docker-compose.override.yml`).
Without VPN those tunnelled routers are absent; that is **blocked**,
not a missing label bug, until the tunnel is up.

```bash
HTTPS=$(grep ^TRAEFIK_HTTPS_PORT= .env | cut -d= -f2)
probe() {
  local host="$1"
  echo -n "$host "
  curl -sk -o /dev/null -w '%{http_code}\n' --resolve "${host}:443:127.0.0.1" \
    "https://${host}:${HTTPS}/" || true
}
# Traefik's entrypoint is TRAEFIK_HTTPS_PORT, not necessarily 443.
# Use --connect-to so the Host header stays the app name.
probe_port() {
  local host="$1"
  echo -n "$host "
  curl -sk -o /dev/null -w '%{http_code}\n' \
    --connect-to "${host}:${HTTPS}:127.0.0.1:${HTTPS}" \
    -H "Host: ${host}" \
    "https://${host}:${HTTPS}/" || true
}
DOM=$(grep ^KINE_DOMAIN= .env | cut -d= -f2)
for name in kine-admin emby tdarr beets seerr grafana thumbs pihole sonarr radarr prowlarr transmission jackett bazarr nzbget tv channels sports mcp; do
  probe_port "${name}.${DOM}"
done
```

| Check | Pass |
|---|---|
| R1 | `kine-admin.$DOMAIN` returns something other than connection refused (200/302/401 from forwardAuth are all fine) |
| R2 | Each **running** app's hostname answers through Traefik (not 000) |
| R3 | A hostname whose container never started is **blocked**, not fail |

## 7. Optional apps

Enable with the CLI once dependencies that the CLI does not add are
already satisfied, or with the API (the API adds `requires`).

```bash
enable_app() {
  local app="$1"
  curl -sS -D - -o /tmp/kine-app.json -b /tmp/kine.cookies \
    -X POST "http://127.0.0.1:${HP}/api/apps/${app}/enable" \
    -H 'Content-Type: application/json' -d '{}'
  echo
}
```

| App | Needs healthy Gluetun | Traefik host |
|---|---|---|
| emby | no | `emby` |
| lidarr | yes | `lidarr` (router on the tunnel) |
| beets | no | `beets` |
| jackett | yes | `jackett` |
| bazarr | yes | `bazarr` |
| nzbget | yes | `nzbget` |
| unpackerr | yes | none (no UI) |
| seerr | no | `seerr` |
| ecm-mcp | yes | `mcp` |
| pihole | yes | `pihole` |

```bash
for app in emby beets seerr; do enable_app "$app"; wait_healthy "kine-$app" || true; done
# Game Thumbs is a Live TV default. If the live section was blocked, enable it alone:
enable_app game-thumbs || true
wait_healthy kine-game-thumbs || true
# Tunnelled optionals. One attempt each. Blocked if Gluetun is not healthy.
for app in lidarr jackett bazarr nzbget unpackerr ecm-mcp pihole; do
  enable_app "$app" || true
done
```

`COMPOSE_PROFILES` after a full successful enable contains every app
above plus the section defaults and `mdns`, `gluetun`, `prometheus`,
`cadvisor`, `node-exporter`. Hidden apps do not appear as their own
Dashboard rows; they do appear in `docker compose ps`.

| Check | Pass |
|---|---|
| O1 | Each optional app that can start is `running` and `healthy` (Unpackerr has no healthcheck: `running` is enough) |
| O2 | Profile list matches what was enabled. Nothing extra from another stack |

## 8. Provision wiring

Run wire once after the enables. It is safe to repeat.

```bash
cd "$CLONE"
sudo ./kine provision
sudo tail -n 80 "$STACK_ROOT/provision.log"
```

Then, only for apps that are actually healthy, check the wiring.
API keys live in each app's config after seed (`ApiKey` in
`config.xml`, Jackett `ServerConfig.json`). Read them on the host;
do not print them in a shared log.

```bash
# Example shape. Replace the key from the seeded config.xml. Do not paste keys into the report.
# sudo docker exec kine-helm curl -fsS -H "X-Api-Key: $SONARR_KEY" http://gluetun:8989/api/v3/rootfolder
```

| Check | Command result that passes | If the app never started |
|---|---|---|
| W-arr | Root folder path is `/data/media/tv` (Sonarr), `/data/media/movies` (Radarr), `/data/media/music` (Lidarr). Transmission download client exists with the category in the table above | blocked |
| W-prowlarr | Applications include Sonarr and Radarr (Lidarr if enabled) | blocked |
| W-recyclarr | `recyclarr.yml` and `secrets.yml` exist. `docker exec kine-recyclarr recyclarr sync` exits 0. Sonarr quality profile name `WEB-1080p`, Radarr `HD Bluray + WEB` | blocked (container waits on Gluetun) |
| W-bazarr | provision log contains `bazarr: linked Sonarr/Radarr` | blocked |
| W-unpackerr | `unpackerr.env` has Sonarr and Radarr API key lines | blocked |
| W-jackett | provision log contains `jackett: configured` | blocked |
| W-nzbget | provision log contains `nzbget: control password set` | blocked |
| W-seerr | Before Sign In: log contains `admin user not ready`. That is pass for an unattended install. After Sign In in the Seerr UI, re-run `./kine provision` and the log must contain a successful Sonarr and Radarr add | container up, link blocked on the wizard |
| W-emby | Log contains `emby: startup wizard completed` or `emby: already configured`, and `emby: library Movies` (and TV, Music, Sports) | fail if Emby is healthy but libraries were not created |
| W-livetv | Log contains `dispatcharr: no API token yet` until a token is pasted under Settings → Live TV. That is pass for scratch. After a token, ECM `settings.json` `url` and Teamarr `DISPATCHARR_URL` match the mode in section 10 | blocked if Dispatcharr is not up |

Emby libraries, from `provision/recipes/emby.py`: Movies
`/data/media/movies`, TV `/data/media/tv`, Music `/data/media/music`,
Sports `/data/media/sports`.

## 9. Pi-hole enable path

Kine's Pi-hole is `kine-pihole`, profile `pihole`. It is not the old
standalone Pi-hole container. Do not publish `10.100.100.4`. Do not
start keepalived.

Port 53 is published on every address in `KINE_DNS_BIND`. An empty
value means every interface (`0.0.0.0:53`), which fails when anything
already holds port 53. Enable then writes the host's other global IPv4
addresses into `KINE_DNS_BIND` and publishes `IP:53:53/tcp` and `udp`
for each. A listener on `0.0.0.0` or `::` rejects enable (HTTP 409).

Upstream in Compose is only `${KINE_DNS_GLUETUN}#53` (default
`172.16.53.2#53`). There must be no public resolver in
`FTLCONF_dns_upstreams`. Queries fail closed while the tunnel is down.
That is expected. Do not “fix” it with `1.1.1.1`.

```bash
ss -H -lntup 'sport = :53' || true
enable_app pihole || true
grep ^KINE_DNS_BIND= .env || true
grep ^PIHOLE_WEBPASSWORD= .env | awk -F= '{print $1, ($2==""||$2=="change-me"?"FAIL":"pass")}'
sudo docker inspect kine-pihole --format '{{json .HostConfig.PortBindings}}' 2>/dev/null || echo 'pihole not created'
sudo docker exec kine-pihole printenv FTLCONF_dns_upstreams 2>/dev/null || true
ip -4 addr | grep -F '10.100.100.4' || echo 'vip still absent'
```

| Check | Pass |
|---|---|
| N1 | If port 53 was free, `KINE_DNS_BIND` stays empty and Pi-hole publishes `53:53` on all interfaces |
| N2 | If some other address held 53, `KINE_DNS_BIND` is the remaining global IPv4s and published ports match. No `10.100.100.4` |
| N3 | If a wildcard holds 53, enable returns 409 and Pi-hole is not started. **Fail** only if enable ignored that and collided |
| N4 | `FTLCONF_dns_upstreams` is `172.16.53.2#53` (or the configured `KINE_DNS_GLUETUN`) and nothing else |
| N5 | With the tunnel down, container start is **blocked** on Gluetun health. With the tunnel up, `kine-pihole` becomes healthy and `https://pihole.$DOMAIN:$HTTPS` answers |
| N6 | Enabling Pi-hole recreates DNS consumers (Traefik, Helm, provisioner, and enabled Emby, Tdarr, Beets, Seerr, Recyclarr, Game Thumbs, metrics). They come back healthy |

## 10. Direct Live TV

Skip unless `kine-dispatcharr`, `kine-ecm`, and `kine-teamarr` are up.
ECM MCP follows the group when it is enabled. Game Thumbs stays
untunnelled either way.

While they share Gluetun, peer URLs use `127.0.0.1` and the ports in
`docs/port-map.md` (Dispatcharr 9191, ECM 6100, Teamarr 9195). Direct
moves the four apps onto their own containers. Helm rewrites peers to
service names (`dispatcharr:9191`, `ecm:6100`, `teamarr:9195`).

```bash
# Tunnelled (Direct off):
sudo grep -E 'DISPATCHARR_URL|url' "$STACK_ROOT/config/ecm/ecm.env" "$STACK_ROOT/config/teamarr/teamarr.env" || true
python3 - <<'PY'
import json, os
p=os.environ["STACK_ROOT"]+"/config/ecm/settings.json"
print(open(p).read() if os.path.exists(p) else "no settings.json")
PY

curl -sS -b /tmp/kine.cookies -X PUT "http://127.0.0.1:${HP}/api/vpn/live-tv/direct" \
  -H 'Content-Type: application/json' -d '{"enabled": true}'
# Wait until the four containers are running on their own (not network_mode gluetun).
sudo docker inspect -f '{{.Name}} {{.HostConfig.NetworkMode}}' kine-dispatcharr kine-ecm kine-teamarr

curl -sS -b /tmp/kine.cookies -X PUT "http://127.0.0.1:${HP}/api/vpn/live-tv/direct" \
  -H 'Content-Type: application/json' -d '{"enabled": false}'
```

| Check | Pass |
|---|---|
| L1 | Before Direct: ECM and Teamarr URLs contain `127.0.0.1:9191`. `NetworkMode` is `container:…gluetun` (or `service:gluetun`) |
| L2 | After `enabled: true`: URLs contain `dispatcharr:9191`. Each app has its own network namespace (`kine_internal`), not Gluetun's |
| L3 | After `enabled: false`: URLs return to `127.0.0.1:9191` and the apps share the tunnel again |
| L4 | If Live TV is not up, this whole section is **blocked** |

A Dispatcharr token is not required to see the URL rewrite in the env
files. The Emby HDHomeRun link stays skipped until
`DISPATCHARR_TOKEN` is set; record that as blocked on the token, not
as a failed toggle.

## 11. Helm UI smoke

The UI is a single page. Prove the tabs through the same APIs the page
calls, then load the HTML shell. From a machine that can open a
browser to the host, also click Dashboard, Stats, VPN, and Updates.

```bash
curl -fsS -b /tmp/kine.cookies "http://127.0.0.1:${HP}/" | head -c 200; echo
curl -fsS -b /tmp/kine.cookies "http://127.0.0.1:${HP}/api/apps" -o /tmp/apps.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/apps.json"))
print("tiers", {k: v.get("enabled") for k,v in d.get("tiers",{}).items()})
print("enabled", [a["id"] for a in d["apps"] if a.get("enabled") and not a.get("hidden")])
PY
curl -fsS -b /tmp/kine.cookies "http://127.0.0.1:${HP}/api/stats/overview" -o /tmp/stats.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/stats.json"))
print("keys", sorted(d))
print("pihole", "present" if "pihole" in d else "absent")
PY
curl -fsS -b /tmp/kine.cookies "http://127.0.0.1:${HP}/api/vpn" -o /tmp/vpn.json
python3 -c 'import json; d=json.load(open("/tmp/vpn.json")); print({k:d.get(k) for k in ("vpn_enabled","live_tv_direct") if k in d} or list(d)[:12])'
curl -fsS -b /tmp/kine.cookies "http://127.0.0.1:${HP}/api/updates" -o /tmp/updates.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/updates.json"))
rows=d.get("containers") or d.get("apps") or []
if isinstance(d, dict) and not rows:
    # fetch() returns {"containers": [...]} after catalogue filter
    rows=d.get("containers") or []
enabled=[r for r in rows if r.get("enabled")]
print("enabled_ids", [r.get("id") for r in enabled])
print("has_pihole", any(r.get("id")=="pihole" and r.get("enabled") for r in rows))
PY
```

| Check | Pass |
|---|---|
| U1 | `GET /` returns the Helm HTML shell (HTTP 200) |
| U2 | `/api/apps` lists sections. Enabled flags match `COMPOSE_PROFILES` |
| U3 | `/api/stats/overview` returns JSON. Key `pihole` is present only when the pihole profile is on **and** Pi-hole answered. When the profile is off, `pihole` is absent |
| U4 | `/api/vpn` returns JSON (page data loads). `vpn_enabled` matches `.env` |
| U5 | `/api/updates` includes every enabled catalogue app. When Pi-hole was enabled, its id is in that list. Disabled apps may appear with `enabled: false`; they must not be the only rows |

Browser: open `http://<host>:8600`, log in as `kine-admin`, and confirm
the Dashboard, Stats, and VPN tabs render. Open Updates from the footer.
If Pi-hole is enabled and healthy, Stats shows the Pi-hole figures
(queries, blocked, percent, domains). If the profile is off, that block
is empty and hidden.

## 12. VPN commands when the tunnel is up

Skip when there is no client config (**blocked**).

```bash
cd "$CLONE"
sudo ./kine vpn status
sudo ./kine vpn leaktest
```

| Check | Pass |
|---|---|
| V4 | `vpn status` shows the tunnel up |
| V5 | leak test egress is the VPN address, not the host's WAN address |

`./kine restart gluetun` must refuse and tell you to use `./kine vpn restart`.

## 13. Record the result

Fill this table. Do not commit it unless you mean to keep a run log;
the procedure above is the rerunnable part.

### Kore run (2026-09-27)

Host `kore` (`10.100.100.90`), clone `$HOME/Docker/kine-scratch`,
`STACK_ROOT=/srv/kine`. WireGuard used: `/etc/wireguard/proton-se-424.conf`
(read with `sudo`; never copied from Osiris). VIP `10.100.100.4` stayed
absent; keepalived stayed inactive. LAN DNS was not changed.

Mid-recovery the scratch tree, `/srv/kine`, `/srv/media-data`, the `kine`
user, `/etc/hosts` kine block, and all containers disappeared (full
teardown, around 10:46 UTC). Images remained in the Docker cache. The
install is **not** left running; re-run sections 4–12 to continue.

| Id | Result | Evidence |
|---|---|---|
| P1–P8 | **pass** | Docker 29.x, Compose ≥2.20, PyYAML installed, TUN present, sudo -n ok, ≥100 GB free on `/`, ports 8080/8443 free, port 53 free |
| H1–H4 | **pass** | no `10.100.100.4`; keepalived inactive; no pre-Kine Pi-hole; leftover media dirs under `$HOME/Docker` left in place |
| D1 | **pass** | defaults `/srv/kine` + `/srv/media-data` on `/` with room |
| I1–I8 | **pass** | `install.sh` exit 0; secrets 64-char / Pi-hole password set; profiles `mdns` then VPN onboarding; `kine` uid/gid matched PUID/PGID; Traefik 8080/8443; media dirs + certs; `/etc/hosts` BEGIN kine |
| C0–C2 | **pass** (post-install) | traefik/helm/dockerproxy/nfs-agent/mdns up; `/api/health` ok; catalogue apps not yet running |
| V0–V3 | **pass** | real WG via sudo; first `/api/setup` succeeded after PermissionError on unprivileged read; second call 409 already configured; `VPN_ENABLED=true`, profiles gained `gluetun`; Gluetun healthy |
| V4–V5 | **blocked** | stack torn down before `./kine vpn status` / leaktest |
| Sections | **pass** then interrupted | metrics/process/acquisition/live enable 200; media/network 404 as expected; optional apps mostly enabled; Pi-hole enable mid-flight |
| O1–O2 | **partial** | beets/seerr/lidarr/jackett/bazarr/nzbget/unpackerr/ecm-mcp came up; emby first enable 409 (busy); pihole enable killed Helm mid-request; later recovery `compose up` restored most apps including pihole start |
| W-* | **partial** | wire logs: seerr admin not ready (expected); `dispatcharr: no API token yet` expected; lidarr rootfolder 400; radarr host/Bazarr notify errors; overlapping concurrent `provision wire` runs observed |
| N1–N6 | **partial / product fail** | port 53 free → `KINE_DNS_BIND` empty; pihole profile written; enable recreates Helm itself → API ConnectionReset; after manual `compose up`, pihole Started then whole install was torn down before N4–N6 confirmed |
| L1–L4 | **blocked** | Direct Live TV toggle not run before teardown |
| U1–U5 | **blocked** | Helm UI smoke not completed after teardown |
| R1–R3 | **partial** | admin Traefik route worked earlier; full hostname matrix not finished |

Product defects found (local working tree only; not committed):

1. **Traefik `:8080` clash** — built-in ping entrypoint and `web` both bound `:8080` → crash loop. Fixed in `compose/core.traefik.yml` (web on `:8088`, host map `8080:8088`) + `tests/test_stack.py`.
2. **`local_ip` / admin URL** — empty pick_ip output fell through poorly; `scripts/lib.sh` returns the first successful pick.
3. **Helm `/api/setup` WireGuard read** — operator must `sudo` to read `/etc/wireguard/*.conf`; unprivileged Python gets PermissionError (operator issue, documented).
4. **ECM admin email** — `kine-admin@kine.local` rejected as reserved TLD. Fixed in `helm/backend/app/ecm_setup.py` (+ test) to use `@example.com` for `.local` domains.
5. **Pi-hole enable recreates Helm** — `pihole_dns.services_to_recreate` includes `helm`, so enable drops its own API mid-request. Needs a product fix (recreate consumers without killing the request process, or defer Helm recreate).
6. **Overlapping provision wires** — concurrent `kine-provision-run-* wire` containers while enabling apps back-to-back; lock did not fully serialize.

What is still running: **nothing from this scratch install** (torn down).
Confirm with `sudo docker ps --format '{{.Names}}\t{{.Status}}'`.

## 14. Teardown (later)

Do not run this at the end of a passing test. Run it when the scratch
install should go away. It does not remove unrelated Docker containers
if you limit the commands to this checkout's project.

```bash
cd "$CLONE"
# All profiles this install turned on, so `down` sees every service.
# shellcheck disable=SC2046
sudo docker compose --profile mdns $(sudo awk -F= '$1=="COMPOSE_PROFILES"{n=split($2,a,","); for(i=1;i<=n;i++) if(a[i]!="") printf " --profile %s", a[i]}' .env) down --remove-orphans
sudo docker network rm kine_edge kine_internal kine_ctrl kine_dns 2>/dev/null || true
sudo rm -rf "$STACK_ROOT" "$DATA_ROOT"
sudo sed -i '/# BEGIN kine/,/# END kine/d' /etc/hosts
sudo userdel kine 2>/dev/null || true
rm -rf "$CLONE"
rm -f "$ADMIN_FILE" /tmp/kine.cookies /tmp/kine-setup.json /tmp/apps.json /tmp/stats.json /tmp/vpn.json /tmp/updates.json
```

Confirm `10.100.100.4` is still absent and keepalived was not enabled.
Do not change the host's DNS resolver as part of teardown.
