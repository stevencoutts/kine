#!/usr/bin/env bash
# Writes Traefik's dynamic configuration for the chosen TLS mode.
# Re-run after changing KINE_TLS_MODE: ./kine tls
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env .env

DYN="${STACK_ROOT}/config/traefik/dynamic"
mkdir -p "$DYN" "${STACK_ROOT}/config/traefik/certs"

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
    cat > "${STACK_ROOT}/config/traefik/acme-args.txt" <<EOF
--certificatesresolvers.kineresolver.acme.email=${KINE_ACME_EMAIL}
--certificatesresolvers.kineresolver.acme.storage=/etc/traefik/acme.json
--certificatesresolvers.kineresolver.acme.caserver=${KINE_ACME_CA}
--certificatesresolvers.kineresolver.acme.dnschallenge=true
--certificatesresolvers.kineresolver.acme.dnschallenge.provider=${KINE_ACME_DNS_PROVIDER}
EOF
    touch "${STACK_ROOT}/config/traefik/acme.json"
    chmod 600 "${STACK_ROOT}/config/traefik/acme.json"
    echo "acme-dns selected: put your DNS provider credentials in"
    echo "  ${STACK_ROOT}/config/traefik/acme.env"
    echo "then run ./kine restart traefik"
    ;;
  custom)
    cat > "${DYN}/tls.yml" <<EOF
tls:
  certificates:
    - certFile: /etc/traefik/certs/fullchain.pem
      keyFile: /etc/traefik/certs/privkey.pem
EOF
    echo "custom TLS selected: place fullchain.pem and privkey.pem in"
    echo "  ${STACK_ROOT}/config/traefik/certs/"
    ;;
  *)
    echo "unknown KINE_TLS_MODE '${KINE_TLS_MODE}'" >&2; exit 1 ;;
esac
