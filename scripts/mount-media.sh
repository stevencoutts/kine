#!/usr/bin/env bash
# Mount optional NFS exports onto the local DATA_ROOT tree.
# Called from install.sh and from Helm via the host nfs-browse-agent.
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO/scripts/lib.sh"
source "$REPO/scripts/nfs-mount-opts.sh"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m+ %s\033[0m\n' "$*"; }

HOST_ROOT="${KINE_HOST_ROOT:-}"
host_path() {
  if [[ -n "$HOST_ROOT" ]]; then
    printf '%s%s' "${HOST_ROOT%/}" "$1"
  else
    printf '%s' "$1"
  fi
}

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -f "$REPO/.env" ]] || die ".env not found; run install.sh first"
load_env "$REPO/.env"

for value in "${NFS_SERVER:-}" "${NFS_MEDIA:-}" "${NFS_TV:-}" "${NFS_MOVIES:-}" "${NFS_DOWNLOADS:-}" "${NFS_CACHE:-}"; do
  [[ "$value" != *[[:space:]]* ]] || die "NFS server and export paths cannot contain whitespace"
done

fstab_line() {
  local mount_point=$1
  local export_path=$2
  [[ -n "$export_path" ]] || return 0
  printf '%s:%s %s nfs %s 0 0\n' \
    "$NFS_SERVER" "$export_path" "$mount_point" "$KINE_NFS_FSTAB_OPTS"
}

# Downloads under the media export must be a bind mount, not a second NFS
# mount — separate NFS mounts cannot hardlink even on the same server path.
downloads_under_media() {
  local media="${NFS_MEDIA:-}"
  local dl="${NFS_DOWNLOADS:-}"
  [[ -n "$media" && -n "$dl" && "$dl" == "$media"/* ]]
}

downloads_media_subdir() {
  printf '%s' "${NFS_DOWNLOADS#"${NFS_MEDIA}/"}"
}

write_fstab() {
  local tmp fstab
  fstab="$(host_path /etc/fstab)"
  tmp=$(mktemp)
  awk '
    /^# BEGIN kine-nfs$/ { managed=1; next }
    /^# END kine-nfs$/ { managed=0; next }
    !managed { print }
  ' "$fstab" > "$tmp"
  {
    echo "# BEGIN kine-nfs"
    fstab_line "${DATA_ROOT}/media" "${NFS_MEDIA:-}"
    if [[ -n "${NFS_TV:-}" && "${NFS_TV:-}" != "${NFS_MEDIA:-}" ]]; then
      fstab_line "${DATA_ROOT}/media/tv" "${NFS_TV:-}"
    fi
    if [[ -n "${NFS_MOVIES:-}" && "${NFS_MOVIES:-}" != "${NFS_MEDIA:-}" ]]; then
      fstab_line "${DATA_ROOT}/media/movies" "${NFS_MOVIES:-}"
    fi
    if downloads_under_media; then
      printf '%s %s none bind,nofail 0 0\n' \
        "${DATA_ROOT}/media/$(downloads_media_subdir)" "${DATA_ROOT}/downloads"
    else
      fstab_line "${DATA_ROOT}/downloads" "${NFS_DOWNLOADS:-}"
    fi
    fstab_line "${DATA_ROOT}/cache/tdarr" "${NFS_CACHE:-}"
    echo "# END kine-nfs"
  } >> "$tmp"
  install -m 644 "$tmp" "$fstab"
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
    current=$(findmnt -n -o SOURCE -M "$mount_point")
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

  if mount -t nfs -o "$KINE_NFS_FSTAB_OPTS" "$spec" "$mount_point"; then
    ok "mounted ${spec} -> ${mount_point}"
    return 0
  fi
  if mount "$mount_point"; then
    ok "mounted ${spec} -> ${mount_point} (via fstab)"
    return 0
  fi
  warn "could not mount ${spec} at ${mount_point}"
  return 1
}

mount_downloads() {
  local export_path="${NFS_DOWNLOADS:-}"
  [[ -n "$export_path" ]] || return 0

  if downloads_under_media; then
    local src="${MEDIA_ROOT}/$(downloads_media_subdir)"
    local media_spec="${NFS_SERVER}:${NFS_MEDIA}"
    local subdir="/$(downloads_media_subdir)"
    mkdir -p "$src" "$DOWNLOADS_ROOT"
    if mountpoint -q "$DOWNLOADS_ROOT" 2>/dev/null; then
      local current fsroot
      current=$(findmnt -n -o SOURCE -M "$DOWNLOADS_ROOT")
      fsroot=$(findmnt -n -o FSROOT -M "$DOWNLOADS_ROOT")
      fsroot="${fsroot%/}"
      # Bind of media/downloads shares the media NFS source with FSROOT=/downloads.
      if [[ "$current" == "$media_spec" && "$fsroot" == "${subdir%/}" ]] \
        || [[ "$current" == "$src" || "$current" == "${src}/" ]]; then
        ok "Downloads already bind-mounted at ${DOWNLOADS_ROOT}"
        return 0
      fi
      warn "Downloads remounting as bind of ${src} (was ${current} fsroot=${fsroot})"
      umount "$DOWNLOADS_ROOT" || {
        warn "could not unmount ${DOWNLOADS_ROOT}; stop dependent apps and retry"
        return 1
      }
    fi
    if mount --bind "$src" "$DOWNLOADS_ROOT"; then
      ok "bind-mounted ${src} -> ${DOWNLOADS_ROOT}"
      return 0
    fi
    warn "could not bind-mount ${src} at ${DOWNLOADS_ROOT}"
    return 1
  fi

  mount_export "$DOWNLOADS_ROOT" "$export_path" "Downloads"
}

MEDIA_ROOT="$(host_path "${DATA_ROOT}/media")"
TV_ROOT="$(host_path "${DATA_ROOT}/media/tv")"
MOVIES_ROOT="$(host_path "${DATA_ROOT}/media/movies")"
DOWNLOADS_ROOT="$(host_path "${DATA_ROOT}/downloads")"
CACHE_ROOT="$(host_path "${DATA_ROOT}/cache/tdarr")"

bold "Media storage mounts"
mkdir -p "$CACHE_ROOT"

if [[ -z "${NFS_MEDIA:-}${NFS_TV:-}${NFS_MOVIES:-}${NFS_DOWNLOADS:-}${NFS_CACHE:-}" ]]; then
  echo "No NFS exports configured; using local directories."
  exit 0
fi
[[ -n "${NFS_SERVER:-}" ]] || die "NFS_SERVER is required when an export is configured"
command -v mountpoint >/dev/null || die "mountpoint is required (install util-linux)"
command -v findmnt >/dev/null || die "findmnt is required (install util-linux)"
write_fstab

# Nested TV/Movies mounts break hardlinks even on the same NFS server.
# When those keys are empty, drop any leftover mounts (either case).
drop_mount_if_cleared() {
  local mount_point=$1
  local wanted=$2
  [[ -n "$wanted" ]] && return 0
  if mountpoint -q "$mount_point" 2>/dev/null; then
    warn "unmounting ${mount_point} (cleared so media+downloads stay one filesystem)"
    umount "$mount_point" || warn "could not unmount ${mount_point}; stop apps and retry"
  fi
}
drop_mount_if_cleared "$TV_ROOT" "${NFS_TV:-}"
drop_mount_if_cleared "$(host_path "${DATA_ROOT}/media/TV")" "${NFS_TV:-}"
drop_mount_if_cleared "$MOVIES_ROOT" "${NFS_MOVIES:-}"
drop_mount_if_cleared "$(host_path "${DATA_ROOT}/media/Movies")" "${NFS_MOVIES:-}"

mount_export "$MEDIA_ROOT" "${NFS_MEDIA:-}" "Media"
if [[ -n "${NFS_TV:-}" && "${NFS_TV:-}" != "${NFS_MEDIA:-}" ]]; then
  mount_export "$TV_ROOT" "${NFS_TV:-}" "TV"
fi
if [[ -n "${NFS_MOVIES:-}" && "${NFS_MOVIES:-}" != "${NFS_MEDIA:-}" ]]; then
  mount_export "$MOVIES_ROOT" "${NFS_MOVIES:-}" "Movies"
fi
mount_downloads
mount_export "$CACHE_ROOT" "${NFS_CACHE:-}" "Tdarr cache"

# Apps expect lowercase tv/movies paths; link when the share uses Title case.
link_media_subdir() {
  local lower=$1
  local upper=$2
  local target="${MEDIA_ROOT}/${lower}"
  local source="${MEDIA_ROOT}/${upper}"

  [[ -n "${NFS_MEDIA:-}" ]] || return 0
  [[ -d "$source" ]] || return 0

  if [[ -d "$target" ]] && [[ ! -L "$target" ]] && [[ -z "$(ls -A "$target" 2>/dev/null)" ]]; then
    rmdir "$target" || {
      warn "${target} exists but is not empty; leave content in ${upper}/ or remove it manually"
      return 1
    }
    ok "removed empty placeholder ${target}"
  fi

  if [[ -e "$target" ]]; then
    if [[ -L "$target" ]]; then
      ok "${target} already linked"
    fi
    return 0
  fi

  ln -s "$upper" "$target"
  ok "linked ${target} -> ${upper}"
}

link_media_subdir tv TV
link_media_subdir movies Movies
