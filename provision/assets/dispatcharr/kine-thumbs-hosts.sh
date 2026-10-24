#!/bin/bash
# Docker forbids extra_hosts with network_mode: service:gluetun, and Proton
# DNS cannot resolve thumbs.$KINE_DOMAIN. Pin it to the LAN IP so Dispatcharr
# can cache Game-Thumbs channel logos.
set -e
lan="${KINE_LAN_IP:-}"
domain="${KINE_DOMAIN:-}"
local_domain="${KINE_LOCAL_DOMAIN:-}"
if [ -n "$lan" ] && [ -n "$domain" ]; then
  grep -q "thumbs.${domain}" /etc/hosts 2>/dev/null \
    || echo "$lan thumbs.${domain}" >> /etc/hosts
fi
if [ -n "$lan" ] && [ -n "$local_domain" ]; then
  grep -q "thumbs.${local_domain}" /etc/hosts 2>/dev/null \
    || echo "$lan thumbs.${local_domain}" >> /etc/hosts
fi
# KINE_HOST_PINS: extra "ip host" pairs, comma-separated, for upstreams whose
# DNS rotates through an address the VPN exit cannot reach (my.xc4.net).
IFS=, read -ra pins <<< "${KINE_HOST_PINS:-}"
for pin in "${pins[@]}"; do
  pin="$(echo "$pin" | xargs)"
  [ -n "$pin" ] || continue
  grep -qxF "$pin" /etc/hosts 2>/dev/null || echo "$pin" >> /etc/hosts
done
exec /app/docker/entrypoint.sh "$@"
