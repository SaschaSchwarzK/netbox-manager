#!/bin/sh
set -eu

image_name=${1:-netbox-manager:test}
test_suffix=$$
container_name="netbox-manager-smoke-$test_suffix"
volume_name="netbox-manager-smoke-data-$test_suffix"
cert_volume_name="netbox-manager-smoke-certs-$test_suffix"
test_secret="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="

cleanup() {
    result=$?
    trap - EXIT INT TERM
    if [ "$result" -ne 0 ]; then
        docker logs "$container_name" 2>/dev/null || true
    fi
    docker rm --force "$container_name" >/dev/null 2>&1 || true
    docker volume rm "$volume_name" >/dev/null 2>&1 || true
    docker volume rm "$cert_volume_name" >/dev/null 2>&1 || true
    exit "$result"
}
trap cleanup EXIT INT TERM

docker image inspect "$image_name" >/dev/null
docker volume create "$volume_name" >/dev/null
docker volume create "$cert_volume_name" >/dev/null
docker run --detach \
    --name "$container_name" \
    --env "NBM_SECRET_KEY=$test_secret" \
    --env "NBM_DATABASE_URL=sqlite:////tmp/must-not-be-used.db" \
    --volume "$volume_name:/app/data" \
    --volume "$cert_volume_name:/app/certs" \
    --read-only \
    --tmpfs /tmp:size=64m,mode=1777 \
    --cap-drop ALL \
    --security-opt no-new-privileges=true \
    --memory 256m \
    --cpus 1.0 \
    --pids-limit 128 \
    "$image_name" >/dev/null

health_state=starting
attempt=0
while [ "$attempt" -lt 45 ]; do
    health_state=$(docker inspect --format '{{.State.Health.Status}}' "$container_name")
    [ "$health_state" = healthy ] && break
    [ "$health_state" = unhealthy ] && break
    attempt=$((attempt + 1))
    sleep 1
done
[ "$health_state" = healthy ]

docker exec "$container_name" /app/venv/bin/python -c \
    "import ssl, urllib.request; context=ssl._create_unverified_context(); assert urllib.request.urlopen('https://127.0.0.1:8443/', context=context).status == 200; assert urllib.request.urlopen('https://127.0.0.1:8443/api/health', context=context).status == 200; assert urllib.request.urlopen('https://127.0.0.1:8443/docs', context=context).status == 200"

docker exec "$container_name" /app/venv/bin/python -c \
    "import os; assert os.getuid() == 65532 and os.getgid() == 65532"

docker exec "$container_name" /app/venv/bin/python -c \
    "import os, sqlite3; assert os.path.isfile('/app/data/netbox_manager.db'); assert not os.path.exists('/tmp/must-not-be-used.db'); connection=sqlite3.connect('/app/data/netbox_manager.db'); connection.execute('begin immediate'); connection.rollback()"

docker exec "$container_name" /app/venv/bin/python -c \
    "from pathlib import Path; from cryptography import x509; certificate=Path('/app/certs/cert.pem'); key=Path('/app/certs/key.pem'); assert certificate.is_file() and key.is_file(); x509.load_pem_x509_certificate(certificate.read_bytes())"

if docker exec "$container_name" /app/venv/bin/python -c \
    "open('/app/rootfs-write-test', 'w').close()" >/dev/null 2>&1; then
    echo "The container root filesystem is writable." >&2
    exit 1
fi

if docker exec "$container_name" /bin/sh -c true >/dev/null 2>&1; then
    echo "A shell is present in the runtime image." >&2
    exit 1
fi

if docker exec "$container_name" /sbin/apk --version >/dev/null 2>&1; then
    echo "A package manager is present in the runtime image." >&2
    exit 1
fi

[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container_name")" = true ]
[ "$(docker inspect --format '{{.Config.User}}' "$container_name")" = "65532:65532" ]
[ "$(docker inspect --format '{{json .HostConfig.CapDrop}}' "$container_name")" = '["ALL"]' ]
[ "$(docker inspect --format '{{.HostConfig.PidsLimit}}' "$container_name")" = 128 ]

docker stop --time 15 "$container_name" >/dev/null
[ "$(docker inspect --format '{{.State.ExitCode}}' "$container_name")" = 0 ]

echo "Container smoke tests passed for $image_name"
