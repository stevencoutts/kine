#!/usr/bin/env bash
# Image updates: check by digest, apply with a snapshot and a rollback.
#
# Deliberately not Watchtower. Unattended updates on a media stack are
# how a working system becomes a broken one overnight, usually while
# you are asleep and something is mid-import.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source ./scripts/lib.sh
set -a; source .env; set +a

var_for() { echo "$(echo "$1" | tr 'a-z-' 'A-Z_')"; }

check() {
  printf '%-14s %-12s %s\n' APP STATUS IMAGE
  for svc in $(docker compose config --services 2>/dev/null); do
    image=$(docker compose config --format json 2>/dev/null \
            | python3 -c "import json,sys;print(json.load(sys.stdin)['services'].get('$svc',{}).get('image',''))")
    [[ -z "$image" || "$image" == *local* ]] && continue
    remote=$(docker manifest inspect "$image" 2>/dev/null \
             | sha256_hex | cut -c1-12) || remote="?"
    local_d=$(docker image inspect "$image" --format '{{index .RepoDigests 0}}' 2>/dev/null \
             | sha256_hex | cut -c1-12) || local_d="none"
    if [[ "$remote" == "$local_d" ]]; then
      printf '%-14s %-12s %s\n' "$svc" "current" "$image"
    else
      printf '%-14s %-12s %s\n' "$svc" "UPDATE" "$image"
    fi
  done
}

apply() {
  svc="$1"
  echo "Snapshotting config before updating ${svc}..."
  snap=$(./scripts/backup.sh)
  echo "  ${snap}"

  prev=$(docker image inspect "$(docker compose config --format json \
        | python3 -c "import json,sys;print(json.load(sys.stdin)['services']['$svc']['image'])")" \
        --format '{{index .RepoDigests 0}}' 2>/dev/null || echo "")

  docker compose pull "$svc"
  docker compose up -d "$svc"

  echo "Waiting up to 90s for ${svc} to come back healthy..."
  for _ in $(seq 1 18); do
    state=$(docker inspect --format '{{.State.Health.Status}}' "mc-${svc}" 2>/dev/null || echo "none")
    [[ "$state" == "healthy" ]] && { echo "OK: ${svc} healthy on the new image"; exit 0; }
    [[ "$state" == "none" ]] && {
      running=$(docker inspect --format '{{.State.Running}}' "mc-${svc}" 2>/dev/null || echo false)
      [[ "$running" == "true" ]] && { echo "OK: ${svc} running (no healthcheck defined)"; exit 0; }
    }
    sleep 5
  done

  echo "FAILED: ${svc} did not come back. Rolling back."
  docker compose logs --tail=50 "$svc" || true
  if [[ -n "$prev" ]]; then
    var="$(var_for "$svc")_DIGEST"
    sedi "s|^${var}=.*|${var}=${prev#*@}|" .env
    docker compose up -d "$svc"
  fi
  ./scripts/restore.sh "$snap" "$svc"
  exit 1
}

case "${1:-check}" in
  check) check ;;
  apply) apply "${2:?app}" ;;
  *) echo "usage: updates.sh check|apply <app>" >&2; exit 1 ;;
esac
