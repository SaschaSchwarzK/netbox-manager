#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
lock_file="$repo_root/baseimage.lock"
dockerfile="$repo_root/Dockerfile"

resolve_image() {
  tag=$1
  digest=$(docker buildx imagetools inspect "$tag" | awk '$1 == "Digest:" { print $2; exit }')
  case "$digest" in
    sha256:[0-9a-f][0-9a-f]*) ;;
    *)
      echo "Could not resolve a sha256 digest for $tag" >&2
      exit 1
      ;;
  esac
  printf '%s@%s\n' "$tag" "$digest"
}

go_dev=$(resolve_image cgr.dev/chainguard/go:latest-dev)
python_dev=$(resolve_image cgr.dev/chainguard/python:latest-dev)
python_runtime=$(resolve_image cgr.dev/chainguard/python:latest)

lock_tmp=$(mktemp "${lock_file}.XXXXXX")
docker_tmp=$(mktemp "${dockerfile}.XXXXXX")
trap 'rm -f "$lock_tmp" "$docker_tmp"' EXIT HUP INT TERM

{
  echo '# Pinned Chainguard image references used by Dockerfile.'
  echo '# Update with scripts/update-baseimage-lock.sh; do not edit digests independently.'
  printf 'GO_DEV=%s\n' "$go_dev"
  printf 'PYTHON_DEV=%s\n' "$python_dev"
  printf 'PYTHON_RUNTIME=%s\n' "$python_runtime"
} > "$lock_tmp"

awk -v go_dev="$go_dev" -v python_dev="$python_dev" -v python_runtime="$python_runtime" '
  /^FROM cgr\.dev\/chainguard\/go:latest-dev@sha256:/ {
    print "FROM " go_dev " AS caddy-builder"
    next
  }
  /^FROM cgr\.dev\/chainguard\/python:latest-dev@sha256:/ {
    print "FROM " python_dev " AS app-builder"
    next
  }
  /^FROM cgr\.dev\/chainguard\/python:latest@sha256:/ {
    print "FROM " python_runtime " AS runner"
    next
  }
  { print }
' "$dockerfile" > "$docker_tmp"

grep -Fqx "FROM $go_dev AS caddy-builder" "$docker_tmp"
grep -Fqx "FROM $python_dev AS app-builder" "$docker_tmp"
grep -Fqx "FROM $python_runtime AS runner" "$docker_tmp"

mv "$lock_tmp" "$lock_file"
mv "$docker_tmp" "$dockerfile"
trap - EXIT HUP INT TERM

echo "Updated baseimage.lock and Dockerfile."
