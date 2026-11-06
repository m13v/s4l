#!/usr/bin/env bash
# s4l_halt.sh — FULL, reversible stop of S4L on a dev box (everything Pause leaves running).
#
# The product's Pause (pause_s4l / tray "Pause S4L") unloads only the five pipeline
# daemons in index.ts PAUSE_TARGETS. A dev box also carries the fleet jobs (linkedin,
# github, instagram, stats, audits, backfills, browser-watch, ...) plus the harness
# browsers and their daemons; those keep firing while "paused", and a harness Chrome
# being activated over CDP is what yanks a fullscreen app out of focus.
#
#   halt    write paused.flag (the SAME sentinel pause_s4l uses, so an MCP boot skips
#           the pipeline daemons and claude_job.py reports "no work"), boot out EVERY
#           loaded S4L LaunchAgent except the tray + dashboard, remember which ones
#           were loaded in <state>/halt-manifest.txt, then quit the harness browsers
#           (Chrome Beta on CDP 955x under ~/.claude/browser-profiles) and their
#           browser_harness daemons. Nothing is deleted.
#   resume  bootstrap exactly the manifest's labels, drop the flag. Browsers relaunch
#           on demand. Idempotent: already-loaded labels are skipped.
#   status  flag, loaded S4L labels, harness processes.
#
# Not covered (by design): the Claude-native s4l-worker scheduled task keeps firing
# every minute; it is headless and claude_job.py honours paused.flag, so it drains
# nothing. Disable it in Claude Desktop (Routines) if even that is unwanted.
set -euo pipefail

STATE="${S4L_HALT_STATE:-$HOME/.social-autoposter-mcp}"   # override only for tests
FLAG="$STATE/paused.flag"
MANIFEST="$STATE/halt-manifest.txt"
LA="$HOME/Library/LaunchAgents"
KEEP="com.m13v.social-autoposter.menubar com.m13v.social-dashboard"
UID_="$(id -u)"

# Every LaunchAgent label that belongs to S4L on this box, from the union of the
# plists on disk and the labels loaded in launchd (same dynamic sweep as
# reset-test-machine.sh; a hardcoded list missed new agents twice).
s4l_labels() {
  {
    for pl in "$LA"/com.m13v.social-*.plist "$LA"/com.m13v.s4l-*.plist \
              "$LA"/com.m13v.browser-watch.plist "$LA"/com.m13v.gmail-dwd-keepalive-s4l.plist; do
      [ -e "$pl" ] && basename "$pl" .plist
    done
    launchctl list 2>/dev/null | awk '$3 ~ /^com\.m13v\.(social-|s4l-|browser-watch$|gmail-dwd-keepalive-s4l$)/ {print $3}'
  } | sort -u | while IFS= read -r l; do
    case " $KEEP " in *" $l "*) ;; *) echo "$l" ;; esac
  done
}

loaded() { launchctl list 2>/dev/null | awk -v l="$1" '$3==l' | grep -q .; }

# Harness Chrome main processes: CDP port 955x + a profile under ~/.claude/browser-profiles.
harness_browsers() {
  ps -eo pid=,args= | awk -v home="$HOME" \
    '/remote-debugging-port=955[0-9]/ && index($0, "--user-data-dir=" home "/.claude/browser-profiles/") && !/Helper/ {print $1}'
}
harness_daemons() { pgrep -f "browser_harness.daemon" || true; }

case "${1:-}" in
  halt)
    mkdir -p "$STATE"
    [ -e "$FLAG" ] || echo "paused at $(date +%Y-%m-%dT%H:%M:%S%z) (s4l_halt)" > "$FLAG"
    touch "$MANIFEST"
    n=0
    for l in $(s4l_labels); do
      loaded "$l" || continue
      grep -qx "$l" "$MANIFEST" || echo "$l" >> "$MANIFEST"
      launchctl bootout "gui/$UID_/$l" >/dev/null 2>&1 || true
      n=$((n + 1))
    done
    echo "booted out $n LaunchAgents (manifest: $MANIFEST, $(wc -l < "$MANIFEST" | tr -d ' ') total)"
    b="$(harness_browsers)"; d="$(harness_daemons)"
    [ -n "$b" ] && { echo "$b" | xargs kill 2>/dev/null || true; echo "quit harness browsers: $(echo $b)"; }
    [ -n "$d" ] && { echo "$d" | xargs kill 2>/dev/null || true; echo "killed harness daemons: $(echo $d)"; }
    echo "halted: paused.flag set, $( [ -z "$b$d" ] && echo 'no harness processes were running' || echo 'harness processes stopped')"
    ;;
  resume)
    rm -f "$FLAG"
    n=0; skipped=0
    if [ -s "$MANIFEST" ]; then
      while IFS= read -r l; do
        [ -n "$l" ] || continue
        if loaded "$l"; then skipped=$((skipped + 1)); continue; fi
        if [ -e "$LA/$l.plist" ]; then
          launchctl bootstrap "gui/$UID_" "$LA/$l.plist" >/dev/null 2>&1 && n=$((n + 1)) || echo "  ! could not bootstrap $l" >&2
        else
          echo "  ! no plist for $l, skipped" >&2
        fi
      done < "$MANIFEST"
      rm -f "$MANIFEST"
    fi
    echo "resumed: paused.flag cleared, $n LaunchAgents reloaded, $skipped already loaded; harness browsers relaunch on demand"
    ;;
  status)
    [ -e "$FLAG" ] && echo "paused.flag: SET ($(cat "$FLAG"))" || echo "paused.flag: absent"
    l=0; for x in $(s4l_labels); do loaded "$x" && l=$((l + 1)); done
    echo "S4L LaunchAgents loaded: $l of $(s4l_labels | wc -l | tr -d ' ') (tray + dashboard excluded)"
    [ -s "$MANIFEST" ] && echo "halt manifest: $(wc -l < "$MANIFEST" | tr -d ' ') labels waiting for resume"
    b="$(harness_browsers)"; d="$(harness_daemons)"
    echo "harness browsers: ${b:-none}"
    echo "harness daemons: ${d:-none}"
    ;;
  *)
    echo "usage: s4l_halt.sh {halt|resume|status}" >&2; exit 64 ;;
esac
