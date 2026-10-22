#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
version="${1:-local}"
[[ "$version" =~ ^[a-zA-Z0-9_.-]+$ ]] || { echo 'Invalid image tag'; exit 1; }
builder="telegram-reader-build-$$"
# Dedicated temporary builder: its cache never accumulates on the shared VPS.
docker buildx create --name "$builder" --driver docker-container >/dev/null
trap 'docker buildx rm --force "$builder" >/dev/null 2>&1 || true' EXIT
docker buildx build --builder "$builder" --load -t "telegram-reader-mcp:$version" .
