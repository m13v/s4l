#!/usr/bin/env bash
# Programmatic equivalent of the menu-bar "Please update now" button
# (mcp/menubar/s4l_menubar.py::_mcpb_update_work). Pulls the latest .mcpb from
# GitHub releases, unzips it over the Claude Desktop extension dir in place, and
# restarts Claude so the new MCP server loads. Designed to be run over SSH on a
# .mcpb box (e.g. `ssh macstadium 'bash -s' < scripts/s4l_box_update.sh`), where
# npm/npx is absent so the `runtime action:update` (npx) path is dead.
#
# Flags:
#   --check        Print installed vs latest and exit (no download, no restart).
#   --no-restart   Download + unpack the new .mcpb but do NOT restart Claude.
#   (default)      Download + unpack + restart Claude.
#
# Exits: 0 ok / already current, 2 download failed, 3 unpack failed, 4 no install.
set -euo pipefail

# Resolve the Claude Desktop extension dir. Claude derives its name from the
# manifest author, so the id changed `local.mcpb.m13v.social-autoposter` ->
# `local.mcpb.s4l.ai.social-autoposter` when the author became "S4L.ai". A
# hardcoded id silently breaks the updater on every fresh install (the box
# reported "no .mcpb install" for exactly this reason). Glob for any
# `*social-autoposter` extension dir that actually has a manifest.json, newest
# first, so this keeps working across future renames.
EXT_ROOT="$HOME/Library/Application Support/Claude/Claude Extensions"
EXT_DIR=""
if [ -d "$EXT_ROOT" ]; then
  for d in "$EXT_ROOT"/*social-autoposter; do
    [ -f "$d/manifest.json" ] || continue
    if [ -z "$EXT_DIR" ] || [ "$d" -nt "$EXT_DIR" ]; then EXT_DIR="$d"; fi
  done
fi
# Last-resort fallback to the historical id so behavior is unchanged on old boxes.
[ -n "$EXT_DIR" ] || EXT_DIR="$EXT_ROOT/local.mcpb.m13v.social-autoposter"
MCPB_URL="https://github.com/m13v/social-autoposter/releases/latest/download/social-autoposter.mcpb"
RELEASE_API="https://api.github.com/repos/m13v/social-autoposter/releases/latest"
PY="/usr/bin/python3"

mode="run"
case "${1:-}" in
  --check)      mode="check" ;;
  --no-restart) mode="no-restart" ;;
  "")           mode="run" ;;
  *) echo "unknown flag: $1" >&2; exit 64 ;;
esac

[ -f "$EXT_DIR/manifest.json" ] || { echo "no .mcpb install at $EXT_DIR" >&2; exit 4; }

installed="$("$PY" -c "import json,sys;print((json.load(open(sys.argv[1])) or {}).get('version',''))" "$EXT_DIR/manifest.json" 2>/dev/null || true)"
latest_tag="$(curl -fsSL -m 15 "$RELEASE_API" | "$PY" -c "import sys,json;print((json.load(sys.stdin) or {}).get('tag_name',''))" 2>/dev/null || true)"
latest="${latest_tag#v}"
echo "installed=$installed latest=$latest"

if [ "$mode" = "check" ]; then
  [ -n "$latest" ] && [ "$installed" != "$latest" ] && echo "update_available=true" || echo "update_available=false"
  exit 0
fi

if [ -n "$latest" ] && [ "$installed" = "$latest" ]; then
  echo "already on latest ($installed); re-applying anyway would just restart Claude. skipping."
  # Comment the next line out if you want a forced re-unpack even when current.
  exit 0
fi

tmpd="$(mktemp -d -t s4l-update-XXXXXX)"
trap 'rm -rf "$tmpd"' EXIT
mcpb="$tmpd/social-autoposter.mcpb"

echo "downloading $MCPB_URL ..."
curl -fLs -m 300 "$MCPB_URL" -o "$mcpb" || { echo "download failed" >&2; exit 2; }
sz=$(stat -f%z "$mcpb" 2>/dev/null || echo 0)
[ "$sz" -ge 100000 ] || { echo "download too small ($sz bytes), aborting" >&2; exit 2; }

echo "unpacking into extension dir ..."
unzip -oq "$mcpb" -d "$EXT_DIR" || { echo "unpack failed" >&2; exit 3; }
new_ver="$("$PY" -c "import json,sys;print((json.load(open(sys.argv[1])) or {}).get('version',''))" "$EXT_DIR/manifest.json" 2>/dev/null || true)"
echo "unpacked version=$new_ver"

if [ "$mode" = "no-restart" ]; then
  echo "done (no restart requested); restart Claude to load v$new_ver."
  exit 0
fi

# Restart Claude. From an SSH session we skip the osascript graceful-quit the
# menu bar uses (it can trip an Automation TCC prompt for sshd and block
# unattended); killall sends SIGTERM and needs no automation grant.
echo "restarting Claude ..."
killall Claude 2>/dev/null || true
sleep 4
killall -9 Claude 2>/dev/null || true
sleep 1
# Relocate the autopilot scheduled tasks' working dir to ~/.s4l-worker so their
# once-a-minute runs stop flooding the user's interactive `claude --resume`
# history (Claude buckets sessions by cwd). MUST run while Claude is DOWN — the
# running app caches the scheduled-tasks registry in memory and clobbers a live
# edit on the next fire. Kept in sync with the menu-bar updater's
# _rewrite_scheduled_task_cwd() and queueWorkerCwd() in mcp/src/index.ts.
echo "relocating autopilot task cwd -> ~/.s4l-worker ..."
/usr/bin/python3 - <<'PYCWD' 2>/dev/null || true
import json, os, glob, tempfile
home = os.path.expanduser("~")
worker = os.path.join(home, ".s4l-worker")
os.makedirs(worker, exist_ok=True)
IDS = {"saps-phase1-query", "saps-phase2b-draft", "social-autoposter-autopilot"}
pat = os.path.join(home, "Library/Application Support/Claude/claude-code-sessions/*/*/scheduled-tasks.json")
for f in glob.glob(pat):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    dirty = False
    for t in d.get("scheduledTasks", []):
        if t.get("id") in IDS and t.get("cwd") != worker:
            t["cwd"] = worker; dirty = True
    if dirty:
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(f))
            with os.fdopen(fd, "w") as fh: json.dump(d, fh, indent=2)
            os.replace(tmp, f)
            print("  cwd-fix: relocated tasks in", os.path.basename(os.path.dirname(f)))
        except Exception as e:
            print("  cwd-fix: write failed:", e)
PYCWD
open -a Claude 2>/dev/null || true
echo "done; Claude restarting on v$new_ver."
