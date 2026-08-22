#!/usr/bin/env bash
# Refuses to let you build a system that will disappoint you later.
set -uo pipefail
fail=0
ok()   { printf '\033[32m+ %s\033[0m\n' "$*"; }
bad()  { printf '\033[31mx %s\033[0m\n' "$*"; fail=1; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }

command -v docker >/dev/null || bad "docker not installed"
docker compose version >/dev/null 2>&1 || bad "docker compose v2 not available"

if docker compose version >/dev/null 2>&1; then
  cv=$(docker compose version --short 2>/dev/null | tr -d 'v')
  major=${cv%%.*}; rest=${cv#*.}; minor=${rest%%.*}
  # `include:` landed in Compose 2.20.
  if (( major < 2 || (major == 2 && minor < 20) )); then
    bad "docker compose ${cv} is too old; 2.20+ needed for include:"
  else
    ok "docker compose ${cv}"
  fi
fi

# The hardlink rule. Sonarr and Radarr can only hardlink or atomically
# move within one filesystem. If media and downloads straddle a mount
# boundary, every import silently becomes a full copy: slow, and it
# doubles your disk use until the download is cleaned up.
if [[ -f .env ]]; then
  set -a; source .env; set +a
  mkdir -p "${DATA_ROOT}/media" "${DATA_ROOT}/downloads" 2>/dev/null
  fs_media=$(stat -c '%d' "${DATA_ROOT}/media" 2>/dev/null || echo x)
  fs_dl=$(stat -c '%d' "${DATA_ROOT}/downloads" 2>/dev/null || echo y)
  if [[ "$fs_media" == "$fs_dl" ]]; then
    ok "media and downloads share a filesystem (hardlinks will work)"
  else
    bad "${DATA_ROOT}/media and ${DATA_ROOT}/downloads are on different filesystems"
    echo "    Imports would be copies, not hardlinks. Put both under one mount."
  fi
fi

for p in 80 443; do
  if ss -ltn "( sport = :$p )" 2>/dev/null | grep -q LISTEN; then
    bad "port $p already in use"
  else
    ok "port $p free"
  fi
done

[[ -e /dev/net/tun ]] || warn "/dev/net/tun missing; the VPN container will not start"
[[ -e /dev/dri/renderD128 ]] || warn "no /dev/dri; hardware transcoding unavailable"

avail=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
(( avail >= 20 )) && ok "root filesystem has ${avail}G free" || warn "only ${avail}G free on /"

exit $fail
