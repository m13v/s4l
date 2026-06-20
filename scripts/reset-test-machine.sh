#!/usr/bin/env bash
# reset-test-machine.sh — wipe a social-autoposter install back to factory-fresh.
#
# For TEST MACHINES. Removes everything an install scatters so you can re-run a
# clean first-install and reproduce new-user bugs:
#   - MCP state dir (owned uv-python venv + materialized repo + runtime.json + setup-state)
#   - the global npm library
#   - the MCP registration (claude mcp + Claude Desktop config)
#   - packaged Chrome profiles + imported cookies (browser-harness / reddit / linkedin)
#   - the browser-harness backend (clone + uv-tool CLI + server.py)
#   - (with --deep) the shared toolchain we package: uv binary, uv cache, packaged Chromium
#
# DEFAULT IS A DRY RUN. Nothing is deleted until you pass --yes.
#
# Usage:
#   scripts/reset-test-machine.sh            # dry run: print what WOULD be removed
#   scripts/reset-test-machine.sh --yes      # remove owned state (safe set)
#   scripts/reset-test-machine.sh --yes --deep   # also nuke uv + uv cache + ms-playwright (1.7G)
#
set -u

HOME_DIR="${HOME}"
DRY=1
DEEP=0
for a in "$@"; do
  case "$a" in
    --yes|-y) DRY=0 ;;
    --deep)   DEEP=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '/^!/d'
      exit 0 ;;
    *) echo "unknown arg: $a (use --yes, --deep, --help)"; exit 2 ;;
  esac
done

if [ "$DRY" -eq 1 ]; then
  echo "=== DRY RUN — nothing will be deleted. Re-run with --yes to apply. ==="
else
  echo "=== APPLYING — removing social-autoposter install ==="
fi
echo

# ---- helpers ---------------------------------------------------------------
rm_path() {  # rm_path <path> <label>
  local p="$1" label="${2:-}"
  if [ -e "$p" ] || [ -L "$p" ]; then
    local size; size="$(du -sh "$p" 2>/dev/null | cut -f1)"
    echo "  remove  ${label:+[$label] }$p   (${size:-?})"
    [ "$DRY" -eq 0 ] && rm -rf "$p"
  fi
}
run() {  # run <description> <cmd...>
  echo "  run     $1"
  [ "$DRY" -eq 0 ] && shift && "$@" >/dev/null 2>&1 || true
}

# ---- 0. stop anything still running ---------------------------------------
echo "[0] stop running processes"
if [ "$DRY" -eq 0 ]; then
  pkill -f 'social-autoposter-mcp' 2>/dev/null || true
  pkill -f 'browser-harness'       2>/dev/null || true
  pkill -f 's4l_menubar.py'        2>/dev/null || true
  # packaged Chromium launched from the owned playwright / harness profiles
  pkill -f '\.claude/browser-profiles/browser-harness' 2>/dev/null || true
  sleep 1
else
  echo "  (would pkill: social-autoposter-mcp, browser-harness, menu bar app, packaged Chrome)"
fi
echo

# ---- 0b. menu bar LaunchAgent ---------------------------------------------
# The menu bar app runs as a KeepAlive LaunchAgent; its plist lives outside the
# state dir, so unload + remove it explicitly (its python files under the state
# dir are removed in [1]). KEEP MENUBAR_LABEL in sync with src/runtime.ts.
echo "[0b] menu bar LaunchAgent"
MENUBAR_LABEL="com.m13v.social-autoposter.menubar"
MENUBAR_PLIST="$HOME_DIR/Library/LaunchAgents/$MENUBAR_LABEL.plist"
if [ "$DRY" -eq 0 ]; then
  launchctl bootout "gui/$(id -u)/$MENUBAR_LABEL" 2>/dev/null \
    || launchctl unload "$MENUBAR_PLIST" 2>/dev/null || true
else
  echo "  (would bootout $MENUBAR_LABEL)"
fi
rm_path "$MENUBAR_PLIST" "menubar-plist"
echo

# ---- 1. MCP state dir (owned python + venv + materialized repo) ------------
echo "[1] MCP state dir (uv-owned python, venv, materialized repo, runtime.json, setup-state)"
rm_path "$HOME_DIR/.social-autoposter-mcp" "state"
echo

# ---- 2. global npm library -------------------------------------------------
echo "[2] global npm library"
if command -v npm >/dev/null 2>&1; then
  if npm ls -g social-autoposter >/dev/null 2>&1; then
    echo "  run     npm rm -g social-autoposter"
    [ "$DRY" -eq 0 ] && npm rm -g social-autoposter >/dev/null 2>&1 || true
  else
    echo "  (social-autoposter not installed as a global npm package)"
  fi
else
  echo "  (npm not on PATH — skipping)"
fi
echo

# ---- 3. MCP registration ---------------------------------------------------
echo "[3] MCP registration (claude CLI + config files)"
if command -v claude >/dev/null 2>&1; then
  for name in social-autoposter twitter-harness reddit-agent linkedin-agent; do
    echo "  run     claude mcp remove $name"
    [ "$DRY" -eq 0 ] && claude mcp remove "$name" >/dev/null 2>&1 || true
  done
else
  echo "  (claude CLI not on PATH — scrub config files manually if needed)"
fi
# Surface (but do NOT auto-edit) the config files that may still carry a stanza.
for cfg in \
  "$HOME_DIR/.claude.json" \
  "$HOME_DIR/Library/Application Support/Claude/claude_desktop_config.json"; do
  if [ -f "$cfg" ] && grep -q 'social-autoposter\|twitter-harness' "$cfg" 2>/dev/null; then
    echo "  NOTE    $cfg still references the MCP — review/remove the stanza by hand"
  fi
done
echo

# ---- 4. packaged Chrome profiles + imported cookies ------------------------
echo "[4] packaged Chrome profiles + imported cookies"
PROF="$HOME_DIR/.claude/browser-profiles"
for d in browser-harness browser-harness-linkedin browser-harness-reddit reddit linkedin twitter; do
  rm_path "$PROF/$d" "profile"
done
# cookie mirrors + harness logs/pids
if [ -d "$PROF" ]; then
  for f in "$PROF"/*.x-cookies.json "$PROF"/*.chrome.log "$PROF"/*.chrome.pid \
           "$PROF"/*.mcp.log "$PROF"/browser-activity.log*; do
    [ -e "$f" ] && rm_path "$f" "cookie/log"
  done
fi
echo

# ---- 5. browser-harness backend -------------------------------------------
echo "[5] browser-harness backend (clone + uv-tool CLI + server.py)"
rm_path "$HOME_DIR/Developer/browser-harness"               "harness-clone"
rm_path "$HOME_DIR/.claude/mcp-servers/browser-harness"     "harness-server"
rm_path "$HOME_DIR/.local/bin/browser-harness"              "harness-cli"
if command -v uv >/dev/null 2>&1; then
  echo "  run     uv tool uninstall browser-harness"
  [ "$DRY" -eq 0 ] && uv tool uninstall browser-harness >/dev/null 2>&1 || true
fi
echo

# ---- 6. DEEP: shared toolchain we package (uv + chromium) ------------------
echo "[6] shared toolchain (uv, uv cache, packaged Chromium) — ${DEEP:+}$([ "$DEEP" -eq 1 ] && echo ENABLED || echo 'skipped (pass --deep)')"
if [ "$DEEP" -eq 1 ]; then
  rm_path "$HOME_DIR/Library/Caches/ms-playwright" "chromium"
  rm_path "$HOME_DIR/.local/bin/uv"                "uv-bin"
  rm_path "$HOME_DIR/.local/bin/uvx"               "uvx-bin"
  rm_path "$HOME_DIR/.cache/uv"                    "uv-cache"
  echo "  WARN    --deep removed uv + ms-playwright; other tools on this machine that rely on them will re-provision."
else
  echo "  (left uv + ~/Library/Caches/ms-playwright + ~/.cache/uv in place)"
fi
echo

if [ "$DRY" -eq 1 ]; then
  echo "=== DRY RUN complete. Re-run with --yes to actually remove. ==="
else
  echo "=== Reset complete. Machine is ready for a clean first-install test. ==="
fi
