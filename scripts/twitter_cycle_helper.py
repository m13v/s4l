#!/usr/bin/env python3
"""twitter_cycle_helper.py — small CLI wrapper used by skill/run-twitter-cycle.sh
to replace the six `psql -t -A -c "..."` one-liners the cycle orchestrator
used to embed inline. Every subcommand prints exactly one value to stdout
(string / int / pipe-separated row dump) so bash can capture it with $(...)
without changing shape.

Subcommands:
  status-counts --batch-id ID
      -> GET /api/v1/twitter-candidates/counts-by-batch
      -> prints two integers space-separated: "<posted> <skipped_or_expired>"
         (mirrors the two timeout-10 psql one-liners in the run-summary block)

  phase0-salvage --batch-id ID --freshness-hours N --legacy-cutoff BATCH_ID
      -> POST /api/v1/twitter-candidates/phase0-salvage
      -> prints "<expired_count>|<salvaged_count>"
         (matches the legacy PHASE0_RESULT pipe-shape)

  engaged-tweet-ids [--window-hours 48]
      -> GET /api/v1/twitter/engaged-tweet-ids
      -> prints a single JSON array on stdout

  batch-count --batch-id ID
      -> GET /api/v1/twitter-candidates/counts-by-batch
      -> prints the total integer (all statuses)

  candidates --batch-id ID
      -> GET /api/v1/twitter-candidates?batch_id=ID&status=pending
      -> prints pipe-separated rows in the exact column order
         run-twitter-cycle.sh's old psql query produced:
           id|tweet_url|author_handle|tweet_text|virality_score|
           delta_score|matched_project|search_topic|likes_t1|retweets_t1|
           replies_t1|views_t1|author_followers|age_hours|
           draft_reply_text|draft_engagement_style|drafted_minutes_ago

  expire-batch --batch-id ID
      -> POST /api/v1/twitter-candidates/expire-batch
      -> prints the resulting expired_count integer

  batch-summary --batch-id ID
      -> GET /api/v1/twitter-candidates/counts-by-batch
      -> prints "status1|count1\\nstatus2|count2" (mirrors the legacy
         SUMMARY = `psql -F '|' SELECT status, COUNT(*) ... GROUP BY status`
         pipe-format the cycle log line consumes)

Migrated 2026-05-18: removes 6 direct psql calls from
skill/run-twitter-cycle.sh. The cycle no longer requires DATABASE_URL for
its core SQL surface (only for the few legacy non-twitter paths that still
embed psql in other shells).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402
from twitter_account import resolve_handle as _resolve_twitter_handle  # noqa: E402


def _counts(batch_id: str) -> dict:
    resp = api_get(
        "/api/v1/twitter-candidates/counts-by-batch",
        query={"batch_id": batch_id},
    )
    return resp.get("data") or {}


def cmd_status_counts(batch_id: str) -> int:
    c = _counts(batch_id)
    posted = int(c.get("posted") or 0)
    skipped_or_expired = int(c.get("skipped_or_expired") or 0)
    sys.stdout.write(f"{posted} {skipped_or_expired}\n")
    return 0


def cmd_batch_count(batch_id: str) -> int:
    c = _counts(batch_id)
    sys.stdout.write(f"{int(c.get('total') or 0)}\n")
    return 0


def cmd_batch_summary(batch_id: str) -> int:
    c = _counts(batch_id)
    by_status = c.get("by_status") or {}
    # Pipe-separated, one row per status, like the legacy psql -F '|' output.
    parts = [f"{status}|{count}" for status, count in by_status.items()]
    sys.stdout.write("\n".join(parts) + ("\n" if parts else ""))
    return 0


def cmd_phase0_salvage(batch_id: str, freshness_hours: int, legacy_cutoff: str) -> int:
    resp = api_post(
        "/api/v1/twitter-candidates/phase0-salvage",
        {
            "batch_id": batch_id,
            "freshness_hours": freshness_hours,
            "legacy_salvage_cutoff_batch_id": legacy_cutoff,
        },
    )
    d = resp.get("data") or {}
    expired = int(d.get("expired_count") or 0)
    salvaged = int(d.get("salvaged_count") or 0)
    try:
        _salvaged = int(d.get("salvaged_count", 0) or 0)
        _expired = int(d.get("expired_count", 0) or 0)
        _sources = d.get("salvaged_from_batches") or []
        if _salvaged > 0:
            _src_str = ",".join(str(s) for s in _sources) if _sources else "unknown"
            print(
                f"[twitter_salvage] batch={batch_id} salvaged_count={_salvaged} "
                f"expired_count={_expired} salvaged_from_batches={_src_str}",
                file=sys.stderr,
                flush=True,
            )
    except Exception:
        pass
    sys.stdout.write(f"{expired}|{salvaged}\n")
    return 0


def cmd_engaged_tweet_ids(window_hours: int) -> int:
    # Scope the dedupe pool to THIS machine's Twitter handle. Without
    # this, two machines posting as different handles (e.g. @m13v_ on the
    # local cron, @matt_diak on the VM) share one 486-ID pool and starve
    # each other's candidate supply. Falls back to unscoped (legacy) when
    # no handle is configured, preserving single-account behavior.
    query: dict[str, object] = {"window_hours": window_hours}
    handle = _resolve_twitter_handle()
    if handle:
        query["our_account"] = handle
    resp = api_get("/api/v1/twitter/engaged-tweet-ids", query=query)
    ids = (resp.get("data") or {}).get("tweet_ids") or []
    # The legacy shell expects a JSON array string; mirror that exactly so
    # `python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'`
    # downstream parses unchanged.
    sys.stdout.write(json.dumps(ids))
    sys.stdout.write("\n")
    return 0


def cmd_expire_batch(batch_id: str) -> int:
    resp = api_post(
        "/api/v1/twitter-candidates/expire-batch",
        {"batch_id": batch_id},
    )
    d = resp.get("data") or {}
    sys.stdout.write(f"{int(d.get('expired_count') or 0)}\n")
    return 0


def cmd_stamp_cycle_variant(batch_id: str, variant: str) -> int:
    resp = api_post(
        "/api/v1/twitter-candidates/stamp-cycle-variant",
        {"batch_id": batch_id, "cycle_variant": variant},
    )
    d = resp.get("data") or {}
    sys.stdout.write(f"{int(d.get('stamped_count') or 0)}\n")
    return 0


def _sanitize(s) -> str:
    """Mirror the SQL `REPLACE(REPLACE(..., E'\n', ' '), E'\r', ' ')` so a
    multi-line tweet/draft body doesn't break the pipe-delimited row format."""
    if s is None:
        return ""
    return str(s).replace("\n", " ").replace("\r", " ")


def cmd_candidates(batch_id: str) -> int:
    """List pending candidates for a batch in the EXACT pipe-separated
    column order run-twitter-cycle.sh's old psql query produced.

    Sort key (2026-05-27): virality_score DESC.
    virality_score is the composite predictor stamped by score_twitter_candidates.py
    at discovery (velocity * reach_mult * age_decay * rt_bonus * (1+reply_bonus)
    * (1+discussion_bonus)). Cohort analysis on 30d posted data showed the
    [10k+) virality bucket gets ~36x the median reply views of the [0-10)
    bucket, while the previous sort (delta_score + flat-5 intent regex
    boost) ignored author reach, age decay, and discussion quality. The
    intent regex was a crutch when the sort key was raw delta; the model
    reads tweet text directly in the prep prompt and can detect intent
    itself, so the lexical layer is now redundant.
    The 25-row cap is unchanged (draft budget, not a quality gate).
    """
    from datetime import datetime, timezone

    # Scope by our_account so a peer machine's pending rows on the same
    # tweet_url don't surface in this machine's batch. The composite
    # (tweet_url, our_account) unique guarantees each machine has its own
    # candidate row; this filter just makes the GET match the INSERT shape.
    query: dict[str, object] = {
        "batch_id": batch_id,
        "status": "pending",
        "limit": 500,
    }
    handle = _resolve_twitter_handle()
    if handle:
        query["our_account"] = handle
    resp = api_get("/api/v1/twitter-candidates", query=query)
    rows = (resp.get("data") or {}).get("candidates") or []

    def composite(r):
        return float(r.get("virality_score") or 0)

    now = datetime.now(timezone.utc)

    def age_hours(r):
        ts = r.get("tweet_posted_at")
        if not ts:
            return 0.0
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return (now - dt).total_seconds() / 3600.0
        except Exception:
            return 0.0

    def drafted_minutes_ago(r):
        ts = r.get("drafted_at")
        if not ts:
            return -1.0
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return (now - dt).total_seconds() / 60.0
        except Exception:
            return -1.0

    rows.sort(key=composite, reverse=True)
    rows = rows[:25]

    for r in rows:
        cols = [
            str(r.get("id") or ""),
            str(r.get("tweet_url") or ""),
            str(r.get("author_handle") or ""),
            _sanitize(r.get("tweet_text")),
            f"{float(r.get('virality_score') or 0):g}",
            f"{float(r.get('delta_score') or 0):g}",
            str(r.get("matched_project") or ""),
            str(r.get("search_topic") or ""),
            str(r.get("likes_t1") or ""),
            str(r.get("retweets_t1") or ""),
            str(r.get("replies_t1") or ""),
            str(r.get("views_t1") or ""),
            str(r.get("author_followers") or ""),
            f"{age_hours(r):g}",
            _sanitize(r.get("draft_reply_text")),
            str(r.get("draft_engagement_style") or ""),
            f"{drafted_minutes_ago(r):g}",
        ]
        sys.stdout.write("|".join(cols) + "\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Helper for run-twitter-cycle.sh")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_sc = sub.add_parser("status-counts")
    p_sc.add_argument("--batch-id", required=True)

    p_bc = sub.add_parser("batch-count")
    p_bc.add_argument("--batch-id", required=True)

    p_bs = sub.add_parser("batch-summary")
    p_bs.add_argument("--batch-id", required=True)

    p_p0 = sub.add_parser("phase0-salvage")
    p_p0.add_argument("--batch-id", required=True)
    p_p0.add_argument("--freshness-hours", type=int, required=True)
    p_p0.add_argument("--legacy-cutoff", required=True)

    p_et = sub.add_parser("engaged-tweet-ids")
    p_et.add_argument("--window-hours", type=int, default=48)

    p_eb = sub.add_parser("expire-batch")
    p_eb.add_argument("--batch-id", required=True)

    p_ca = sub.add_parser("candidates")
    p_ca.add_argument("--batch-id", required=True)

    p_cv = sub.add_parser("stamp-cycle-variant")
    p_cv.add_argument("--batch-id", required=True)
    p_cv.add_argument("--variant", required=True)

    args = ap.parse_args()

    if args.cmd == "status-counts":
        return cmd_status_counts(args.batch_id)
    if args.cmd == "batch-count":
        return cmd_batch_count(args.batch_id)
    if args.cmd == "batch-summary":
        return cmd_batch_summary(args.batch_id)
    if args.cmd == "phase0-salvage":
        return cmd_phase0_salvage(args.batch_id, args.freshness_hours, args.legacy_cutoff)
    if args.cmd == "engaged-tweet-ids":
        return cmd_engaged_tweet_ids(args.window_hours)
    if args.cmd == "expire-batch":
        return cmd_expire_batch(args.batch_id)
    if args.cmd == "candidates":
        return cmd_candidates(args.batch_id)
    if args.cmd == "stamp-cycle-variant":
        return cmd_stamp_cycle_variant(args.batch_id, args.variant)
    return 1


if __name__ == "__main__":
    sys.exit(main())
