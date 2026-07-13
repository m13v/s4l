#!/usr/bin/env bash
# run-linkedin-unipile.sh: LinkedIn "post comments" pipeline via the UniPile API
# (search posts, then post a comment). This is the UniPile-backed counterpart to
# the browser-driven run-linkedin.sh. It talks ONLY to the UniPile REST API
# through scripts/linkedin_unipile.py: no browser, no linkedin-agent, no browser
# lock, no killswitch gate (there is no browser session to compromise here).
#
# "post comments" means searching for posts and commenting on them (outbound).
# This is NOT the "engage" pipeline (engage-linkedin.sh, which replies to replies
# left on our own comments).
#
# Default behavior is DRY RUN: it probes credentials, searches, and prints the
# comment it *would* post. A posted comment is visible to real people, so it only
# publishes when you pass --post AND --text.
#
# Usage:
#   ./run-linkedin-unipile.sh --keywords "ai agents" --date-posted past_week --limit 5
#   ./run-linkedin-unipile.sh --keywords "..." --text "nice breakdown" --post
#   ./run-linkedin-unipile.sh --social-id "urn:li:activity:NNN" --text "..." --post
#
# Creds: the live UniPile account is under matt@mediar.ai (keychain entries scoped
# to that account; DSN api45.unipile.com:17570, account_id wHDpysUnRbm7Q0lvyv9pQQ).
# An older i@m13v.com trial shares the same service names but is dead.
# linkedin_unipile.py reads the mediar-scoped entry; we also pin UNIPILE_DSN here
# as a default (override by exporting it).

set -euo pipefail

: "${UNIPILE_DSN:=api45.unipile.com:17570}"
export UNIPILE_DSN

REPO_DIR="$HOME/social-autoposter"
PY="$REPO_DIR/scripts/linkedin_unipile.py"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/engage-linkedin-unipile-$(date +%Y-%m-%d_%H%M%S).log"

KEYWORDS=""
SEARCH_URL=""
DATE_POSTED="past_week"
SORT_BY="date"
LIMIT=5
TEXT=""
SOCIAL_ID=""
REPLY_TO=""
DO_POST=0

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --keywords)     KEYWORDS="$2"; shift 2 ;;
        --url)          SEARCH_URL="$2"; shift 2 ;;
        --date-posted)  DATE_POSTED="$2"; shift 2 ;;
        --sort-by)      SORT_BY="$2"; shift 2 ;;
        --limit)        LIMIT="$2"; shift 2 ;;
        --text)         TEXT="$2"; shift 2 ;;
        --social-id)    SOCIAL_ID="$2"; shift 2 ;;
        --reply-to)     REPLY_TO="$2"; shift 2 ;;
        --post)         DO_POST=1; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

log "=== UniPile LinkedIn post comments (search, then comment) ==="
log "DSN=$UNIPILE_DSN  dry_run=$([ "$DO_POST" -eq 1 ] && echo no || echo YES)"

# ── Step 1: validate credentials ──────────────────────────────────────────
log "Step 1: probing credentials (/accounts)..."
if ! python3 "$PY" probe 2>>"$LOG_FILE" | tee -a "$LOG_FILE"; then
    log "CREDENTIAL PROBE FAILED. UniPile rejected the API key (see above)."
    log "Fix: the live account is matt@mediar.ai (the i@m13v.com trial is dead). Refresh the"
    log "     subscription if needed, then store the key under the mediar-scoped entry:"
    log "     security add-generic-password -U -s unipile-api-key -a matt@mediar.ai -w '<KEY>'"
    log "     and DSN:   security add-generic-password -U -s unipile-dsn -a matt@mediar.ai -w 'apiNN.unipile.com:PORT'"
    exit 1
fi

# ── Step 2: resolve a target post ─────────────────────────────────────────
# Either the caller named a --social-id directly, or we search and take the top
# result. We always print the search results so the run is auditable.
if [ -z "$SOCIAL_ID" ]; then
    if [ -z "$KEYWORDS" ] && [ -z "$SEARCH_URL" ]; then
        log "Nothing to do: pass --keywords/--url to search, or --social-id to target a post."
        exit 2
    fi
    log "Step 2: searching posts (keywords='${KEYWORDS}' url='${SEARCH_URL}' date=$DATE_POSTED limit=$LIMIT)..."
    SEARCH_ARGS=(search --limit "$LIMIT" --sort-by "$SORT_BY")
    [ -n "$KEYWORDS" ]    && SEARCH_ARGS+=(--keywords "$KEYWORDS" --date-posted "$DATE_POSTED")
    [ -n "$SEARCH_URL" ]  && SEARCH_ARGS+=(--url "$SEARCH_URL")
    SEARCH_JSON=$(python3 "$PY" "${SEARCH_ARGS[@]}" 2>>"$LOG_FILE") || {
        log "SEARCH FAILED (see log)."; exit 1; }
    echo "$SEARCH_JSON" | tee -a "$LOG_FILE"
    SOCIAL_ID=$(echo "$SEARCH_JSON" | python3 -c "import json,sys; items=json.load(sys.stdin).get('items',[]); print(items[0]['social_id'] if items and items[0].get('social_id') else '')")
    if [ -z "$SOCIAL_ID" ]; then
        log "No usable post (no social_id in top result). Stopping before comment."
        exit 0
    fi
    log "Top result social_id: $SOCIAL_ID"
else
    log "Step 2: skipping search, using provided --social-id $SOCIAL_ID"
fi

# ── Step 3: comment (or dry-run) ──────────────────────────────────────────
if [ -z "$TEXT" ]; then
    log "No --text provided; nothing to post. Target post: https://www.linkedin.com/feed/update/$SOCIAL_ID/"
    log "Done (search-only)."
    exit 0
fi

if [ "$DO_POST" -ne 1 ]; then
    log "DRY RUN — would comment on $SOCIAL_ID:"
    log "  \"$TEXT\""
    log "Re-run with --post to actually publish. (No API write performed.)"
    exit 0
fi

log "Step 3: posting comment on $SOCIAL_ID ..."
COMMENT_ARGS=(comment --social-id "$SOCIAL_ID" --text "$TEXT")
[ -n "$REPLY_TO" ] && COMMENT_ARGS+=(--reply-to "$REPLY_TO")
if python3 "$PY" "${COMMENT_ARGS[@]}" 2>>"$LOG_FILE" | tee -a "$LOG_FILE"; then
    log "Comment posted. Watch the m13v LinkedIn account for any flag/challenge over the next 24-48h."
else
    log "COMMENT FAILED (see log)."
    exit 1
fi

log "=== Done ==="
