#!/bin/bash
# social-autoposter-update.sh — standalone self-updater for SHIPPED client
# installs. Driven by the launchd/systemd job com.m13v.social-autoposter-update
# (daily). The per-cycle guard that also invoked it (run-cycle-update-guard.sh)
# was removed 2026-07-06 when the legacy launchd->guard->singleton chain was
# retired in favor of run-draft-and-publish.sh.
#
# WHAT IT DOES
#   1. Refuses to run on a dev/source checkout (presence of .git). A dev box
#      edits code in place; `npx social-autoposter@latest update` would clobber
#      the working tree. This is the single most important guard here.
#   2. Compares the installed package version to the latest published on npm.
#   3. If behind, runs `npx -y social-autoposter@latest update`, which pulls the
#      latest tarball, copies it over the install, re-runs install.mjs (re-stamps
#      dist/version.json + re-registers the MCP). The running MCP keeps the old
#      code until the client reconnects; the next headless cycle picks it up.
#
# Safe to call frequently: the version check is one `npm view` call; the heavy
# `npx update` only fires when actually behind.
#
# Exit codes: 0 = up to date OR updated OK OR skipped (dev box / offline);
#             non-zero only when the update command itself failed.

set -u

REPO_DIR="${S4L_REPO_DIR:-$HOME/social-autoposter}"
LOG_DIR="$REPO_DIR/skill/logs"
LOG="$LOG_DIR/self-update.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" | tee -a "$LOG" >&2; }

# --- guard 1: never self-update a dev/source checkout -----------------------
if [ -d "$REPO_DIR/.git" ]; then
  log "skip: $REPO_DIR is a git checkout (dev mode); self-update disabled to avoid clobbering the working tree."
  exit 0
fi

# --- resolve installed version ----------------------------------------------
installed=""
if [ -f "$REPO_DIR/mcp/dist/version.json" ]; then
  installed="$(node -e 'try{process.stdout.write(require(process.argv[1]).version||"")}catch(e){}' "$REPO_DIR/mcp/dist/version.json" 2>/dev/null)"
fi
if [ -z "$installed" ] && [ -f "$REPO_DIR/package.json" ]; then
  installed="$(node -e 'try{process.stdout.write(require(process.argv[1]).version||"")}catch(e){}' "$REPO_DIR/package.json" 2>/dev/null)"
fi

# --- resolve latest published version ---------------------------------------
latest="$(npm view social-autoposter version 2>/dev/null | tr -d '[:space:]')"
if [ -z "$latest" ]; then
  log "skip: could not reach npm to check latest version (offline or registry error). installed=${installed:-unknown}"
  exit 0
fi

if [ -n "$installed" ] && [ "$installed" = "$latest" ]; then
  log "up to date: installed=$installed latest=$latest"
  exit 0
fi

log "update available: installed=${installed:-unknown} latest=$latest — running npx social-autoposter@latest update"
if npx -y social-autoposter@latest update >>"$LOG" 2>&1; then
  log "update OK -> $latest (takes effect on next MCP reconnect / next headless cycle)"
  exit 0
else
  rc=$?
  log "update FAILED (exit $rc); staying on installed=${installed:-unknown}"
  exit "$rc"
fi
