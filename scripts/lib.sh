#!/usr/bin/env bash
# Portability shims. The appliance targets Linux (README says so), but
# these are the handful of places GNU coreutils syntax would otherwise
# hard-fail on macOS during local dev/testing on Docker Desktop.
load_env() {
  local file="${1:-.env}" line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# || "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    case "$value" in
      \"*\") value="${value:1:${#value}-2}" ;;
      \'*\') value="${value:1:${#value}-2}" ;;
    esac
    export "$key=$value"
  done < "$file"
}

# Append keys from .env.example that are missing from an existing .env.
# Keeps hand-edited values; fills gaps after upgrades or partial copies.
merge_missing_env_keys() {
  local env_file="${1:-.env}" example="${2:-.env.example}" line key
  local -a existing=()
  [[ -f "$env_file" && -f "$example" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" != *=* || "$line" =~ ^[[:space:]]*# ]] && continue
    key="${line%%=*}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    existing+=("$key")
  done < "$env_file"
  local added=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# || "$line" != *=* ]] && continue
    key="${line%%=*}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    local found=0 k
    for k in "${existing[@]+"${existing[@]}"}"; do
      [[ "$k" == "$key" ]] && { found=1; break; }
    done
    if [[ $found -eq 0 ]]; then
      printf '%s\n' "$line" >> "$env_file"
      existing+=("$key")
      added=1
    fi
  done < "$example"
  [[ $added -eq 1 ]]
}

is_darwin() { [[ "$(uname)" == "Darwin" ]]; }

# In-place sed: GNU wants `-i pattern`, BSD/macOS requires `-i ''`.
sedi() {
  if is_darwin; then sed -i '' "$@"; else sed -i "$@"; fi
}

# Filesystem device id of a path, for the hardlink-safety check.
dev_id() {
  if is_darwin; then stat -f '%d' "$1" 2>/dev/null; else stat -c '%d' "$1" 2>/dev/null; fi
}

# GB free on the filesystem holding a path. `-P` (POSIX format) and
# `-k` (1024-byte blocks) are the options GNU and BSD df agree on.
avail_gb() {
  df -Pk "${1:-/}" | awk 'NR==2 {printf "%d", $4/1048576}'
}

# Is anything already listening on this TCP port? Uses bash's own
# /dev/tcp instead of `ss`, which macOS doesn't have.
port_busy() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# Next free TCP port at or above ``start``, skipping ``avoid`` when set.
find_free_port() {
  local start="${1:?}" avoid="${2:-}"
  local p=$start
  while (( p < 65535 )); do
    if [[ -n "$avoid" && "$p" -eq "$avoid" ]]; then
      p=$((p + 1))
      continue
    fi
    if ! port_busy "$p"; then
      echo "$p"
      return 0
    fi
    p=$((p + 1))
  done
  return 1
}

# Ensure Traefik host ports in .env are free; rewrite when taken.
# Returns 0 when it changed .env, 1 when ports were already free.
ensure_traefik_ports() {
  local env_file="${1:-.env}"
  local http https new_http new_https
  http="${TRAEFIK_HTTP_PORT:-8080}"
  https="${TRAEFIK_HTTPS_PORT:-8443}"
  new_http=$http
  new_https=$https
  if port_busy "$http"; then
    new_http=$(find_free_port "$http") || return 2
  fi
  if port_busy "$https" || [[ "$https" == "$new_http" ]]; then
    local start=$https
    (( start <= new_http )) && start=$((new_http + 1))
    new_https=$(find_free_port "$start" "$new_http") || return 2
  fi
  if [[ "$new_http" == "$http" && "$new_https" == "$https" ]]; then
    return 1
  fi
  if grep -q '^TRAEFIK_HTTP_PORT=' "$env_file"; then
    sedi "s|^TRAEFIK_HTTP_PORT=.*|TRAEFIK_HTTP_PORT=${new_http}|" "$env_file"
  else
    printf 'TRAEFIK_HTTP_PORT=%s\n' "$new_http" >> "$env_file"
  fi
  if grep -q '^TRAEFIK_HTTPS_PORT=' "$env_file"; then
    sedi "s|^TRAEFIK_HTTPS_PORT=.*|TRAEFIK_HTTPS_PORT=${new_https}|" "$env_file"
  else
    printf 'TRAEFIK_HTTPS_PORT=%s\n' "$new_https" >> "$env_file"
  fi
  export TRAEFIK_HTTP_PORT="$new_http" TRAEFIK_HTTPS_PORT="$new_https"
  return 0
}

# Best-effort LAN IP for the "open this URL" hint. hostname -I is Linux-only.
local_ip() {
  if is_darwin; then
    ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1"
  else
    local script
    script="$(dirname "${BASH_SOURCE[0]}")/../mdns/pick_ip.py"
    if [[ -f "$script" ]] && command -v python3 >/dev/null 2>&1; then
      python3 "$script" 2>/dev/null || true
    fi
    hostname -I 2>/dev/null | awk '{print $1}'
  fi
}

# sha256sum is a coreutils binary; macOS ships `shasum -a 256` instead.
sha256_hex() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi
}
