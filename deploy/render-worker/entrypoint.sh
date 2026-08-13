#!/bin/bash
# Render-worker entrypoint: seed the installation identity (so /api/v1 writes
# attribute to a stable install instead of a fresh one per container), then
# hand every arg to the orchestrator.
set -euo pipefail

if [ -n "${S4L_IDENTITY_JSON:-}" ]; then
  mkdir -p /root/.social-autoposter
  printf '%s' "$S4L_IDENTITY_JSON" > /root/.social-autoposter/identity.json
fi

# post_to_ig.py resolves the repo via ~/social-autoposter; point it at /app so
# every Path.home()-based reference works unchanged in the container.
ln -sfn /app /root/social-autoposter

mkdir -p /app/mixer/remotion/out
exec python3 /app/scripts/ig_render_gemini.py "$@"
