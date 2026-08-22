#!/usr/bin/env bash
# ./scripts/restore.sh <tarball> [app]
set -Eeuo pipefail
tarball="${1:?usage: restore.sh <tarball> [app]}"
app="${2:-}"
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env .env
docker compose down
if [[ -n "$app" ]]; then
  tar xzf "$tarball" -C "${STACK_ROOT}" "config/${app}"
  echo "restored config for ${app}"
else
  tar xzf "$tarball" -C "${STACK_ROOT}" config
  tar xzf "$tarball" -C . .env docker-compose.yml compose catalogue.yml
  echo "restored full appliance"
fi
docker compose up -d
