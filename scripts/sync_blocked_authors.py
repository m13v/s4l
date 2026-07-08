#!/usr/bin/env python3
"""Reconcile author_blocklist with every confirmed X-told-us-blocked signal.

LOCAL OPERATOR USE ONLY. Uses db_direct for reads (see its docstring: excluded
from the published npm package), so this script is excluded too (see
package.json `files`) and only ever runs from this repo checkout via its own
launchd job (launchd/com.m13v.social-twitter-blocklist-sync.plist) -- it is
NOT part of the shipped customer pipeline.

Why this exists (2026-07-08): twitter_browser._probe_author_block correctly
detects a live "this author has blocked us" signal and twitter_post_plan.py
marks that ONE tweet permanently skipped, but nothing ever promoted the
AUTHOR into author_blocklist, so future tweets from the same handle could
still be drafted and re-attempted. This script is the missing link, run on a
schedule so new detections get account-blocklisted within minutes.

Two sources of truth, both read-only:
  1. twitter_candidates.skip_reason = 'blocked_by_author' -- the clean,
     current-state signal.
  2. skill/logs/post-results.jsonl candidate_results with
     reason == "blocked_by_author" -- catches detections that never made it
     into (1) because a racing cycle had already flipped the same row to
     'skipped' for an unrelated reason moments earlier (candidate 345517,
     @apistudies, 2026-07-07: a peer cycle logged "Twitter browser locked...
     giving up" at 01:33:21 UTC; this cycle's real reply attempt discovered
     the block at 01:50:16 UTC, but its mark_skipped call 404'd against the
     already-non-pending row and twitter_post_plan.update_candidate's
     ok_on_404 fallback silently treated that as "nothing more to do"). The
     2026-07-08 fix to /api/v1/twitter-candidates/by-id lets a later
     blocked_by_author result overwrite an earlier non-blocked skip reason,
     but this log cross-check stays as a backstop for anything that still
     slips through.

For every author found by either source who isn't already hard-blocked on
X, POST /api/v1/blocklist (classification=blocked_by_author, severity=hard,
added_by=block_probe).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post, load_env  # noqa: E402

REPO_DIR = Path(__file__).resolve().parent.parent
POST_RESULTS_LOG = REPO_DIR / "skill" / "logs" / "post-results.jsonl"


def _already_blocked_handles() -> set[str]:
    resp = api_get("/api/v1/blocklist", query={"platform": "x", "severity": "hard"})
    rows = ((resp or {}).get("data") or {}).get("rows") or []
    return {(r.get("handle") or "").strip().lower() for r in rows}


def _from_db() -> dict[str, dict]:
    """skip_reason='blocked_by_author' rows, keyed by lowercase handle."""
    from db_direct import get_conn
    found: dict[str, dict] = {}
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT id, author_handle, tweet_url, skipped_at FROM twitter_candidates "
            "WHERE skip_reason = 'blocked_by_author' AND author_handle IS NOT NULL "
            "ORDER BY skipped_at ASC"
        )
        for row in cur.fetchall():
            handle = (row["author_handle"] or "").strip().lstrip("@").lower()
            if handle:
                found[handle] = dict(row)
    finally:
        conn.close()
    return found


def _from_post_results_log() -> dict[str, dict]:
    """blocked_by_author candidate_ids logged in post-results.jsonl but not
    necessarily reflected in the DB's current skip_reason (race-swallow)."""
    if not POST_RESULTS_LOG.exists():
        return {}
    cids: dict[int, str] = {}
    with open(POST_RESULTS_LOG) as f:
        for line in f:
            line = line.strip()
            if not line or "blocked_by_author" not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for cr in rec.get("candidate_results") or []:
                if cr.get("reason") == "blocked_by_author" and cr.get("candidate_id"):
                    cids[int(cr["candidate_id"])] = rec.get("at")
    if not cids:
        return {}
    from db_direct import get_conn
    found: dict[str, dict] = {}
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT id, author_handle, tweet_url FROM twitter_candidates "
            "WHERE id = ANY(%s)",
            [list(cids.keys())],
        )
        for row in cur.fetchall():
            handle = (row["author_handle"] or "").strip().lstrip("@").lower()
            if handle:
                found[handle] = {**dict(row), "skipped_at": cids.get(row["id"])}
    finally:
        conn.close()
    return found


def main() -> None:
    load_env()
    dry_run = "--dry-run" in sys.argv
    already = _already_blocked_handles()
    candidates = {**_from_db(), **_from_post_results_log()}
    new_count = 0
    for handle, info in sorted(candidates.items()):
        if handle in already:
            continue
        new_count += 1
        reason = (
            f"X reported this author has blocked our account during a live "
            f"reply attempt on {info.get('tweet_url')} (candidate {info.get('id')}, "
            f"detected {info.get('skipped_at')})"
        )
        print(f"[sync_blocked_authors] {'would block' if dry_run else 'blocking'} "
              f"@{handle}: {reason}", flush=True)
        if dry_run:
            continue
        try:
            resp = api_post("/api/v1/blocklist", body={
                "platform": "x",
                "handle": handle,
                "classification": "blocked_by_author",
                "severity": "hard",
                "reason": reason,
                "added_by": "block_probe",
            })
            action = ((resp or {}).get("data") or {}).get("action", "?")
            print(f"[sync_blocked_authors] @{handle}: {action}", flush=True)
        except SystemExit as e:
            print(f"[sync_blocked_authors] @{handle} failed (non-fatal): {e}",
                  file=sys.stderr, flush=True)
    print(f"[sync_blocked_authors] done: {new_count} newly blocked, "
          f"{len(candidates) - new_count} already present", flush=True)


if __name__ == "__main__":
    main()
