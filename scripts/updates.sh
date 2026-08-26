#!/usr/bin/env bash
# Image updates: check by digest, apply with a snapshot and a rollback.
#
# Deliberately not Watchtower. Unattended updates on a media stack are
# how a working system becomes a broken one overnight, usually while
# you are asleep and something is mid-import.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source ./scripts/lib.sh
load_env .env

var_for() { echo "$(echo "$1" | tr 'a-z-' 'A-Z_')"; }

_digest() {
  sha256_hex | cut -c1-12
}

_row() {
  local svc="$1" image="$2"
  remote=$(docker manifest inspect "$image" 2>/dev/null | _digest) || remote="?"
  local_d=$(docker image inspect "$image" --format '{{index .RepoDigests 0}}' 2>/dev/null | _digest) || local_d="none"
  if [[ "$remote" == "$local_d" ]]; then
    status="current"
  else
    status="UPDATE"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$svc" "$status" "$image" "$local_d" "$remote" "${image##*:}"
}

check() {
  printf '%-14s %-12s %s\n' APP STATUS IMAGE
  for svc in $(docker compose config --services 2>/dev/null); do
    image=$(docker compose config --format json 2>/dev/null \
            | python3 -c "import json,sys;print(json.load(sys.stdin)['services'].get('$svc',{}).get('image',''))")
    [[ -z "$image" || "$image" == *local* ]] && continue
    row=$(_row "$svc" "$image")
    IFS=$'\t' read -r _ status image _ _ _ <<< "$row"
    printf '%-14s %-12s %s\n' "$svc" "$status" "$image"
  done
}

check_json() {
  python3 - <<'PY'
import hashlib, json, subprocess

def digest(raw: str) -> str:
    if not raw or not raw.strip():
        return "none"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]

cfg = json.loads(subprocess.check_output(
    ["docker", "compose", "config", "--format", "json"], text=True))
rows = []
for svc, meta in sorted(cfg.get("services", {}).items()):
    image = (meta or {}).get("image") or ""
    if not image or "local" in image:
        continue
    try:
        remote_raw = subprocess.check_output(
            ["docker", "manifest", "inspect", image],
            stderr=subprocess.DEVNULL, text=True)
        remote = digest(remote_raw)
    except subprocess.CalledProcessError:
        remote = "?"
    try:
        local_raw = subprocess.check_output(
            ["docker", "image", "inspect", image,
             "--format", "{{index .RepoDigests 0}}"],
            stderr=subprocess.DEVNULL, text=True)
        local_d = digest(local_raw)
    except subprocess.CalledProcessError:
        local_d = "none"
    tag = image.rsplit(":", 1)[-1] if ":" in image else "latest"
    update = remote not in ("?", "none") and local_d != "none" and remote != local_d
    rows.append({
        "id": svc,
        "image": image,
        "tag": tag,
        "local_digest": local_d,
        "remote_digest": remote,
        "update_available": update,
        "status": "update" if update else "current",
    })
print(json.dumps(rows))
PY
}

apply() {
  svc="$1"
  echo "1/4 Snapshotting config before updating ${svc}..."
  snap=$(./scripts/backup.sh)
  echo "  ${snap}"

  prev=$(docker image inspect "$(docker compose config --format json \
        | python3 -c "import json,sys;print(json.load(sys.stdin)['services']['$svc']['image'])")" \
        --format '{{index .RepoDigests 0}}' 2>/dev/null || echo "")

  echo "2/4 Pulling ${svc} image..."
  docker compose pull "$svc"
  echo "3/4 Recreating ${svc}..."
  docker compose up -d "$svc"

  # Gluetun owns the network namespace for every tunnelled app. Recreating
  # it alone leaves those containers pinned to the old namespace — Seerr
  # then reports "Unable to connect to Radarr, Sonarr". Pull the whole
  # group onto the new gateway the same way the VPN restart button does.
  if [[ "$svc" == "gluetun" ]] || [[ "$svc" == gluetun_* ]] || [[ "$svc" == gluetun-* ]]; then
    mapfile -t tunnelled < <(python3 - <<'PY'
import json, os, subprocess
cfg = json.loads(subprocess.check_output(
    ["docker", "compose", "config", "--format", "json"], text=True))
profiles = {p for p in os.environ.get("COMPOSE_PROFILES", "").split(",") if p}
for name, meta in sorted((cfg.get("services") or {}).items()):
    mode = (meta or {}).get("network_mode") or ""
    if not mode.startswith("service:gluetun"):
        continue
    svc_profiles = set((meta or {}).get("profiles") or [])
    if svc_profiles and not (svc_profiles & profiles):
        continue
    print(name)
PY
)
    if ((${#tunnelled[@]})); then
      echo "3b/4 Recreating tunnelled apps onto the new gluetun: ${tunnelled[*]}"
      docker compose up -d --force-recreate "${tunnelled[@]}"
    fi
  fi

  # Any path that recreates gluetun (or leaves peers behind) can orphan
  # tunnelled apps. Heal after every apply, not only when gluetun is the
  # target — Update All and host CLI updates hit this footgun often.
  echo "3c/4 Healing tunnel orphans (if any)..."
  PYTHONPATH=helm/backend python3 -m app.tunnel_heal || true

  echo "4/4 Waiting up to 90s for ${svc} to come back healthy..."
  for _ in $(seq 1 18); do
    state=$(docker inspect --format '{{.State.Health.Status}}' "kine-${svc}" 2>/dev/null || echo "none")
    [[ "$state" == "healthy" ]] && { echo "OK: ${svc} healthy on the new image"; exit 0; }
    [[ "$state" == "none" ]] && {
      running=$(docker inspect --format '{{.State.Running}}' "kine-${svc}" 2>/dev/null || echo false)
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
  check-json) check_json ;;
  apply) apply "${2:?app}" ;;
  *) echo "usage: updates.sh check|check-json|apply <app>" >&2; exit 1 ;;
esac
