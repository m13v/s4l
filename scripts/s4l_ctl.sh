#!/usr/bin/env bash
# s4l-ctl: programmatic control of the running social-autoposter (S4L) plugin via
# its loopback tool server, for QA. Runs the same handlers as the in-chat MCP
# tools (POST /tool/<name>). MUST run ON the box (the loopback is 127.0.0.1-only),
# so over SSH use:  ssh macstadium 'bash -s -- <subcommand> [args]' < scripts/s4l_ctl.sh
#
# Subcommands:
#   status                 Dashboard snapshot (read-only).
#   count                  Number of pending (unposted) drafts (read-only).
#   drafts                 List pending drafts with their 1-based numbers (read-only).
#   approve <n> [n...]     Post the given card numbers. DESTRUCTIVE — requires --yes.
#   approve-all            Post EVERY pending card. DESTRUCTIVE — requires --yes.
#
# DESTRUCTIVE note: "approve" really posts replies to live X/Twitter threads. The
# write subcommands refuse to run unless --yes is present (no interactive prompt,
# because over piped SSH there is no tty). For host-level plugin UPDATE use the
# separate scripts/s4l_box_update.sh (different mechanism: works even when the
# loopback is down, and it restarts Claude).
set -euo pipefail

BATCH="review-queue"
PLAN="/tmp/twitter_cycle_plan_${BATCH}.json"
EP="$HOME/.social-autoposter-mcp/panel-endpoint.json"
PY="/usr/bin/python3"

YES=0; ARGS=()
for a in "$@"; do
  case "$a" in --yes|-y) YES=1 ;; *) ARGS+=("$a") ;; esac
done
set -- ${ARGS[@]+"${ARGS[@]}"}
cmd="${1:-}"; [ $# -gt 0 ] && shift || true

[ -f "$EP" ] || { echo "no panel-endpoint.json — is Claude Desktop / the MCP running?" >&2; exit 1; }
URL="$("$PY" -c "import json;print(json.load(open('$EP'))['url'])")"
curl -s -m 3 "${URL}health" >/dev/null || { echo "loopback unreachable at $URL" >&2; exit 1; }

tool() { curl -s -m "${2:-900}" -X POST "${URL}tool/$1" -H 'Content-Type: application/json' -d "${3:-{}}"; }

pending_count() {
  [ -f "$PLAN" ] || { echo 0; return; }
  "$PY" -c "import json;d=json.load(open('$PLAN'));print(sum(1 for c in d.get('candidates',[]) if not c.get('posted')))"
}

case "$cmd" in
  status)
    tool dashboard 20 ; echo ;;
  count)
    echo "pending=$(pending_count)" ;;
  drafts)
    if [ ! -f "$PLAN" ]; then echo "no review-queue plan on box (0 drafts)"; exit 0; fi
    "$PY" - "$PLAN" <<'PYEOF'
import json,sys
d=json.load(open(sys.argv[1]))
for i,c in enumerate(d.get("candidates",[]),1):
    if c.get("posted"): continue
    txt=(c.get("reply_text") or "").replace("\n"," ")
    print(f"#{i:<4} @{(c.get('thread_author') or '?'):<18} {txt[:90]}")
PYEOF
    echo "pending=$(pending_count)" ;;
  approve)
    [ $# -ge 1 ] || { echo "usage: approve <n> [n...] --yes" >&2; exit 64; }
    nums="$(printf '%s\n' "$@" | paste -sd, -)"
    if [ "$YES" != "1" ]; then
      echo "REFUSING: 'approve $*' will POST those cards to live X. Re-run with --yes to confirm." >&2; exit 3; fi
    echo "posting cards [$nums] ..."
    tool post_drafts 900 "{\"batch_id\":\"$BATCH\",\"post\":[$nums]}" ; echo ;;
  approve-all)
    n="$(pending_count)"
    if [ "$YES" != "1" ]; then
      echo "REFUSING: approve-all will POST all $n pending cards to live X. Re-run with --yes to confirm." >&2; exit 3; fi
    echo "posting all $n pending cards ..."
    tool post_drafts 1800 "{\"batch_id\":\"$BATCH\",\"post_all\":true}" ; echo ;;
  *)
    echo "usage: s4l_ctl.sh {status|count|drafts|approve <n...>|approve-all} [--yes]" >&2; exit 64 ;;
esac
