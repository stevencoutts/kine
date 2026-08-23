FROM python:3.12-slim

# Debian carries no docker-compose-plugin package; fetch the official
# static binary instead of pulling in Docker's whole apt repo for one file.
ARG TARGETARCH
ARG COMPOSE_VERSION=v2.32.4
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates docker-cli nfs-common \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH}" in \
         amd64) COMPOSE_ARCH=x86_64 ;; \
         arm64) COMPOSE_ARCH=aarch64 ;; \
         *) echo "unsupported arch: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}" \
       -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
# main.py finds the frontend two parents up from itself (matches the
# helm/backend/app -> helm/frontend layout in source), so it lands at
# /frontend here, not /app/frontend.
COPY frontend /frontend

EXPOSE 8600
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8600"]
