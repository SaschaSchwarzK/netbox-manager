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
  HOST_PORT       Host HTTP port               (default: 8088)
  ENV_FILE        Application environment file (default: backend/.env)
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
host_port=${HOST_PORT:-8088}
env_file=${ENV_FILE:-$script_dir/backend/.env}

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
        echo "Open http://localhost:$host_port"
        exit 0
    fi
fi

docker volume create "$data_volume" >/dev/null

docker run --detach \
    --name "$container_name" \
    --restart unless-stopped \
    --env-file "$env_file" \
    --publish "$host_port:8080" \
    --volume "$data_volume:/app/data" \
    --read-only \
    --tmpfs /tmp:size=64m,mode=1777 \
    --cap-drop ALL \
    --security-opt no-new-privileges=true \
    --stop-timeout 15 \
    "$image_name" >/dev/null

echo "Started '$container_name' from '$image_name'."
echo "Open http://localhost:$host_port"
echo "Follow logs with: docker logs -f $container_name"
