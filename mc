#!/usr/bin/env bash
# Media Centre CLI. Everything the GUI does, you can do here.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./scripts/lib.sh
set -a; source .env 2>/dev/null || true; set +a

usage() {
cat <<'USAGE'
mc <command>

  up                    start everything enabled
  down                  stop everything
  restart [service]     restart one service, or all
  logs <service>        follow a service's logs
  ps                    what is running

  enable <app>          add an app to COMPOSE_PROFILES, start, wire it
  disable <app>         stop an app and remove it from COMPOSE_PROFILES
  apps                  list the catalogue and what is enabled

  provision [--force]   re-run the wiring (idempotent)
  seed                  write app config before first start
  rekey                 rotate MC_SECRET and re-key every app

  updates               check for newer images (no pull)
  update <app>          snapshot, pull, recreate, roll back on failure

  vpn status            tunnel state and forwarded port
  vpn restart           restart the tunnel and everything inside it
  vpn leaktest          prove the tunnel is actually carrying traffic

  tls                   re-apply TLS config after changing MC_TLS_MODE
  backup                config + env tarball
  restore <file> [app]  restore all, or one app's config
USAGE
}

profiles_add() {
  local app="$1"
  grep -q "^COMPOSE_PROFILES=" .env || echo "COMPOSE_PROFILES=" >> .env
  local cur; cur=$(grep '^COMPOSE_PROFILES=' .env | cut -d= -f2-)
  [[ ",$cur," == *",$app,"* ]] && return 0
  sedi "s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=${cur:+$cur,}${app}|" .env
}

profiles_remove() {
  local app="$1"
  local cur; cur=$(grep '^COMPOSE_PROFILES=' .env | cut -d= -f2-)
  local new; new=$(echo "$cur" | tr ',' '\n' | grep -vx "$app" | paste -sd,)
  sedi "s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=${new}|" .env
}

# gluetun's dependants lose their networking when it restarts, so the
# whole tunnel group goes together or not at all. That group is now the
# entire acquisition tier, which makes this restart a bigger event than
# it looks: expect roughly a minute of no *arr and no downloads.
vpn_group() {
  echo "gluetun ${VPN_TUNNELLED_APPS//,/ } vpn-portsync"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up)        docker compose up -d ;;
  down)      docker compose down ;;
  restart)
    if [[ "${1:-}" == "gluetun" ]]; then
      echo "Restarting gluetun alone would sever every app inside it:"
      echo "  ${VPN_TUNNELLED_APPS}"
      echo "Use: ./mc vpn restart"; exit 1
    fi
    docker compose restart ${1:-} ;;
  logs)      docker compose logs -f --tail=200 "${1:?service}" ;;
  ps)        docker compose ps ;;

  enable)
    app="${1:?app}"
    profiles_add "$app"
    set -a; source .env; set +a
    docker compose up -d
    docker compose run --rm provision wire ;;
  disable)
    app="${1:?app}"
    docker compose stop "$app" 2>/dev/null || true
    docker compose rm -f "$app" 2>/dev/null || true
    profiles_remove "$app" ;;
  apps)
    python3 - <<'PY'
import os, yaml
cat = yaml.safe_load(open('catalogue.yml'))['apps']
env = dict(l.strip().split('=',1) for l in open('.env') if '=' in l and not l.startswith('#'))
on = set(env.get('COMPOSE_PROFILES','').split(','))
for k, v in cat.items():
    print(f"  [{'x' if k in on else ' '}] {k:<14} {v.get('summary','')}")
PY
    ;;

  seed)      docker compose run --rm provision seed ;;
  rekey)
    # Every internal API key is derived from MC_SECRET, so rotating it
    # re-keys the whole stack at once. Anything outside the stack that
    # holds an old key (a phone app, a script, Tautulli) stops working
    # until it is given the new one.
    cat <<'WARN'
This rotates MC_SECRET and every derived API key.

  - all stack-internal wiring is rebuilt automatically
  - any EXTERNAL client holding an old API key will break
  - a config snapshot is taken first

WARN
    read -r -p "Type 'rekey' to continue: " confirm
    [[ "$confirm" == "rekey" ]] || { echo "aborted"; exit 1; }
    ./scripts/backup.sh
    sedi "s|^MC_SECRET=.*|MC_SECRET=$(openssl rand -hex 32)|" .env
    docker compose down
    # Existing config.xml files hold the old key and are never
    # overwritten by the seeder, so they have to go first.
    for a in sonarr radarr prowlarr; do
      rm -f "${STACK_ROOT}/config/${a}/config.xml"
    done
    docker compose run --rm provision seed
    docker compose up -d
    docker compose run --rm provision wire
    echo "rekeyed" ;;
  provision) docker compose run --rm provision wire ;;

  updates)   ./scripts/updates.sh check ;;
  update)    ./scripts/updates.sh apply "${1:?app}" ;;

  vpn)
    case "${1:-status}" in
      status)
        docker exec mc-gluetun wget -qO- http://127.0.0.1:8000/v1/openvpn/status || true
        echo
        docker exec mc-gluetun wget -qO- http://127.0.0.1:8000/v1/openvpn/portforwarded || true
        echo ;;
      restart)   docker compose restart $(vpn_group) ;;
      leaktest)  ./scripts/vpn-leaktest.sh ;;
      *) usage; exit 1 ;;
    esac ;;

  tls)       ./scripts/tls-setup.sh && docker compose restart traefik ;;
  backup)    ./scripts/backup.sh ;;
  restore)   ./scripts/restore.sh "$@" ;;
  ""|-h|--help|help) usage ;;
  *) usage; exit 1 ;;
esac
