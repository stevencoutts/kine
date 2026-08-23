ARG ALPINE_TAG=3.20
FROM alpine:${ALPINE_TAG}
RUN apk add --no-cache avahi avahi-tools dbus python3 py3-yaml iproute2
COPY gen_hosts.py /gen_hosts.py
COPY pick_ip.py /pick_ip.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
