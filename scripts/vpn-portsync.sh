#!/bin/sh
# Proton rotates the forwarded port. Transmission needs to be told, or
# you get no incoming peers and wonder why everything is slow.
#
# Busybox tools only. This process lives inside the tunnel, so an
# `apk add` at startup would depend on the very connection it exists to
# watch: if the VPN is down when the container starts, the install
# fails and the sidecar never runs. wget and sed are always present.
set -eu

CONTROL="http://127.0.0.1:8000/v1/openvpn/portforwarded"
STATIC="${VPN_FORWARDED_PORT:-}"
STATIC="${STATIC%%,*}"
last=""

json_field() {
  # {"port":54321} -> 54321. Crude, but the payload is one flat object
  # and this avoids a dependency that cannot be guaranteed here.
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p"
}

log() { echo "$(date -Iseconds) portsync: $*"; }

while true; do
  port=$(wget -qO- "$CONTROL" 2>/dev/null | json_field port || true)
  if [ -z "${port:-}" ] || [ "$port" = "0" ]; then
    port="$STATIC"
  fi

  if [ -n "${port:-}" ] && [ "$port" != "0" ] && [ "$port" != "$last" ]; then
    # Transmission's RPC hands out a session id on the first 409 and
    # requires it on every subsequent call.
    sid=$(wget -qS -O /dev/null "$TRANSMISSION_RPC" 2>&1 \
          | sed -n 's/.*X-Transmission-Session-Id: *\([A-Za-z0-9]*\).*/\1/p' | head -1)

    if [ -n "${sid:-}" ] && wget -q -O /dev/null \
         --header="X-Transmission-Session-Id: ${sid}" \
         --post-data="{\"method\":\"session-set\",\"arguments\":{\"peer-port\":${port}}}" \
         "$TRANSMISSION_RPC" 2>/dev/null
    then
      log "forwarded port -> ${port}"
      last="$port"
    else
      log "port ${port} available but Transmission would not accept it"
    fi
  fi
  sleep 60
done
