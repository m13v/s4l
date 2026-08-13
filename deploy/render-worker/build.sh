#!/bin/bash
# Build + push the render-worker image via Cloud Build.
#
# Stages ONLY the files the Dockerfile needs into a temp context dir (the s4l
# repo itself is multi-GB of logs/screenshots/node_modules), then submits that
# dir. Usage:
#   bash deploy/render-worker/build.sh [image-tag]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="s4l-app-prod"
REGION="us-central1"
IMAGE="${1:-${REGION}-docker.pkg.dev/${PROJECT}/render/ig-render-worker:latest}"

CTX="$(mktemp -d /tmp/s4l-render-ctx.XXXXXX)"
trap 'rm -rf "$CTX"' EXIT

mkdir -p "$CTX/mixer/remotion" "$CTX/scripts" "$CTX/deploy/render-worker"
cp "$REPO_DIR/config.json" "$CTX/"
cp "$REPO_DIR/mixer/post_to_ig.py" "$CTX/mixer/"
cp -R "$REPO_DIR/mixer/audio" "$CTX/mixer/audio"
cp "$REPO_DIR/mixer/remotion/package.json" \
   "$REPO_DIR/mixer/remotion/package-lock.json" \
   "$REPO_DIR/mixer/remotion/remotion.config.ts" \
   "$REPO_DIR/mixer/remotion/tsconfig.json" \
   "$CTX/mixer/remotion/"
cp -R "$REPO_DIR/mixer/remotion/src" "$CTX/mixer/remotion/src"
cp -R "$REPO_DIR/mixer/remotion/public" "$CTX/mixer/remotion/public"
# Orchestrator + its imports only (identity/http_api/draft_provider).
for f in ig_render_gemini.py http_api.py identity.py draft_provider.py; do
  cp "$REPO_DIR/scripts/$f" "$CTX/scripts/"
done
cp "$REPO_DIR/deploy/render-worker/Dockerfile" "$CTX/Dockerfile"
cp "$REPO_DIR/deploy/render-worker/entrypoint.sh" "$CTX/deploy/render-worker/"

du -sh "$CTX"
gcloud builds submit "$CTX" \
  --project="$PROJECT" \
  --region="$REGION" \
  --tag="$IMAGE" \
  --timeout=1800s \
  --account=i@m13v.com
echo "image: $IMAGE"
