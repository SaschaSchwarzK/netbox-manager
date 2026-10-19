# syntax=docker/dockerfile:1

ARG CADDY_VERSION=v2.11.7
ARG XCADDY_VERSION=v0.4.5

FROM cgr.dev/chainguard/go:latest-dev@sha256:8c5e682389f30e2f4152cc7404123f48a6b6808f98ac2c2519ef028c269cd7e4 AS caddy-builder
ARG CADDY_VERSION
ARG XCADDY_VERSION
COPY security/go-fixes.txt /build/security/go-fixes.txt
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build <<'EOF'
set -eu
go install "github.com/caddyserver/xcaddy/cmd/xcaddy@${XCADDY_VERSION}"
replacements="$(awk '
        function trim(value) { gsub(/^[ \t]+|[ \t]+$/, "", value); return value }
        /^[ \t]*($|#)/ { next }
        {
            line = $0
            sub(/[ \t]*#.*/, "", line)
            count = split(line, fields, "=")
            if (count != 2) {
                print "Invalid go-fixes.txt entry: " $0 > "/dev/stderr"
                exit 2
            }
            source = trim(fields[1])
            target = trim(fields[2])
            module = source
            sub(/@.*/, "", module)
            if (target ~ /^v[0-9]/) target = module "@" target
            printf "--replace %s=%s ", source, target
        }
    ' /build/security/go-fixes.txt)"
# Intentional word splitting: each generated --replace pair is a separate xcaddy argument.
# An empty or comments-only file expands to no extra arguments.
CGO_ENABLED=0 /root/go/bin/xcaddy build "${CADDY_VERSION}" $replacements --output /out/caddy
EOF

FROM cgr.dev/chainguard/python:latest-dev@sha256:905b31747ebcae4242e0b71fcd2640c712b79a96f929811d3202b9716b66592a AS app-builder
USER root
RUN apk add --no-cache nodejs npm

WORKDIR /build
COPY backend/requirements.lock backend/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /app/venv && \
    /app/venv/bin/pip install --require-hashes -r backend/requirements.lock && \
    rm -rf /app/venv/lib/python*/site-packages/pip \
        /app/venv/lib/python*/site-packages/pip-*.dist-info \
        /app/venv/bin/pip* && \
    find /app/venv -type f -name '*.pyc' -delete && \
    find /app/venv -type d -name __pycache__ -empty -delete

COPY frontend/package.json frontend/package-lock.json frontend/
RUN --mount=type=cache,target=/root/.npm \
    cd frontend && npm ci
COPY frontend frontend
RUN cd frontend && npm run build

COPY backend/app /app/app
COPY backend/alembic.ini /app/alembic.ini
COPY backend/alembic /app/alembic
COPY config /app/config
COPY templates /app/templates
COPY deploy/serve.py /app/serve.py
COPY Caddyfile /app/Caddyfile
RUN mkdir -p /app/data /app/certs /app/exports && chown -R 65532:65532 /app

FROM cgr.dev/chainguard/python:latest@sha256:26a608c1549ee52eeb717bf66bce5e60cebaa4a0cd2a57774dfd49484bd38c9a AS runner
WORKDIR /app

ENV PATH="/app/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    XDG_CONFIG_HOME=/tmp/caddy/config \
    XDG_DATA_HOME=/tmp/caddy/data

COPY --from=caddy-builder /out/caddy /usr/bin/caddy
COPY --from=app-builder --chown=65532:65532 /app/venv /app/venv
COPY --from=app-builder --chown=65532:65532 /app/app /app/app
COPY --from=app-builder --chown=65532:65532 /app/alembic.ini /app/alembic.ini
COPY --from=app-builder --chown=65532:65532 /app/alembic /app/alembic
COPY --from=app-builder --chown=65532:65532 /app/config /app/config
COPY --from=app-builder --chown=65532:65532 /app/templates /app/templates
COPY --from=app-builder --chown=65532:65532 /app/serve.py /app/serve.py
COPY --from=app-builder --chown=65532:65532 /app/Caddyfile /app/Caddyfile
COPY --from=app-builder --chown=65532:65532 /build/frontend/dist /app/frontend
COPY --from=app-builder --chown=65532:65532 /app/data /app/data
COPY --from=app-builder --chown=65532:65532 /app/certs /app/certs
COPY --from=app-builder --chown=65532:65532 /app/exports /app/exports

USER 65532:65532
EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["/app/venv/bin/python", "-c", "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:8443/api/health/ready', context=ssl._create_unverified_context(), timeout=2)"]
ENTRYPOINT ["/app/venv/bin/python", "/app/serve.py"]
