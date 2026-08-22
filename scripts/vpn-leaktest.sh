#!/usr/bin/env bash
# Proves the tunnel is doing its job, rather than assuming it is.
# The GUI runs this and shows both addresses side by side.
set -Eeuo pipefail
host_ip=$(docker run --rm --network kine_internal curlimages/curl:latest \
          -fsS https://api.ipify.org 2>/dev/null || echo "unavailable")
vpn_ip=$(docker exec kine-gluetun wget -qO- https://api.ipify.org \
         2>/dev/null || echo "unavailable")

echo "untunnelled exit : ${host_ip}"
echo "tunnelled exit   : ${vpn_ip}"

if [[ "$host_ip" == "unavailable" || "$vpn_ip" == "unavailable" ]]; then
  echo "RESULT: inconclusive"; exit 2
elif [[ "$host_ip" == "$vpn_ip" ]]; then
  echo "RESULT: LEAKING. Tunnelled traffic is leaving on your own address."; exit 1
else
  echo "RESULT: OK"; exit 0
fi
