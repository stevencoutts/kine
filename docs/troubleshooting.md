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
./mc down
rm /srv/media-centre/config/<app>/config.xml
./mc seed && ./mc up && ./mc provision
```

## Downloads have no incoming peers

The forwarded port has rotated and the sync did not take.

```bash
./mc vpn status
docker logs mc-vpn-portsync --tail 20
```

## Nothing downloads at all after a VPN change

gluetun was restarted on its own, so its dependants are sitting in a
dead namespace.

```bash
./mc vpn restart
```

## Browser warns about the certificate

`MC_TLS_MODE=internal` uses Traefik's own CA. Either trust
`/srv/media-centre/config/traefik/certs/ca.crt`, or switch to
`acme-dns` in the GUI's settings page and supply DNS credentials.

## Hardware transcoding is not being used

```bash
grep RENDER_GID .env
stat -c '%g' /dev/dri/renderD128
```

They must match. If `/dev/dri/renderD128` does not exist, the host has
no usable GPU and Emby will transcode in software.
