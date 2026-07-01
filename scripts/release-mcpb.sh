#!/usr/bin/env bash
# THE single release flow for social-autoposter. One command does EVERYTHING and
# keeps the npm path (Story A) and the .mcpb double-click path (Story B) on one
# version, because both derive from one bumped repo-root package.json.
#
# What it does, end to end:
#   1. Bump the repo-root package.json (the SINGLE source of truth) and lockfile.
#      Default: patch bump. --bump minor|major, or pin with --version / --tag,
#      or --no-bump to re-release the current version as-is.
#   2. Build the MCP bundle: vite (panel) + tsc (server) + bundle-pipeline. The
#      embedded pipeline.tgz is `npm pack` of the (now-bumped) repo, so the shell
#      and the bundled Python pipeline CANNOT diverge.
#   3. Stamp dist/version.json + manifest.json + mcp/package.json(+lock) to match.
#   4. Pack mcp/ into mcp/social-autoposter.mcpb via the mcpb CLI.
#   5. Verify: size cap, embedded pipeline.tgz present, version.json + manifest +
#      the pipeline.tgz's OWN internal version all == VERSION (the guard that was
#      missing when 1.6.84 shipped a 1.6.83 pipeline), install tools present.
#   6. npm publish social-autoposter@VERSION (idempotent; skipped if already live).
#   7. Create/update GitHub release vX.Y.Z and upload the .mcpb (--clobber).
#
# Boxes self-update from the GitHub release: the menu-bar updater polls
# releases/latest, pulls the new .mcpb, and on next server boot
# ensurePipelineCurrent() re-extracts pipeline.tgz. No manual box step needed.
#
# WHERE THE "⬆ Update available" BANNER COMES FROM (read this before touching the
# release/detection path): the menu-bar banner is driven by versionStatus() in
# mcp/src/version.ts, which resolves "latest" from GitHub releases/latest (via
# curl), NOT from npm. .mcpb boxes have no npm/npx on PATH, so an npm-based probe
# is permanently blind there. Consequence for releasing: the banner fires only
# after the GitHub release step (7) lands and releases/latest serves the new tag,
# NOT after npm publish (6). Step 8 below verifies that so a release can't
# "succeed" while every box stays silent on the old version.
#
# Usage:
#   bash scripts/release-mcpb.sh                 # patch bump, npm + .mcpb + GitHub
#   bash scripts/release-mcpb.sh --bump minor
#   bash scripts/release-mcpb.sh --version 1.7.0 # pin an exact version
#   bash scripts/release-mcpb.sh --no-bump       # re-release current package.json version
#   bash scripts/release-mcpb.sh --no-npm        # skip npm publish (only .mcpb + GitHub)
#   bash scripts/release-mcpb.sh --no-release    # build + pack + verify only (no npm, no GitHub)
#   bash scripts/release-mcpb.sh --draft         # GitHub release as a draft

set -euo pipefail

# Homebrew node/gh/mcpb are not on the default Fazm/launchd PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MCP_DIR="$REPO_ROOT/mcp"
BUNDLE="$MCP_DIR/social-autoposter.mcpb"
GH_REPO="m13v/social-autoposter"
SIZE_CAP_MB=180

TAG_OVERRIDE=""
VERSION_OVERRIDE=""
DO_RELEASE=1
DO_NPM=1
DO_BUMP=1
BUMP_LEVEL="patch"
DRAFT_FLAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) TAG_OVERRIDE="$2"; shift 2 ;;
    --tag=*) TAG_OVERRIDE="${1#*=}"; shift ;;
    --version) VERSION_OVERRIDE="$2"; shift 2 ;;
    --version=*) VERSION_OVERRIDE="${1#*=}"; shift ;;
    --bump) BUMP_LEVEL="$2"; shift 2 ;;
    --bump=*) BUMP_LEVEL="${1#*=}"; shift ;;
    --no-bump) DO_BUMP=0; shift ;;
    --no-npm) DO_NPM=0; shift ;;
    --no-release) DO_RELEASE=0; shift ;;
    --draft) DRAFT_FLAG="--draft"; shift ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

case "$BUMP_LEVEL" in
  patch|minor|major) ;;
  *) echo "invalid --bump level: $BUMP_LEVEL (want patch|minor|major)" >&2; exit 2 ;;
esac

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v node >/dev/null || die "node not found on PATH"
command -v mcpb >/dev/null || die "mcpb CLI not found (npm i -g @anthropic-ai/mcpb)"

# ---- 1. Resolve + WRITE version into the repo-root package.json -------------
# The repo-root package.json is the SINGLE source of truth: `npm pack` reads it
# to build the embedded pipeline.tgz, so the bundle shell and the bundled
# pipeline cannot diverge as long as we bump it BEFORE building (step 2). This
# closes the 1.6.84-class bug where the shell said X but pipeline.tgz carried
# the prior version because only the satellites were stamped.
PKG_VERSION="$(node -p "require('$REPO_ROOT/package.json').version")"
if [[ -n "$VERSION_OVERRIDE" ]]; then
  VERSION="${VERSION_OVERRIDE#v}"
elif [[ -n "$TAG_OVERRIDE" ]]; then
  VERSION="${TAG_OVERRIDE#v}"
elif [[ "$DO_BUMP" == "1" ]]; then
  # Compute the next version without writing a git tag; we own the write below.
  VERSION="$(node -e "
    let [a,b,c]='$PKG_VERSION'.split('.').map(Number);
    const lvl='$BUMP_LEVEL';
    if(lvl==='major'){a++;b=0;c=0;} else if(lvl==='minor'){b++;c=0;} else {c++;}
    console.log(a+'.'+b+'.'+c);
  ")"
else
  VERSION="$PKG_VERSION"
fi
TAG="v$VERSION"

# Write the resolved version into the repo-root package.json + lockfile so the
# pipeline tarball, npm publish, and all satellites share one number.
if [[ "$VERSION" != "$PKG_VERSION" ]]; then
  say "Bumping repo-root package.json $PKG_VERSION -> $VERSION"
  node -e "
    const fs=require('fs');
    for (const p of ['$REPO_ROOT/package.json','$REPO_ROOT/package-lock.json']) {
      if (!fs.existsSync(p)) continue;
      const j=JSON.parse(fs.readFileSync(p,'utf8'));
      j.version='$VERSION';
      if (j.packages && j.packages['']) j.packages[''].version='$VERSION';
      fs.writeFileSync(p, JSON.stringify(j,null,2)+'\n');
      console.log('  '+p.replace('$REPO_ROOT/','')+' -> '+j.version);
    }
  "
else
  say "Releasing $TAG (repo-root package.json already $VERSION)"
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

# ---- 3b. Stamp manifest.json + mcp/package.json + lockfile version ----------
# Claude Desktop's extension "Details" panel reads version from manifest.json,
# so it must track the release version too (not the frozen 0.0.1 placeholder).
# mcp/package.json and its lockfile are stamped in lockstep so the three stay
# consistent (npm errors if package.json and package-lock.json disagree).
say "Stamping mcp/manifest.json + mcp/package.json + mcp/package-lock.json -> $VERSION"
node -e "
const fs=require('fs');
const V='$VERSION';
for (const p of ['$MCP_DIR/manifest.json','$MCP_DIR/package.json']) {
  const j=JSON.parse(fs.readFileSync(p,'utf8'));
  j.version=V;
  fs.writeFileSync(p, JSON.stringify(j,null,2)+'\n');
  console.log('  '+p.replace('$MCP_DIR/','mcp/')+' -> '+j.version);
}
const lp='$MCP_DIR/package-lock.json';
if (fs.existsSync(lp)) {
  const l=JSON.parse(fs.readFileSync(lp,'utf8'));
  l.version=V;
  if (l.packages && l.packages['']) l.packages[''].version=V;
  fs.writeFileSync(lp, JSON.stringify(l,null,2)+'\n');
  console.log('  mcp/package-lock.json -> '+l.version);
}
"

# ---- 3c. Regenerate manifest.json `tools` from the SERVER's registrations ---
# Claude Desktop exposes a .mcpb extension's tools to agent chats from the
# manifest's `tools` array. It was hand-written and drifted: it listed 5 old
# tools while the server registers ~10 (queue_setup, run_draft_cycle, …), so the
# newer ones were INVISIBLE to the in-chat agent — which silently broke onboarding
# / re-arm on every .mcpb install (the agent couldn't call queue_setup). Derive
# the list from the source's tool()/appTool() registrations so it can never drift
# again. (name + title; the title is the human description Desktop shows.)
say "Regenerating mcp/manifest.json tools from src/index.ts registrations"
node -e "
const fs=require('fs');
const src=fs.readFileSync('$MCP_DIR/src/index.ts','utf8');
const re=/(?:^|\n)\s*(?:tool|appTool)\(\s*\n\s*\"([a-z0-9_]+)\"\s*,\s*\{\s*\n\s*title:\s*\"((?:[^\"\\\\]|\\\\.)*)\"/g;
const tools=[]; let m;
while((m=re.exec(src))!==null) tools.push({name:m[1], description:m[2]});
if(tools.length < 6) { console.error('  refusing: only '+tools.length+' tools parsed (regex drift?)'); process.exit(1); }
const p='$MCP_DIR/manifest.json';
const j=JSON.parse(fs.readFileSync(p,'utf8'));
j.tools=tools;
fs.writeFileSync(p, JSON.stringify(j,null,2)+'\n');
console.log('  manifest tools ('+tools.length+'): '+tools.map(t=>t.name).join(', '));
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

MANIFEST_VER=$(unzip -p "$BUNDLE" manifest.json 2>/dev/null | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).version" 2>/dev/null || echo "?")
[[ "$MANIFEST_VER" == "$VERSION" ]] || die "bundle manifest.json=$MANIFEST_VER != $VERSION (Desktop Details panel would show the wrong version)"
echo "  manifest.json: $MANIFEST_VER ok"

# THE guard that was missing when 1.6.84 shipped a 1.6.83 pipeline: assert the
# version INSIDE the embedded pipeline.tgz matches the bundle. ensurePipelineCurrent()
# on the box trusts version.json to decide whether to re-extract; if the tarball's
# own package.json lags, the box materializes stale Python and never knows.
PIPELINE_VER=$(unzip -p "$BUNDLE" dist/pipeline.tgz 2>/dev/null | tar -xzO package/package.json 2>/dev/null | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).version" 2>/dev/null || echo "?")
[[ "$PIPELINE_VER" == "$VERSION" ]] || die "embedded pipeline.tgz version=$PIPELINE_VER != $VERSION (box would materialize a STALE pipeline; bump repo-root package.json BEFORE build)"
echo "  pipeline.tgz internal version: $PIPELINE_VER ok"

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

# ---- 6. npm publish (Story A: `npx social-autoposter@<v> init`) -------------
# Same VERSION as the bundle, from the SAME bumped repo-root package.json, so the
# npm install path and the .mcpb path can never disagree. Idempotent: a version
# already on the registry is skipped, not failed.
if [[ "$DO_NPM" == "1" ]]; then
  command -v npm >/dev/null || die "npm not found on PATH"
  NPM_HTTP=$(curl -s -o /dev/null -w "%{http_code}" "https://registry.npmjs.org/social-autoposter/$VERSION" || echo "000")
  if [[ "$NPM_HTTP" == "200" ]]; then
    say "npm: social-autoposter@$VERSION already published — skipping"
  else
    say "Publishing social-autoposter@$VERSION to npm"
    ( cd "$REPO_ROOT" && npm publish ) || die "npm publish failed"
    # Confirm it actually landed (granular-token whoami lies; a version fetch doesn't).
    for _ in 1 2 3 4 5; do
      sleep 2
      [[ "$(curl -s -o /dev/null -w '%{http_code}' "https://registry.npmjs.org/social-autoposter/$VERSION")" == "200" ]] && break
    done
    echo "  npm: social-autoposter@$VERSION live"
  fi
else
  say "npm publish skipped (--no-npm)"
fi

# ---- 7. GitHub release ------------------------------------------------------
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

# ---- 8. Verify the update banner will fire ---------------------------------
# The menu-bar "⬆ Update available" banner (mcp/src/version.ts::versionStatus)
# resolves "latest" from GitHub releases/latest, which is what .mcpb boxes (no
# npm) can actually read. A draft release is deliberately excluded by GitHub's
# releases/latest, so it also won't (and shouldn't) trigger the banner — skip
# the check then. For a normal release, poll releases/latest until it serves the
# new tag so we don't declare success while every box stays silent on the old
# version (the 1.6.177-vs-1.6.181 blind-banner bug this guards against).
if [[ -z "$DRAFT_FLAG" ]]; then
  say "Verifying releases/latest serves $TAG (drives the menu-bar update banner)"
  LATEST_SEEN=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    LATEST_SEEN="$(curl -fsSL -m 15 "https://api.github.com/repos/$GH_REPO/releases/latest" \
      | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).tag_name || ''" 2>/dev/null || echo "")"
    [[ "$LATEST_SEEN" == "$TAG" ]] && break
    sleep 6
  done
  if [[ "$LATEST_SEEN" == "$TAG" ]]; then
    echo "  releases/latest -> $LATEST_SEEN — boxes will detect the update within version.ts's 10-min TTL"
  else
    echo "  WARNING: releases/latest still reports '${LATEST_SEEN:-<none>}', not $TAG." >&2
    echo "  The menu-bar update banner will NOT fire until this resolves. If it stays" >&2
    echo "  wrong, the release is likely a draft/prerelease or GitHub hasn't propagated." >&2
  fi
else
  say "Draft release — skipping banner verification (releases/latest excludes drafts by design)"
fi
