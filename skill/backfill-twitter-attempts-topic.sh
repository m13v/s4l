#!/bin/bash
# Periodic backfill of twitter_search_attempts.search_topic from
# twitter_candidates (Pass A) + (batch_id, project_name) fanout (Pass B).
# Runs every 5 minutes via com.m13v.social-twitter-attempt-topic-backfill.
# Idempotent: each pass only touches rows where search_topic IS NULL, so
# repeated runs converge to zero work once the topics are populated.
#
# Why this exists: skill/run-twitter-cycle.sh and score_twitter_candidates.py
# are both chflags-locked. The picker (pick_search_topic.py) stamps
# twitter_candidates.search_topic when it scores tweets, but the attempt row
# stays NULL because the SCAN_SCHEMA in the locked shell doesn't carry the
# topic through queries_used. This script closes that gap.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/Users/matthewdi/social-autoposter}"
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.11}"

cd "$REPO_DIR"
exec "$PYTHON_BIN" scripts/backfill_twitter_attempts_topic.py --days 14
