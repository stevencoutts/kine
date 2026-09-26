# Troubleshooting

## Imports are slow and the disk fills up

Media and downloads are on different filesystems, so Sonarr and Radarr
are copying rather than hardlinking. Check with:

```bash
stat -c '%d' /srv/media-data/media /srv/media-data/downloads
```

Two different numbers is the problem. There is no configuration fix;
the paths have to live on one mount.

## An *arr app rejects the API key

The container started before it was seeded, so it minted its own key.

```bash
./kine down
rm /srv/kine/config/<app>/config.xml   # Sonarr, Radarr, Prowlarr
# Jackett: rm /srv/kine/config/jackett/Jackett/ServerConfig.json
./kine seed && ./kine up && ./kine provision
```

## Downloads have no incoming peers

The forwarded port has rotated and the sync did not take.

```bash
./kine vpn status
docker logs kine-vpn-portsync --tail 20
```

## Nothing downloads at all after a VPN change

gluetun was restarted on its own, so its dependants are sitting in a
dead namespace.

```bash
./kine vpn restart
```

## Browser warns about the certificate

`KINE_TLS_MODE=internal` uses Traefik's own CA. Either trust
`/srv/kine/config/traefik/certs/ca.crt`, or switch to
`acme-dns` in Settings, set an ACME email, and put ClouDNS API
credentials (`CLOUDNS_AUTH_ID` / `CLOUDNS_AUTH_PASSWORD`) in
`/srv/kine/config/traefik/acme.env`.

## Hardware transcoding is not being used

```bash
grep RENDER_GID .env
stat -c '%g' /dev/dri/renderD128
```

They must match. If `/dev/dri/renderD128` does not exist, the host has
no usable GPU and Emby will transcode in software.

## Sonarr, Radarr and Prowlarr are all offline at once

They are not broken. They live inside the tunnel, so they go down with
it. Check the tunnel first, before touching any of them:

```bash
./kine vpn status
docker logs kine-gluetun --tail 50
```

A bad WireGuard key, an expired subscription or a dead endpoint all
present the same way: the entire acquisition tier unreachable while
Emby and Dispatcharr carry on fine. That split is the diagnostic.

## Pi-hole will not start, or LAN DNS dies with the VPN

Publishing `0.0.0.0:53` fails when anything on the host already holds
port 53, including a listener on a single address such as libvirt's
dnsmasq on `virbr0`. Enable does not stop that program. It publishes
Pi-hole on the host's other addresses (the LAN bridge) and records them
in `KINE_DNS_BIND`. A listener on every interface still stops enable.
Free that socket and enable again.

External lookups from Pi-hole go only to the primary tunnel. While that
tunnel is down, LAN DNS and DNS for apps outside the tunnel fail on
purpose. The Pi-hole web UI can still be up. Tunnelled apps such as
Sonarr keep using their own tunnel's resolver and are unaffected by
Pi-hole being enabled.

`FIREWALL_OUTBOUND_SUBNETS` has to contain `KINE_DNS_SUBNET` (default
`172.16.53.0/27`). The default list already does, via `172.16.0.0/12`.
`KINE_DNS_GLUETUN` and `KINE_DNS_PIHOLE` have to be different hosts
inside that subnet, and neither may be the Docker gateway (`.1`). The
defaults are `.2` and `.3`, with other containers drawn from
`KINE_DNS_POOL` (`172.16.53.16/28`). If enable says the pool overlaps
another network, pick a free subnet and set the subnet, the pool, and
both addresses together.

"Address already in use" on `kine_dns`, or "no available IPv4 addresses",
means the pool is handing out `.2` or `.3`, or the subnet is too small.
Keep those two addresses outside `KINE_DNS_POOL`. Start Gluetun before
the other DNS consumers so it can claim `.2`.

Gravity that stops with "DNS resolution is currently unavailable" means
Gluetun is not listening on port 53. The primary tunnel has to keep
`DNS_SERVER=on` with a plaintext resolver. `DOT=off` on current Gluetun
turns that listener off. Pi-hole's general upstream stays
`${KINE_DNS_GLUETUN}#53`.

DNS answers only on the addresses in `KINE_DNS_BIND`. Putting another
address on the host, including a VRRP address moved from another machine,
does nothing until that address is added to `KINE_DNS_BIND` and Pi-hole
is recreated. The address has to be on the host first, or Docker cannot
bind the socket.

## A media server stays offline and Watching is empty

Plex or Emby is configured with a hostname Pi-hole does not know. Helm
uses Pi-hole once that profile is on, so a name that used to resolve via
the router now fails and the server shows offline with no sessions.

In the Pi-hole UI, set the local domain and add a conditional forwarder
for it to the router (`dns.revServers`: enabled, the LAN CIDR, the router
address, and the domain). Both are required. With the domain marked
local and no forwarder, Pi-hole answers NXDOMAIN and never asks upstream.
Check from Helm's resolver:

```bash
docker exec kine-helm python -c "import socket; print(socket.getaddrinfo('emby.example.com', 443)[0][4])"
```

## Live TV apps cannot reach each other after Direct

ECM, Teamarr, and Dispatcharr stored `http://127.0.0.1:<port>` while
they shared Gluetun. On Direct each has its own namespace, so loopback
is the wrong host. They need `http://dispatcharr:9191` and
`http://teamarr:9195`. Toggling Direct rewrites those URLs. If a guide
source still shows the loopback address, toggle Direct off and on again,
or run `./kine provision`.

## A newly added tier 2 app will not start

Two apps in the tunnel cannot claim the same port, because they share
one network stack. Check `docs/port-map.md` and pick a free one.

```bash
docker logs kine-<app> --tail 20   # look for "address already in use"
```
