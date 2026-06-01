#!/usr/bin/env python3
"""HTTP-only stdout shims for the psql one-liners in skill/engage-dm-replies.sh.

The engage pipeline used to embed raw `psql "$DATABASE_URL" -t -A -c "..."`
calls for its read-side gates and end-of-run summary. The direct-Postgres lane
was removed 2026-06-01; DATABASE_URL is deliberately ignored, no DB, no
fallback. Each subcommand here calls the s4l.ai HTTP API (scripts/http_api.py)
and prints EXACTLY what the corresponding psql call printed, so the shell
parsing around it (json.load, `tr '|' ' '`, integer compares) is unchanged.

Subcommands (each maps 1:1 to a former psql call):
  pending --platform X --limit 30   -> PENDING_CONVOS: JSON array, or 'null'
  needs-reply --platform X          -> needs_reply_count_for: integer
  run-counts --platform X --since N -> dm_counts_for: 'POSTED STALE'
  summary                           -> DM_SUMMARY: json object
  reddit-authors                    -> KNOWN_REDDIT_AUTHORS: 'a, b, c'
  reddit-campaign-suffix            -> REDDIT_CAMPAIGN_SUFFIX_LITERAL
  reddit-campaign-sample-rate       -> REDDIT_CAMPAIGN_SAMPLE_RATE
  flagged-count                     -> FLAGGED_COUNT: integer

All endpoints live under /api/v1/dms/engage except the campaign subcommands
(/api/v1/campaigns) and flagged-count (/api/v1/dms/flagged).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get


def _data(resp):
    return (resp or {}).get("data") or {}


def cmd_pending(args):
    resp = api_get(
        "/api/v1/dms/engage",
        query={"mode": "pending", "platform": args.platform or "", "limit": args.limit},
    )
    rows = _data(resp).get("rows")
    # Mirror psql's `json_agg(...) -> NULL when empty` which the shell echoed as
    # the literal string 'null'.
    if not rows:
        print("null")
    else:
        print(json.dumps(rows))


def cmd_needs_reply(args):
    resp = api_get(
        "/api/v1/dms/engage",
        query={"mode": "needs_reply", "platform": args.platform or ""},
    )
    print(_data(resp).get("count", 0))


def cmd_run_counts(args):
    resp = api_get(
        "/api/v1/dms/engage",
        query={"mode": "run_counts", "platform": args.platform or "", "since": args.since},
    )
    d = _data(resp)
    # psql printed 'posted|stale' then the shell did `tr '|' ' '`; emit the
    # already-split form so `read -r POSTED STALE` works directly.
    print(f"{d.get('posted', 0)} {d.get('stale', 0)}")


def cmd_summary(_args):
    resp = api_get("/api/v1/dms/engage", query={"mode": "summary"})
    summary = _data(resp).get("summary") or {}
    # Match the json_build_object the shell logs verbatim.
    print(json.dumps(summary))


def cmd_reddit_authors(_args):
    resp = api_get("/api/v1/dms/engage", query={"mode": "reddit_authors"})
    print(_data(resp).get("authors") or "")


def _active_reddit_campaign():
    """First active reddit campaign with budget remaining + a non-empty suffix.

    Mirrors the two REDDIT_CAMPAIGN_* psql queries: status='active',
    platforms includes reddit, max_posts_total set AND posts_made < it,
    suffix non-empty, ORDER BY id LIMIT 1.
    """
    resp = api_get(
        "/api/v1/campaigns",
        query={
            "status": "active",
            "platform": "reddit",
            "has_suffix": "true",
            "with_budget_remaining": "true",
            "limit": 500,
        },
    )
    rows = _data(resp).get("campaigns") or []
    for r in rows:  # already ORDER BY id ASC server-side
        max_total = r.get("max_posts_total")
        posts_made = r.get("posts_made") or 0
        suffix = r.get("suffix")
        if max_total is None or posts_made >= max_total:
            continue
        if not suffix:
            continue
        return r
    return None


def cmd_reddit_campaign_suffix(_args):
    c = _active_reddit_campaign()
    # psql piped through `tr -d '\n'`; print with no trailing newline.
    sys.stdout.write(c.get("suffix") if c else "")


def cmd_reddit_campaign_sample_rate(_args):
    c = _active_reddit_campaign()
    if not c:
        sys.stdout.write("")
        return
    rate = c.get("sample_rate")
    sys.stdout.write("1.000" if rate is None else str(rate))


def cmd_flagged_count(_args):
    resp = api_get("/api/v1/dms/flagged")
    print(_data(resp).get("count", 0))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pending")
    sp.add_argument("--platform", default="")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_pending)

    sp = sub.add_parser("needs-reply")
    sp.add_argument("--platform", default="")
    sp.set_defaults(func=cmd_needs_reply)

    sp = sub.add_parser("run-counts")
    sp.add_argument("--platform", default="")
    sp.add_argument("--since", type=int, required=True)
    sp.set_defaults(func=cmd_run_counts)

    sub.add_parser("summary").set_defaults(func=cmd_summary)
    sub.add_parser("reddit-authors").set_defaults(func=cmd_reddit_authors)
    sub.add_parser("reddit-campaign-suffix").set_defaults(func=cmd_reddit_campaign_suffix)
    sub.add_parser("reddit-campaign-sample-rate").set_defaults(func=cmd_reddit_campaign_sample_rate)
    sub.add_parser("flagged-count").set_defaults(func=cmd_flagged_count)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
