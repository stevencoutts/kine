# Client VPN (laptop WireGuard)

Date: 2026-11-01

Status: building from the agreed behaviour

## Why

One VPN login, the one Kine already holds, should cover a laptop as well.
The laptop connects to Kine with WireGuard. Kine sends that laptop's
internet out through the existing provider tunnel when the tunnel is up,
and out the home connection when it is not. The house stays reachable
either way. The laptop is connected only when its owner turns WireGuard on.

## Streaming constraint

Emby and Live TV that are already working must keep working. This feature
is additive.

- Do not restart or recreate gluetun.
- Do not move apps into or out of a tunnel.
- Do not change the kill switch, Traefik routes, or DNS the current
  streams use.
- A laptop connecting or disconnecting must not drop an in-progress stream.

The acquisition kill switch stays what it is. The laptop does not join
gluetun's network namespace, so a VPN drop does not cut the laptop off,
and bringing the laptop up does not rebuild the tunnel group.

## Behaviour

- WireGuard on the laptop. Full tunnel (`AllowedIPs = 0.0.0.0/0`).
- At home the endpoint is Kine's LAN address, direct.
- Away from home the endpoint is a public host or IP. The router must
  forward one UDP port to Kine. Kine does not configure the router.
- Two configs, same keys, different endpoints. The owner picks the one
  that matches where the laptop is.
- House addresses (RFC1918) are routed to the home LAN and masqueraded
  from the access container. Sites on the internet see the provider
  address when that tunnel is up, and the home address when it is not.
- Names of Kine apps (`emby.kine.local` and the other catalogue names)
  resolve to the LAN address of Kine. Other internet names follow the
  same exit as the laptop's internet. Other devices in the house are
  reached by IP address.
- No kill switch. If the provider tunnel is down, or the path into it
  is not ready, laptop internet uses the home connection.
- Several named devices can be added. The first one is the laptop.

## How the exit is shared

The provider session stays the one gluetun already has. A second
WireGuard session with the same keys would fight that session and can
take the live tunnel down, so this does not open one.

`kine-wg-access` terminates the laptop's WireGuard. When gluetun reports
`running`, internet packets from the laptop subnet are handed to a
userspace relay whose sockets live in gluetun's existing network
namespace (`nsenter -n` only). Those sockets leave through the tunnel
gluetun already built. The relay is a new process. It does not change
gluetun's firewall, routes, or lifetime.

When gluetun is not running, the relay is not used. The laptop subnet's
default route is the access container's normal gateway, which is the
home connection.

Policy routing applies only to the laptop subnet inside the access
container. It does not change the host's default route or gluetun's
routes.

## What Helm does

The VPN tab gains a Laptop VPN card: LAN address, public address, UDP
port, device name, and two config downloads. Turning it on runs
`docker compose up -d --no-deps` for `wg-access` only. Turning it off
stops that container only. Peer changes rewrite the access container's
config; WireGuard `syncconf` picks them up without deleting the
interface. Neither path names gluetun.

The service profile is `wg-access` and is not added to
`COMPOSE_PROFILES`, so a normal stack reconcile does not start it.
An explicit `up` of that one service does.

## Non-goals

- Replacing gluetun, or giving the laptop the acquisition kill switch.
- Configuring the home router's port forward.
- IPv6. The laptop's IPv6 can still use the local network.
- mDNS across the tunnel. Kine app names are answered directly; other
  house devices are reached by IP.
- A second provider login, or choosing which gluetun profile the laptop
  uses. The exit is whichever tunnel `kine-gluetun` is running.
