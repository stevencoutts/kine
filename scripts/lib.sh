#!/usr/bin/env bash
# Portability shims. The appliance targets Linux (README says so), but
# these are the handful of places GNU coreutils syntax would otherwise
# hard-fail on macOS during local dev/testing on Docker Desktop.
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

# Best-effort LAN IP for the "open this URL" hint. hostname -I is Linux-only.
local_ip() {
  if is_darwin; then
    ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1"
  else
    hostname -I 2>/dev/null | awk '{print $1}'
  fi
}

# sha256sum is a coreutils binary; macOS ships `shasum -a 256` instead.
sha256_hex() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi
}
