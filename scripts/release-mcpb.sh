#!/usr/bin/env bash
# THE single release flow for social-autoposter. One command does EVERYTHING and
# keeps the npm path (Story A) and the .mcpb double-click path (Story B) on one
# version, because both derive from one bumped repo-root package.json.
#
# What it does, end to end:
#   1. Bump the repo-root package.json (the SINGLE source of truth) and lockfile.
#      Default: patch bump. --bump minor|major, or pin with --version / --tag,
#      or --no-bump to re-release the current version as-is.
#   2. Stamp EVERY version satellite from that one source, THEN build, so the
#      embedded pipeline.tgz (an `npm pack` of the repo) captures an all-current
#      mcp/ subtree. Order is load-bearing: satellites (manifest.json,
#      mcp/package.json+lock, dist/version.json) are stamped BEFORE the pack, not
#      after, or the tarball ships a stale mcp/ subtree (the 1.6.181-menu bug).
#      Sub-steps: 2a stamp source satellites -> 2b build panel+server -> 2c stamp
#      dist/version.json -> 2d pack pipeline.tgz.
#   3. (Regenerate manifest.json `tools` from the server's registrations.)
#   4. Pack mcp/ into mcp/social-autoposter.mcpb via the mcpb CLI.
#   5. Verify: size cap, embedded pipeline.tgz present, version.json + manifest +
#      the pipeline.tgz's OWN internal version AND its mcp/ subtree (dist/version.json,
#      mcp/package.json) all == VERSION (guards the 1.6.84 stale-pipeline and the
#      1.6.181 stale-menu bugs), install tools present.
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
# CHANNELS (2026-07-02): a box can opt into pre-release builds by setting its
# channel to `staging` (scripts/s4l_channel.py). Staging releases are GitHub
# PRE-releases with an -rc.N version, so releases/latest and npm `latest` do NOT
# move — only staging boxes pull them (they resolve the newest release from the
# releases LIST endpoint). `--promote <tag>` flips a tested pre-release to stable
# IN PLACE (no rebuild): it clears the prerelease flag + moves npm `latest`, so
# the EXACT tested artifact ships to everyone. Nothing can drift between test and
# ship because there is no repack.
#
# FIXED STAGING DOWNLOAD LINK (2026-07-07, step 7b): stable has a permanent
# "latest" URL (releases/latest/download/social-autoposter.mcpb); staging did
# not, because GitHub's releases/latest hard-excludes prereleases with no
# override. Every --staging run now also mirrors the .mcpb onto a separate,
# non-version-tagged release ("staging-latest") so this URL never changes:
#   https://github.com/m13v/s4l/releases/download/staging-latest/social-autoposter.mcpb
# The tag deliberately isn't semver-shaped, so version.ts/snapshot.py's real
# "newest rc" resolution (which requires a ^\d+\.\d+\.\d+ tag) ignores it —
# it is a convenience download link only, never a candidate update source.
#
# Usage:
#   bash scripts/release-mcpb.sh                 # patch bump, npm + .mcpb + GitHub (STABLE)
#   bash scripts/release-mcpb.sh --bump minor
#   bash scripts/release-mcpb.sh --version 1.7.0 # pin an exact version
#   bash scripts/release-mcpb.sh --no-bump       # re-release current package.json version
#   bash scripts/release-mcpb.sh --no-npm        # skip npm publish (only .mcpb + GitHub)
#   bash scripts/release-mcpb.sh --no-release    # build + pack + verify only (no npm, no GitHub)
#   bash scripts/release-mcpb.sh --draft         # GitHub release as a draft
#   bash scripts/release-mcpb.sh --staging       # PRE-release -rc.N (staging channel only)
#   bash scripts/release-mcpb.sh --promote v1.6.193-rc.2   # BLOCKED for -rc tags: stable must
#                                  # carry clean digits (user rule, 2026-07-06). Commit + push,
#                                  # then cut stable with --version X.Y.Z. ALLOW_RC_PROMOTE=1 forces.

set -euo pipefail

# Homebrew node/gh/mcpb are not on the default Fazm/launchd PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MCP_DIR="$REPO_ROOT/mcp"
BUNDLE="$MCP_DIR/social-autoposter.mcpb"
GH_REPO="m13v/s4l"
SIZE_CAP_MB=180
# Floating alias release for a fixed "always the newest staging build" download
# link, mirroring what releases/latest/download/<asset> already gives stable.
# GitHub's own releases/latest excludes prereleases with no override, so there is
# no built-in equivalent for the rc channel — see step 7b below for why a
# non-semver tag here is safe (never confuses the real update-detection code).
STAGING_ALIAS_TAG="staging-latest"

TAG_OVERRIDE=""
VERSION_OVERRIDE=""
DO_RELEASE=1
DO_NPM=1
DO_BUMP=1
BUMP_LEVEL="patch"
DRAFT_FLAG=""
# CHANNEL (2026-07-02): staging releases are GitHub PRE-releases carrying an
# -rc.N version, so they are invisible to releases/latest (stable boxes) and to
# npm's `latest` dist-tag — only a box on the `staging` channel pulls them. See
# scripts/s4l_channel.py. `--promote <tag>` flips a tested pre-release to stable
# IN PLACE (no rebuild, the exact tested artifact) by clearing its prerelease
# flag + moving npm `latest`.
DO_STAGING=0
PROMOTE_TAG=""

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
    --staging) DO_STAGING=1; shift ;;
    --promote) PROMOTE_TAG="$2"; shift 2 ;;
    --promote=*) PROMOTE_TAG="${1#*=}"; shift ;;
    -h|--help) sed -n '2,46p' "$0"; exit 0 ;;
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

# ---- 0. Promote a tested pre-release to stable (no rebuild) ------------------
# Flip the SAME artifact the staging box tested: clear GitHub's prerelease flag
# and mark it latest (so releases/latest + the stable boxes pick it up), and move
# npm's `latest` dist-tag onto it. Byte-for-byte identical to what was tested;
# there is no repack, so nothing can drift between test and ship.
#
# USER RULE (2026-07-06, after the SECOND in-place rc promote; the first left
# v1.6.197-rc.16 flagged stable on 2026-07-03): the stable channel must show
# clean version digits, never an -rc.N label. So promoting an -rc tag in place
# is BLOCKED by default. Ship the same code with a clean number instead:
# commit + push (the pack ships the working tree), then
# `bash scripts/release-mcpb.sh --version X.Y.Z`. Set ALLOW_RC_PROMOTE=1 only
# for an emergency where a rebuild is riskier than the label.
if [[ -n "$PROMOTE_TAG" ]]; then
  command -v gh >/dev/null || die "gh CLI not found"
  gh auth status >/dev/null 2>&1 || die "gh not authenticated (run: gh auth login)"
  PTAG="$PROMOTE_TAG"; [[ "$PTAG" == v* ]] || PTAG="v$PTAG"
  PVER="${PTAG#v}"
  if [[ "$PVER" == *-rc* && "${ALLOW_RC_PROMOTE:-0}" != "1" ]]; then
    CLEAN_VER="${PVER%%-rc*}"
    die "stable releases carry clean digits (user rule, 2026-07-06); refusing to promote $PTAG in place.
  Ship the same code under a clean number instead:
    1. commit + push the repo (the pipeline pack ships the working tree)
    2. bash scripts/release-mcpb.sh --version $CLEAN_VER   (pick the next patch if $CLEAN_VER is already published)
  Emergency override (ships the -rc label to every stable box): ALLOW_RC_PROMOTE=1 bash scripts/release-mcpb.sh --promote $PTAG"
  fi
  gh release view "$PTAG" -R "$GH_REPO" >/dev/null 2>&1 || die "no release $PTAG to promote"
  say "Promoting $PTAG to stable (in place; same tested artifact, no rebuild)"
  gh release edit "$PTAG" -R "$GH_REPO" --prerelease=false --latest
  if [[ "$DO_NPM" == "1" ]]; then
    command -v npm >/dev/null || die "npm not found on PATH"
    say "Moving npm 'latest' dist-tag -> $PVER"
    npm dist-tag add "social-autoposter@$PVER" latest || die "npm dist-tag add failed"
    # Keep the dual-published "s4l" package's dist-tag in lockstep. Best-effort:
    # releases cut before the 2026-07-03 dual-publish have no s4l@$PVER, so a
    # miss here warns instead of failing the promote.
    npm dist-tag add "@m13v/s4l@$PVER" latest \
      || echo "  WARNING: npm dist-tag add @m13v/s4l@$PVER latest failed (version may predate the dual-publish)" >&2
  fi
  say "Verifying releases/latest serves $PTAG (drives stable boxes' update banner)"
  LATEST_SEEN=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    LATEST_SEEN="$(curl -fsSL -m 15 "https://api.github.com/repos/$GH_REPO/releases/latest" \
      | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).tag_name || ''" 2>/dev/null || echo "")"
    [[ "$LATEST_SEEN" == "$PTAG" ]] && break
    sleep 6
  done
  [[ "$LATEST_SEEN" == "$PTAG" ]] \
    && echo "  releases/latest -> $LATEST_SEEN; stable boxes detect within ~1 min" \
    || echo "  WARNING: releases/latest still reports '${LATEST_SEEN:-<none>}', not $PTAG (GitHub may still be propagating)." >&2
  say "Promoted $PTAG"
  exit 0
fi

command -v mcpb >/dev/null || die "mcpb CLI not found (npm i -g @anthropic-ai/mcpb)"

# ---- 0b. Regression gate: no silent-fallback / bare-python3 reintroductions --
# scripts/test_no_silent_fallbacks.py locks in two recurring bug classes that
# have separately shipped to customers more than once (X handle -> hardcoded
# "m13v_" impersonation, DEFAULT_ACCOUNTS, and bare "python3" subprocess spawns
# of Playwright-dependent scripts silently dying on a fresh install with no
# Playwright on PATH -- see bug_twitter_reply_bare_python3_no_playwright.md
# and bug_twitter_handle_scrape_brittle_m13v_fallback.md). Runs BEFORE any
# build/pack/publish work so a regression blocks the release, not a customer.
# Applies to every path that actually builds a new artifact (stable + both
# --staging and default runs); --promote ships an already-tested artifact
# byte-for-byte with no rebuild, so it's exempt (handled in step 0 above,
# which exits before reaching here) -- that artifact already passed this gate
# when it was first built.
say "Regression gate: scripts/test_no_silent_fallbacks.py"
python3 "$REPO_ROOT/scripts/test_no_silent_fallbacks.py" \
  || die "regression gate failed -- a silent-fallback / bare-python3 issue was (re)introduced. See output above; do not release until it's clean or the finding is reviewed into the test's ALLOWLIST with a reason."

# Cross-language lockstep gate: candidate_state (s4l_state.py vs index.ts) and
# the short-link fallback host constant (dm_short_links.py vs bin/server.js)
# are duplicated across runtimes BY NECESSITY; this test is what turns their
# "keep in lockstep" comments into an enforced invariant. Divergence here has
# shipped real incidents (post_failed drain asymmetry 2026-07-17).
say "Regression gate: scripts/test_cross_language_parity.py"
python3 "$REPO_ROOT/scripts/test_cross_language_parity.py" \
  || die "cross-language parity gate failed -- a TS/Python lockstep pair diverged (candidate_state or fallback host). Fix BOTH sides before releasing."

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
elif [[ "$DO_STAGING" == "1" ]]; then
  # Staging = the next -rc.N. If package.json already carries an -rc.N for an
  # unreleased patch, bump the rc; otherwise start rc.1 on the next patch of the
  # current full release. The -rc.N rides through EVERY satellite (manifest,
  # pipeline.tgz, dist/version.json) so a staging box's installed version string
  # distinguishes rc.1 from rc.2 (the rc-aware compare in version.ts/snapshot.py
  # depends on that).
  VERSION="$(node -e "
    const v='$PKG_VERSION';
    const m=v.match(/^(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?/);
    let [_,a,b,c,rc]=m.map((x)=>x);
    a=+a;b=+b;c=+c;
    if(rc!==undefined){ rc=+rc+1; } else { c=c+1; rc=1; }
    console.log(a+'.'+b+'.'+c+'-rc.'+rc);
  ")"
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

# Staging publishes as a GitHub pre-release + npm `next` dist-tag, so neither
# releases/latest nor npm `latest` moves — only staging-channel boxes see it.
GH_PRERELEASE_FLAG=""
NPM_TAG_ARGS=()
if [[ "$DO_STAGING" == "1" ]]; then
  # Symmetric guard: --staging with a stable-shaped version would publish a
  # "pre-release" that staging boxes rank ABOVE the real stable line while npm
  # `next` points at a non-rc build. Always an operator mistake; refuse.
  if [[ "$VERSION" != *-* ]]; then
    die "--staging requires an -rc.N version but resolved $VERSION (stable-shaped). Drop --staging, or pin one with --version ${VERSION}-rc.1"
  fi
  GH_PRERELEASE_FLAG="--prerelease"
  NPM_TAG_ARGS=(--tag next)
  say "STAGING pre-release $TAG (invisible to stable boxes; promote later with --promote $TAG)"
fi

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

# ---- 2. Stamp EVERY version satellite BEFORE packing, then build ------------
# ONE source of truth (repo-root package.json, bumped in step 1); every other
# file that carries a version is a SATELLITE stamped from it here. The ordering
# is load-bearing: the embedded pipeline.tgz is `npm pack` of the repo root
# (bundle-pipeline.mjs), so it captures mcp/package.json AND mcp/dist/version.json
# AS THEY ARE ON DISK at pack time. If a satellite is stamped AFTER the pack, the
# tarball ships a stale mcp/ subtree while its top-level package.json is current.
# The menu bar resolves its version from mcp/dist/version.json FIRST
# (scripts/snapshot.py::_resolve_version), so a late stamp shows the OLD version
# in the menu bar even though the install is current (the 1.6.181-menu-on-a-
# 1.6.182-box bug, 2026-07-01). So: stamp source-tree satellites (2a) -> build
# panel + server (2b) -> stamp dist/version.json now that tsc emitted dist/ (2c)
# -> pack pipeline.tgz, now capturing an all-$VERSION mcp/ subtree (2d).

# ---- 2a. Stamp manifest.json + mcp/package.json + lockfile (PRE-pack) --------
# manifest.json feeds Claude Desktop's extension "Details" panel; mcp/package.json
# + its lockfile are stamped in lockstep (npm errors if they disagree). All three
# are inside the repo `files` allowlist, so they land in pipeline.tgz — they MUST
# be current before 2d packs them.
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

# ---- 2b. Build panel + server (emits dist/) ---------------------------------
# Split out of `build:bundle` so we can stamp dist/version.json (2c) AFTER tsc
# emits dist/ but BEFORE the pipeline pack (2d). tsc does not touch dist/version.json
# (it is JSON we author, not a TS output), so a 2c stamp survives to the pack.
say "Building MCP panel + server"
( cd "$MCP_DIR" && npm run build:panel && npm run build:server )

# ---- 2c. Stamp mcp/dist/version.json (after tsc, before the pipeline pack) ---
say "Stamping mcp/dist/version.json -> $VERSION"
node -e "
const fs=require('fs'),p='$MCP_DIR/dist/version.json';
fs.writeFileSync(p, JSON.stringify({version:'$VERSION',installedAt:new Date().toISOString()},null,2)+'\n');
console.log('  '+fs.readFileSync(p,'utf8').trim());
"

# ---- 2d. Pack embedded pipeline.tgz (now captures an all-$VERSION mcp/) ------
say "Packing embedded pipeline.tgz"
( cd "$MCP_DIR" && npm run bundle:pipeline )

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

# ---- 3d. Vendor an owned Node runtime (bare .mcpb installs) ------------------
# manifest.json's mcp_config.command used to be the bare string "node", resolved
# by whatever spawns the extension. Claude Desktop's own chat bridge covers this
# with an internal "built-in Node" shortcut (an Electron-hosted UtilityProcess),
# but a DIFFERENT Desktop session type (agent-mode/Cowork, which spawns a real
# `claude` CLI subprocess with --mcp-config) does a literal subprocess spawn of
# "node" resolved via system PATH instead. On a box with no system Node/Homebrew
# (true of every MacStadium QA box we've checked), that spawn fails with ENOENT
# and that session type sees zero S4L tools — even though Desktop's own bridge
# reports "connected" for the SAME extension. Confirmed 2026-07-15: installing a
# real Node on a broken box fixed it immediately, no other change. Root cause is
# Desktop's own internal built-in-node eligibility, which is undocumented and
# apparently shifted between app versions without any change on our side (the
# manifest's "node" declaration is unchanged since the very first commit). Owning
# our Node the same way we already own Python (uv) and Chromium removes the
# dependency on Desktop's internal behavior entirely: manifest.json's command now
# points at this vendored binary's absolute path, so every session type — main
# chat, agent-mode, any future session type — spawns the SAME real binary.
# macOS arm64 only for now (matches every deployed box); revisit if x64 support
# is ever needed.
NODE_RUNTIME_VERSION="v24.18.0"
NODE_RUNTIME_PLATFORM="darwin-arm64"
NODE_VENDOR_DIR="$MCP_DIR/vendor/node-$NODE_RUNTIME_PLATFORM"
NODE_VENDOR_BIN="$NODE_VENDOR_DIR/bin/node"
NODE_CACHE_DIR="$REPO_ROOT/.cache/node-runtime"
# Can THIS build host execute the vendored target binary? The .mcpb always
# vendors darwin-arm64 (that is where it runs), but the release itself is cut
# from CI on linux-x64. Executing a darwin-arm64 node on a linux-x64 runner is
# an `Exec format error` (exit 126) -- what broke every rc from 1.7.6-rc.11 on.
# Run the exec-based `--version` sanity checks ONLY when the host os/arch matches
# the vendored target; otherwise verify by presence + executable bit, which is
# all a cross-build can honestly assert.
case "$(uname -s)" in
  Darwin) HOST_OS="darwin" ;;
  Linux)  HOST_OS="linux" ;;
  *)      HOST_OS="unknown" ;;
esac
case "$(uname -m)" in
  arm64|aarch64) HOST_ARCH="arm64" ;;
  x86_64|amd64)  HOST_ARCH="x64" ;;
  *)             HOST_ARCH="unknown" ;;
esac
if [[ "$HOST_OS-$HOST_ARCH" == "$NODE_RUNTIME_PLATFORM" ]]; then
  HOST_CAN_EXEC_VENDORED_NODE=1
else
  HOST_CAN_EXEC_VENDORED_NODE=0
fi
say "Vendoring Node $NODE_RUNTIME_VERSION ($NODE_RUNTIME_PLATFORM) for the .mcpb"
if [[ ! -x "$NODE_VENDOR_BIN" ]]; then
  NODE_TARBALL="node-$NODE_RUNTIME_VERSION-$NODE_RUNTIME_PLATFORM.tar.gz"
  NODE_CACHED_TARBALL="$NODE_CACHE_DIR/$NODE_TARBALL"
  mkdir -p "$NODE_CACHE_DIR"
  if [[ ! -f "$NODE_CACHED_TARBALL" ]]; then
    say "  downloading $NODE_TARBALL (not cached)"
    curl -sL "https://nodejs.org/dist/$NODE_RUNTIME_VERSION/$NODE_TARBALL" -o "$NODE_CACHED_TARBALL.tmp" \
      || die "Node download failed"
    mv "$NODE_CACHED_TARBALL.tmp" "$NODE_CACHED_TARBALL"
  else
    say "  using cached $NODE_CACHED_TARBALL"
  fi
  rm -rf "$NODE_VENDOR_DIR"
  mkdir -p "$NODE_VENDOR_DIR/bin"
  # Extract ONLY the node binary itself (no npm/npx/corepack — the server is
  # invoked directly as `node dist/index.js`, nothing here ever needs npm).
  tar -xzf "$NODE_CACHED_TARBALL" -O "node-$NODE_RUNTIME_VERSION-$NODE_RUNTIME_PLATFORM/bin/node" > "$NODE_VENDOR_BIN" \
    || die "failed to extract node binary from $NODE_CACHED_TARBALL"
  chmod +x "$NODE_VENDOR_BIN"
fi
[[ -x "$NODE_VENDOR_BIN" ]] || die "vendored node binary missing/not executable at $NODE_VENDOR_BIN"
if [[ "$HOST_CAN_EXEC_VENDORED_NODE" == "1" ]]; then
  VENDORED_NODE_VER="$("$NODE_VENDOR_BIN" --version)"
  [[ "$VENDORED_NODE_VER" == "$NODE_RUNTIME_VERSION" ]] || die "vendored node reports $VENDORED_NODE_VER, expected $NODE_RUNTIME_VERSION"
  echo "  vendored node: $VENDORED_NODE_VER at mcp/vendor/node-$NODE_RUNTIME_PLATFORM/bin/node ($(du -h "$NODE_VENDOR_BIN" | cut -f1))"
else
  echo "  vendored node: present at mcp/vendor/node-$NODE_RUNTIME_PLATFORM/bin/node ($(du -h "$NODE_VENDOR_BIN" | cut -f1)); --version self-check skipped (host $HOST_OS-$HOST_ARCH cannot exec $NODE_RUNTIME_PLATFORM)"
fi

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

# The menu bar reads package/mcp/dist/version.json FIRST, then package/mcp/package.json
# (scripts/snapshot.py::_resolve_version), both from the SAME pipeline.tgz the box
# extracts into S4L_REPO_DIR. Assert the whole mcp/ subtree matches so a satellite
# stamped after the pack can't ship a menu that shows the wrong version on an
# otherwise-current box (the 1.6.181-menu-on-a-1.6.182-box bug). Empty/missing =
# fail: _resolve_version would fall through and could read a stale file.
for sub in mcp/dist/version.json mcp/package.json; do
  SUBV=$(unzip -p "$BUNDLE" dist/pipeline.tgz 2>/dev/null | tar -xzO "package/$sub" 2>/dev/null | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).version" 2>/dev/null || echo "?")
  [[ "$SUBV" == "$VERSION" ]] || die "embedded pipeline.tgz package/$sub=$SUBV != $VERSION (menu bar would show the wrong version; stamp satellites BEFORE the pipeline pack)"
  echo "  pipeline.tgz $sub: $SUBV ok"
done

for f in "dist/index.js" "dist/runtime.js" "manifest.json" "vendor/node-$NODE_RUNTIME_PLATFORM/bin/node"; do
  # grep -c reads all input (no SIGPIPE); anchor on the time column + 3-space
  # gutter so node_modules/.../dist/index.js does not false-match the top-level.
  n=$(printf '%s\n' "$LISTING" | grep -c "[0-9:]   $f\$" || true)
  [[ "$n" -ge 1 ]] || die "bundle missing $f"
done
echo "  runtime + server + manifest + vendored node: ok"

# The vendored binary must survive the zip round-trip with its executable bit
# intact (unzip -x extracts to a temp file and runs --version; a permissions
# regression here would silently produce a Node that Desktop can spawn but that
# refuses to execute, or vice versa).
VENDOR_CHECK_DIR="$(mktemp -d)"
unzip -q -o "$BUNDLE" "vendor/node-$NODE_RUNTIME_PLATFORM/bin/node" -d "$VENDOR_CHECK_DIR"
VENDOR_CHECK_BIN="$VENDOR_CHECK_DIR/vendor/node-$NODE_RUNTIME_PLATFORM/bin/node"
[[ -x "$VENDOR_CHECK_BIN" ]] || die "vendored node in bundle lost its executable bit"
if [[ "$HOST_CAN_EXEC_VENDORED_NODE" == "1" ]]; then
  VENDOR_CHECK_VER="$("$VENDOR_CHECK_BIN" --version)"
  [[ "$VENDOR_CHECK_VER" == "$NODE_RUNTIME_VERSION" ]] || die "vendored node in bundle reports $VENDOR_CHECK_VER, expected $NODE_RUNTIME_VERSION"
  rm -rf "$VENDOR_CHECK_DIR"
  echo "  vendored node runs from bundle: $VENDOR_CHECK_VER ok"
else
  rm -rf "$VENDOR_CHECK_DIR"
  echo "  vendored node survives bundle round-trip with exec bit intact; --version run skipped (host $HOST_OS-$HOST_ARCH cannot exec $NODE_RUNTIME_PLATFORM)"
fi

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
    say "Publishing social-autoposter@$VERSION to npm${NPM_TAG_ARGS:+ (tag: ${NPM_TAG_ARGS[*]})}"
    # Guarded array expansion: an EMPTY array under `set -u` trips "unbound
    # variable" on macOS bash 3.2, so expand to nothing when no --tag is set.
    ( cd "$REPO_ROOT" && npm publish ${NPM_TAG_ARGS[@]+"${NPM_TAG_ARGS[@]}"} ) || die "npm publish failed"
    # Confirm it actually landed (granular-token whoami lies; a version fetch doesn't).
    for _ in 1 2 3 4 5; do
      sleep 2
      [[ "$(curl -s -o /dev/null -w '%{http_code}' "https://registry.npmjs.org/social-autoposter/$VERSION")" == "200" ]] && break
    done
    echo "  npm: social-autoposter@$VERSION live"
  fi

  # ---- 6b. Dual-publish the SAME content as "@m13v/s4l" (brand rename) -------
  # `npx @m13v/s4l init` == `npx social-autoposter init`. Same version, same
  # dist-tag logic (stable -> default `latest`; --staging -> `next`).
  # npm REJECTS the bare name "s4l" (403: too similar to st/swr/sax/...), so the
  # alias lives under the m13v scope, published --access=public.
  # The repo package.json is NEVER mutated: we `npm pack` the repo root (the
  # exact content step 6 shipped), extract into a temp dir, rewrite ONLY the
  # temp copy's name field, and publish that copy. Idempotent like step 6.
  # BEST-EFFORT BY DESIGN: every failure in 6b warns and falls through — it must
  # NEVER die. On 2026-07-03 a die here aborted the run between the npm publish
  # (step 6, done) and the GitHub release (step 7, never ran), leaving npm `next`
  # on rc.6 with no matching GH release: exactly the diverged-lanes state this
  # script exists to prevent. The alias package is a convenience; the
  # social-autoposter npm lane + GH release lockstep is the contract.
  S4L_ALIAS_PKG="@m13v/s4l"
  S4L_ALIAS_URL="https://registry.npmjs.org/@m13v%2fs4l/$VERSION"
  S4L_NPM_HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$S4L_ALIAS_URL" || echo "000")
  if [[ "$S4L_NPM_HTTP" == "200" ]]; then
    say "npm: $S4L_ALIAS_PKG@$VERSION already published — skipping dual-publish"
  else
    say "Dual-publishing $S4L_ALIAS_PKG@$VERSION to npm${NPM_TAG_ARGS:+ (tag: ${NPM_TAG_ARGS[*]})}"
    S4L_PUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/s4l-dual-publish.XXXXXX")"
    if ( cd "$REPO_ROOT" && npm pack --pack-destination "$S4L_PUB_DIR" >/dev/null ) \
       && S4L_TGZ="$(ls "$S4L_PUB_DIR"/social-autoposter-*.tgz 2>/dev/null | head -1)" \
       && [[ -n "$S4L_TGZ" ]] \
       && tar -xzf "$S4L_TGZ" -C "$S4L_PUB_DIR" \
       && node -e "
         const fs=require('fs');
         const p='$S4L_PUB_DIR/package/package.json';
         const j=JSON.parse(fs.readFileSync(p,'utf8'));
         if (j.version!=='$VERSION') { console.error('  temp copy version '+j.version+' != $VERSION'); process.exit(1); }
         j.name='$S4L_ALIAS_PKG';
         fs.writeFileSync(p, JSON.stringify(j,null,2)+'\n');
         console.log('  temp copy renamed to $S4L_ALIAS_PKG@'+j.version+' (repo package.json untouched)');
       " \
       && (
            # The registry's large-upload path flakes (TLS bad record mac / EPIPE)
            # often enough that one attempt let the alias lane silently drift for
            # three rc's (rc.16/21/22, caught 2026-07-10). Retry, and between
            # attempts check whether the upload actually landed despite the
            # client-side error (EPIPE can be a false negative).
            cd "$S4L_PUB_DIR/package" || exit 1
            for _try in 1 2 3; do
              npm publish --access=public ${NPM_TAG_ARGS[@]+"${NPM_TAG_ARGS[@]}"} && exit 0
              sleep 3
              [[ "$(curl -s -o /dev/null -w '%{http_code}' "$S4L_ALIAS_URL")" == "200" ]] && exit 0
              echo "  dual-publish attempt $_try/3 failed" >&2
            done
            exit 1
          ); then
      for _ in 1 2 3 4 5; do
        sleep 2
        [[ "$(curl -s -o /dev/null -w '%{http_code}' "$S4L_ALIAS_URL")" == "200" ]] && break
      done
      echo "  npm: $S4L_ALIAS_PKG@$VERSION live"
    else
      echo "  WARNING: $S4L_ALIAS_PKG dual-publish failed (alias lane only; social-autoposter + GH release proceed)" >&2
    fi
    rm -rf "$S4L_PUB_DIR"
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
  # HARD GUARD (2026-07-03): never CREATE a release for a prerelease-suffixed
  # version through the stable path. Without --prerelease, gh marks it latest,
  # releases/latest moves onto the rc, and every STABLE box updates to a
  # prerelease. CI (release-mcpb.yml) used to run this script on any v* tag
  # push with no --staging — an rc tag push was one npm-auth accident away from
  # exactly that. The clobber branch above is deliberately unguarded: uploading
  # assets onto an EXISTING (correctly-flagged) release is always safe.
  if [[ "$VERSION" == *-* && "$DO_STAGING" != "1" ]]; then
    die "refusing to CREATE a stable release for prerelease version $VERSION. Use --staging (new rc), --promote <tag> (ship a tested rc), or create the release locally first so this run only uploads assets."
  fi
  say "Creating release $TAG${GH_PRERELEASE_FLAG:+ (pre-release)}"
  gh release create "$TAG" "$BUNDLE" \
    -R "$GH_REPO" \
    --title "social-autoposter $TAG" \
    --notes "$NOTES" \
    $DRAFT_FLAG $GH_PRERELEASE_FLAG
fi

URL=$(gh release view "$TAG" -R "$GH_REPO" --json url -q .url 2>/dev/null || echo "")
say "Released $TAG"
echo "  asset: social-autoposter.mcpb (${MB}MB)"
[[ -n "$URL" ]] && echo "  $URL"

# ---- 7b. Fixed "always latest staging" download link ------------------------
# Mirrors the asset just uploaded above onto a SEPARATE release tagged
# "$STAGING_ALIAS_TAG" (not a version), so the download URL never changes:
#   https://github.com/$GH_REPO/releases/download/$STAGING_ALIAS_TAG/social-autoposter.mcpb
# Safe by construction: latestFromGithubListStaging() (mcp/src/version.ts) and
# _latest_from_github_list_staging() (scripts/snapshot.py) both require
# tag_name to match ^\d+\.\d+\.\d+ before treating a release as a candidate
# "newest" build; a tag literally named "staging-latest" fails that check and
# is silently skipped by both, so this alias can never get mistaken for a real
# rc and can never confuse which build a box installs. It exists ONLY to give
# humans (and scripts) one fixed bookmark — actual boxes still resolve "newest"
# from the real per-release rc tags via the releases LIST endpoint, unchanged.
if [[ "$DO_STAGING" == "1" ]]; then
  say "Mirroring asset onto the fixed staging-latest alias"
  ALIAS_NOTES="Always mirrors the newest staging pre-release's .mcpb asset (currently $TAG).

Fixed download link (never changes): https://github.com/$GH_REPO/releases/download/$STAGING_ALIAS_TAG/social-autoposter.mcpb

Not a real release — do not install from the tag page; use the asset link above. Actual per-build releases are tagged by version (e.g. $TAG)."
  if gh release view "$STAGING_ALIAS_TAG" -R "$GH_REPO" >/dev/null 2>&1; then
    gh release upload "$STAGING_ALIAS_TAG" "$BUNDLE" -R "$GH_REPO" --clobber
    gh release edit "$STAGING_ALIAS_TAG" -R "$GH_REPO" --notes "$ALIAS_NOTES" >/dev/null
  else
    gh release create "$STAGING_ALIAS_TAG" "$BUNDLE" \
      -R "$GH_REPO" \
      --title "social-autoposter (staging — always latest)" \
      --notes "$ALIAS_NOTES" \
      --prerelease
  fi
  echo "  https://github.com/$GH_REPO/releases/download/$STAGING_ALIAS_TAG/social-autoposter.mcpb"
fi

# ---- 8. Verify the update banner will fire ---------------------------------
# The menu-bar "⬆ Update available" banner (mcp/src/version.ts::versionStatus)
# resolves "latest" from GitHub releases/latest, which is what .mcpb boxes (no
# npm) can actually read. A draft release is deliberately excluded by GitHub's
# releases/latest, so it also won't (and shouldn't) trigger the banner — skip
# the check then. For a normal release, poll releases/latest until it serves the
# new tag so we don't declare success while every box stays silent on the old
# version (the 1.6.177-vs-1.6.181 blind-banner bug this guards against).
if [[ -n "$DRAFT_FLAG" ]]; then
  say "Draft release — skipping banner verification (releases/latest excludes drafts by design)"
elif [[ "$DO_STAGING" == "1" ]]; then
  say "Staging pre-release — releases/latest deliberately EXCLUDES it, so stable boxes stay put."
  echo "  Only boxes on the staging channel pull $TAG (via the releases LIST endpoint)."
  echo "  To ship it to everyone once tested (stable = clean digits, never -rc): commit + push, then"
  echo "    bash scripts/release-mcpb.sh --version ${VERSION%%-rc*}   # pick the next patch if that version is already published"
else
  say "Verifying releases/latest serves $TAG (drives the menu-bar update banner)"
  LATEST_SEEN=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    LATEST_SEEN="$(curl -fsSL -m 15 "https://api.github.com/repos/$GH_REPO/releases/latest" \
      | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).tag_name || ''" 2>/dev/null || echo "")"
    [[ "$LATEST_SEEN" == "$TAG" ]] && break
    sleep 6
  done
  if [[ "$LATEST_SEEN" == "$TAG" ]]; then
    echo "  releases/latest -> $LATEST_SEEN; boxes detect within version.ts's ~1-min TTL (55s, ETag-conditional; boxes older than 1.6.188 poll every 10 min)"
  else
    echo "  WARNING: releases/latest still reports '${LATEST_SEEN:-<none>}', not $TAG." >&2
    echo "  The menu-bar update banner will NOT fire until this resolves. If it stays" >&2
    echo "  wrong, the release is likely a draft/prerelease or GitHub hasn't propagated." >&2
  fi
fi
