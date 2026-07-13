#!/bin/bash
# run-linkedin-launchd.sh — detach wrapper invoked by launchd.
#
# Why this exists:
#   launchd's StartInterval silently SUPPRESSES a scheduled fire when the prior
#   invocation of the same Label is still alive. Before 2026-05-01 the
#   linkedin pipeline held the linkedin-browser lock for the full 25-45min
#   run, so wrapping it would just queue blocked-on-lock processes with no
#   throughput gain. On 2026-05-01 run-linkedin.sh was refactored to acquire
#   linkedin-browser only around its two Claude phases (~5min each), freeing
#   the lock during DB ingest / candidate pick / Phase B prep windows.
#   With that change the wrapper becomes useful: parallel cycles can run
#   their non-browser phases concurrently, and the lock + lock.sh's FIFO
#   ticket queue serialize the brief browser windows fairly.
#
# How it works:
#   Python double-fork daemon idiom — first fork gives launchd a parent that
#   exits immediately (so the job is marked complete in milliseconds), setsid
#   detaches the session, second fork prevents reacquiring a controlling
#   terminal, then we exec the real pipeline. macOS lacks `setsid(1)` and
#   `nohup ... & disown` is not enough because launchd reaps the wrapper's
#   pgid, taking the nohup child with it.
#
# LinkedIn-specific risk:
#   Per CLAUDE.md memory feedback_linkedin_session_fingerprint, anti-bot can
#   fire on sequential agent re-auths even when the lock prevents parallel
#   Chromes. Pre-wrapper steady state was ~2 cycles/hr × 2 fresh Chrome
#   sessions/cycle = 4 sessions/hr. Post-wrapper it can rise to ~4 cycles/hr
#   × 2 = 8 sessions/hr (peak). If LinkedIn restrictions return, REVERT this
#   wrapper first (point the plist back at run-linkedin.sh directly) before
#   touching the OAuth API or browser profile.

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

# LinkedIn killswitch (2026-05-27): refuse to even fork the daemon if a
# prior fire detected session compromise (http_999, authwall, throttle,
# li_at cleared). Cheaper than letting the inner script discover it.
# State: ~/.claude/social-autoposter/linkedin.killswitch
# Clear: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear
if [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] LINKEDIN_KILLSWITCH active. Skipping run-linkedin." \
        >> "$LOG_DIR/launchd-linkedin-stderr.log"
    exit 0
fi

SCRIPT="$REPO_DIR/skill/run-linkedin.sh"
OUT="$LOG_DIR/launchd-linkedin-stdout.log"
ERR="$LOG_DIR/launchd-linkedin-stderr.log"

# Preflight (added 2026-05-02): skip cleanly if Claude is blocked on a
# quota cap, or if the system is under memory pressure. See
# scripts/preflight.sh for full design.
SA_PREFLIGHT_SCRIPT="run-linkedin"
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
