#!/usr/bin/env bash
# Writes Traefik's dynamic configuration for the chosen TLS mode.
# Re-run after changing MC_TLS_MODE: ./mc tls
set -Eeuo pipefail
set -a; source .env; set +a

DYN="${STACK_ROOT}/config/traefik/dynamic"
mkdir -p "$DYN" "${STACK_ROOT}/config/traefik/certs"

# Forward-auth in front of every app, backed by Helm's session.
cat > "${DYN}/middlewares.yml" <<EOF
http:
  middlewares:
    mc-auth:
      forwardAuth:
        address: "http://helm:8600/api/auth/verify"
        trustForwardHeader: true
        authResponseHeaders:
          - X-Mc-User
    mc-headers:
      headers:
        stsSeconds: 31536000
        contentTypeNosniff: true
        browserXssFilter: true
        referrerPolicy: same-origin
EOF

case "${MC_TLS_MODE}" in
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
    [[ -n "${MC_ACME_EMAIL}" ]] || { echo "MC_ACME_EMAIL is required for acme-dns" >&2; exit 1; }
    cat > "${DYN}/tls.yml" <<EOF
tls:
  stores:
    default:
      defaultGeneratedCert:
        resolver: mcresolver
        domain:
          main: "${MC_DOMAIN}"
          sans:
            - "*.${MC_DOMAIN}"
EOF
    # DNS-01 rather than HTTP-01 on purpose: this appliance should not
    # need an inbound hole in the firewall to renew a certificate.
    cat > "${STACK_ROOT}/config/traefik/acme-args.txt" <<EOF
--certificatesresolvers.mcresolver.acme.email=${MC_ACME_EMAIL}
--certificatesresolvers.mcresolver.acme.storage=/etc/traefik/acme.json
--certificatesresolvers.mcresolver.acme.caserver=${MC_ACME_CA}
--certificatesresolvers.mcresolver.acme.dnschallenge=true
--certificatesresolvers.mcresolver.acme.dnschallenge.provider=${MC_ACME_DNS_PROVIDER}
EOF
    touch "${STACK_ROOT}/config/traefik/acme.json"
    chmod 600 "${STACK_ROOT}/config/traefik/acme.json"
    echo "acme-dns selected: put your DNS provider credentials in"
    echo "  ${STACK_ROOT}/config/traefik/acme.env"
    echo "then run ./mc restart traefik"
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
    echo "unknown MC_TLS_MODE '${MC_TLS_MODE}'" >&2; exit 1 ;;
esac
