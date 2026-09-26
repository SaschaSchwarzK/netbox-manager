# syntax=docker/dockerfile:1.7

ARG CADDY_VERSION=v2.11.4
ARG XCADDY_VERSION=v0.4.5

FROM cgr.dev/chainguard/go:latest-dev@sha256:11b08ed26e99379f8df32197348c1a16093b15a071bbd2793125040982f46f91 AS caddy-builder
ARG CADDY_VERSION
ARG XCADDY_VERSION
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go install github.com/caddyserver/xcaddy/cmd/xcaddy@${XCADDY_VERSION} && \
    CGO_ENABLED=0 /root/go/bin/xcaddy build ${CADDY_VERSION} --output /out/caddy

FROM cgr.dev/chainguard/python:latest-dev@sha256:eb0d45dfc69fecb471d2eaee7a8eea281bf860578ef44cb85db1bfa8165c47fe AS app-builder
USER root
RUN apk add --no-cache nodejs npm

WORKDIR /build
COPY backend/requirements.txt backend/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /app/venv && \
    /app/venv/bin/pip install -r backend/requirements.txt && \
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
COPY deploy/serve.py /app/serve.py
COPY Caddyfile /app/Caddyfile
RUN mkdir -p /app/data /app/certs && chown -R 65532:65532 /app

FROM cgr.dev/chainguard/python:latest@sha256:565af762d7f3efedc4e60d7ac7815e41588211d3f5757be33d8303e915ee6c72 AS runner
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
COPY --from=app-builder --chown=65532:65532 /app/serve.py /app/serve.py
COPY --from=app-builder --chown=65532:65532 /app/Caddyfile /app/Caddyfile
COPY --from=app-builder --chown=65532:65532 /build/frontend/dist /app/frontend
COPY --from=app-builder --chown=65532:65532 /app/data /app/data
COPY --from=app-builder --chown=65532:65532 /app/certs /app/certs

USER 65532:65532
EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["/app/venv/bin/python", "-c", "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:8443/api/health', context=ssl._create_unverified_context(), timeout=2)"]
ENTRYPOINT ["/app/venv/bin/python", "/app/serve.py"]
