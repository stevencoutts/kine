#!/usr/bin/env bash
# Kine installer. Idempotent: safe to re-run.
# Linux one-liner (clones the repo, then re-execs this file from disk):
#   curl -fsSL https://raw.githubusercontent.com/stevencoutts/kine/master/install.sh | sudo bash
set -Eeuo pipefail

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m+ %s\033[0m\n' "$*"; }

# True when this process was started from a file inside a Kine checkout.
# A pipe (`curl | sudo bash`) leaves BASH_SOURCE as "bash" or empty, and
# stdin is the script, so the rest of the install cannot run from there.
in_kine_checkout() {
  local self dir
  self="${BASH_SOURCE[0]:-}"
  [[ -n "$self" && "$self" != "bash" && "$self" != "-" && -f "$self" ]] || return 1
  dir="$(cd "$(dirname "$self")" && pwd)"
  [[ -f "$dir/catalogue.yml" && -f "$dir/docker-compose.yml" ]]
}

# Prefer the invoking user's home so the clone is not left in /root.
# An already-root shell (no SUDO_USER) lands in /opt/kine.
clone_dest() {
  local home
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    home="$(getent passwd "$SUDO_USER" | cut -d: -f6 || true)"
    [[ -n "$home" && -d "$home" ]] || die "no home directory for ${SUDO_USER}"
    printf '%s\n' "${home}/kine"
    return 0
  fi
  printf '%s\n' "/opt/kine"
}

# git in $1, as the user who owns the clone when invoked via sudo.
git_in_checkout() {
  local dest=$1
  shift
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    sudo -u "$SUDO_USER" -H env GIT_TERMINAL_PROMPT=0 git -C "$dest" "$@"
  else
    GIT_TERMINAL_PROMPT=0 git -C "$dest" "$@"
  fi
}

# Clone $1, or fast-forward a checkout that is already there.
# A second `curl | sudo bash` must not die because ~/kine exists:
# pull so the re-exec'd install.sh is the one just published.
sync_checkout() {
  local dest=$1
  if [[ -f "$dest/install.sh" && -f "$dest/catalogue.yml" && -f "$dest/docker-compose.yml" ]]; then
    ok "using existing checkout ${dest}"
    if [[ -d "$dest/.git" ]]; then
      if git_in_checkout "$dest" pull --ff-only; then
        ok "updated ${dest}"
      else
        warn "could not fast-forward ${dest}; continuing with the files already there"
      fi
    fi
    return 0
  fi
  if [[ -e "$dest" ]]; then
    die "${dest} exists but is not a Kine checkout"
  fi
  ok "cloning https://github.com/stevencoutts/kine.git into ${dest}"
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    sudo -u "$SUDO_USER" -H git clone --branch master https://github.com/stevencoutts/kine.git "$dest"
  else
    git clone --branch master https://github.com/stevencoutts/kine.git "$dest"
  fi
}

# Clone once, then re-exec the copy on disk. The second run sees
# catalogue.yml and docker-compose.yml and does not clone again.
# stdin is /dev/null so the child does not keep reading the curl pipe.
# That fd is not a console. Compose commands below must not open one.
ensure_checkout() {
  if in_kine_checkout; then
    return 0
  fi
  [[ $EUID -eq 0 ]] || die "run with sudo"

  local dest
  dest="$(clone_dest)"
  sync_checkout "$dest"
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    chown -R "${SUDO_USER}:$(id -g "$SUDO_USER")" "$dest"
  fi
  exec bash "$dest/install.sh" </dev/null
}

# Tests source the helpers above without cloning or installing.
if [[ "${KINE_INSTALL_SOURCE_ONLY:-}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

ensure_checkout

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"
source ./scripts/lib.sh

[[ $EUID -eq 0 ]] || die "run with sudo"

bold "Kine installer"
echo

# ── 1. Configuration ────────────────────────────────────────────
# Create .env before preflight so ports and paths can be filled in
# automatically — nobody should need to hand-edit for a first install.
if [[ ! -f .env ]]; then
  cp .env.example .env
  ok "created .env"

  # Secrets. KINE_SECRET derives every internal API key, so it must be
  # strong and must not change casually.
  sedi "s|^KINE_SECRET=.*|KINE_SECRET=$(openssl rand -hex 32)|" .env
  sedi "s|^HELM_SESSION_SECRET=.*|HELM_SESSION_SECRET=$(openssl rand -hex 32)|" .env
  ok "generated secrets"
else
  ok ".env already present"
fi

if merge_missing_env_keys .env .env.example; then
  ok "merged missing keys from .env.example into .env"
fi
pw=$(grep '^PIHOLE_WEBPASSWORD=' .env | cut -d= -f2- || true)
if [[ -z "$pw" || "$pw" == "change-me" ]]; then
  generated=$(openssl rand -hex 16)
  if grep -q '^PIHOLE_WEBPASSWORD=' .env; then
    sedi "s|^PIHOLE_WEBPASSWORD=.*|PIHOLE_WEBPASSWORD=${generated}|" .env
  else
    printf 'PIHOLE_WEBPASSWORD=%s\n' "$generated" >> .env
  fi
  ok "generated Pi-hole web password"
fi
# Helm runs compose inside its container; relative binds are resolved
# there then sent to dockerd. The checkout must exist at this path.
if grep -q '^KINE_CHECKOUT=' .env; then
  sedi "s|^KINE_CHECKOUT=.*|KINE_CHECKOUT=${REPO}|" .env
else
  printf 'KINE_CHECKOUT=%s\n' "$REPO" >> .env
fi
if ! is_darwin; then
  agent=$(grep '^NFS_BROWSE_AGENT=' .env 2>/dev/null | cut -d= -f2- || true)
  if [[ -z "$agent" ]]; then
    if grep -q '^NFS_BROWSE_AGENT=' .env; then
      sedi 's|^NFS_BROWSE_AGENT=.*|NFS_BROWSE_AGENT=http://host.docker.internal:8611|' .env
    else
      printf 'NFS_BROWSE_AGENT=http://host.docker.internal:8611\n' >> .env
    fi
    ok "NFS browse agent URL set for Linux host mounts"
  fi
fi
load_env .env
# Defensive defaults so a partial .env cannot trip set -u later.
: "${STACK_ROOT:=/srv/kine}"
: "${DATA_ROOT:=/srv/media-data}"
: "${KINE_DOMAIN:=kine.local}"
: "${KINE_TLS_MODE:=internal}"
: "${HELM_PORT:=8600}"
: "${COMPOSE_PROFILES:=mdns}"
: "${TRAEFIK_HTTP_PORT:=8080}"
: "${TRAEFIK_HTTPS_PORT:=8443}"
export STACK_ROOT DATA_ROOT KINE_DOMAIN KINE_TLS_MODE HELM_PORT COMPOSE_PROFILES
export TRAEFIK_HTTP_PORT TRAEFIK_HTTPS_PORT

# If 8080/8443 (or whatever is in .env) are taken, pick the next free
# pair and write them back — no manual .env editing required.
ports_rc=0
ensure_traefik_ports .env || ports_rc=$?
case $ports_rc in
  0) ok "Traefik ports busy; using ${TRAEFIK_HTTP_PORT}/${TRAEFIK_HTTPS_PORT}" ;;
  1) ok "Traefik ports ${TRAEFIK_HTTP_PORT}/${TRAEFIK_HTTPS_PORT} free" ;;
  *) die "could not find free Traefik HTTP/HTTPS ports" ;;
esac

# ── 2. Preflight ────────────────────────────────────────────────
./scripts/preflight.sh || die "preflight failed; fix the above and re-run"

# ── 3. Service user and directories ─────────────────────────────
if is_darwin; then
  # No useradd, no systemd-style service accounts, and Docker Desktop's
  # VM already isolates container UIDs from the host — run as whoever
  # invoked sudo instead of minting a dedicated user.
  target_user="${SUDO_USER:-$(id -un)}"
  PUID_ACTUAL=$(id -u "$target_user")
  PGID_ACTUAL=$(id -g "$target_user")
else
  if ! id -u kine >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin kine
    ok "created kine service user"
  fi
  PUID_ACTUAL=$(id -u kine)
  PGID_ACTUAL=$(id -g kine)
fi
sedi "s|^PUID=.*|PUID=${PUID_ACTUAL}|" .env
sedi "s|^PGID=.*|PGID=${PGID_ACTUAL}|" .env

# Intel QSV: the render group GID differs across distributions, so it is
# detected rather than assumed. Getting this wrong is the usual cause of
# "hardware transcoding silently falls back to software". Not applicable
# on macOS at all — Docker Desktop's VM has no /dev/dri passthrough.
if is_darwin; then
  warn "macOS: hardware transcoding is unavailable under Docker Desktop"
elif [[ -e /dev/dri/renderD128 ]]; then
  RGID=$(stat -c '%g' /dev/dri/renderD128)
  sedi "s|^RENDER_GID=.*|RENDER_GID=${RGID}|" .env
  ok "detected render group GID ${RGID}"
else
  warn "no /dev/dri/renderD128; hardware transcoding will be unavailable"
fi

mkdir -p "${STACK_ROOT}"/{config,backups,nzbget-incomplete} "${DATA_ROOT}"/{media,downloads,cache}
mkdir -p "${STACK_ROOT}"/config/{traefik/dynamic,traefik/certs,unpackerr,recyclarr,seerr/logs,ecm,teamarr,game-thumbs/cache,tdarr/{server,configs,logs},pihole}
mkdir -p "${DATA_ROOT}"/media/{movies,tv,music,sports,recordings}
mkdir -p "${DATA_ROOT}"/downloads/{incomplete,complete/{tv-sonarr,radarr}}
mkdir -p "${DATA_ROOT}/cache/tdarr"
# Compose checks env_file paths before the provisioner can write them.
touch "${STACK_ROOT}/config/"{ecm/ecm.env,teamarr/teamarr.env,unpackerr/unpackerr.env}
chown -R "${PUID_ACTUAL}:${PGID_ACTUAL}" "${STACK_ROOT}" "${DATA_ROOT}"
chown -R 1000:1000 "${STACK_ROOT}/config/seerr"
chmod -R g+rwX "${DATA_ROOT}"
./scripts/mount-media.sh || warn "NFS mounts failed; using local directories"
ok "directory tree ready"

# ── 4. TLS ──────────────────────────────────────────────────────
./scripts/tls-setup.sh
ok "TLS mode: ${KINE_TLS_MODE}"

# ── 4b. Loopback resolution ──────────────────────────────────────
# mDNS (the mdns profile, on by default) advertises the domain to other
# devices on the LAN, but the host doesn't need multicast to reach
# itself: a plain /etc/hosts entry is instant and doesn't depend on
# avahi coming up cleanly. Re-run-safe: the old block is replaced.
HOSTS_BLOCK=$(KINE_DOMAIN="${KINE_DOMAIN}" COMPOSE_PROFILES="${COMPOSE_PROFILES}" python3 - <<'PY'
import os, sys, yaml
sys.path.insert(0, "mdns")
from gen_hosts import build_names
domain = os.environ.get("KINE_DOMAIN", "kine.local")
profiles = {p.strip() for p in os.environ.get("COMPOSE_PROFILES", "").split(",") if p.strip()}
cat = yaml.safe_load(open("catalogue.yml"))["apps"]
print("127.0.0.1 " + " ".join(build_names(domain, profiles, cat)))
PY
)
sedi '/# BEGIN kine/,/# END kine/d' /etc/hosts
{ echo "# BEGIN kine"; echo "$HOSTS_BLOCK"; echo "# END kine"; } >> /etc/hosts
ok "loopback resolution for ${KINE_DOMAIN} written to /etc/hosts"

# ── 5. Seed application config before anything starts ───────────
# The *arr apps mint a random API key on first run. Writing config.xml
# first makes them adopt ours instead, which is what allows the stack to
# ship pre-wired.
# `curl | sudo bash` re-execs with stdin on /dev/null, which is not a
# console. `run` allocates a TTY unless -T is set, and the progress UI
# opens a console when stdout is a terminal. Seed and wire do not read
# from the terminal. A real `sudo ./install.sh` still prints their output.
docker compose --progress plain build provision >/dev/null
docker compose --progress plain run -T --rm provision seed
ok "application config seeded"

# ── 6. Bring it up ──────────────────────────────────────────────
# Build local images first. nfs-browse-agent also uses kine/helm:local;
# pulling before the image exists makes Compose try a registry fetch.
docker compose --progress plain build helm mdns
docker compose --progress plain pull --ignore-buildable
docker compose --progress plain up -d
ok "containers started"

# ── 7. Wire the apps together ───────────────────────────────────
echo
bold "Waiting for applications and wiring them together"
docker compose --progress plain run -T --rm provision wire

# Recyclarr only runs on a daily cron; sync once now so TRaSH profiles exist
# before the user opens Sonarr/Radarr.
if [[ ",${COMPOSE_PROFILES}," == *",recyclarr,"* ]]; then
  if docker exec kine-recyclarr recyclarr sync; then
    ok "recyclarr synced TRaSH Guide profiles"
  else
    warn "recyclarr sync failed; retry: docker exec kine-recyclarr recyclarr sync"
  fi
fi

# ── 8. Done ─────────────────────────────────────────────────────
echo
bold "Ready"
https_port="${TRAEFIK_HTTPS_PORT:-8443}"
https_suffix=""; [[ "$https_port" == "443" ]] || https_suffix=":${https_port}"
echo "  Admin GUI   https://kine-admin.${KINE_DOMAIN}${https_suffix}    (or http://$(local_ip):${HELM_PORT})"
echo "  Emby        https://emby.${KINE_DOMAIN}${https_suffix}"
echo
echo "Finish setup in the admin GUI: set the admin password, then add"
echo "your VPN key and indexer accounts. Everything else is already wired."
if [[ "${KINE_TLS_MODE}" == "internal" ]]; then
  echo
  warn "TLS mode 'internal' uses Traefik's own CA. Browsers will warn until"
  warn "you trust ${STACK_ROOT}/config/traefik/certs/ca.crt, or switch"
  warn "KINE_TLS_MODE to acme-dns in the GUI."
fi

# .local is multicast DNS. Any other domain needs an A record for every
# name the stack serves once apps are enabled, not only the profiles
# running at this moment.
echo
domain_lc=$(printf '%s' "${KINE_DOMAIN}" | tr '[:upper:]' '[:lower:]')
domain_lc="${domain_lc%.}"
if [[ "$domain_lc" == *.local ]]; then
  echo "${KINE_DOMAIN} is multicast DNS, not the DNS server. This machine resolves"
  echo "it from /etc/hosts. Other machines on the LAN resolve it only if they"
  echo "do mDNS. On Linux that is Avahi and libnss-mdns, and the hosts line"
  echo "in /etc/nsswitch.conf includes mdns4_minimal."
  echo "host and dig ask the DNS server and will return NXDOMAIN."
else
  lan_ip="$(local_ip)"
  echo "mDNS will not be how other machines find ${KINE_DOMAIN}."
  echo "Add an A record for each name below, all pointing at ${lan_ip}:"
  echo
  KINE_DOMAIN="${KINE_DOMAIN}" KINE_LAN_IP="${lan_ip}" python3 - <<'PY'
import os, sys, yaml
sys.path.insert(0, "mdns")
from gen_hosts import dns_names
domain = os.environ.get("KINE_DOMAIN", "")
ip = os.environ.get("KINE_LAN_IP", "")
cat = yaml.safe_load(open("catalogue.yml"))["apps"]
for name in dns_names(domain, cat):
    print(f"  {name}  A  {ip}")
PY
  echo
  echo "A single wildcard A record for *.${KINE_DOMAIN} pointing at ${lan_ip}"
  echo "covers the same set."
fi
