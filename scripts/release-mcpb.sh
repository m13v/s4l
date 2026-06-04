#!/usr/bin/env bash
# One-command release pipeline for the social-autoposter .mcpb (Story B double-click install).
#
# What it does, end to end:
#   1. Resolve the release version from the repo-root package.json (or --tag override).
#   2. Build the MCP bundle: vite (panel) + tsc (server) + bundle-pipeline (embeds npm tarball).
#   3. Stamp mcp/dist/version.json so the bundle self-reports the right version.
#   4. Pack mcp/ into mcp/social-autoposter.mcpb via the mcpb CLI.
#   5. Verify the bundle (size cap, embedded pipeline.tgz, stamped version, install tools).
#   6. Create (or update) the GitHub release vX.Y.Z and upload the .mcpb as an asset.
#
# Re-running for the same version is idempotent: the asset is re-uploaded with --clobber.
#
# Usage:
#   bash scripts/release-mcpb.sh              # version from package.json
#   bash scripts/release-mcpb.sh --tag v1.6.56
#   bash scripts/release-mcpb.sh --no-release # build + pack + verify only, skip GitHub
#   bash scripts/release-mcpb.sh --draft      # create the GitHub release as a draft

set -euo pipefail

# Homebrew node/gh/mcpb are not on the default Fazm/launchd PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MCP_DIR="$REPO_ROOT/mcp"
BUNDLE="$MCP_DIR/social-autoposter.mcpb"
GH_REPO="m13v/social-autoposter"
SIZE_CAP_MB=180

TAG_OVERRIDE=""
DO_RELEASE=1
DRAFT_FLAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) TAG_OVERRIDE="$2"; shift 2 ;;
    --tag=*) TAG_OVERRIDE="${1#*=}"; shift ;;
    --no-release) DO_RELEASE=0; shift ;;
    --draft) DRAFT_FLAG="--draft"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v node >/dev/null || die "node not found on PATH"
command -v mcpb >/dev/null || die "mcpb CLI not found (npm i -g @anthropic-ai/mcpb)"

# ---- 1. Resolve version -----------------------------------------------------
PKG_VERSION="$(node -p "require('$REPO_ROOT/package.json').version")"
if [[ -n "$TAG_OVERRIDE" ]]; then
  TAG="$TAG_OVERRIDE"
  VERSION="${TAG#v}"
else
  VERSION="$PKG_VERSION"
  TAG="v$VERSION"
fi
say "Releasing social-autoposter .mcpb $TAG (package.json=$PKG_VERSION)"
if [[ "$VERSION" != "$PKG_VERSION" ]]; then
  echo "  note: tag version ($VERSION) differs from package.json ($PKG_VERSION)"
fi

# ---- 2. Build the bundle ----------------------------------------------------
say "Building MCP bundle (panel + server + embedded pipeline.tgz)"
( cd "$MCP_DIR" && npm run build:bundle )

# ---- 3. Stamp version.json --------------------------------------------------
say "Stamping mcp/dist/version.json -> $VERSION"
node -e "
const fs=require('fs'),p='$MCP_DIR/dist/version.json';
fs.writeFileSync(p, JSON.stringify({version:'$VERSION',installedAt:new Date().toISOString()},null,2)+'\n');
console.log('  '+fs.readFileSync(p,'utf8').trim());
"

# ---- 4. Pack the .mcpb ------------------------------------------------------
say "Packing $BUNDLE"
rm -f "$BUNDLE"
mcpb pack "$MCP_DIR" "$BUNDLE"

# ---- 5. Verify --------------------------------------------------------------
say "Verifying bundle"
[[ -f "$BUNDLE" ]] || die "bundle was not produced"
BYTES=$(stat -f%z "$BUNDLE" 2>/dev/null || stat -c%s "$BUNDLE")
MB=$(( BYTES / 1024 / 1024 ))
echo "  size: ${MB}MB (cap ${SIZE_CAP_MB}MB)"
(( MB <= SIZE_CAP_MB )) || die "bundle ${MB}MB exceeds ${SIZE_CAP_MB}MB cap"

# Capture the listing once (grep -q on a live pipe trips SIGPIPE under pipefail).
LISTING="$(unzip -l "$BUNDLE" 2>/dev/null || true)"

PIPELINE_COUNT=$(printf '%s\n' "$LISTING" | grep -c 'dist/pipeline.tgz' || true)
[[ "$PIPELINE_COUNT" == "1" ]] || die "expected exactly 1 embedded dist/pipeline.tgz, found $PIPELINE_COUNT"
echo "  embedded pipeline.tgz: ok"

BUNDLE_VER=$(unzip -p "$BUNDLE" dist/version.json 2>/dev/null | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).version" 2>/dev/null || echo "?")
[[ "$BUNDLE_VER" == "$VERSION" ]] || die "bundle version.json=$BUNDLE_VER != $VERSION"
echo "  version.json: $BUNDLE_VER ok"

for f in "dist/index.js" "dist/runtime.js" "manifest.json"; do
  # grep -c reads all input (no SIGPIPE); anchor on the time column + 3-space
  # gutter so node_modules/.../dist/index.js does not false-match the top-level.
  n=$(printf '%s\n' "$LISTING" | grep -c "[0-9:]   $f\$" || true)
  [[ "$n" -ge 1 ]] || die "bundle missing $f"
done
echo "  runtime + server + manifest: ok"

if [[ "$DO_RELEASE" == "0" ]]; then
  say "Done (--no-release). Bundle ready at: $BUNDLE"
  exit 0
fi

# ---- 6. GitHub release ------------------------------------------------------
command -v gh >/dev/null || die "gh CLI not found"
gh auth status >/dev/null 2>&1 || die "gh not authenticated (run: gh auth login)"

NOTES="social-autoposter ${TAG}

Double-click install for Claude Desktop. Drag \`social-autoposter.mcpb\` into Settings > Extensions, enable it, then open the panel in a Chat tab and click Install runtime (provisions uv + Python 3.12 + Chromium on first run). The pipeline source is bundled, so no separate clone or config is needed.

Power-user / CLI install: \`npx social-autoposter@${VERSION} init\`."

if gh release view "$TAG" -R "$GH_REPO" >/dev/null 2>&1; then
  say "Release $TAG exists -> uploading asset (clobber)"
  gh release upload "$TAG" "$BUNDLE" -R "$GH_REPO" --clobber
else
  say "Creating release $TAG"
  gh release create "$TAG" "$BUNDLE" \
    -R "$GH_REPO" \
    --title "social-autoposter $TAG" \
    --notes "$NOTES" \
    $DRAFT_FLAG
fi

URL=$(gh release view "$TAG" -R "$GH_REPO" --json url -q .url 2>/dev/null || echo "")
say "Released $TAG"
echo "  asset: social-autoposter.mcpb (${MB}MB)"
[[ -n "$URL" ]] && echo "  $URL"
