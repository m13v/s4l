#!/usr/bin/env bash
# reset-test-machine.sh — wipe a social-autoposter install back to factory-fresh.
#
# For TEST MACHINES. Removes everything an install scatters so you can re-run a
# clean first-install and reproduce new-user bugs:
#   - MCP state dir (owned uv-python venv + materialized repo + runtime.json + setup-state)
#   - the Claude Desktop .mcpb extension (install dir + per-extension settings + installations registry entry)
#   - the global npm library
#   - the MCP registration (claude mcp + Claude Desktop config)
#   - packaged Chrome profiles + imported cookies (browser-harness / reddit / linkedin)
#   - the browser-harness backend (clone + uv-tool CLI + server.py)
#   - (with --deep) the shared toolchain we package: uv binary, uv cache, packaged Chromium
#
# DEFAULT IS A DRY RUN. Nothing is deleted until you pass --yes.
#
# Three scopes, widest to narrowest:
#   --deep         full set + the SHARED toolchain (uv binary, uv cache, packaged
#                  Chromium). Other tools on the machine that rely on uv/Chromium
#                  re-provision after this. Use to reproduce a true bare-metal box.
#   (default)      everything social-autoposter owns AND the shared browser layer
#                  it touches: per-agent Chrome profiles + cookies (reddit/linkedin/
#                  twitter) and the browser-harness backend. Leaves the toolchain.
#   --plugin-only  JUST the plugin: menu-bar app, the .mcpb/npm plugin itself, the
#                  scheduled tasks, and the user's state+config (~/.social-autoposter-mcp,
#                  including config.json + mode.json). PRESERVES the imported X login
#                  (browser-harness profile), all shared browser profiles, the
#                  browser-harness backend, and the toolchain. The lightest reset:
#                  uninstall + forget the plugin without disturbing the environment.
#
# Usage:
#   scripts/reset-test-machine.sh                     # dry run: print what WOULD be removed
#   scripts/reset-test-machine.sh --yes               # remove owned state + shared browser layer
#   scripts/reset-test-machine.sh --yes --deep        # also nuke uv + uv cache + ms-playwright (1.7G)
#   scripts/reset-test-machine.sh --yes --plugin-only # JUST the plugin; keep X login + browser layer + toolchain
#
set -u

HOME_DIR="${HOME}"
DRY=1
DEEP=0
PLUGIN_ONLY=0
for a in "$@"; do
  case "$a" in
    --yes|-y)      DRY=0 ;;
    --deep)        DEEP=1 ;;
    --plugin-only) PLUGIN_ONLY=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '/^!/d'
      exit 0 ;;
    *) echo "unknown arg: $a (use --yes, --deep, --plugin-only, --help)"; exit 2 ;;
  esac
done

# --plugin-only is the narrowest scope; it always wins over --deep (which only
# widens by removing the shared toolchain). Surface the conflict instead of
# silently doing something between the two.
if [ "$PLUGIN_ONLY" -eq 1 ] && [ "$DEEP" -eq 1 ]; then
  echo "NOTE: --plugin-only overrides --deep (the shared toolchain is preserved)."
  echo
  DEEP=0
fi

if [ "$DRY" -eq 1 ]; then
  echo "=== DRY RUN — nothing will be deleted. Re-run with --yes to apply. ==="
else
  echo "=== APPLYING — removing social-autoposter install ==="
fi
if [ "$PLUGIN_ONLY" -eq 1 ]; then
  echo "=== SCOPE: --plugin-only (app + plugin + tasks + state/config; keeping X login, browser layer, toolchain) ==="
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
json_edit() {  # json_edit <description> <cmd...>
  echo "  edit    $1"
  [ "$DRY" -eq 0 ] && shift && "$@" || true
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

# ---- 0c. autopilot LaunchAgents (twitter-cycle kicker + daily updater) ------
# The queue-backed draft autopilot kicks the pipeline from a launchd job
# (com.m13v.social-twitter-cycle) and a daily self-updater. Both plists live in
# ~/Library/LaunchAgents, OUTSIDE the state dir removed in [1], so a reset that
# skips them leaves a loaded job that keeps firing cycles against a half-wiped
# install. Bootout + remove them (and any .disabled-* variant left by a manual
# stop). KEEP these labels in sync with src/index.ts.
echo "[0c] autopilot LaunchAgents (twitter-cycle kicker + daily updater)"
for LBL in com.m13v.social-twitter-cycle com.m13v.social-autoposter-update; do
  PL="$HOME_DIR/Library/LaunchAgents/$LBL.plist"
  if [ "$DRY" -eq 0 ]; then
    launchctl bootout "gui/$(id -u)/$LBL" 2>/dev/null \
      || launchctl unload "$PL" 2>/dev/null || true
  else
    echo "  (would bootout $LBL)"
  fi
  rm_path "$PL" "launchagent"
  for d in "$PL".disabled*; do [ -e "$d" ] && rm_path "$d" "launchagent-disabled"; done
done
echo

# ---- 1. MCP state dir (owned python + venv + materialized repo) ------------
echo "[1] MCP state dir (uv-owned python, venv, materialized repo, runtime.json, setup-state)"
rm_path "$HOME_DIR/.social-autoposter-mcp" "state"
echo

# ---- 1b. Claude Desktop .mcpb extension -----------------------------------
# A .mcpb (Desktop) install scatters THREE artifacts under ~/Library/Application
# Support/Claude/, NONE of which live in the state dir removed in [1]: the
# unpacked extension, its per-extension settings, and an entry in the
# installations registry. Remove all three so a reinstall is genuinely first-run.
# Scoped to social-autoposter only — other extensions (lede, s4l-plugin) are left
# untouched.
echo "[1b] Claude Desktop .mcpb extension (install dir + settings + registry)"
CLAUDE_SUPPORT="$HOME_DIR/Library/Application Support/Claude"
INSTALL_REG="$CLAUDE_SUPPORT/extensions-installations.json"
# The registry edit only sticks if Claude Desktop is NOT running (it rewrites
# these files on quit). Warn rather than force-quit — the script may be invoked
# from inside a live Claude session and killing the app would take it down.
if pgrep -x "Claude" >/dev/null 2>&1; then
  echo "  WARN    Claude Desktop is running — quit it (Cmd+Q) so the registry edit"
  echo "          isn't rewritten on exit, then re-run this step if needed."
fi
EXT_IDS=()
for eid in \
  local.mcpb.m13v.social-autoposter \
  local.mcpb.s4l.ai.social-autoposter; do
  EXT_IDS+=("$eid")
done
for d in "$CLAUDE_SUPPORT/Claude Extensions"/local.mcpb.*social-autoposter; do
  [ -e "$d" ] && EXT_IDS+=("$(basename "$d")")
done
for f in "$CLAUDE_SUPPORT/Claude Extensions Settings"/local.mcpb.*social-autoposter.json; do
  [ -e "$f" ] && EXT_IDS+=("$(basename "$f" .json)")
done
if [ -f "$INSTALL_REG" ] && command -v python3 >/dev/null 2>&1; then
  while IFS= read -r eid; do [ -n "$eid" ] && EXT_IDS+=("$eid"); done < <(python3 - "$INSTALL_REG" <<'PY' 2>/dev/null || true
import json, sys
d = json.load(open(sys.argv[1]))
for eid in sorted((d.get("extensions") or {}).keys()):
    if "social-autoposter" in eid:
        print(eid)
PY
  )
fi
if [ "${#EXT_IDS[@]}" -gt 0 ]; then
  EXT_IDS=($(printf '%s\n' "${EXT_IDS[@]}" | awk 'NF && !seen[$0]++'))
fi
for EXT_ID in "${EXT_IDS[@]}"; do
  rm_path "$CLAUDE_SUPPORT/Claude Extensions/$EXT_ID" "ext-install"
  rm_path "$CLAUDE_SUPPORT/Claude Extensions Settings/$EXT_ID.json" "ext-settings"
done
# Surgically drop only OUR entries from the installations registry (keep others).
if [ -f "$INSTALL_REG" ]; then
  if command -v python3 >/dev/null 2>&1; then
    if python3 - "$INSTALL_REG" "${EXT_IDS[@]}" <<'PY' >/dev/null 2>&1; then
import json, sys
p, ids = sys.argv[1], set(sys.argv[2:])
d = json.load(open(p))
sys.exit(0 if ids.intersection((d.get("extensions") or {}).keys()) else 1)
PY
      json_edit "remove social-autoposter extension entries from $INSTALL_REG" \
        sh -c 'cp "$1" "$1.bak" 2>/dev/null || true; shift; python3 - "$@"' sh "$INSTALL_REG" "$INSTALL_REG" "${EXT_IDS[@]}" <<'PY'
import json, sys
p, ids = sys.argv[1], set(sys.argv[2:])
d = json.load(open(p))
for eid in ids:
    (d.get("extensions") or {}).pop(eid, None)
json.dump(d, open(p, "w"), indent=2)
open(p, "a").write("\n")
PY
    else
      echo "  (registry has no social-autoposter extension entry)"
    fi
  else
    echo "  NOTE    python3 not on PATH — remove social-autoposter entries from $INSTALL_REG by hand"
  fi
fi
echo

# ---- 1c. Claude Desktop scheduled tasks -----------------------------------
# Onboarding creates the queue-worker tasks (and historically the deprecated
# single autopilot) under ~/.claude/scheduled-tasks/. They live OUTSIDE the
# state dir, so a reset that skips them leaves idle workers firing every minute
# against a wiped install. Remove the task dirs. Like the .mcpb registry, the
# host (Claude Desktop) owns the live schedule and rewrites it on quit, so this
# only fully sticks when Claude isn't running.
echo "[1c] Claude Desktop scheduled tasks (queue workers + deprecated autopilot)"
SCHED_DIR="$HOME_DIR/.claude/scheduled-tasks"
if pgrep -x "Claude" >/dev/null 2>&1; then
  echo "  WARN    Claude Desktop is running — it owns the task schedule; quit it (Cmd+Q)"
  echo "          before reset so the removed tasks aren't rewritten on exit."
fi
TASK_IDS=()
if [ -d "$SCHED_DIR" ]; then
  for d in "$SCHED_DIR"/saps-* "$SCHED_DIR"/social-autoposter*; do
    [ -e "$d" ] && TASK_IDS+=("$(basename "$d")")
  done
fi
for t in saps-phase1-query saps-phase2b-draft social-autoposter-autopilot; do
  TASK_IDS+=("$t")
done
if [ "${#TASK_IDS[@]}" -gt 0 ]; then
  TASK_IDS=($(printf '%s\n' "${TASK_IDS[@]}" | awk 'NF && !seen[$0]++'))
fi
for t in "${TASK_IDS[@]}"; do
  rm_path "$SCHED_DIR/$t" "scheduled-task"
done
if command -v python3 >/dev/null 2>&1; then
  while IFS= read -r reg; do
    if python3 - "$reg" "$SCHED_DIR" "${TASK_IDS[@]}" <<'PY' >/dev/null 2>&1; then
import json, os, sys
p, sched_dir, ids = sys.argv[1], os.path.realpath(sys.argv[2]), set(sys.argv[3:])
d = json.load(open(p))
tasks = d.get("scheduledTasks") or []
def owned(t):
    tid = str(t.get("id") or "")
    fp = str(t.get("filePath") or "")
    return (
        tid in ids
        or tid.startswith("saps-")
        or tid.startswith("social-autoposter")
        or os.path.realpath(fp).startswith(sched_dir + os.sep + "saps-")
        or os.path.realpath(fp).startswith(sched_dir + os.sep + "social-autoposter")
    )
sys.exit(0 if any(owned(t) for t in tasks) else 1)
PY
      json_edit "remove social-autoposter scheduled-task entries from $reg" \
        sh -c 'cp "$1" "$1.bak" 2>/dev/null || true; shift; python3 - "$@"' sh "$reg" "$reg" "$SCHED_DIR" "${TASK_IDS[@]}" <<'PY'
import json, os, sys
p, sched_dir, ids = sys.argv[1], os.path.realpath(sys.argv[2]), set(sys.argv[3:])
d = json.load(open(p))
tasks = d.get("scheduledTasks") or []
def owned(t):
    tid = str(t.get("id") or "")
    fp = str(t.get("filePath") or "")
    return (
        tid in ids
        or tid.startswith("saps-")
        or tid.startswith("social-autoposter")
        or os.path.realpath(fp).startswith(sched_dir + os.sep + "saps-")
        or os.path.realpath(fp).startswith(sched_dir + os.sep + "social-autoposter")
    )
d["scheduledTasks"] = [t for t in tasks if not owned(t)]
json.dump(d, open(p, "w"), indent=2)
open(p, "a").write("\n")
PY
    fi
  done < <(find "$CLAUDE_SUPPORT/claude-code-sessions" -path '*/scheduled-tasks.json' -type f 2>/dev/null || true)
else
  echo "  NOTE    python3 not on PATH — could not scrub Claude scheduled-tasks.json registries"
fi
echo

# ---- 1d. /tmp scratch (draft plans, queue, browser locks, run_claude) ------
# The draft pipeline scatters state across /tmp that the state-dir wipe in [1]
# does NOT cover: the review-queue + per-batch plan files, mkdir-based browser
# locks, and run_claude.sh's quota stamp / active-session sidecars. Left behind,
# stale plans resurface as phantom drafts and a stale lock blocks the first new
# cycle. (The queue dir itself, ~/.social-autoposter-mcp/claude-queue, is removed
# with the state dir in [1].)
echo "[1d] /tmp scratch (draft plans, browser locks, run_claude artifacts)"
if [ "$DRY" -eq 0 ]; then
  rm -f /tmp/twitter_cycle_plan_*.json 2>/dev/null || true
  rm -rf /tmp/social-autoposter-*.lock 2>/dev/null || true
  rm -rf /tmp/sa-active-claude /tmp/sa-claude-blocked.json 2>/dev/null || true
  rm -f /tmp/sa_run_claude_stdout.* 2>/dev/null || true
else
  echo "  (would remove /tmp/twitter_cycle_plan_*.json, /tmp/social-autoposter-*.lock,"
  echo "   /tmp/sa-active-claude, /tmp/sa-claude-blocked.json, /tmp/sa_run_claude_stdout.*)"
fi
echo

# ---- 1e. scheduled-task session transcripts (autopilot run history) --------
# Each autopilot fire writes a Claude Code session transcript under
# ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl. Those are what flood the
# interactive `claude --resume` / history picker. Step [1c] removes the TASKS but
# NOT their accumulated run transcripts. Clear only OUR automated runs, identified
# by the injected <scheduled-task name="saps-…"/"social-autoposter…"> marker in
# the first user message — never the user's interactive sessions. Only reads the
# head of each .jsonl (the marker is in the opening message) and is scoped to
# *.jsonl files, so it can't block on a stray FIFO.
echo "[1e] scheduled-task session transcripts (autopilot run history)"
PROJDIR="$HOME_DIR/.claude/projects"
if [ -d "$PROJDIR" ]; then
  found=0
  while IFS= read -r jf; do
    [ -n "$jf" ] || continue
    if head -c 8000 "$jf" 2>/dev/null | grep -qE 'scheduled-task name=\\"(saps-|social-autoposter)'; then
      found=$((found+1))
      [ "$DRY" -eq 0 ] && rm -f "$jf"
    fi
  done < <(find "$PROJDIR" -type f -name '*.jsonl' 2>/dev/null)
  if [ "$DRY" -eq 1 ]; then
    echo "  would remove $found scheduled-task transcript(s) under $PROJDIR"
  else
    echo "  removed $found scheduled-task transcript(s)"
  fi
else
  echo "  (~/.claude/projects not present)"
fi
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
# Under --plugin-only, deregister ONLY social-autoposter; the browser-agent MCPs
# (twitter-harness/reddit-agent/linkedin-agent) are shared infra we're preserving
# (their profiles + backend survive in steps 4/5).
echo "[3] MCP registration (claude CLI + config files)"
if [ "$PLUGIN_ONLY" -eq 1 ]; then
  MCP_NAMES="social-autoposter"
else
  MCP_NAMES="social-autoposter twitter-harness reddit-agent linkedin-agent"
fi
if command -v claude >/dev/null 2>&1; then
  for name in $MCP_NAMES; do
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
# Skipped under --plugin-only: the per-agent profiles (reddit/linkedin/twitter)
# belong to the OTHER agents, and the browser-harness profile holds the imported
# X login the user asked to preserve.
if [ "$PLUGIN_ONLY" -eq 1 ]; then
  echo "[4] packaged Chrome profiles + imported cookies — skipped (--plugin-only: X login + shared profiles preserved)"
else
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
fi
echo

# ---- 5. browser-harness backend -------------------------------------------
# Skipped under --plugin-only: the harness backend is shared infra (twitter-harness
# MCP + per-platform agents drive it), not the plugin itself.
if [ "$PLUGIN_ONLY" -eq 1 ]; then
  echo "[5] browser-harness backend — skipped (--plugin-only: shared harness backend preserved)"
else
  echo "[5] browser-harness backend (clone + uv-tool CLI + server.py)"
  rm_path "$HOME_DIR/Developer/browser-harness"               "harness-clone"
  rm_path "$HOME_DIR/.claude/mcp-servers/browser-harness"     "harness-server"
  rm_path "$HOME_DIR/.local/bin/browser-harness"              "harness-cli"
  if command -v uv >/dev/null 2>&1; then
    echo "  run     uv tool uninstall browser-harness"
    [ "$DRY" -eq 0 ] && uv tool uninstall browser-harness >/dev/null 2>&1 || true
  fi
fi
echo

# ---- 6. DEEP: shared toolchain we package (uv + chromium) ------------------
if [ "$PLUGIN_ONLY" -eq 1 ]; then
  echo "[6] shared toolchain (uv, uv cache, packaged Chromium) — skipped (--plugin-only)"
else
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
fi
echo

if [ "$DRY" -eq 1 ]; then
  echo "=== DRY RUN complete. Re-run with --yes to actually remove. ==="
else
  echo "=== Reset complete. Machine is ready for a clean first-install test. ==="
fi
