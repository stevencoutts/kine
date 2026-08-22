#!/usr/bin/env bash
# Config, .env and compose files. Never the media: that is what your
# actual storage strategy is for.
set -Eeuo pipefail
set -a; source .env; set +a
stamp=$(date +%Y%m%d-%H%M%S)
out="${STACK_ROOT}/backups/kine-${stamp}.tar.gz"
mkdir -p "${STACK_ROOT}/backups"
tar czf "$out" \
  --exclude='*/logs/*' --exclude='*/cache/*' --exclude='*/transcoding-temp/*' \
  -C "${STACK_ROOT}" config \
  -C "$(pwd)" .env docker-compose.yml compose catalogue.yml
echo "$out"
# Keep the last 10
# `-r`/`--no-run-if-empty` is GNU-only; `rm -f` with no args is a no-op
# on BSD/macOS xargs too, so it's portable without the flag.
ls -1t "${STACK_ROOT}"/backups/kine-*.tar.gz 2>/dev/null | tail -n +11 | xargs rm -f --
