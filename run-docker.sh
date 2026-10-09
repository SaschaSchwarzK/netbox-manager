#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./run-docker.sh [--build] [--replace]

  --build    Build the image before starting the container.
  --replace  Stop and remove an existing container with the same name.

Optional environment overrides:
  IMAGE_NAME      Image to build or run       (default: netbox-manager:latest)
  CONTAINER_NAME  Container name              (default: netbox-manager)
  DATA_VOLUME     Persistent SQLite volume    (default: netbox-manager_dbdata)
  CERT_VOLUME     Persistent certificate volume (default: netbox-manager_certdata)
  EXPORT_VOLUME   Persistent Data Export volume (default: netbox-manager_exportdata)
  HOST_PORT       Host HTTPS port              (default: 8443)
  ENV_FILE        Application environment file (default: ./.env, then backend/.env)
  MEMORY_LIMIT    Container memory limit       (default: 256m)
  CPU_LIMIT       Container CPU limit          (default: 1.0)
  PIDS_LIMIT      Container process limit      (default: 128)
EOF
}

build=false
replace=false

while [ "$#" -gt 0 ]; do
    case "$1" in
        --build)
            build=true
            ;;
        --replace)
            replace=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
image_name=${IMAGE_NAME:-netbox-manager:latest}
container_name=${CONTAINER_NAME:-netbox-manager}
data_volume=${DATA_VOLUME:-netbox-manager_dbdata}
cert_volume=${CERT_VOLUME:-netbox-manager_certdata}
export_volume=${EXPORT_VOLUME:-netbox-manager_exportdata}
host_port=${HOST_PORT:-8443}
if [ -n "${ENV_FILE:-}" ]; then
    env_file=$ENV_FILE
elif [ -f "$script_dir/.env" ]; then
    env_file=$script_dir/.env
else
    env_file=$script_dir/backend/.env
fi
memory_limit=${MEMORY_LIMIT:-256m}
cpu_limit=${CPU_LIMIT:-1.0}
pids_limit=${PIDS_LIMIT:-128}

command -v docker >/dev/null 2>&1 || {
    echo "Docker is not installed or is not available in PATH." >&2
    exit 1
}

docker info >/dev/null 2>&1 || {
    echo "The Docker daemon is not available." >&2
    exit 1
}

if [ ! -f "$env_file" ]; then
    echo "Environment file not found: $env_file" >&2
    echo "Create it from backend/.env.example and set NBM_SECRET_KEY." >&2
    exit 1
fi

if [ "$build" = true ]; then
    docker build --tag "$image_name" "$script_dir"
elif ! docker image inspect "$image_name" >/dev/null 2>&1; then
    echo "Image '$image_name' does not exist. Run this script with --build first." >&2
    exit 1
fi

if docker container inspect "$container_name" >/dev/null 2>&1; then
    if [ "$replace" = true ]; then
        docker rm --force "$container_name" >/dev/null
    elif [ "$(docker inspect --format '{{.State.Running}}' "$container_name")" = true ]; then
        echo "Container '$container_name' is already running."
        echo "Use --replace to recreate it."
        exit 0
    else
        docker start "$container_name" >/dev/null
        echo "Started existing container '$container_name'."
        echo "Open https://localhost:$host_port"
        exit 0
    fi
fi

docker volume create "$data_volume" >/dev/null
docker volume create "$cert_volume" >/dev/null
docker volume create "$export_volume" >/dev/null

# The 64 MiB tmpfs leaves headroom above the 48 MiB archive cap and counts against --memory.
docker run --detach \
    --name "$container_name" \
    --restart unless-stopped \
    --env-file "$env_file" \
    --publish "$host_port:8443" \
    --volume "$data_volume:/app/data" \
    --volume "$cert_volume:/app/certs" \
    --volume "$export_volume:/app/exports" \
    --read-only \
    --tmpfs /tmp:size=64m,mode=1777 \
    --cap-drop ALL \
    --security-opt no-new-privileges=true \
    --stop-timeout 15 \
    --memory "$memory_limit" \
    --cpus "$cpu_limit" \
    --pids-limit "$pids_limit" \
    "$image_name" >/dev/null

echo "Started '$container_name' from '$image_name'."
echo "Open https://localhost:$host_port"
echo "Follow logs with: docker logs -f $container_name"
