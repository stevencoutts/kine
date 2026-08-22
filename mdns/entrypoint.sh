#!/bin/sh
# mDNS only advertises the .local TLD (RFC 6762), and only single names,
# not wildcards -- so every enabled app's subdomain needs its own alias.
# avahi-daemon runs as the real daemon; each name is then registered as
# an alias with avahi-publish over D-Bus, not via /etc/avahi/hosts --
# verified that static-hosts entries get rejected ("Local name
# collision") when they point at an address the daemon already owns as
# its own interface address, which is exactly this container's case
# under network_mode: host.
set -e

HOST_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
[ -n "$HOST_IP" ] || HOST_IP=$(hostname -i | awk '{print $1}')

mkdir -p /var/run/dbus
dbus-daemon --system --fork
avahi-daemon --no-drop-root &

until avahi-daemon --check 2>/dev/null; do sleep 0.5; done

python3 /gen_hosts.py | while read -r name; do
  avahi-publish -a -R "$name" "$HOST_IP" &
done

wait
