#!/bin/bash
# run-reddit-search-launchd.sh — detach wrapper invoked by launchd.
#
# Why this exists:
#   launchd's StartInterval silently SUPPRESSES a scheduled fire when the prior
#   invocation of the same Label is still alive. Reddit-search cycles vary
#   (5 iterations of plan+post) and routinely run 15-25 min, so 1-3 of every
#   4 fires were getting dropped. Throughput collapsed to ~2 cycles/hr instead
#   of 4.
#
# How it works:
#   This wrapper is what launchd actually invokes. It uses Python's classic
#   double-fork daemon idiom to spawn the real pipeline in a fresh session
#   (os.setsid), then exits. macOS lacks the `setsid` binary and a plain
#   `nohup ... &; disown` is NOT sufficient — launchd cleans up the wrapper's
#   process group after the wrapper exits and SIGTERMs the nohup'd child too.
#   The double-fork moves the cycle into its own session/pgid that launchd
#   no longer tracks, so it survives wrapper exit.
#
# Net effect: launchd marks the job complete in milliseconds and fires the
# next 15-min slot on schedule. Multiple run-reddit-search.sh processes can
# now live in parallel; the reddit-browser lock (skill/lock.sh) serializes
# the brief post-phase windows, and post_reddit.py's already_posted filter
# prevents double-posting across overlapping cycles.

# Honor S4L_REPO_DIR (set by the MCP-managed plist on customer boxes, where the
# repo is the materialized package copy, not ~/social-autoposter). The fallback
# keeps the operator Mac's hand-built plist working unchanged.
REPO_DIR="${S4L_REPO_DIR:-$HOME/social-autoposter}"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

SCRIPT="$REPO_DIR/skill/run-reddit-search.sh"
OUT="$LOG_DIR/launchd-reddit-search-stdout.log"
ERR="$LOG_DIR/launchd-reddit-search-stderr.log"

# Preflight (added 2026-05-02): skip cleanly if Claude is blocked on a
# quota cap, or if the system is under memory pressure. Both paths exit 0
# with a stderr [skipped:] line so launchd treats the slot as consumed.
# See scripts/preflight.sh for full design.
SA_PREFLIGHT_SCRIPT="run-reddit-search"
source "$REPO_DIR/scripts/preflight.sh"
preflight_skip_if_claude_blocked
preflight_skip_if_jetsam_pressure

exec /usr/bin/python3 -c "
import os, sys
script  = '$SCRIPT'
out_log = '$OUT'
err_log = '$ERR'

if os.fork() != 0:
    os._exit(0)
os.setsid()

if os.fork() != 0:
    os._exit(0)

os.chdir('/')
out_fd = os.open(out_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
err_fd = os.open(err_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
nul_fd = os.open('/dev/null', os.O_RDONLY)
os.dup2(nul_fd, 0)
os.dup2(out_fd, 1)
os.dup2(err_fd, 2)
os.execv(script, [script])
"
