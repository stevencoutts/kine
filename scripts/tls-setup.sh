#!/usr/bin/env bash
# Writes Traefik's dynamic configuration for the chosen TLS mode.
# Re-run after changing KINE_TLS_MODE: ./kine tls
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env .env

DYN="${STACK_ROOT}/config/traefik/dynamic"
TRAEFIK_CFG="${STACK_ROOT}/config/traefik"
mkdir -p "$DYN" "${TRAEFIK_CFG}/certs"

# Forward-auth in front of every app, backed by Helm's session.
cat > "${DYN}/middlewares.yml" <<EOF
http:
  middlewares:
    kine-auth:
      forwardAuth:
        address: "http://helm:8600/api/auth/verify"
        trustForwardHeader: true
        authResponseHeaders:
          - X-Kine-User
    kine-headers:
      headers:
        stsSeconds: 31536000
        contentTypeNosniff: true
        browserXssFilter: true
        referrerPolicy: same-origin
EOF

# Drop stale ACME CLI flags unless this run re-creates them.
rm -f "${TRAEFIK_CFG}/acme-args.txt"

# Ensure ClouDNS (or other provider) env file always exists for Compose env_file=.
if [[ ! -f "${TRAEFIK_CFG}/acme.env" ]]; then
  cat > "${TRAEFIK_CFG}/acme.env" <<'EOF'
# DNS-01 credentials for Traefik / lego (loaded when KINE_TLS_MODE=acme-dns).
# ClouDNS (https://www.cloudns.net/): API auth id + password from
#   CloudDNS → API → Auth ID
# CLOUDNS_AUTH_ID=
# CLOUDNS_AUTH_PASSWORD=
#
# Optional sub-user:
# CLOUDNS_SUB_AUTH_ID=
#
# Other providers: https://doc.traefik.io/traefik/https/acme/#providers
EOF
  chmod 600 "${TRAEFIK_CFG}/acme.env"
fi

case "${KINE_TLS_MODE}" in
  internal)
    cat > "${DYN}/tls.yml" <<EOF
tls:
  stores:
    default: {}
EOF
    # Traefik's internal resolver needs no ACME config; certificates are
    # signed by its own CA. Fine on a LAN, warns in browsers until the
    # CA is trusted.
    ;;
  acme-dns)
    [[ -n "${KINE_ACME_EMAIL}" ]] || { echo "KINE_ACME_EMAIL is required for acme-dns" >&2; exit 1; }
    provider="${KINE_ACME_DNS_PROVIDER:-cloudns}"
    ca="${KINE_ACME_CA:-https://acme-v02.api.letsencrypt.org/directory}"
    cat > "${DYN}/tls.yml" <<EOF
tls:
  stores:
    default:
      defaultGeneratedCert:
        resolver: kineresolver
        domain:
          main: "${KINE_DOMAIN}"
          sans:
            - "*.${KINE_DOMAIN}"
EOF
    # DNS-01 rather than HTTP-01 on purpose: this appliance should not
    # need an inbound hole in the firewall to renew a certificate.
    # delayBeforeCheck gives ClouDNS time to publish the TXT record.
    cat > "${TRAEFIK_CFG}/acme-args.txt" <<EOF
--certificatesresolvers.kineresolver.acme.email=${KINE_ACME_EMAIL}
--certificatesresolvers.kineresolver.acme.storage=/etc/traefik/acme.json
--certificatesresolvers.kineresolver.acme.caserver=${ca}
--certificatesresolvers.kineresolver.acme.dnschallenge=true
--certificatesresolvers.kineresolver.acme.dnschallenge.provider=${provider}
--certificatesresolvers.kineresolver.acme.dnschallenge.delaybeforecheck=30
--entrypoints.websecure.http.tls.certresolver=kineresolver
EOF
    touch "${TRAEFIK_CFG}/acme.json"
    chmod 600 "${TRAEFIK_CFG}/acme.json"
    echo "acme-dns selected (provider=${provider}): put DNS API credentials in"
    echo "  ${TRAEFIK_CFG}/acme.env"
    echo "then recreate Traefik (Settings → Save, or: docker compose up -d --force-recreate traefik)"
    ;;
  custom)
    cat > "${DYN}/tls.yml" <<EOF
tls:
  certificates:
    - certFile: /etc/traefik/certs/fullchain.pem
      keyFile: /etc/traefik/certs/privkey.pem
EOF
    echo "custom TLS selected: place fullchain.pem and privkey.pem in"
    echo "  ${TRAEFIK_CFG}/certs/"
    ;;
  *)
    echo "unknown KINE_TLS_MODE '${KINE_TLS_MODE}'" >&2; exit 1 ;;
esac
