#!/bin/sh
# Queue a Lidarr import for in-place beets tagging. Beet cannot run here:
# Lidarr is tunnelled and has no beet binary; beets is a separate container.
#
# Queue the album folder(s) that just changed, not the whole artist folder:
# beets re-reads and rewrites every file it is given, so queueing the artist made
# every import (and every rename) retag that artist's entire library.
# Rename events are ignored: renaming files does not change their tags.
set -eu

QUEUE="${KINE_BEETS_QUEUE:-/data/media/downloads/.kine-beets-queue}"
MUSIC="${KINE_LIDARR_MUSIC:-/data/media/music}"

event="${lidarr_eventtype:-}"
case "$event" in
  Download|Upgrade) ;;
  *) exit 0 ;;
esac

# Album folders from the files Lidarr just imported (pipe-separated). A disc
# subfolder (CD 01, Disc 2 ...) is walked up to its album folder so a multi-disc
# album is imported as one album.
dirs=""
added="${lidarr_addedtrackpaths:-}"
if [ -n "$added" ]; then
  dirs=$(printf '%s' "$added" | tr '|' '\n' | while IFS= read -r f; do
    [ -n "$f" ] || continue
    d=$(dirname "$f")
    case "$(basename "$d" | tr '[:upper:]' '[:lower:]')" in
      cd\ [0-9]*|disc\ [0-9]*|disk\ [0-9]*|vinyl\ [0-9]*|digital\ media\ [0-9]*) d=$(dirname "$d") ;;
    esac
    printf '%s\n' "$d"
  done | sort -u)
fi
# Older Lidarr events carry no track list: fall back to the artist folder.
if [ -z "$dirs" ]; then
  dirs="${lidarr_artist_path:-${lidarr_albumfile_path:-}}"
fi
[ -n "$dirs" ] || exit 0

mkdir -p "$(dirname "$QUEUE")"
printf '%s\n' "$dirs" | while IFS= read -r path; do
  [ -n "$path" ] || continue
  case "$path" in
    "$MUSIC"|"$MUSIC"/*) beets_path="/music${path#"$MUSIC"}" ;;
    *) beets_path="$path" ;;
  esac
  echo "$beets_path" >> "$QUEUE"
done
