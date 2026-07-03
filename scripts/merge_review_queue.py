#!/usr/bin/env python3
"""
merge_review_queue.py — deliver a DRAFT_ONLY cycle's plan into the approval cards.

The deterministic pipeline (run-twitter-cycle.sh DRAFT_ONLY) writes its drafts to
a per-batch plan file (/tmp/twitter_cycle_plan_<batch>.json) and prints
`DRAFT_ONLY_PLAN=<path>`. On a customer box NOTHING used to consume that — the
only writer of the review-queue cards was the (now-removed) host-draft
submit_drafts path. This script closes that gap: it merges the batch plan's
candidates into the single review-queue plan the menu-bar cards read, deduped by
thread/candidate URL, and refreshes the review-request marker the menu bar polls.

This is the SAME merge submit_drafts did, reimplemented in Python so the launchd
kicker (no node/MCP) can run it after the cycle. ONE pipeline, one set of cards.

Usage:
  merge_review_queue.py --plan /tmp/twitter_cycle_plan_<batch>.json [--project NAME]
  merge_review_queue.py --plan-from-marker '<stdout containing DRAFT_ONLY_PLAN=...>'

State dir (for review-request.json) honors $S4L_STATE_DIR; the review-queue plan
lives in $S4L_TMP_DIR or /tmp (matching the MCP's planPath()).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

REVIEW_QUEUE_ID = "review-queue"


def tmp_dir() -> str:
    return os.environ.get("S4L_TMP_DIR") or "/tmp"


def state_dir() -> str:
    return os.environ.get("S4L_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def plan_path(batch_id: str) -> str:
    return os.path.join(tmp_dir(), f"twitter_cycle_plan_{batch_id}.json")


def review_request_path() -> str:
    return os.path.join(state_dir(), "review-request.json")


def _atomic_write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _dedup_key(c: dict) -> str:
    """Match submit_drafts: dedup by the thread/candidate URL, else candidate_id."""
    for k in ("candidate_url", "tweet_url", "thread_url", "candidate_id"):
        v = c.get(k)
        if v:
            return str(v)
    # last resort: the reply text, so identical drafts don't double up
    return (c.get("reply_text") or "")[:120]


def _thread_url(c: dict) -> str:
    for k in ("candidate_url", "tweet_url", "thread_url"):
        v = c.get(k)
        if v:
            return str(v)
    return ""


# Discovery-time author/engagement fields stamped onto each plan candidate so the
# approval card can show them. All already captured on the twitter_candidates row
# by the discovery pipeline (and refreshed at T1); no scrape happens here.
STATS_KEYS = (
    "author_handle",
    "author_followers",
    "likes",
    "retweets",
    "replies",
    "views",
    "virality_score",
    "tweet_posted_at",
)


def _enrich_with_stats(cands: list) -> int:
    """Stamp a `stats` sidecar onto plan candidates that lack one, from the
    twitter_candidates rows the discovery pipeline already wrote. ONE listing
    call (/api/v1/twitter-candidates?tweet_urls=...) covers the whole queue.
    Best-effort: any failure (offline box, missing identity, API error) leaves
    candidates unstamped and NEVER blocks card delivery. Returns count stamped."""
    want = [c for c in cands if not c.get("stats") and not c.get("posted") and _thread_url(c)]
    if not want:
        return 0
    urls = sorted({_thread_url(c) for c in want})[:500]
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get

        resp = api_get(
            "/api/v1/twitter-candidates",
            query={"tweet_urls": ",".join(urls), "limit": 500},
        )
        rows = (resp.get("data") or {}).get("candidates") or []
    except BaseException as e:  # http_api raises SystemExit on terminal failure
        print(f"[merge_review_queue] stats enrichment skipped: {e}", file=sys.stderr)
        return 0
    by_url = {str(r.get("tweet_url")): r for r in rows if r.get("tweet_url")}
    stamped = 0
    for c in want:
        row = by_url.get(_thread_url(c))
        if not row:
            continue
        c["stats"] = {k: row.get(k) for k in STATS_KEYS}
        stamped += 1
    return stamped


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge a DRAFT_ONLY plan into the review-queue cards")
    ap.add_argument("--plan", help="path to the per-batch DRAFT_ONLY plan file")
    ap.add_argument(
        "--plan-from-marker",
        help="text containing a DRAFT_ONLY_PLAN=<path> marker (e.g. cycle stdout)",
    )
    ap.add_argument("--project", default=None, help="project name for the review-request marker")
    ns = ap.parse_args()

    src = ns.plan
    if not src and ns.plan_from_marker:
        m = re.search(r"DRAFT_ONLY_PLAN=(\S+\.json)", ns.plan_from_marker)
        if m:
            src = m.group(1)
    if not src:
        print("[merge_review_queue] no source plan (need --plan or a DRAFT_ONLY_PLAN marker)", file=sys.stderr)
        return 2
    if not os.path.exists(src):
        print(f"[merge_review_queue] source plan not found: {src}", file=sys.stderr)
        return 2

    try:
        with open(src) as f:
            batch = json.load(f)
    except Exception as e:
        print(f"[merge_review_queue] could not read source plan: {e}", file=sys.stderr)
        return 2

    new_cands = batch.get("candidates") or []
    if not new_cands:
        print("[merge_review_queue] source plan has 0 candidates; nothing to merge", file=sys.stderr)
        return 0

    dst = plan_path(REVIEW_QUEUE_ID)
    existing = []
    if os.path.exists(dst):
        try:
            with open(dst) as f:
                existing = json.load(f).get("candidates") or []
        except Exception:
            existing = []

    seen = {_dedup_key(c) for c in existing}
    added = 0
    merged = list(existing)
    for c in new_cands:
        k = _dedup_key(c)
        if k in seen:
            continue
        seen.add(k)
        merged.append(c)
        added += 1

    stamped = _enrich_with_stats(merged)
    if stamped:
        print(f"[merge_review_queue] stamped stats on {stamped} candidate(s)", file=sys.stderr)

    _atomic_write(dst, {"candidates": merged})

    # Refresh the review-request marker the menu bar polls (count = pending, not posted).
    pending = len([c for c in merged if not c.get("posted")])
    project = ns.project or batch.get("project") or (new_cands[0].get("matched_project") if new_cands else None)
    _atomic_write(
        review_request_path(),
        {
            "batch_id": REVIEW_QUEUE_ID,
            "project": project,
            "count": pending,
            "plan_path": dst,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    print(
        f"[merge_review_queue] merged {added} new draft(s) into {REVIEW_QUEUE_ID} "
        f"({pending} pending total) from {os.path.basename(src)}",
        file=sys.stderr,
    )
    # Clean up the consumed batch plan so /tmp doesn't fill with orphans.
    try:
        os.remove(src)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
