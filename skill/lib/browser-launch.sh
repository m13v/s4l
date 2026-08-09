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
    # SINGLETON GATE (2026-08-07): if a live Chrome already owns this profile,
    # NEVER exec another launch. A duplicate launch does not create a second
    # instance — Chrome's singleton handoff makes the EXISTING instance FRONT
    # ITSELF (and reopen a blank window when it has none): the reproduced root
    # cause of the residual focus steals / "browser restarted on a blank page"
    # reports. The handoff's "Opening in existing browser session." goes to
    # stderr that `open` discards, so it never appeared in any log. Probes can
    # false-negative under load; process liveness is the authority. Callers
    # that genuinely need a relaunch kill the owner first
    # (_hc_reap_profile_owners), which makes this gate pass. Trailing space in
    # the pattern keeps browser-harness from matching browser-harness-linkedin
    # (same convention as the reaper).
    local _bl_owner
    _bl_owner=$(pgrep -f -- "--user-data-dir=$_bl_prof_dir " 2>/dev/null | head -1)
    if [ -n "$_bl_owner" ]; then
        echo "[browser-launch] SKIP: pid $_bl_owner already owns $_bl_prof_dir; a duplicate launch would singleton-handoff and front the existing window" >&2
        return 0
    fi
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
        # -j launches HIDDEN on top of -g's no-foreground: -g alone still let
        # every wedge-heal relaunch register a `cause: launched` activation in
        # the browser-foreground telemetry (the last remaining window-pop
        # class, 2026-07-15). Occlusion/backgrounding flags keep a hidden
        # Chrome rendering, so the pipeline is unaffected.
        open -n -g -j -a "$_bl_app_bundle" --args "$@" >/dev/null 2>&1 || true
    else
        "${S4L_PYTHON:-python3}" -c 'import os,sys
os.setsid()
os.execv(sys.argv[1], sys.argv[1:])' \
            "$_bl_chrome_bin" "$@" >/dev/null 2>&1 &
        disown
    fi
}
