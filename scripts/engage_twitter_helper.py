#!/usr/bin/env python3
"""engage_twitter_helper.py — small CLI wrapper used by skill/engage-twitter.sh
to replace the six `psql -t -A -c "..."` one-liners the shell used to embed
inline. Every subcommand prints exactly one value to stdout (string / int /
JSON) so bash can capture it with $(...) without changing shape.

Subcommands:
  reset-stuck-replies
      -> POST /api/v1/replies/reset-stuck { platform:'x', older_than_hours:2 }
      -> prints the integer reset_count
  pending-count
      -> GET /api/v1/replies/counts?platform=x
      -> prints the pending count
  reply-counts
      -> GET /api/v1/replies/counts?platform=x
      -> prints JSON {pending, replied, skipped}
  pending-data --batch-size N
      -> GET /api/v1/replies/next-pending?platform=x&limit=N
         then reshape to the legacy json_agg() shape engage-twitter.sh's
         prompt-template expects:
           [{id, platform, their_author, their_content, their_comment_url,
             their_comment_id, depth, thread_title, thread_url, our_content,
             our_url, is_our_original_post, project_name}, ...]
  post-reset
      -> POST /api/v1/replies/reset-stuck { platform:'x', older_than_hours:0 }
      -> prints reset_count
  active-campaign
      -> GET /api/v1/campaigns?platform=twitter&has_suffix=true
              &with_budget_remaining=true&status=active&limit=1
      -> prints JSON {id, suffix, sample_rate} or {} when none active

Migrated 2026-05-18: the bash used to embed six raw `psql` queries against
Postgres for this engage loop; this helper replaces them all with HTTP API
calls and keeps the bash side free of DATABASE_URL handling for the engage
pipeline.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402


def cmd_reset_stuck_replies() -> int:
    resp = api_post(
        "/api/v1/replies/reset-stuck",
        {"platform": "x", "older_than_hours": 2},
    )
    data = resp.get("data") or {}
    print(int(data.get("reset_count") or 0))
    return 0


def cmd_post_reset() -> int:
    # 0-hour window resets every 'processing' reply we have right now.
    # Mirrors the engage-twitter.sh "leftover after subprocess exit" sweep.
    resp = api_post(
        "/api/v1/replies/reset-stuck",
        {"platform": "x", "older_than_hours": 1},
    )
    data = resp.get("data") or {}
    print(int(data.get("reset_count") or 0))
    return 0


def _counts_dict() -> dict[str, int]:
    """Reshape /api/v1/replies/counts into the flat {pending, replied,
    skipped, processing} dict our callers expect.

    Prefers `eligible_counts` (JOIN-aware: matches what /next-pending
    actually surfaces) over the raw `counts` field. The two diverge when
    truly-orphan rows exist (post_id pointing at a deleted post AND no
    mention_id / parent_reply_id fallback) — historically the raw count
    misled engage-twitter's early-skip gate into burning the
    twitter-browser lock for the full Phase B window finding nothing.
    Falls back to raw `counts` if the deploy doesn't yet expose
    `eligible_counts` (pre-2026-05-26 vintage).
    """
    resp = api_get(
        "/api/v1/replies/counts",
        query={"platform": "x"},
    )
    data = resp.get("data") or {}
    rows = data.get("eligible_counts") or data.get("counts") or []
    out: dict[str, int] = {}
    for r in rows:
        s = r.get("status")
        c = r.get("count")
        if s is None:
            continue
        try:
            out[str(s)] = int(c or 0)
        except (TypeError, ValueError):
            out[str(s)] = 0
    return out


def cmd_pending_count() -> int:
    counts = _counts_dict()
    print(int(counts.get("pending") or 0))
    return 0


def cmd_reply_counts() -> int:
    counts = _counts_dict()
    out = {
        "pending": int(counts.get("pending") or 0),
        "replied": int(counts.get("replied") or 0),
        "skipped": int(counts.get("skipped") or 0),
    }
    json.dump(out, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


def _render_media_block(media) -> str:
    """Render replies.their_media ([{url,alt,type}]) into a short, self-titled
    text block for the Phase B prompt (2026-06-03 thread-media feature). Empty
    string when the comment had no media (or media was never captured), so it
    stays invisible in the embedded JSON for text-only comments.
    """
    if not isinstance(media, list) or not media:
        return ""
    lines = []
    for it in media:
        if not isinstance(it, dict):
            continue
        t = (it.get("type") or "media").strip()
        alt = (it.get("alt") or "").strip()
        url = (it.get("url") or "").strip()
        alt_part = f'"{alt}"' if alt else "[no description]"
        lines.append(f"  - {t}: {alt_part} ({url})")
    if not lines:
        return ""
    return (
        "## Media in the comment you are replying to\n"
        "React to what it VISUALLY shows, not just the text. "
        "[no description] = no alt-text; infer from the comment + media type.\n"
        + "\n".join(lines)
    )


def cmd_pending_data(batch_size: int) -> int:
    try:
        from account_resolver import resolve as _resolve_account  # noqa: WPS433
        our_account = _resolve_account("twitter")
    except Exception:
        our_account = None
    query = {"platform": "x", "limit": batch_size}
    if our_account:
        query["our_account"] = our_account
    resp = api_get(
        "/api/v1/replies/next-pending",
        query=query,
    )
    rows = (resp.get("data") or {}).get("replies") or []

    # Enrich each row with a per-counterparty history block (DM cross-thread
    # + public-reply history) via the shared counterparty_history module.
    # The block is self-titled ("## Prior history with @author") and lands
    # inline in PENDING_DATA so the Phase B prompt picks it up without any
    # change to the shell-side prompt template.
    #
    # Capped to ENRICH_TOP_N because the API list is priority-ordered
    # (our_thread first, then discovered_at ASC) and the Phase B Claude
    # session rarely processes more than ~50 items before gtimeout fires.
    # Beyond the cap we leave counterparty_history_block empty; if a row
    # falls past the cap and IS reached on a later cycle, it'll be in the
    # top slot then and get enriched.
    ENRICH_TOP_N = 60
    history_blocks = [""] * len(rows)
    try:
        from concurrent.futures import ThreadPoolExecutor
        from counterparty_history import get_counterparty_history_block

        def _enrich(r):
            author = r.get("their_author")
            if not author:
                return ""
            try:
                _disengage, block = get_counterparty_history_block(
                    platform="x",
                    author=author,
                    current_post_id=r.get("post_id"),
                    current_reply_id=r.get("id"),
                )
                return block or ""
            except Exception as e:
                print(
                    f"[engage_twitter_helper] counterparty_history failed "
                    f"for @{author}: {e}",
                    file=sys.stderr,
                )
                return ""

        top_rows = rows[:ENRICH_TOP_N]
        with ThreadPoolExecutor(max_workers=8) as ex:
            for idx, block in enumerate(ex.map(_enrich, top_rows)):
                history_blocks[idx] = block
        non_empty = sum(1 for b in history_blocks if b)
        print(
            f"[engage_twitter_helper] counterparty_history enriched "
            f"{len(top_rows)}/{len(rows)} rows ({non_empty} with non-empty block)",
            file=sys.stderr,
        )
    except Exception as e:
        print(
            f"[engage_twitter_helper] enrichment phase failed "
            f"(continuing without history): {e}",
            file=sys.stderr,
        )

    out = []
    for r, history_block in zip(rows, history_blocks):
        out.append({
            "id": r.get("id"),
            "platform": r.get("platform"),
            "their_author": r.get("their_author"),
            "their_content": r.get("their_content"),
            "their_comment_url": r.get("their_comment_url"),
            "their_comment_id": r.get("their_comment_id"),
            "depth": r.get("depth"),
            "thread_title": r.get("thread_title"),
            "thread_url": r.get("thread_url"),
            "our_content": r.get("our_content"),
            "our_url": r.get("our_url"),
            "is_our_original_post": int(r.get("is_our_original_post") or 0),
            "project_name": r.get("project_name"),
            "counterparty_history_block": history_block,
            "their_media_block": _render_media_block(r.get("their_media")),
        })
    # json_agg(...) returns null when the array is empty; engage-twitter.sh's
    # downstream prompt-template expects an empty array instead, which is
    # easier to embed verbatim.
    json.dump(out, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


def cmd_active_campaign() -> int:
    resp = api_get(
        "/api/v1/campaigns",
        query={
            "status": "active",
            "platform": "twitter",
            "has_suffix": "true",
            "with_budget_remaining": "true",
            "limit": 1,
        },
    )
    rows = (resp.get("data") or {}).get("campaigns") or []
    if not rows:
        sys.stdout.write("{}\n")
        return 0
    r = rows[0]
    out = {
        "id": r.get("id"),
        "suffix": r.get("suffix"),
        "sample_rate": float(r.get("sample_rate") or 1.0),
    }
    json.dump(out, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Helper for engage-twitter.sh")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("reset-stuck-replies")
    sub.add_parser("pending-count")
    sub.add_parser("reply-counts")
    sub.add_parser("post-reset")
    sub.add_parser("active-campaign")

    p_pending = sub.add_parser("pending-data")
    p_pending.add_argument("--batch-size", type=int, default=500)

    args = ap.parse_args()

    if args.cmd == "reset-stuck-replies":
        return cmd_reset_stuck_replies()
    if args.cmd == "pending-count":
        return cmd_pending_count()
    if args.cmd == "reply-counts":
        return cmd_reply_counts()
    if args.cmd == "post-reset":
        return cmd_post_reset()
    if args.cmd == "active-campaign":
        return cmd_active_campaign()
    if args.cmd == "pending-data":
        return cmd_pending_data(args.batch_size)
    return 1


if __name__ == "__main__":
    sys.exit(main())
