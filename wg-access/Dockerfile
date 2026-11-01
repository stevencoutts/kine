FROM python:3.12-alpine

# musl build: this image is Alpine, and the relay runs from this filesystem
# even when its sockets are in gluetun's network namespace.
ARG SINGBOX_VERSION=1.14.4
ARG TARGETARCH
RUN apk add --no-cache ca-certificates curl iproute2 iptables util-linux wireguard-tools \
    && case "${TARGETARCH}" in \
         amd64) ARCH=amd64 ;; \
         arm64) ARCH=arm64 ;; \
         *) echo "unsupported arch: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/sing-box.tgz \
         "https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-linux-${ARCH}-musl.tar.gz" \
    && tar -xzf /tmp/sing-box.tgz -C /tmp \
    && mv /tmp/sing-box-*/sing-box /usr/local/bin/sing-box \
    && chmod 755 /usr/local/bin/sing-box \
    && rm -rf /tmp/sing-box* \
    && sing-box version

WORKDIR /app
COPY helm/backend/app/client_vpn.py /app/client_vpn.py
COPY wg-access/entrypoint.py /app/entrypoint.py

ENTRYPOINT ["python", "-u", "/app/entrypoint.py"]
