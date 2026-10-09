#!/bin/sh
set -eu

output=${1:-netbox-manager-source.tar.gz}
git archive --format=tar.gz --output="$output" HEAD
echo "Created $output from tracked files at HEAD"
