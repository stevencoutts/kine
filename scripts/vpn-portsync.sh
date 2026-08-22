#!/bin/sh
# Proton rotates the forwarded port. Transmission needs to be told, or
# you get no incoming peers and wonder why everything is slow.
set -eu
apk add --no-cache curl jq >/dev/null 2>&1 || true
last=""
while true; do
  port=$(curl -fsS http://127.0.0.1:8000/v1/openvpn/portforwarded 2>/dev/null \
         | jq -r '.port // empty' || true)
  if [ -n "$port" ] && [ "$port" != "0" ] && [ "$port" != "$last" ]; then
    sid=$(curl -sS -i "$TRANSMISSION_RPC" 2>/dev/null \
          | grep -i 'X-Transmission-Session-Id' | tr -d '\r' | awk '{print $2}')
    curl -fsS "$TRANSMISSION_RPC" \
      -H "X-Transmission-Session-Id: ${sid}" \
      -d "{\"method\":\"session-set\",\"arguments\":{\"peer-port\":${port}}}" \
      >/dev/null 2>&1 && {
        echo "$(date -Iseconds) forwarded port -> ${port}"
        last="$port"
      }
  fi
  sleep 60
done
