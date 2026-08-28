#!/bin/sh
# Queue a Lidarr import for in-place beets tagging. Beet cannot run here:
# Lidarr is tunnelled and has no beet binary; beets is a separate container.
set -eu

QUEUE="${KINE_BEETS_QUEUE:-/data/downloads/.kine-beets-queue}"
MUSIC="${KINE_LIDARR_MUSIC:-/data/media/music}"

event="${lidarr_eventtype:-}"
case "$event" in
  Test) exit 0 ;;
  Download|Upgrade|Rename) ;;
  *) exit 0 ;;
esac

path="${lidarr_artist_path:-}"
if [ -z "$path" ]; then
  path="${lidarr_albumfile_path:-}"
fi
[ -n "$path" ] || exit 0

case "$path" in
  "$MUSIC"|"$MUSIC"/*)
    beets_path="/music${path#"$MUSIC"}"
    ;;
  *)
    beets_path="$path"
    ;;
esac

mkdir -p "$(dirname "$QUEUE")"
echo "$beets_path" >> "$QUEUE"
