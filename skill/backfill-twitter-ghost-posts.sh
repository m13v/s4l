#!/bin/bash
# Periodic recovery of "ghost" Twitter posts: replies that landed on x.com but
# whose POST /api/v1/posts log call failed (rate-limit cap, transient 500, or
# timeout), so twitter_post_plan.py marked the candidate 'skipped' and reported
# log_post_no_id. The tweet is live; the DB forgot it.
#
# scripts/backfill_twitter_log_post_no_id.py reconstructs the missing posts rows
# from skill/logs/twitter-cycle-*.log. It is idempotent: the API dedups on
# (platform, thread_url), so already-recovered posts no-op.
#
# Runs every 30 min via com.m13v.social-twitter-ghost-backfill. We only scan the
# last 3 days of cycle logs (rolling window) to keep each run fast; the original
# 64 KB generation_trace outage (2026-05-12..13) was already backfilled once and
# the cap is now 1 MB, so the steady-state cause is rate-limit / transient only.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/Users/matthewdi/social-autoposter}"
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.11}"

# macOS date: 3 days ago, YYYY-MM-DD. (date -v is macOS-only; this job is Aqua.)
SINCE="$(date -v-3d +%Y-%m-%d)"

cd "$REPO_DIR"
exec "$PYTHON_BIN" scripts/backfill_twitter_log_post_no_id.py --since "$SINCE"
