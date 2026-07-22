#!/usr/bin/env bash
# Build the sandbox image. Context is the project root so the engine (../steel_engine) is in scope.
set -e
cd "$(dirname "$0")/.."
IMAGE="${SANDBOX_IMAGE:-steel-sandbox:latest}"
echo "building $IMAGE ..."
docker build -f sandbox_image/Dockerfile -t "$IMAGE" .
echo "done: $IMAGE"
