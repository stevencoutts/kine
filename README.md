# Media Centre

A pre-built, portable media appliance. Clone it onto a Linux host, run
one script, and you get Emby, the *arr stack, a VPN-tunnelled download
client, IPTV and sports EPG, all already configured to talk to each
other, behind a single sign-on and a single HTTPS front door.

The apps are not just installed side by side. Sonarr and Radarr come up
with their root folders set and their download clients registered.
Prowlarr comes up already linked to both, so every indexer you add
appears in them automatically. Emby comes up with its libraries created.
Transmission comes up inside a WireGuard tunnel with a kill switch and
port forwarding that follows the provider's rotation.

## Requirements

- Linux x86_64, Docker Engine, Docker Compose **2.20 or newer**
- One filesystem holding both your media and your downloads
- Optional: `/dev/dri` for Intel QuickSync transcoding

## Install

```bash
git clone <this repo> media-centre
cd media-centre
sudo ./install.sh
```

Then open the admin GUI (printed at the end of the install) and set your
admin password, domain and certificate mode. Add your VPN key and your
indexer accounts. That is the whole setup.

## Day to day

```bash
./mc apps                 # what exists and what is on
./mc enable bazarr        # add an app: profile, start, wire, done
./mc updates              # what has a newer image
./mc update sonarr        # snapshot, pull, recreate, roll back on failure
./mc vpn leaktest         # prove the tunnel is carrying the traffic
./mc backup               # config and env, never the media
```

Everything the GUI does is one of these commands. The GUI is a
convenience, not a second source of truth.

## How it holds together

**One file decides what runs.** Each app is a Compose fragment
declaring a profile equal to its own name, and `COMPOSE_PROFILES` in
`.env` lists what is on. Enabling an app is a one-word edit followed by
`docker compose up -d`. There is no template rendering and no generated
state, so the repo plus `.env` reproduces the appliance exactly.

**Keys are derived, not discovered.** Every internal API key comes from
`MC_SECRET` by SHA-256. That means the provisioner knows Sonarr's key
before Sonarr has ever started, which is what makes shipping a pre-wired
stack possible at all: no chicken-and-egg where you must boot an app,
log in, copy a key and paste it into another app.

**One `/data` mount, not two.** Media and downloads are mounted into
the *arr containers under a single parent. Split them and you lose
hardlinks and atomic moves, and every import becomes a full copy. The
preflight check refuses to install if they straddle a filesystem.

**The VPN is a namespace, not a route, and it covers the whole
acquisition tier.** Sonarr, Radarr, Prowlarr, Bazarr, Transmission,
NZBGet, Unpackerr and Recyclarr all join gluetun's network namespace.
None of them has an interface of its own or any path out except `wg0`,
and gluetun's firewall drops the rest. If the tunnel drops, all of that
traffic stops rather than falling back to your own address.

Three things follow, and they are worth understanding before you rely
on this:

- Those apps carry no ports and no Traefik labels of their own. Every
  router for them lives on the gluetun service, because that is the
  container holding their sockets. From inside they reach each other on
  `127.0.0.1`; from outside they are `gluetun:<port>`.
- They share one port space, so no two of them may claim the same port.
  `docs/port-map.md` is the register.
- The tunnel is now a hard dependency for far more than torrenting. A
  flapping VPN presents as a broken *arr stack, and restarting the
  tunnel takes the whole acquisition tier down for about a minute. The
  CLI and GUI both refuse to restart gluetun on its own for that
  reason.

**The domain is `.local` because that's the one mDNS actually
resolves.** `MC_DOMAIN` defaults to `media.local`; the `mdns` profile
(on by default) advertises it and every enabled app's subdomain over
mDNS via avahi, so LAN devices reach `https://emby.media.local` with no
DNS setup. Only `.local` gets this for free — RFC 6762 reserves it for
mDNS, and stock macOS/Windows/iOS/Android resolvers don't look up
anything else that way. The appliance host itself doesn't need
multicast to reach its own domain; install.sh writes a loopback
`/etc/hosts` entry for it regardless of the `mdns` profile.

**Updates are opt-in and reversible.** Images are pinned by tag and
digest. An update snapshots config, pulls, recreates, and watches the
healthcheck for 90 seconds; if it does not come back, it reverts the
digest and restores the snapshot.

## Tests

```bash
python -m pytest tests -q
```

104 checks over the structural invariants Compose will not enforce for
you: port collisions inside the shared VPN namespace, tunnelled apps
that still carry their own ports or labels, apps starting before the
tunnel is healthy, unpinned images, split `/data` mounts, and the
seeder overwriting a key something else already holds. Two of them
caught real mistakes in this repo before it was ever installed.

## Layout

```
compose/        one fragment per app
provision/      the wiring: derived keys, seeding, API recipes
helm/           the admin GUI (FastAPI + a single-file front end)
scripts/        preflight, TLS, backup, restore, updates, VPN
tests/          structural invariants, run before every commit
catalogue.yml   app metadata the GUI and provisioner both read
```

Runtime state lives outside the repo, under `STACK_ROOT`
(default `/srv/media-centre`) and `DATA_ROOT` (default `/srv/media-data`).

## Security

Helm can create containers, which makes it root-equivalent on the host.
The socket proxy narrows what a bug in it can reach; it does not make a
compromise of it survivable. Helm sits behind forward-auth, binds to the
LAN, and should not be exposed to the internet. Nothing in this design
assumes otherwise.
