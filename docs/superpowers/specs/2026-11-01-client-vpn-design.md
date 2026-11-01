# Client VPN (WireGuard for client devices)

Date: 2026-11-01

Status: building from the agreed behaviour

## Why

One VPN login, the one Kine already holds, should cover a client device as well.
The device connects to Kine with WireGuard. Kine sends that device's
internet out through the existing provider tunnel when the tunnel is up,
and out the home connection when it is not. The house stays reachable
either way. The device is connected only when its owner turns WireGuard on.

## Streaming constraint

Emby and Live TV that are already working must keep working. This feature
is additive.

- Do not restart or recreate gluetun.
- Do not move apps into or out of a tunnel.
- Do not change the kill switch, Traefik routes, or DNS the current
  streams use.
- A client device connecting or disconnecting must not drop an in-progress stream.

The acquisition kill switch stays what it is. A client device does not join
gluetun's network namespace, so a VPN drop does not cut the device off,
and bringing the client VPN up does not rebuild the tunnel group.

## Behaviour

- WireGuard on the client device. Full tunnel (`AllowedIPs = 0.0.0.0/0`).
- At home the endpoint is Kine's LAN address, direct.
- Away from home the endpoint is a public host or IP. The router must
  forward one UDP port to Kine. Kine does not configure the router.
- Two configs, same keys, different endpoints. The owner picks the one
  that matches where the device is.
- House addresses (RFC1918) are routed to the home LAN and masqueraded
  from the access container. Sites on the internet see the provider
  address when that tunnel is up, and the home address when it is not.
- Names of Kine apps (`emby.kine.local` and the other catalogue names)
  resolve to the LAN address of Kine. Other internet names follow the
  same exit as the device's internet. Other devices in the house are
  reached by IP address.
- No kill switch. If the provider tunnel is down, or the path into it
  is not ready, client internet uses the home connection.
- Several named devices can be added.

## How the exit is shared

The provider session stays the one gluetun already has. A second
WireGuard session with the same keys would fight that session and can
take the live tunnel down, so this does not open one.

`kine-wg-access` terminates each client device's WireGuard. When gluetun reports
`running`, internet packets from the client subnet are handed to a
userspace relay whose sockets live in gluetun's existing network
namespace (`nsenter -n` only). Those sockets leave through the tunnel
gluetun already built. The relay is a new process. It does not change
gluetun's firewall, routes, or lifetime.

When gluetun is not running, the relay is not used. The client subnet's
default route is the access container's normal gateway, which is the
home connection.

Policy routing applies only to the client subnet inside the access
container. It does not change the host's default route or gluetun's
routes.

## Client subnet

The default is `10.66.66.0/24` (`CLIENT_CIDR` in `client_vpn.py`). The
Client VPN card can store another one as `client_cidr` in the state file.
It must be an RFC1918 network from /16 to /29, smaller than a whole
private range, with room for the server and every device. It must not
overlap the host's interface networks (reported by `wg-access` in
`status.json`) or `KINE_LAN_IP`, the Kine Docker networks (`kine_edge`,
`kine_internal`, `kine_ctrl`, `kine_dns` / `KINE_DNS_SUBNET`), the gluetun
tunnel address (`WIREGUARD_ADDRESSES`), or the access container's tun link.

The server takes the first host address, which is also the DNS address in
every config. Devices take the next free ones. A new subnet re-addresses
every device in order and keeps its keys; each one is marked until its
config is downloaded again. While the client VPN is on, Helm recreates
`wg-access` only, so nothing from the old subnet stays in its namespace.

Gluetun does not need to know the client subnet. Client traffic is
masqueraded to the access container's Docker address before it reaches the
relay in gluetun's namespace, so `FIREWALL_OUTBOUND_SUBNETS` and gluetun's
routes are unchanged and gluetun is never recreated for this.

## What Helm does

The VPN tab gains a Client VPN card: LAN address, public address, UDP
port, device name, and two config downloads. Turning it on runs
`docker compose up -d --no-deps` for `wg-access` only. Turning it off
stops that container only. Peer changes rewrite the access container's
config; WireGuard `syncconf` picks them up without deleting the
interface. Neither path names gluetun.

The service profile is `wg-access` and is not added to
`COMPOSE_PROFILES`, so a normal stack reconcile does not start it.
An explicit `up` of that one service does.

## Non-goals

- Replacing gluetun, or giving client devices the acquisition kill switch.
- Configuring the home router's port forward.
- IPv6. A client device's IPv6 can still use the local network.
- mDNS across the tunnel. Kine app names are answered directly; other
  house devices are reached by IP.
- A second provider login, or choosing which gluetun profile client devices
  use. The exit is whichever tunnel `kine-gluetun` is running.
