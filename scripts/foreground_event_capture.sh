#!/bin/bash
# Diagnosis-window capture (2026-08-06): on every harness focus-steal event,
# snapshot the system state THAT SECOND — transient CDP clients, process ages,
# frontmost app, and the AppleEvents debug log (enabled via `sudo log config`)
# which names the PROCESS that requested Chrome's activation. Everything we
# could never reconstruct minutes later.
#
# Run under nohup during the diagnosis window; not a pipeline component.
# Output: skill/logs/foreground-captures.log
LOG="$HOME/.social-autoposter-mcp/menubar/menubar.err.log"
OUT="$HOME/social-autoposter/skill/logs/foreground-captures.log"

tail -F -n0 "$LOG" 2>/dev/null | while IFS= read -r line; do
    case "$line" in
        *harness_browser_foregrounded*) ;;
        *) continue ;;
    esac
    {
        echo "===================================================================="
        echo "CAPTURE $(date -u +%FT%T.%3NZ 2>/dev/null || date -u +%FT%TZ)"
        echo "TRIGGER: $line"
        echo "--- cdp clients 9557/9555/9556 ---"
        lsof -nP -i :9557 -i :9555 -i :9556 2>/dev/null | grep -v 'Google Chrome' | grep -v COMMAND | \
        while read -r c p rest; do
            echo "$c $p started=$(ps -p "$p" -o lstart= 2>/dev/null | tr -s ' ') cmd=$(ps -p "$p" -o command= 2>/dev/null | cut -c1-140)"
        done
        echo "--- frontmost ---"
        osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true' 2>/dev/null
        echo "--- appleevents debug (last 30s, chrome-related) ---"
        log show --last 30s --predicate 'subsystem == "com.apple.appleevents"' --style compact 2>/dev/null | \
            grep -iE 'chrome|activate|reopen|open' | head -20
        echo "--- launchservices (last 30s, chrome-related) ---"
        log show --last 30s --predicate 'subsystem == "com.apple.launchservices"' --style compact 2>/dev/null | \
            grep -iE 'chrome|front|activate' | head -12
        echo "--- fresh short-lived python (started last 3 min) ---"
        for p in $(pgrep -f python 2>/dev/null); do
            et=$(ps -p "$p" -o etime= 2>/dev/null | tr -d ' ')
            case "$et" in
                0[0-2]:*|[0-9]:[0-9][0-9]) echo "$p etime=$et $(ps -p "$p" -o command= 2>/dev/null | cut -c1-120)" ;;
            esac
        done
        echo ""
    } >> "$OUT" 2>&1
done
