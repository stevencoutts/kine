#!/usr/bin/env bash
# Kine installer. Idempotent: safe to re-run.
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"
source ./scripts/lib.sh

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m+ %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || die "run with sudo"

bold "Kine installer"
echo

# ── 1. Preflight ────────────────────────────────────────────────
./scripts/preflight.sh || die "preflight failed; fix the above and re-run"

# ── 2. Configuration ────────────────────────────────────────────
if [[ ! -f .env ]]; then
  cp .env.example .env
  ok "created .env"

  # Secrets. KINE_SECRET derives every internal API key, so it must be
  # strong and must not change casually.
  sedi "s|^KINE_SECRET=.*|KINE_SECRET=$(openssl rand -hex 32)|" .env
  sedi "s|^HELM_SESSION_SECRET=.*|HELM_SESSION_SECRET=$(openssl rand -hex 32)|" .env
  ok "generated secrets"
else
  ok ".env already present, leaving it alone"
fi

load_env .env

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

mkdir -p "${STACK_ROOT}"/{config,backups} "${DATA_ROOT}"/{media,downloads}
mkdir -p "${STACK_ROOT}"/config/{traefik/dynamic,traefik/certs,unpackerr,recyclarr,ecm,teamarr}
mkdir -p "${DATA_ROOT}"/media/{movies,tv,sports,recordings}
mkdir -p "${DATA_ROOT}"/downloads/{incomplete,complete/{tv-sonarr,radarr}}
chown -R "${PUID_ACTUAL}:${PGID_ACTUAL}" "${STACK_ROOT}" "${DATA_ROOT}"
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
import os, yaml
domain = os.environ.get("KINE_DOMAIN", "kine.local")
profiles = {p.strip() for p in os.environ.get("COMPOSE_PROFILES", "").split(",") if p.strip()}
cat = yaml.safe_load(open("catalogue.yml"))["apps"]
names = [domain] + [f"{v['subdomain']}.{domain}" for k, v in cat.items()
                     if v.get("subdomain") and k in profiles]
print("127.0.0.1 " + " ".join(names))
PY
)
sedi '/# BEGIN kine/,/# END kine/d' /etc/hosts
{ echo "# BEGIN kine"; echo "$HOSTS_BLOCK"; echo "# END kine"; } >> /etc/hosts
ok "loopback resolution for ${KINE_DOMAIN} written to /etc/hosts"

# ── 5. Seed application config before anything starts ───────────
# The *arr apps mint a random API key on first run. Writing config.xml
# first makes them adopt ours instead, which is what allows the stack to
# ship pre-wired.
docker compose build provision >/dev/null
docker compose run --rm provision seed
ok "application config seeded"

# ── 6. Bring it up ──────────────────────────────────────────────
docker compose pull --ignore-buildable
docker compose build helm
docker compose up -d
ok "containers started"

# ── 7. Wire the apps together ───────────────────────────────────
echo
bold "Waiting for applications and wiring them together"
docker compose run --rm provision wire

# ── 8. Done ─────────────────────────────────────────────────────
echo
bold "Ready"
echo "  Admin GUI   https://admin.${KINE_DOMAIN}    (or http://$(local_ip):${HELM_PORT})"
echo "  Emby        https://emby.${KINE_DOMAIN}"
echo
echo "Finish setup in the admin GUI: set the admin password, then add"
echo "your VPN key and indexer accounts. Everything else is already wired."
if [[ "${KINE_TLS_MODE}" == "internal" ]]; then
  echo
  warn "TLS mode 'internal' uses Traefik's own CA. Browsers will warn until"
  warn "you trust ${STACK_ROOT}/config/traefik/certs/ca.crt, or switch"
  warn "KINE_TLS_MODE to acme-dns in the GUI."
fi
