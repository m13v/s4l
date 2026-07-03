#!/usr/bin/env bash
# Programmatic equivalent of the menu-bar "Update now & restart Claude Desktop" button
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
# Scan EVERY "Claude*/Claude Extensions" root, not just plain "Claude/": a box
# whose Desktop build is renamed (e.g. the account-rotator's "Claude-mediar" /
# "Claude-m13vduck" variants) keeps its extension under that suffixed dir, and a
# plain-"Claude/" glob misses it entirely (the "no .mcpb install" exit-4 bug on
# those boxes). Mirrors the menu bar's _ext_dir glob. Pick the newest matching
# `*social-autoposter` dir that actually carries a manifest.json.
APP_SUPPORT="$HOME/Library/Application Support"
EXT_DIR=""
for d in "$APP_SUPPORT"/Claude*/"Claude Extensions"/*social-autoposter; do
  [ -f "$d/manifest.json" ] || continue
  if [ -z "$EXT_DIR" ] || [ "$d" -nt "$EXT_DIR" ]; then EXT_DIR="$d"; fi
done
# Last-resort fallback to the historical path so behavior is unchanged on old boxes.
[ -n "$EXT_DIR" ] || EXT_DIR="$APP_SUPPORT/Claude/Claude Extensions/local.mcpb.m13v.social-autoposter"
PY="/usr/bin/python3"

mode="run"
case "${1:-}" in
  --check)      mode="check" ;;
  --no-restart) mode="no-restart" ;;
  "")           mode="run" ;;
  *) echo "unknown flag: $1" >&2; exit 64 ;;
esac

[ -f "$EXT_DIR/manifest.json" ] || { echo "no .mcpb install at $EXT_DIR" >&2; exit 4; }

# CHANNEL (2026-07-02, single-path 2026-07-04): ONE resolution path for both
# channels — a single unauthenticated curl to the releases LIST endpoint.
# `staging` picks the newest release overall (prereleases included); `stable`
# picks the newest non-prerelease. There are NO fallbacks by design: no
# fallback endpoint, no fallback channel, no fallback URL. Any failure prints
# "unresolved" and the script aborts with nothing changed (the old fallback
# chain quietly retargeted stable and DOWNGRADED a staging box from rc.9 to
# 1.6.196 when the anonymous API lane was rate-limited on 2026-07-03). This
# script is often piped over SSH (`ssh box 'bash -s' < s4l_box_update.sh`)
# with no repo on PATH, so resolution is a self-contained python block reading
# the same <state dir>/channel.json marker every other surface uses. It prints
# four space-separated tokens: "<channel> <tag> <version> <mcpb_url>".
STATE_DIR="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}"
RESOLVED="$(S4L_STATE_DIR="$STATE_DIR" "$PY" - <<'PYEOF' 2>/dev/null || true
import json, os, re, subprocess

state = os.environ.get("S4L_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".social-autoposter-mcp")
try:
    ch = (json.load(open(os.path.join(state, "channel.json"))) or {}).get("channel")
except Exception:
    ch = None
channel = ch if ch in ("stable", "staging") else "stable"

REPO = "m13v/s4l"
TAG_DL = "https://github.com/%s/releases/download/%s/social-autoposter.mcpb"

def curl(url):
    try:
        r = subprocess.run(["/usr/bin/curl", "-fsSL", "-m", "15",
                            "-H", "Accept: application/vnd.github+json", url],
                           capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""

def ver_key(v):
    s = str(v).strip().lstrip("v")
    core, _, pre = s.partition("-")
    nums = [int(x) if x.isdigit() else 0 for x in core.split("+")[0].split(".")]
    while len(nums) < 3:
        nums.append(0)
    if not pre:
        return (nums[0], nums[1], nums[2], 1, 0)
    m = re.findall(r"\d+", pre)
    return (nums[0], nums[1], nums[2], 0, int(m[-1]) if m else 0)

try:
    rels = json.loads(curl("https://api.github.com/repos/%s/releases?per_page=30" % REPO) or "[]")
except Exception:
    rels = []
best = None
for r in (rels if isinstance(rels, list) else []):
    if not isinstance(r, dict) or r.get("draft"):
        continue
    if channel == "stable" and r.get("prerelease"):
        continue
    t = r.get("tag_name")
    if not isinstance(t, str) or not t.lstrip("v")[:1].isdigit():
        continue
    k = ver_key(t)
    if best is None or k > best[0]:
        best = (k, t)
if best is None:
    print("unresolved")
    raise SystemExit(0)
tag = best[1]
print("%s %s %s %s" % (channel, tag, tag.lstrip("v"), TAG_DL % (REPO, tag)))
PYEOF
)"
CHANNEL="$(printf '%s' "$RESOLVED" | awk '{print $1}')"
latest_tag="$(printf '%s' "$RESOLVED" | awk '{print $2}')"
latest="$(printf '%s' "$RESOLVED" | awk '{print $3}')"
MCPB_URL="$(printf '%s' "$RESOLVED" | awk '{print $4}')"

# Fail closed, both channels. Covers the resolver reporting "unresolved" AND
# the resolver process dying outright (RESOLVED empty). Nothing is guessed,
# nothing is downloaded, nothing changes.
if [ -z "$RESOLVED" ] || [ "$CHANNEL" = "unresolved" ] || [ -z "$MCPB_URL" ]; then
  echo "could not resolve the newest release from GitHub (network down or API rate-limited)." >&2
  echo "no fallback by design; nothing changed. Retry later." >&2
  exit 5
fi

installed="$("$PY" -c "import json,sys;print((json.load(open(sys.argv[1])) or {}).get('version',''))" "$EXT_DIR/manifest.json" 2>/dev/null || true)"
echo "channel=$CHANNEL installed=$installed latest=$latest"

if [ "$mode" = "check" ]; then
  [ -n "$latest" ] && [ "$installed" != "$latest" ] && echo "update_available=true" || echo "update_available=false"
  exit 0
fi

if [ -n "$latest" ] && [ "$installed" = "$latest" ]; then
  echo "already on latest ($installed); re-applying anyway would just restart Claude. skipping."
  # Comment the next line out if you want a forced re-unpack even when current.
  exit 0
fi

# Never downgrade silently: whatever the channels say, refuse to unpack an
# OLDER version over a newer install unless explicitly forced. Same ver_key
# ordering as the resolver (prerelease < its own release, rc.N ordered by N).
if [ -n "$latest" ] && [ -n "$installed" ] && [ "${S4L_ALLOW_DOWNGRADE:-0}" != "1" ]; then
  if ! "$PY" -c '
import re, sys
def key(v):
    s = str(v).strip().lstrip("v")
    core, _, pre = s.partition("-")
    n = [int(x) if x.isdigit() else 0 for x in core.split("+")[0].split(".")]
    while len(n) < 3:
        n.append(0)
    if not pre:
        return (n[0], n[1], n[2], 1, 0)
    m = re.findall(r"\d+", pre)
    return (n[0], n[1], n[2], 0, int(m[-1]) if m else 0)
sys.exit(0 if key(sys.argv[2]) >= key(sys.argv[1]) else 1)
' "$installed" "$latest"; then
    echo "resolved $latest is OLDER than installed $installed; refusing downgrade." >&2
    echo "set S4L_ALLOW_DOWNGRADE=1 to force a rollback on purpose." >&2
    exit 6
  fi
fi

tmpd="$(mktemp -d -t s4l-update-XXXXXX)"
trap 'rm -rf "$tmpd"' EXIT
mcpb="$tmpd/social-autoposter.mcpb"

echo "downloading $MCPB_URL ..."
# Retry: a freshly-cut GitHub release's asset download endpoint 404s for up to a
# couple minutes while the CDN propagates (the release API shows the tag/asset as
# "uploaded" before the download URL serves it). A single curl loses that race and
# the update silently "fails." Retry with backoff so the standard pipeline is
# robust to that window.
sz=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fLs -m 300 "$MCPB_URL" -o "$mcpb" 2>/dev/null; then
    sz=$(stat -f%z "$mcpb" 2>/dev/null || echo 0)
    [ "$sz" -ge 100000 ] && break
  fi
  echo "  download attempt $attempt not ready yet (asset propagating); retrying in 15s..." >&2
  sz=0
  sleep 15
done
[ "$sz" -ge 100000 ] || { echo "download failed after retries (asset never became available)" >&2; exit 2; }

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
echo "relocating worker task cwd -> ~/.s4l-worker + removing deprecated autopilot ..."
/usr/bin/python3 - <<'PYCWD' 2>/dev/null || true
import json, os, glob, tempfile, shutil
home = os.path.expanduser("~")
worker = os.path.join(home, ".s4l-worker")
os.makedirs(worker, exist_ok=True)
# s4l-worker is the universal type-blind worker (2026-07-02); saps-worker
# (staging rc.2/rc.3) and the phase pair are legacy. This script only heals cwd
# here — the legacy->s4l-worker consolidation runs via the menubar's
# _rewrite_scheduled_task_cwd() self-heal.
WORKERS = {"s4l-worker", "saps-worker", "saps-phase1-query", "saps-phase2b-draft"}
DEPRECATED = {"social-autoposter-autopilot"}
pat = os.path.join(home, "Library/Application Support/Claude/claude-code-sessions/*/*/scheduled-tasks.json")
for f in glob.glob(pat):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    out, dirty = [], False
    for t in d.get("scheduledTasks", []) or []:
        tid = t.get("id")
        if tid in DEPRECATED:
            dirty = True; continue           # drop deprecated autopilot
        if tid in WORKERS and t.get("cwd") != worker:
            t["cwd"] = worker; dirty = True
        out.append(t)
    if dirty:
        d["scheduledTasks"] = out
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(f))
            with os.fdopen(fd, "w") as fh: json.dump(d, fh, indent=2)
            os.replace(tmp, f)
            print("  cwd-fix: updated", os.path.basename(os.path.dirname(f)))
        except Exception as e:
            print("  cwd-fix: write failed:", e)
for tid in DEPRECATED:
    shutil.rmtree(os.path.join(home, ".claude", "scheduled-tasks", tid), ignore_errors=True)
PYCWD
open -a Claude 2>/dev/null || true
echo "done; Claude restarting on v$new_ver."
