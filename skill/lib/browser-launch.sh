#!/bin/bash
# browser-launch.sh — the ONE way any backend spawns a harness Chrome.
#
# Consolidates what twitter-backend.sh and linkedin-backend.sh grew as
# hand-rolled near-duplicates on 2026-07-13 (and reddit-backend.sh never got
# at all). Source this from a backend lib and call:
#
#     launch_harness_chrome "$_chrome_bin" "$_prof_dir" <chrome arg>...
#
# What every launch gets, uniformly:
#   1. CLEAN-EXIT STAMP: mark the profile's last exit as clean first, so a
#      SIGKILLed Chrome (wedge heal, stall abort) doesn't session-restore a
#      crashed "Aw, Snap" corpse tab on relaunch.
#   2. NO FOCUS STEAL (macOS + .app bundle): `open -n -g` launches a NEW
#      instance (-n: the user's personal Chrome may already be running;
#      without it LaunchServices pokes that instance and drops our --args)
#      without activation (-g). A directly-exec'd Chrome always activates
#      itself and planted a window over the user's work on every relaunch.
#      LaunchServices also parents the process outside the caller's launchd
#      job process group — independent cover for the pgroup-reaping bug.
#      (Tested alternatives that DON'T work: `-j` — Chrome unhides and
#      activates itself anyway; `--no-startup-window` — the activation just
#      moves to first-tab creation.)
#   3. DETACHED FALLBACK (Linux / bare chromium binaries): direct exec in a
#      NEW SESSION via os.setsid, so a transient launchd job's exit can't
#      SIGKILL Chrome with the job's process group (2026-07-12 root cause of
#      the kill-relaunch-foreground loop).
launch_harness_chrome() {
    local _bl_chrome_bin="$1"; shift
    local _bl_prof_dir="$1"; shift
    "${S4L_PYTHON:-python3}" -c 'import json, os, sys
p = os.path.join(sys.argv[1], "Default", "Preferences")
try:
    d = json.load(open(p))
except Exception:
    raise SystemExit(0)
prof = d.setdefault("profile", {})
prof["exit_type"] = "Normal"
prof["exited_cleanly"] = True
json.dump(d, open(p, "w"))' "$_bl_prof_dir" 2>/dev/null || true
    local _bl_app_bundle=""
    case "$_bl_chrome_bin" in
        *.app/Contents/MacOS/*) _bl_app_bundle="${_bl_chrome_bin%%/Contents/MacOS/*}" ;;
    esac
    if [ "$(uname -s)" = "Darwin" ] && [ -n "$_bl_app_bundle" ] && [ -d "$_bl_app_bundle" ]; then
        open -n -g -a "$_bl_app_bundle" --args "$@" >/dev/null 2>&1 || true
    else
        "${S4L_PYTHON:-python3}" -c 'import os,sys
os.setsid()
os.execv(sys.argv[1], sys.argv[1:])' \
            "$_bl_chrome_bin" "$@" >/dev/null 2>&1 &
        disown
    fi
}
