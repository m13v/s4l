#!/usr/bin/env bash
# check-web-chats.sh, poll Postgres for unread web-chat messages and spawn one
# Claude session per visitor. Called by launchd every 15 seconds.
#
# Mirror of ~/fazm/inbox/skill/check-founder-chat.sh: same lock pattern, same
# claim/cooldown/retry/rate-limit guardrails, same email-summary escalation.
# The only difference is the data layer: Postgres web_chat_threads /
# web_chat_messages instead of Firestore founder_chats.

set -euo pipefail

# Ensure Homebrew bins (gtimeout, jq) AND the user's npm-global bin (claude)
# are findable regardless of how the script is invoked. Launchd has these via
# the plist's PATH; manual / sandboxed shells may not.
export PATH="/Users/matthewdi/.nvm/versions/node/v20.19.4/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

source "$(dirname "$0")/lock.sh"
acquire_lock "check-web-chats" 60

# DB access is HTTP-only via scripts/http_api.py -> s4l.ai /api/v1/web-chat/*.
# No DATABASE_URL needed here any more.

# send-email.js needs RESEND_API_KEY + the analytics node_modules.
ANALYTICS_ENV="$HOME/analytics/.env.production.local"
if [ -f "$ANALYTICS_ENV" ]; then
    export RESEND_API_KEY=$(grep '^RESEND_API_KEY=' "$ANALYTICS_ENV" | sed 's/^RESEND_API_KEY=//' | tr -d '"' | tr -d '\\n')
fi
export NODE_PATH="$HOME/analytics/node_modules"

REPO_DIR="$HOME/social-autoposter"
SCRIPTS_DIR="$REPO_DIR/scripts"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.11}"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="/usr/bin/python3"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG_DIR/web-chat.log"; }

# Step 1: query Postgres for unread threads.
CHATS=$("$PYTHON_BIN" "$SCRIPTS_DIR/check_unread_web_chats.py" 2>>"$LOG_DIR/web-chat.log")
if [ "$CHATS" = "[]" ] || [ -z "$CHATS" ]; then
    exit 0
fi

NUM=$(echo "$CHATS" | "$PYTHON_BIN" -c "import json,sys; print(len(json.load(sys.stdin)))")

for i in $(seq 0 $((NUM - 1))); do
    THREAD_ID=$(echo "$CHATS" | "$PYTHON_BIN" -c "import json,sys; print(json.load(sys.stdin)[$i]['thread_id'])")
    PROJECT=$(echo "$CHATS" | "$PYTHON_BIN" -c "import json,sys; print(json.load(sys.stdin)[$i]['project'])")
    EMAIL=$(echo "$CHATS" | "$PYTHON_BIN" -c "import json,sys; print(json.load(sys.stdin)[$i].get('visitor_email',''))")
    NAME=$(echo "$CHATS" | "$PYTHON_BIN" -c "import json,sys; d=json.load(sys.stdin)[$i]; print(d.get('visitor_name') or d.get('visitor_email') or 'visitor')")
    UNREAD=$(echo "$CHATS" | "$PYTHON_BIN" -c "import json,sys; print(json.load(sys.stdin)[$i]['unread'])")
    PAGE_URL=$(echo "$CHATS" | "$PYTHON_BIN" -c "import json,sys; print(json.load(sys.stdin)[$i].get('page_url',''))")

    PID_FILE="/tmp/web-chat-${THREAD_ID}.pid"

    # Rate-limit circuit breaker (mirror Fazm /tmp/fazm-chat-ratelimit).
    if [ -f "/tmp/web-chat-ratelimit" ]; then
        RL_TS=$(awk '{print $2}' /tmp/web-chat-ratelimit 2>/dev/null || echo "0")
        NOW_TS=$(date +%s)
        if [ $((NOW_TS - RL_TS)) -lt 3600 ]; then
            continue
        else
            rm -f /tmp/web-chat-ratelimit
        fi
    fi

    # Skip if a Claude session is already alive for this thread.
    if [ -f "$PID_FILE" ]; then
        EXISTING_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
            log "Session already active for $PROJECT/$THREAD_ID (pid $EXISTING_PID), skipping"
            continue
        fi
        rm -f "$PID_FILE"
    fi

    log "Spawning session for $PROJECT/$THREAD_ID ($EMAIL, $UNREAD unread)"

    # Cooldown check (mirror claim-chat --check-only).
    if ! "$PYTHON_BIN" "$SCRIPTS_DIR/claim_web_chat.py" "$THREAD_ID" --check-only 2>>"$LOG_DIR/web-chat.log"; then
        log "Thread $THREAD_ID in cooldown, skipping"
        continue
    fi

    # Claim (resets unread, sets 5-min cooldown).
    "$PYTHON_BIN" "$SCRIPTS_DIR/claim_web_chat.py" "$THREAD_ID" 2>>"$LOG_DIR/web-chat.log" \
        || log "WARNING: claim failed for $THREAD_ID"

    # Build prompt (history is dumped from Postgres for freshness).
    HISTORY_JSON=$("$PYTHON_BIN" "$SCRIPTS_DIR/dump_web_chat_history.py" --thread "$THREAD_ID")

    # Pull this project's config block to give Claude context.
    PROJECT_CFG=$(/opt/homebrew/bin/jq --arg n "$PROJECT" '.projects[] | select(.name==$n)' "$REPO_DIR/config.json" 2>/dev/null || echo "{}")

    PROMPT_FILE=$(mktemp)
    cat > "$PROMPT_FILE" <<PROMPT_EOF
Read ~/social-autoposter/skill/WEB-CHAT-SKILL.md for the workflow.
Read ~/social-autoposter/skill/WEB-CHAT-VOICE.md for tone rules.

## Web chat to handle

PROJECT: $PROJECT
THREAD_ID: $THREAD_ID
VISITOR_EMAIL: $EMAIL
VISITOR_NAME: $NAME
PAGE_URL: $PAGE_URL
UNREAD MESSAGES: $UNREAD

## Project config (config.json)
\`\`\`json
$PROJECT_CFG
\`\`\`

## Full conversation history (from Postgres)
\`\`\`json
$HISTORY_JSON
\`\`\`

Process this chat now. Follow WEB-CHAT-SKILL.md exactly.
Remember to remove the PID file /tmp/web-chat-${THREAD_ID}.pid when done.
PROMPT_EOF

    SESSION_LOG="$LOG_DIR/web-chat-session-${THREAD_ID}-$(date +%Y%m%d_%H%M%S).log"
    FAIL_COUNT_FILE="/tmp/web-chat-fail-${THREAD_ID}"

    (
        set +e
        cd "$REPO_DIR"
        echo "[$(date)] Starting Claude session for $PROJECT/$THREAD_ID ($EMAIL)" >> "$SESSION_LOG"
        gtimeout 1200 claude \
            -p "$(cat "$PROMPT_FILE")" \
            --dangerously-skip-permissions \
            >> "$SESSION_LOG" 2>&1
        EXIT_CODE=$?
        echo "[$(date)] Claude exited with code $EXIT_CODE" >> "$SESSION_LOG"

        if [ $EXIT_CODE -ne 0 ]; then
            echo "[$(date)] WARN: session for $THREAD_ID exited with $EXIT_CODE" >> "$LOG_DIR/web-chat.log"

            # Detect persistent-error states that won't recover with quick retry:
            # rate limits, credit/billing, auth/quota, account-level issues.
            # All trip the same 1h pause; the next cycle re-tries automatically.
            # Pending threads stay in Postgres (unread_by_founder>0) so nothing is
            # ever lost; the launchd poller picks them up the moment the 1h
            # marker expires. No human notification — the log line is enough.
            PAUSE_PATTERNS='hit your limit|rate limit|rate.limited|too many requests|usage limit|weekly limit|5.hour limit|credit balance|out of credit|insufficient (credit|funds|balance)|payment required|billing|quota exceeded|api[- ]?key|unauthori[sz]ed|forbidden|account.{0,30}(suspend|disabled)|HTTP 401|HTTP 403|HTTP 429|invalid.*x.api.key'
            if grep -qiE "$PAUSE_PATTERNS" "$SESSION_LOG" 2>/dev/null; then
                echo "[$(date)] PERSISTENT ERROR on $THREAD_ID (rate limit / credits / auth), pausing all spawns for 1h" >> "$LOG_DIR/web-chat.log"
                echo "rate_limited $(date +%s)" > "/tmp/web-chat-ratelimit"
                rm -f "$PROMPT_FILE" "$PID_FILE" "$FAIL_COUNT_FILE"
                exit 0
            fi

            FAILS=0
            [ -f "$FAIL_COUNT_FILE" ] && FAILS=$(cat "$FAIL_COUNT_FILE" 2>/dev/null || echo "0")
            FAILS=$((FAILS + 1))
            echo "$FAILS" > "$FAIL_COUNT_FILE"

            if [ "$FAILS" -ge 3 ]; then
                echo "[$(date)] GIVING UP on $THREAD_ID after $FAILS fails" >> "$LOG_DIR/web-chat.log"
                rm -f "$FAIL_COUNT_FILE"
                # Leave claimed so it stops retrying.
            else
                "$PYTHON_BIN" "$SCRIPTS_DIR/unclaim_web_chat.py" "$THREAD_ID" >> "$LOG_DIR/web-chat.log" 2>&1
                echo "[$(date)] Unclaimed $THREAD_ID (retry $FAILS/3)" >> "$LOG_DIR/web-chat.log"
            fi
        else
            # Claude finished cleanly (replied OR explicitly skipped). Stamp
            # processed_at so the recovery query in check_unread_web_chats.py
            # won't re-flag this thread next cycle. Without this, threads where
            # Claude legitimately skipped (smoke test, off-topic, no useful
            # answer) loop every 5min for 24h, since last_message_sender stays
            # 'visitor' (no agent message inserted on skip).
            "$PYTHON_BIN" "$SCRIPTS_DIR/mark_web_chat_processed.py" "$THREAD_ID" >> "$LOG_DIR/web-chat.log" 2>&1
            rm -f "$FAIL_COUNT_FILE"
        fi

        # No-output guard (silent rate limits sometimes). If Claude exited 0
        # but produced almost no output, treat as a silent failure: unclaim
        # so the next cycle retries via the main unread>0 path. The
        # processed_at stamp above is harmless here because the main SELECT
        # gates on unread_by_founder>0, not on processed_at.
        LINE_COUNT=$(wc -l < "$SESSION_LOG" 2>/dev/null || echo "0")
        if [ "$LINE_COUNT" -le 2 ] && [ "$EXIT_CODE" -eq 0 ]; then
            echo "[$(date)] WARN: $THREAD_ID produced no output, unclaiming" >> "$LOG_DIR/web-chat.log"
            "$PYTHON_BIN" "$SCRIPTS_DIR/unclaim_web_chat.py" "$THREAD_ID" >> "$LOG_DIR/web-chat.log" 2>&1
        fi

        rm -f "$PROMPT_FILE" "$PID_FILE"
    ) &

    CLAUDE_PID=$!
    echo "$CLAUDE_PID" > "$PID_FILE"
    log "Started session for $PROJECT/$THREAD_ID (pid $CLAUDE_PID)"
done

# Trim log to last 2000 lines.
if [ -f "$LOG_DIR/web-chat.log" ]; then
    tail -2000 "$LOG_DIR/web-chat.log" > "$LOG_DIR/web-chat.log.tmp" 2>/dev/null && \
        mv "$LOG_DIR/web-chat.log.tmp" "$LOG_DIR/web-chat.log" || true
fi
