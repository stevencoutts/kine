#!/usr/bin/env bash
# ./scripts/restore.sh <tarball> [app]
#
# Restores app config (and for a full restore: .env with COMPOSE_PROFILES,
# compose files, catalogue). A single-app restore must not stop Gluetun or
# the rest of the stack — that is how a failed Bazarr update took the
# tunnel down. Full restore still stops non-core services, extracts, then
# brings the stack back and recreates the VPN tunnel group.
set -Eeuo pipefail
tarball="${1:?usage: restore.sh <tarball> [app]}"
app="${2:-}"
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[[ -f .env ]] && load_env .env

if [[ ! -f "$tarball" ]]; then
  echo "backup not found: $tarball" >&2
  exit 1
fi

# Keep the control plane up so a Helm-triggered restore can finish.
CORE_RE='^(traefik|helm|dockerproxy)$'

stop_non_core() {
  local svc
  while IFS= read -r svc; do
    [[ -z "$svc" ]] && continue
    [[ "$svc" =~ $CORE_RE ]] && continue
    docker compose stop "$svc" 2>/dev/null || true
  done < <(docker compose config --services 2>/dev/null || true)
}

# Recreate gluetun + enabled tunnelled apps together (avoids orphan namespaces).
heal_tunnel_group() {
  load_env .env
  local -a group=(gluetun)
  local a
  local IFS=,
  for a in ${VPN_TUNNELLED_APPS:-}; do
    a="${a// /}"
    [[ -z "$a" ]] && continue
    [[ ",${COMPOSE_PROFILES:-}," == *",$a,"* ]] || continue
    group+=("$a")
  done
  if [[ ",${COMPOSE_PROFILES:-}," == *",gluetun,"* ]] || [[ ${#group[@]} -gt 1 ]]; then
    echo "Recreating tunnel group: ${group[*]}" >&2
    docker compose up -d --force-recreate "${group[@]}"
  fi
}

if [[ -n "$app" ]]; then
  case "$app" in
    *..*|*/*|*\\*) echo "invalid app name" >&2; exit 1 ;;
  esac
  echo "Stopping ${app}…" >&2
  docker compose stop "$app" 2>/dev/null || true
  tar xzf "$tarball" -C "${STACK_ROOT}" "config/${app}"
  echo "restored config for ${app}"
  load_env .env
  docker compose up -d --no-deps "$app"
else
  echo "Stopping non-core services…" >&2
  stop_non_core
  tar xzf "$tarball" -C "${STACK_ROOT}" config
  tar xzf "$tarball" -C . .env docker-compose.yml compose catalogue.yml 2>/dev/null \
    || tar xzf "$tarball" -C . .env
  echo "restored full appliance (config + .env / profiles)"
  load_env .env
  docker compose up -d
  heal_tunnel_group
fi

echo "restore complete"
