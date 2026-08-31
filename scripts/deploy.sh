#!/usr/bin/env bash
# Deploy the webhook service to Fly (RC1-337). Deploys here are manual, so
# this wrapper is what injects the commit sha the Dockerfile turns into
# Datadog source links — a bare `fly deploy` would ship blank git metadata.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! git diff --quiet HEAD; then
    echo "refusing to deploy: uncommitted changes (the sha would lie)" >&2
    exit 1
fi

exec fly deploy --build-arg GIT_SHA="$(git rev-parse HEAD)" "$@"
