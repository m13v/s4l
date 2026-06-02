#!/bin/bash
# invent-supply-test.sh — supply-test a batch of drafted Twitter queries for the
# topic-invention job (scripts/invent_topics.py).
#
# Mirrors run-twitter-cycle.sh's Phase 1 lean scan loop, but stands alone so the
# hourly invent job can measure how much FRESH (6h) supply each freshly-drafted
# query returns BEFORE committing its parent topic. It:
#
#   1. Acquires the same "twitter-browser" mkdir-lock the cycle uses, so an
#      invent run and a 15-min cycle never fight over the managed Chrome on
#      port 9555. Short timeout (default 600s): if the cycle is mid-scan we'd
#      rather skip this hour than block. acquire_lock exits 0 on timeout, which
#      leaves SCAN_OUT empty and signals "untested" back to Python.
#   2. Runs ONE browser-harness -c invocation that loops twitter_scan.scan()
#      over every query, writing one JSONL record per query to SCAN_OUT (via the
#      SCAN_TWEETS_FILE env the scan module already honors). scan() writes a
#      record even on a zero-tweet result, so a present-but-empty tweets array
#      is a real "tested, 0 supply" signal, distinct from a missing record.
#   3. Releases the lock.
#
# Usage:
#   invent-supply-test.sh <queries_json> <scan_out> [freshness_hours] [lock_timeout_s]
#
#   queries_json : path to a JSON array of {project, query, search_topic}
#   scan_out     : path the per-query JSONL results are written to (truncated first)
#   freshness_hours (default 6)
#   lock_timeout_s  (default 600)
#
# Exit status is always 0 on a clean run (tested or skipped); Python decides
# tested-vs-untested by whether SCAN_OUT has records. Mirrors the cycle's
# fail-open posture so a transient browser hiccup never crashes the invent job.
set -uo pipefail

REPO_DIR="$HOME/social-autoposter"
HARNESS_BIN="$HOME/.local/bin/browser-harness"

QUERIES_JSON="${1:?queries_json path required}"
SCAN_OUT="${2:?scan_out path required}"
FRESHNESS_HOURS="${3:-6}"
LOCK_TIMEOUT="${4:-600}"

if [ ! -s "$QUERIES_JSON" ]; then
  echo "[invent-supply-test] queries json missing/empty: $QUERIES_JSON" >&2
  exit 0
fi
if [ ! -x "$HARNESS_BIN" ]; then
  echo "[invent-supply-test] browser-harness not found at $HARNESS_BIN" >&2
  exit 0
fi

# Truncate the output so a stale file from a prior run can't masquerade as
# fresh results. Python treats an empty SCAN_OUT as "untested this run".
: > "$SCAN_OUT"

# Source the shared lock helpers (functions only; no lock acquired on source).
# shellcheck disable=SC1091
source "$REPO_DIR/skill/lock.sh"

# acquire_lock exits 0 if the lock can't be taken within LOCK_TIMEOUT, which
# unwinds this whole script — leaving SCAN_OUT empty. That's the intended
# "skip this hour" path: the cycle owns the browser right now.
echo "[invent-supply-test] acquiring twitter-browser lock (timeout=${LOCK_TIMEOUT}s)..." >&2
acquire_lock "twitter-browser" "$LOCK_TIMEOUT"
echo "[invent-supply-test] twitter-browser lock held (pid=$$)" >&2

# One harness invocation handles every query so we pay the CLI startup once.
# Each scan() call appends a JSONL record to SCAN_TWEETS_FILE=$SCAN_OUT.
# browser-harness upstream main reads the script from STDIN (the `-c` flag was
# removed). Feed the body via a quoted heredoc and pass $REPO_DIR / $QUERIES_JSON
# through the environment so the Python reads them from os.environ.
BU_NAME=twitter-harness BU_CDP_URL=http://127.0.0.1:9555 \
SCAN_TWEETS_FILE="$SCAN_OUT" \
BATCH_ID="${BATCH_ID:-}" \
FRESHNESS_HOURS_DISCOVER="$FRESHNESS_HOURS" \
REPO_DIR="$REPO_DIR" \
QUERIES_JSON="$QUERIES_JSON" \
  "$HARNESS_BIN" <<'PY' 2>&1
import sys, json, os, time
sys.path.insert(0, os.environ['REPO_DIR'] + '/scripts')
from twitter_scan import scan
queries = json.load(open(os.environ['QUERIES_JSON']))
freshness = int(os.environ.get('FRESHNESS_HOURS_DISCOVER', '6'))
for q in queries:
    project = q.get('project', '')
    query = q.get('query', '')
    topic = q.get('search_topic', '')
    t0 = time.time()
    try:
        kept = scan(query=query, project=project, search_topic=topic,
                    freshness_hours=freshness)
        dt = time.time() - t0
        print(f'  ok  project={project!r}  q={query[:50]!r}  kept={len(kept)}  in {dt:.1f}s', flush=True)
    except Exception as e:
        dt = time.time() - t0
        print(f'  err project={project!r}  q={query[:50]!r}  in {dt:.1f}s  {type(e).__name__}: {e}', flush=True)
PY

release_lock "twitter-browser"
echo "[invent-supply-test] done; results in $SCAN_OUT" >&2
exit 0
