#!/bin/bash
# linkedin-recovery.sh — hourly auto-recovery for the LinkedIn killswitch.
#
# Problem this solves: when LinkedIn logs us out / returns an authwall, the
# killswitch (scripts/linkedin_killswitch.py) engages and every LinkedIn
# pipeline self-aborts at startup until the session is restored and the flag is
# cleared. We want that restore to happen on its own when it safely can.
#
# This job, fired hourly by launchd (com.m13v.social-linkedin-recovery), runs the
# state machine in scripts/linkedin_killswitch.py. `recover-check` decides what
# (if anything) to do this hour and prints the MODE on stdout:
#
#   (nothing)  inactive / terminal / too young / mid-hold -> exit, no Chrome.
#   "login"    active >= LINKEDIN_RECOVERY_MIN_AGE_HOURS (default 24h): spin up a
#              Claude session that drives the REAL harness Chrome to actually log
#              back in (the allowed pattern; scripted Python login is the banned
#              one), then record the verdict:
#                held       -> login worked; enter a pending-hold window and
#                              re-verify later that it STUCK before resuming.
#                hard_block -> checkpoint / captcha / restriction / wrong creds /
#                              2FA: STOP completely, email, never auto-retry.
#                transient  -> ambiguous; re-anchor the 24h clock and try again
#                              later, up to LINKEDIN_RECOVERY_TRANSIENT_MAX_ATTEMPTS.
#   "hold"     a prior login succeeded and the hold window elapsed: run the
#              read-only `recover-hold` re-verify (no Claude, no login).
#                healthy          -> clear the flag, the fleet resumes.
#                dropped/logged-out -> "it didn't hold" -> STOP completely, email.
#
# The 24h wait + single-attempt-then-stop is the anti-bot rule: we never hammer
# the login wall, and we never keep re-poking a session that won't hold.
#
# When the flag clears, the six LinkedIn launchd jobs resume on their next fire
# (they all gate on the killswitch file). There is NO launchctl load/unload.
#
# This script is a no-op (instant exit, no Chrome) on every hour there is nothing
# eligible to do, so it is safe to leave loaded.

set -uo pipefail
export PATH="/opt/homebrew/bin:$PATH"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/linkedin-recovery.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG" >&2; }

PY="/opt/homebrew/bin/python3"
[ -x "$PY" ] || PY="/usr/bin/python3"

KS="$REPO_DIR/scripts/linkedin_killswitch.py"

# Gate + mode. recover-check prints "login" or "hold" on stdout when there is
# work to do (exit 0); exit !=0 means nothing eligible this hour.
MODE="$("$PY" "$KS" recover-check 2>>"$LOG")" || exit 0
MODE="$(printf '%s' "$MODE" | tr -d '[:space:]')"
log "recover-check eligible; mode=${MODE:-?}"

# linkedin-backend.sh exports LINKEDIN_CDP_URL + LINKEDIN_DISCOVER_PYTHON +
# MCP_CONFIG_FILE and provides ensure_linkedin_browser_for_backend (launches the
# port-9556 harness Chrome and acquires the cross-pipeline lock).
export S4L_PIPELINE_NAME="linkedin-recovery"
# shellcheck disable=SC1091
source "$REPO_DIR/skill/lib/linkedin-backend.sh"

if ! ensure_linkedin_browser_for_backend; then
    log "ERROR: could not bring up linkedin-harness Chrome; will retry next hour"
    exit 0
fi

# The read-only probe needs a Playwright-capable interpreter (3.14 lacks it; the
# backend resolves a working one into LINKEDIN_DISCOVER_PYTHON).
PROBE_PY="${LINKEDIN_DISCOVER_PYTHON:-$PY}"

# ---------------------------------------------------------------------------
# MODE: hold — read-only re-verify that a prior successful login actually stuck.
# ---------------------------------------------------------------------------
if [ "$MODE" = "hold" ]; then
    RESULT="$("$PROBE_PY" "$KS" recover-hold --cdp-url "$LINKEDIN_CDP_URL" 2>>"$LOG")"
    log "recover-hold result: $RESULT"
    exit 0
fi

if [ "$MODE" != "login" ]; then
    log "unrecognized recover-check mode '${MODE:-}'; nothing to do"
    exit 0
fi

# ---------------------------------------------------------------------------
# MODE: login — Claude-driven re-login attempt against the real harness Chrome.
# ---------------------------------------------------------------------------
# Credentials live in the login keychain (service "LinkedIn m13v"). Exact service
# name, so a direct lookup is correct here (the auth skill is for interactive,
# fuzzy lookups, not unattended scripts).
LI_EMAIL="$(security find-generic-password -s 'LinkedIn m13v' -g 2>&1 \
    | sed -n 's/^[[:space:]]*"acct"<blob>="\(.*\)"$/\1/p')"
LI_PASSWORD="$(security find-generic-password -s 'LinkedIn m13v' -w 2>/dev/null)"
[ -n "$LI_EMAIL" ] || LI_EMAIL="i@m13v.com"

if [ -z "$LI_PASSWORD" ]; then
    log "ERROR: LinkedIn password unreadable from keychain (locked under launchd?); recording transient"
    "$PY" "$KS" recover-record --verdict transient \
        --detail "keychain password unavailable under launchd" --no-email >>"$LOG" 2>&1
    exit 0
fi

PROMPT_FILE="$(mktemp -t li-relogin.XXXXXX)"
chmod 600 "$PROMPT_FILE"
CLAUDE_OUT="$LOG_DIR/linkedin-recovery-login-$(date +%Y-%m-%d_%H%M%S).out"

# PASSWORD LEAK PREVENTION: the cleartext password must never land in the prompt,
# the Claude tool-call args (stream-json), the session transcript under
# ~/.claude/projects, the session-log copy under skill/logs, or $CLAUDE_OUT.
# So we write it to its own 0600 temp file and have the harness script READ it at
# RUNTIME (open(PW_FILE).read()) and feed it to type_text(pw). The bh_run script
# text that gets recorded contains only the file PATH and `type_text(pw)`, never
# the secret. The harness CLI runs as this same user (server.py does
# env=os.environ.copy()), so the read succeeds. Both temps are shredded below.
PW_FILE="$(mktemp -t li-pw.XXXXXX)"
chmod 600 "$PW_FILE"
printf '%s' "$LI_PASSWORD" > "$PW_FILE"
unset LI_PASSWORD

cat > "$PROMPT_FILE" <<PROMPT_EOF
You are recovering a LOGGED-OUT LinkedIn session inside a real Google Chrome
(the linkedin-harness, CDP-driven, port 9556). Your job: attempt ONE login and
report exactly what happened. This is an authorized account-owner recovery.

You have ONE tool: mcp__linkedin-harness__bh_run(script) — it runs Python with
these helpers pre-imported:
  goto_url(url), wait_for_load(), page_info(), capture_screenshot(),
  js(expression), type_text(text), click_at_xy(x, y), press_key(key)
Reuse the existing tab: use goto_url() for your FIRST navigation as well.

HARD ANTI-BOT RULES (never break these):
- NEVER call /voyager/api/* (Python, fetch(), js()). Internal backend = restriction.
- No scroll-and-expand loops, no opening post permalinks. This is login ONLY.

CREDENTIALS (the account owner authorized this login):
  email:        $LI_EMAIL
  password:     stored in the file at this path, read it at runtime:
                $PW_FILE

SECRET-HANDLING RULES (never break these):
- NEVER write the literal password anywhere: not in a bh_run script, not in a
  js() expression, not in your text output. The password is a SECRET.
- The ONLY way to use it: inside a single bh_run script, read it with
  pw = open("$PW_FILE").read().strip() and pass that VARIABLE to type_text(pw).
- Do NOT build a js() string that contains the password value (e.g. do NOT do
  js('...value="'+pw+'"...')). That would leak it. Use type_text(pw) only.

STEPS (make ONE login attempt only; do not retry, do not click around beyond the
login form):
1. bh_run('goto_url("https://www.linkedin.com/feed/"); wait_for_load()').
   Read bh_run('print(js("""return location.href"""))'). If it is a logged-in
   feed (URL contains /feed/ and NOT login / checkpoint / authwall), we are
   already logged in -> verdict "held".
2. Otherwise bh_run('goto_url("https://www.linkedin.com/login"); wait_for_load()').
   Type the email into the #username field (type_text), then focus the #password
   field and type the password, then click the "Sign in" submit button. Do it in
   ONE bh_run script so the password stays in a local variable, e.g.:
     pw = open("$PW_FILE").read().strip()
     # click the #username field via click_at_xy at the center of its
     # getBoundingClientRect, then type_text("$LI_EMAIL")
     # click the #password field the same way, then type_text(pw)
     # click the Sign in submit button
   Then bh_run wait_for_load().
3. Read bh_run('print(js("""return location.href"""))') AND
   bh_run('print(capture_screenshot())') (Read the PNG) and judge:
   - Landed on /feed/ or any logged-in linkedin page (not login/checkpoint/authwall)
     -> verdict "held".
   - A TEMPORARY restriction that states an explicit lift date/time, e.g.
     "your account is temporarily restricted until June 03 2026 4:05 PM PDT" or
     "you can try again on <date/time>" -> verdict "restricted_temp". Do NOT solve
     anything. In the detail you MUST include the lift time normalized to ISO 8601
     WITH the timezone offset, as a token "lift=<ISO8601>", e.g.:
       lift=2026-06-03T16:05:00-07:00
     (convert the displayed local time to ISO; PDT = -07:00, PST = -08:00, ET in
     summer = -04:00). Keep the rest of the detail short, e.g.
       temporary restriction for automated activity lift=2026-06-03T16:05:00-07:00
   - A checkpoint, captcha, "quick security check", "verify it's you", a phone or
     email verification code, a 2FA prompt, a PERMANENT/no-stated-time restriction,
     or a wrong-password error -> verdict "hard_block". Do NOT solve it, do NOT
     enter any codes. Put the specific reason in the detail.
   - The page failed to load, timed out, or you genuinely cannot tell -> verdict
     "transient".

NEVER print the password anywhere in your output.

FINAL OUTPUT: print EXACTLY one line and nothing after it, in this exact form
(verdict is one of held / hard_block / restricted_temp / transient; detail is a
short plain-ascii phrase with NO quotes and NO pipe characters; for
restricted_temp the detail MUST contain a lift=<ISO8601> token):
===LIVERDICT===<verdict>|<short detail>===END===
PROMPT_EOF

# Pre-assign the session UUID so we know exactly which transcript to scrub
# afterward (run_claude.sh honors a pre-set CLAUDE_SESSION_ID). AUP retries can
# still rotate it, so the scrub below is also content-based, not id-only.
export CLAUDE_SESSION_ID="$(uuidgen | tr 'A-Z' 'a-z')"

log "launching Claude re-login session (account=$LI_EMAIL); output -> $CLAUDE_OUT"
"$REPO_DIR/scripts/run_claude.sh" "linkedin-recovery-login" \
    --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" \
    --output-format stream-json --verbose \
    -p "$(cat "$PROMPT_FILE")" >"$CLAUDE_OUT" 2>>"$LOG"
CLAUDE_RC=$?
log "Claude re-login session exited rc=$CLAUDE_RC"

# DEFENSE-IN-DEPTH SCRUB. The prompt tells Claude to read the password from a
# file and feed it to type_text(pw), so the secret should never reach any
# transcript. But a disobedient model could still type/echo the literal, so we
# redact any occurrence of it from every on-disk surface before deleting the
# password file: $CLAUDE_OUT, the session transcript under ~/.claude/projects,
# and the archived session-log copy under skill/logs/claude-sessions. The
# password is passed to the scrubber via env (SCRUB_PW), never argv (ps leak).
SCRUB_PW="$(cat "$PW_FILE")" SCRUB_OUT="$CLAUDE_OUT" SCRUB_LOGDIR="$LOG_DIR" \
    "$PY" - <<'PYEOF' >>"$LOG" 2>&1
import os, glob, time
pw = os.environ.get("SCRUB_PW", "")
if pw and len(pw) >= 4:
    home = os.path.expanduser("~")
    targets = []
    if os.environ.get("SCRUB_OUT"):
        targets.append(os.environ["SCRUB_OUT"])
    proj = os.path.join(home, ".claude", "projects",
                        "-Users-matthewdi-social-autoposter")
    targets += glob.glob(os.path.join(proj, "*.jsonl"))
    logdir = os.environ.get("SCRUB_LOGDIR", "")
    if logdir:
        targets += glob.glob(os.path.join(logdir, "claude-sessions", "*", "*.jsonl"))
    cutoff = time.time() - 3600  # only touch files modified in the last hour
    redacted = 0
    for f in targets:
        try:
            if not os.path.isfile(f) or os.path.getmtime(f) < cutoff:
                continue
            data = open(f, encoding="utf-8", errors="replace").read()
            if pw in data:
                open(f, "w", encoding="utf-8").write(data.replace(pw, "[REDACTED_PW]"))
                redacted += 1
        except Exception:
            pass
    print(f"[scrub] checked {len(targets)} file(s); redacted password in {redacted}")
PYEOF

# Shred the password file (best-effort overwrite, then remove) and drop the prompt.
command -v gshred >/dev/null 2>&1 && gshred -u "$PW_FILE" 2>/dev/null
rm -f "$PW_FILE" "$PROMPT_FILE"

# Extract the sentinel verdict line (survives stream-json escaping: it carries no
# quotes/backslashes). Take the last match if the model printed more than one.
RAW="$("$PY" - "$CLAUDE_OUT" <<'PYEOF'
import re, sys
try:
    t = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except Exception:
    t = ""
m = re.findall(r"===LIVERDICT===(.*?)===END===", t, re.S)
print(m[-1].strip() if m else "")
PYEOF
)"

VERDICT="${RAW%%|*}"
DETAIL="${RAW#*|}"
VERDICT="$(printf '%s' "$VERDICT" | tr -d '[:space:]')"
[ "$DETAIL" = "$RAW" ] && DETAIL=""   # no pipe present
DETAIL="$(printf '%s' "$DETAIL" | tr -d '\n\r' | cut -c1-300)"

case "$VERDICT" in
    held|hard_block|restricted_temp|transient) ;;
    *)
        log "no usable verdict parsed (raw='$RAW', rc=$CLAUDE_RC); treating as transient"
        VERDICT="transient"
        DETAIL="no verdict parsed from re-login session (rc=$CLAUDE_RC)"
        ;;
esac

log "re-login verdict=$VERDICT detail=$DETAIL"
RESULT="$("$PY" "$KS" recover-record --verdict "$VERDICT" --detail "$DETAIL" 2>>"$LOG")"
log "recover-record result: $RESULT"
exit 0
