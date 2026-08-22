#!/usr/bin/env bash
# Mount optional NFS exports onto the local DATA_ROOT tree.
# Called from install.sh; re-run with sudo after changing settings in Helm.
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m+ %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -f "$REPO/.env" ]] || die ".env not found; run install.sh first"
# shellcheck disable=SC1091
set -a; source "$REPO/.env"; set +a

for value in "${NFS_SERVER:-}" "${NFS_TV:-}" "${NFS_MOVIES:-}" "${NFS_DOWNLOADS:-}"; do
  [[ "$value" != *[[:space:]]* ]] || die "NFS server and export paths cannot contain whitespace"
done

fstab_line() {
  local mount_point=$1
  local export_path=$2
  [[ -n "$export_path" ]] || return 0
  printf '%s:%s %s nfs rw,_netdev,nofail,x-systemd.automount 0 0\n' \
    "$NFS_SERVER" "$export_path" "$mount_point"
}

write_fstab() {
  local tmp
  tmp=$(mktemp)
  awk '
    /^# BEGIN kine-nfs$/ { managed=1; next }
    /^# END kine-nfs$/ { managed=0; next }
    !managed { print }
  ' /etc/fstab > "$tmp"
  {
    echo "# BEGIN kine-nfs"
    fstab_line "${DATA_ROOT}/media/tv" "${NFS_TV:-}"
    fstab_line "${DATA_ROOT}/media/movies" "${NFS_MOVIES:-}"
    fstab_line "${DATA_ROOT}/downloads" "${NFS_DOWNLOADS:-}"
    echo "# END kine-nfs"
  } >> "$tmp"
  install -m 644 "$tmp" /etc/fstab
  rm -f "$tmp"
}

mount_export() {
  local mount_point=$1
  local export_path=$2
  local label=$3

  [[ -n "$export_path" ]] || return 0

  mkdir -p "$mount_point"
  local spec="${NFS_SERVER}:${export_path}"

  if mountpoint -q "$mount_point" 2>/dev/null; then
    local current
    current=$(findmnt -n -o SOURCE --target "$mount_point")
    if [[ "$current" == "$spec" ]]; then
      ok "${label} already mounted at ${mount_point}"
      return 0
    fi
    warn "${label} source changed from ${current}; remounting"
    umount "$mount_point" || {
      warn "could not unmount ${mount_point}; stop dependent apps and retry"
      return 1
    }
  fi

  if mount "$mount_point"; then
    ok "mounted ${spec} -> ${mount_point}"
  else
    warn "could not mount ${spec} at ${mount_point}"
    return 1
  fi
}

bold "Media storage mounts"
if [[ -z "${NFS_TV:-}${NFS_MOVIES:-}${NFS_DOWNLOADS:-}" ]]; then
  echo "No NFS exports configured; using local directories."
  exit 0
fi
[[ -n "${NFS_SERVER:-}" ]] || die "NFS_SERVER is required when an export is configured"
command -v mountpoint >/dev/null || die "mountpoint is required (install util-linux)"
command -v findmnt >/dev/null || die "findmnt is required (install util-linux)"
write_fstab
mount_export "${DATA_ROOT}/media/tv" "${NFS_TV:-}" "TV"
mount_export "${DATA_ROOT}/media/movies" "${NFS_MOVIES:-}" "Movies"
mount_export "${DATA_ROOT}/downloads" "${NFS_DOWNLOADS:-}" "Downloads"
