#!/usr/bin/env bash
# reset-test-machine.sh — wipe a social-autoposter install back to factory-fresh.
#
# For TEST MACHINES. Removes what a social-autoposter install scatters so you can
# re-run a clean first-install and reproduce new-user bugs.
#
# DEFAULT IS A DRY RUN. Nothing is deleted until you pass --yes.
#
# Two scopes:
#   (default)  PLUGIN RESET — just the plugin: the menu-bar app, the .mcpb/npm
#              plugin itself, the scheduled tasks, the user's state+config
#              (~/.social-autoposter-mcp, including config.json + mode.json), the
#              /tmp scratch, autopilot transcripts, and the social-autoposter MCP
#              registration. PRESERVES the imported X login (browser-harness
#              profile), all shared browser profiles, the browser-harness backend,
#              and the packaged toolchain. Uninstall + forget the plugin without
#              disturbing the rest of the environment.
#   --deep     FULL NUKE — the plugin reset above PLUS the shared layer it touches:
#              per-agent Chrome profiles + cookies (reddit/linkedin/twitter), the
#              browser-harness backend, and the packaged toolchain (uv binary, uv
#              cache, Chromium ~1.7G). Other tools that rely on uv/Chromium
#              re-provision afterward. Use to reproduce a true bare-metal box.
#
# Usage:
#   scripts/reset-test-machine.sh            # dry run: print what the plugin reset WOULD remove
#   scripts/reset-test-machine.sh --yes      # plugin reset (light) — quits Claude Desktop first
#   scripts/reset-test-machine.sh --yes --deep   # full nuke incl. shared browser layer + toolchain
#   scripts/reset-test-machine.sh --yes --keep-claude  # don't quit Claude (run from inside a live session)
#
# IMPORTANT: by default --yes QUITS Claude Desktop before wiping AND leaves it
# down. The .mcpb registry ([1b]), scheduled-task ([1c]) and mcpServers ([3])
# edits only STICK if Claude is not running, because the host rewrites those
# files on quit and re-materializes the state dir on launch. A final settle step
# ([7]) re-quits Claude and re-wipes if it auto-relaunches (e.g. an auto-update),
# so the box is genuinely first-run. Relaunch Claude by hand for the fresh
# install. If you invoke this from INSIDE a live Claude session and don't want the
# app taken down, pass --keep-claude (the registry/task/mcp edits then won't
# persist, and the settle step is skipped, until you quit Claude yourself).
#
set -u

HOME_DIR="${HOME}"
DRY=1
DEEP=0
KEEP_CLAUDE=0
for a in "$@"; do
  case "$a" in
    --yes|-y) DRY=0 ;;
    --deep)   DEEP=1 ;;
    --keep-claude) KEEP_CLAUDE=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '/^!/d'
      exit 0 ;;
    *) echo "unknown arg: $a (use --yes, --deep, --keep-claude, --help)"; exit 2 ;;
  esac
done

# The plugin reset is the default; --deep widens it to the shared browser layer +
# toolchain. PLUGIN_ONLY (the inverse of DEEP) gates the shared-layer steps below.
if [ "$DEEP" -eq 1 ]; then PLUGIN_ONLY=0; else PLUGIN_ONLY=1; fi

if [ "$DRY" -eq 1 ]; then
  echo "=== DRY RUN — nothing will be deleted. Re-run with --yes to apply. ==="
else
  echo "=== APPLYING — removing social-autoposter install ==="
fi
if [ "$PLUGIN_ONLY" -eq 1 ]; then
  echo "=== SCOPE: plugin reset (app + plugin + tasks + state/config; keeping X login, browser layer, toolchain). Pass --deep for the full nuke. ==="
else
  echo "=== SCOPE: --deep full nuke (plugin + shared browser profiles + harness backend + toolchain) ==="
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

# ---- 0. quit Claude Desktop (so [1b]/[1c] registry+task edits stick) -------
# Claude Desktop OWNS the .mcpb installations registry and the scheduled-task
# schedule, and REWRITES both files on quit. If it is running while we delete the
# extension entry / task dirs, it resurrects them on its next exit — exactly the
# "plugin still installed + scheduled tasks back" failure. So quit it FIRST
# (graceful AppleScript quit, wait, then SIGKILL fallback) unless --keep-claude.
echo "[0] quit Claude Desktop"
if [ "$KEEP_CLAUDE" -eq 1 ]; then
  echo "  (--keep-claude: leaving Claude Desktop running — [1b]/[1c] edits will NOT persist until you quit it)"
elif [ "$DRY" -eq 0 ]; then
  if pgrep -x "Claude" >/dev/null 2>&1; then
    echo "  quit    Claude Desktop (graceful)"
    osascript -e 'tell application "Claude" to quit' >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      pgrep -x "Claude" >/dev/null 2>&1 || break
      sleep 1
    done
    if pgrep -x "Claude" >/dev/null 2>&1; then
      echo "  kill    Claude Desktop still up — SIGKILL"
      pkill -9 -x "Claude" 2>/dev/null || true
      sleep 1
    fi
    pgrep -x "Claude" >/dev/null 2>&1 && echo "  WARN    Claude Desktop still running after kill" || echo "  ok      Claude Desktop is down"
  else
    echo "  (Claude Desktop not running)"
  fi
else
  echo "  (would quit Claude Desktop: AppleScript quit, then SIGKILL fallback)"
fi
echo

# ---- 0a. stop anything still running ---------------------------------------
echo "[0a] stop running processes"
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

# ---- 0c. ALL other social-autoposter LaunchAgents (dynamic sweep) ----------
# Beyond the menu bar ([0b]), the install has dropped a GROWING set of
# com.m13v.social-* LaunchAgents into ~/Library/LaunchAgents: the twitter-cycle
# kicker, the daily updater, plus autopilot-stall-watch, claude-reaper,
# memory-snapshot, and overlay-watch. All live OUTSIDE the state dir removed in
# [1], so any we miss keep firing against a half-wiped install (or resurrect it).
# Hardcoding the list meant every NEW agent survived the next reset (that bug bit
# us twice). So sweep DYNAMICALLY: the union of every com.m13v.social-* plist on
# disk and every com.m13v.social-* label loaded in launchd, minus the menu bar
# already handled in [0b]. Bootout + remove each (and any .disabled-* variant).
echo "[0c] social-autoposter LaunchAgents (dynamic sweep of all com.m13v.social-*)"
LA_DIR="$HOME_DIR/Library/LaunchAgents"
AGENT_LABELS=()
# labels from plist files on disk
for pl in "$LA_DIR"/com.m13v.social-*.plist; do
  [ -e "$pl" ] || continue
  AGENT_LABELS+=("$(basename "$pl" .plist)")
done
# labels currently loaded in launchd (catches loaded-but-plist-already-gone)
while IFS= read -r lbl; do
  [ -n "$lbl" ] && AGENT_LABELS+=("$lbl")
done < <(launchctl list 2>/dev/null | awk '/com\.m13v\.social-/ {print $3}')
# dedupe + drop the menu bar (handled in [0b]); keep set -u safe with an empty set
FILTERED=()
if [ "${#AGENT_LABELS[@]}" -gt 0 ]; then
  while IFS= read -r lbl; do
    [ -n "$lbl" ] && FILTERED+=("$lbl")
  done < <(printf '%s\n' "${AGENT_LABELS[@]}" | awk -v mb="$MENUBAR_LABEL" 'NF && $0!=mb && !seen[$0]++')
fi
if [ "${#FILTERED[@]}" -eq 0 ]; then
  echo "  (no other com.m13v.social-* LaunchAgents found)"
else
  for LBL in "${FILTERED[@]}"; do
    PL="$LA_DIR/$LBL.plist"
    if [ "$DRY" -eq 0 ]; then
      launchctl bootout "gui/$(id -u)/$LBL" 2>/dev/null \
        || launchctl unload "$PL" 2>/dev/null || true
    else
      echo "  (would bootout $LBL)"
    fi
    rm_path "$PL" "launchagent"
    for d in "$PL".disabled*; do [ -e "$d" ] && rm_path "$d" "launchagent-disabled"; done
  done
fi
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
# these files on quit). Step [0] already quit it; this warns only if it came back
# (e.g. --keep-claude, or a relaunch mid-run).
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
# host (Claude Desktop) owns the live schedule and rewrites it on quit. Step [0]
# already quit it; this warns only if it came back (--keep-claude / relaunch).
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
# In the default plugin reset, deregister ONLY social-autoposter; the browser-agent
# MCPs (twitter-harness/reddit-agent/linkedin-agent) are shared infra we preserve
# (their profiles + backend survive in steps 4/5). --deep deregisters them too.
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
# Surgically remove ONLY the in-scope MCP stanzas from the config files. The
# claude CLI (above) isn't on PATH on the box, and even when it is it leaves the
# raw mcpServers stanza in ~/.claude.json — which the Cowork/Code boot self-heal
# (ensureCoworkMcpRegistered) reads back and RE-materializes the state dir on the
# next Claude launch. So drop the keys ourselves (backup first). Plugin reset
# removes only social-autoposter; --deep also removes the shared browser MCPs.
if command -v python3 >/dev/null 2>&1; then
  for cfg in \
    "$HOME_DIR/.claude.json" \
    "$HOME_DIR/Library/Application Support/Claude/claude_desktop_config.json"; do
    [ -f "$cfg" ] || continue
    if python3 - "$cfg" $MCP_NAMES <<'PY' >/dev/null 2>&1; then
import json, sys
p, names = sys.argv[1], set(sys.argv[2:])
try:
    d = json.load(open(p))
except Exception:
    sys.exit(1)
srv = d.get("mcpServers") or {}
sys.exit(0 if names.intersection(srv.keys()) else 1)
PY
      json_edit "remove MCP stanza(s) [$MCP_NAMES] from $cfg" \
        sh -c 'cp "$1" "$1.bak" 2>/dev/null || true; shift; python3 - "$@"' sh "$cfg" "$cfg" $MCP_NAMES <<'PY'
import json, sys
p, names = sys.argv[1], set(sys.argv[2:])
d = json.load(open(p))
srv = d.get("mcpServers") or {}
for n in names:
    srv.pop(n, None)
d["mcpServers"] = srv
json.dump(d, open(p, "w"), indent=2)
open(p, "a").write("\n")
PY
    else
      [ -f "$cfg" ] && echo "  (no in-scope MCP stanza in $cfg)"
    fi
  done
else
  for cfg in \
    "$HOME_DIR/.claude.json" \
    "$HOME_DIR/Library/Application Support/Claude/claude_desktop_config.json"; do
    if [ -f "$cfg" ] && grep -q 'social-autoposter\|twitter-harness' "$cfg" 2>/dev/null; then
      echo "  NOTE    python3 not on PATH — remove the MCP stanza from $cfg by hand"
    fi
  done
fi
echo

# ---- 4. packaged Chrome profiles + imported cookies ------------------------
# Skipped in the default plugin reset: the per-agent profiles (reddit/linkedin/
# twitter) belong to the OTHER agents, and the browser-harness profile holds the
# imported X login we preserve. Only --deep removes these.
if [ "$PLUGIN_ONLY" -eq 1 ]; then
  echo "[4] packaged Chrome profiles + imported cookies — skipped (plugin reset: X login + shared profiles preserved; pass --deep to remove)"
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
# Skipped in the default plugin reset: the harness backend is shared infra
# (twitter-harness MCP + per-platform agents drive it), not the plugin itself.
# Only --deep removes it.
if [ "$PLUGIN_ONLY" -eq 1 ]; then
  echo "[5] browser-harness backend — skipped (plugin reset: shared harness backend preserved; pass --deep to remove)"
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
  echo "[6] shared toolchain (uv, uv cache, packaged Chromium) — skipped (plugin reset; pass --deep to remove)"
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

# ---- 7. settle & verify (defeat Claude auto-relaunch resurrection) ---------
# Claude Desktop can RELAUNCH after step [0] — on auto-update (ShipIt), a macOS
# re-open, or a login item — and a relaunched host re-materializes a skeleton
# ~/.social-autoposter-mcp/repo within seconds of the wipe, so the box is NOT
# truly first-run. Unless --keep-claude, settle it: keep Claude down and re-remove
# the state dir until it stops coming back (cap 3 rounds), then report. Leaves
# Claude Desktop DOWN on purpose — relaunch it by hand to do the fresh install.
STATE_DIR="$HOME_DIR/.social-autoposter-mcp"
if [ "$DRY" -eq 0 ] && [ "$KEEP_CLAUDE" -eq 0 ]; then
  echo "[7] settle & verify (guard against Claude auto-relaunch resurrection)"
  for round in 1 2 3; do
    if pgrep -x "Claude" >/dev/null 2>&1; then
      echo "  round $round: Claude Desktop is up (relaunched) — quitting again"
      osascript -e 'tell application "Claude" to quit' >/dev/null 2>&1 || true
      for _ in 1 2 3 4 5; do pgrep -x "Claude" >/dev/null 2>&1 || break; sleep 1; done
      pgrep -x "Claude" >/dev/null 2>&1 && pkill -9 -x "Claude" 2>/dev/null || true
      sleep 1
    fi
    if [ -e "$STATE_DIR" ]; then
      echo "  round $round: removing recreated $STATE_DIR"
      rm -rf "$STATE_DIR"
    fi
    sleep 3
    if [ ! -e "$STATE_DIR" ] && ! pgrep -x "Claude" >/dev/null 2>&1; then
      echo "  ok      state dir gone and Claude Desktop down (settled on round $round)"
      break
    fi
  done
  if [ -e "$STATE_DIR" ]; then
    echo "  WARN    $STATE_DIR keeps reappearing after 3 rounds — something beyond"
    echo "          Claude is recreating it; investigate before treating as first-run."
  fi
  echo
fi

if [ "$DRY" -eq 1 ]; then
  echo "=== DRY RUN complete. Re-run with --yes to actually remove. ==="
else
  echo "=== Reset complete. Machine is ready for a clean first-install test."
  echo "=== Claude Desktop was left DOWN so the wipe sticks — relaunch it, then"
  echo "=== re-download the .mcpb for a genuine first-run install. ==="
fi
