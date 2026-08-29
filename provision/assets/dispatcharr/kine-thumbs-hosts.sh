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
exec /app/docker/entrypoint.sh "$@"
