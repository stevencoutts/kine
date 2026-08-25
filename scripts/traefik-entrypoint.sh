#!/bin/sh
# Append Let's Encrypt DNS-01 flags from acme-args.txt (written by tls-setup.sh)
# onto Traefik's normal command line. When the file is empty/missing, Traefik
# starts with the compose command only (internal / custom TLS).
set -eu

# Official image prepends "traefik" when argv starts with a flag.
if [ "$#" -eq 0 ] || [ "${1#-}" != "$1" ]; then
  set -- traefik "$@"
fi

if [ -f /etc/traefik/acme-args.txt ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    # trim CR and skip blanks / comments
    line=$(printf '%s' "$line" | tr -d '\r')
    [ -z "$line" ] && continue
    case "$line" in
      \#*) continue ;;
    esac
    set -- "$@" "$line"
  done < /etc/traefik/acme-args.txt
fi

exec "$@"
