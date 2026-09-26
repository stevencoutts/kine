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
`172.30.53.0/29`). The default list already does, via `172.16.0.0/12`.

## A newly added tier 2 app will not start

Two apps in the tunnel cannot claim the same port, because they share
one network stack. Check `docs/port-map.md` and pick a free one.

```bash
docker logs kine-<app> --tail 20   # look for "address already in use"
```
