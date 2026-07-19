#!/usr/bin/env python3
"""counterparty_history.py — shared cross-pipeline counterparty memory.

Both the Reddit (engage_reddit.py) and Twitter (engage_twitter_helper.py)
public-engagement pipelines call get_counterparty_history_block(...) before
drafting a reply to a specific user. The block surfaces two lanes:

1. DM cross-thread history: from the dms table, scoped to OTHER posts
   (different post_id). Reuses /api/v1/dms?their_author=X&exclude_post_id=N.
   Also returns a same-post disengage signal (hard-skip in callers) when
   the engage-dm-replies pipeline has already classified this person as
   declined / not_our_prospect / stale on the CURRENT post.

2. Public-reply history: prior public comments WE made replying to this
   author, via /api/v1/replies?their_author=X&status=replied. Lets the model
   see whether it's repeating itself with this person, what tone has worked
   before, what archetype has been used.

Returns (same_post_disengage, block_text). block_text is "" when no history
exists in either lane. Callers concatenate block_text into their prompt
(self-titled with its own H2 header, so no caller-side wrapping needed).

Why one shared helper: before 2026-05-19 the Reddit pipeline had its own
check_cross_pipeline_history() that pulled the DM lane only, while Twitter
had no counterparty memory at all. Splitting per-platform forks meant
either pipeline could drift on what gets surfaced (e.g. Reddit added the
public-reply lane while Twitter still flew blind). Single helper, both
callers, symmetric behavior.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


def _fmt_date(s):
    """Format an ISO-ish timestamp string as YYYY-MM-DD, tolerant of None."""
    if not s:
        return "unknown"
    try:
        return str(s)[:10]
    except Exception:
        return "unknown"


def _truncate(text, n=140):
    if not text:
        return ""
    t = str(text).replace("\n", " ").strip()
    return t if len(t) <= n else t[: n - 1] + "..."


_TWITTER_STATUS_RE = re.compile(r"/status/(\d+)")
_REDDIT_COMMENT_RE = re.compile(r"/comments/([a-z0-9]+)/")


def _conversation_root(platform, post_id, their_comment_url):
    """Best-effort key identifying the conversation root for grouping.

    - Our own post: post_id (always wins when present and non-zero).
    - Twitter guest thread: '/status/<id>' from the URL.
    - Reddit guest thread: '/comments/<id>/' from the URL.
    - Fallback: the URL with the last path segment stripped.
    """
    if post_id:
        return f"post:{post_id}"
    if not their_comment_url:
        return None
    if platform == "x":
        m = _TWITTER_STATUS_RE.search(their_comment_url)
        if m:
            return f"x_status:{m.group(1)}"
    if platform == "reddit":
        m = _REDDIT_COMMENT_RE.search(their_comment_url)
        if m:
            return f"r_thread:{m.group(1)}"
    return f"url:{their_comment_url.rsplit('/', 1)[0]}"


# Same-author reply cap advisory (2026-07-14): a run of near-identical
# corrections to one person over a short window reads as one-note bot
# behavior even when each individual reply was earned. This is a SOFT
# nudge appended to the history block, not a hard skip, since a genuine
# direct question from the author (see get_counterparty_history_block)
# still earns a reply past the threshold; only a deterministic count-based
# gate would wrongly suppress that case.
RECENT_CAP_WINDOW_HOURS = 48
RECENT_CAP_THRESHOLD = 3


def _fetch_author_summary(platform, author, days=7):
    """Compute bot/loop-judgment stats for `author` in the last `days` window.

    Returns (summary_line, recent_replied_count). summary_line is "" when
    there is no history. recent_replied_count is how many of our replies to
    this author landed inside RECENT_CAP_WINDOW_HOURS, used for the same-
    author cap advisory below.

    Signals: total candidates, our replied count, our skipped count,
    distinct conversation roots, our_replies / distinct_roots ratio (the
    "engagement-loop shape" metric — closer to 1.0 = farm-shaped), skip
    rate (% of our heuristics filtering this person out — low = bait too
    clean), span_hours.
    """
    if not author:
        return "", 0
    since_ts = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    try:
        resp = api_get(
            "/api/v1/replies",
            query={
                "platform": platform,
                "their_author": author,
                "since": since_ts,
                "limit": 500,
                "order_by": "discovered_at",
            },
        )
        rows = ((resp or {}).get("data") or {}).get("replies") or []
    except Exception as e:
        print(
            f"[counterparty_history] summary fetch failed for "
            f"{platform}/@{author}: {e}",
            file=sys.stderr,
        )
        return "", 0

    total = len(rows)
    if total == 0:
        return "", 0

    replied = sum(1 for r in rows if r.get("status") == "replied")
    skipped = sum(1 for r in rows if r.get("status") == "skipped")
    roots = {_conversation_root(platform, r.get("post_id"), r.get("their_comment_url")) for r in rows}
    roots.discard(None)
    distinct_roots = len(roots) or 1

    skip_pct = (skipped / total * 100.0) if total else 0.0
    ratio = (replied / distinct_roots) if distinct_roots else 0.0

    timestamps = []
    for r in rows:
        ts = r.get("discovered_at") or r.get("replied_at")
        if not ts:
            continue
        try:
            timestamps.append(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
        except Exception:
            continue
    span_h = 0.0
    if len(timestamps) >= 2:
        span_h = (max(timestamps) - min(timestamps)).total_seconds() / 3600.0

    recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=RECENT_CAP_WINDOW_HOURS)
    recent_replied = 0
    for r in rows:
        if r.get("status") != "replied":
            continue
        ts = r.get("replied_at") or r.get("discovered_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            continue
        if dt >= recent_cutoff:
            recent_replied += 1

    summary_line = (
        f"SUMMARY (last {days}d): {total} candidates, {replied} our_replies, "
        f"{skipped} skipped ({skip_pct:.1f}% skip_rate), "
        f"{distinct_roots} distinct conversation_roots "
        f"(replies/root={ratio:.2f}, closer to 1.0 = farm-shaped), "
        f"span={span_h:.1f}h"
    )
    return summary_line, recent_replied


def _fetch_dm_history(platform, author, post_id):
    """Returns (same_post_disengage, other_thread_lines).

    same_post_disengage is a dict {dm_id, interest_level, conversation_status, ...}
    when this person has been classified declined/not_our_prospect/stale on
    THIS post by the engage-dm-replies pipeline. Caller hard-skips.

    other_thread_lines is a list of bullet strings for the soft-context
    block (different post_id; tier, status, target_project, last message).
    """
    same_post_disengage = None
    other_lines = []

    if post_id:
        try:
            same_resp = api_get(
                "/api/v1/dms",
                query={
                    "platform": platform,
                    "their_author": author,
                    "post_id": post_id,
                    "limit": 25,
                    "order_by": "last_message_at",
                },
            )
            same_rows = ((same_resp or {}).get("data") or {}).get("dms") or []
            for d in same_rows:
                interest = d.get("interest_level")
                convo_status = d.get("conversation_status")
                if interest in ("declined", "not_our_prospect") or convo_status == "stale":
                    same_post_disengage = {
                        "dm_id": d.get("id"),
                        "interest_level": interest,
                        "conversation_status": convo_status,
                        "qualification_status": d.get("qualification_status"),
                        "last_message_at": d.get("last_message_at"),
                    }
                    break
        except Exception as e:
            print(
                f"[counterparty_history] same-post dm check failed for "
                f"{platform}/@{author} post={post_id}: {e}",
                file=sys.stderr,
            )

    try:
        query = {
            "platform": platform,
            "their_author": author,
            "min_message_count": 1,
            "with_last_message": "true",
            "order_by": "last_message_at",
            "limit": 5,
        }
        if post_id:
            query["exclude_post_id"] = post_id
        other_resp = api_get("/api/v1/dms", query=query)
        other_rows = ((other_resp or {}).get("data") or {}).get("dms") or []
        for r in other_rows:
            ts = _fmt_date(r.get("last_message_at"))
            interest = r.get("interest_level") or "unset"
            mode = r.get("mode") or "unset"
            status = r.get("conversation_status") or "unset"
            tier = r.get("tier") if r.get("tier") is not None else "?"
            msgs = r.get("message_count") or 0
            target = r.get("target_project") or "-"
            last = _truncate(r.get("last_msg"), 140)
            other_lines.append(
                f"- dm #{r.get('id')} on post #{r.get('post_id')} (last activity {ts}): "
                f"interest={interest}, mode={mode}, status={status}, "
                f"tier={tier}, messages={msgs}, target_project={target}\n"
                f"    last: {last}"
            )
    except Exception as e:
        print(
            f"[counterparty_history] other-thread dm fetch failed for "
            f"{platform}/@{author}: {e}",
            file=sys.stderr,
        )

    return same_post_disengage, other_lines


def _fetch_public_reply_history(platform, author, current_reply_id=None, limit=5):
    """Returns a list of bullet strings describing our prior public replies
    to this author (status=replied, our_reply_content non-empty).

    Pulls limit+2 from the API so the client-side exclude_id filter (drop
    the current reply we are about to draft) still yields `limit` lines.
    """
    try:
        resp = api_get(
            "/api/v1/replies",
            query={
                "platform": platform,
                "their_author": author,
                "status": "replied",
                "has_our_reply_content": "true",
                "order_by": "replied_at",
                "limit": int(limit) + 2,
            },
        )
        rows = ((resp or {}).get("data") or {}).get("replies") or []
    except Exception as e:
        print(
            f"[counterparty_history] public-reply fetch failed for "
            f"{platform}/@{author}: {e}",
            file=sys.stderr,
        )
        return []

    lines = []
    for r in rows:
        if current_reply_id and r.get("id") == current_reply_id:
            continue
        ts = _fmt_date(r.get("replied_at"))
        style = r.get("engagement_style") or "?"
        upv = r.get("upvotes") if r.get("upvotes") is not None else "?"
        cmts = r.get("comments_count") if r.get("comments_count") is not None else "?"
        post_id = r.get("post_id")
        their_snippet = _truncate(r.get("their_content"), 120)
        our_snippet = _truncate(r.get("our_reply_content"), 200)
        lines.append(
            f"- {ts} on post #{post_id} (style={style}, engagement: {upv} upvotes / {cmts} replies)\n"
            f"    they said: {their_snippet}\n"
            f"    we said: {our_snippet}"
        )
        if len(lines) >= limit:
            break
    return lines


def get_counterparty_history_block(platform, author, current_post_id=None, current_reply_id=None):
    """Build the shared 'Prior history with @author' block.

    Returns (same_post_disengage, block_text). block_text is "" when there
    is nothing to surface in either lane.

    Parallelizes the two API lanes (DM check + public-reply fetch) so the
    helper's wall-clock is ~max(dm_latency, replies_latency) rather than
    the sum.
    """
    if not author:
        return None, ""

    with ThreadPoolExecutor(max_workers=3) as ex:
        dm_fut = ex.submit(_fetch_dm_history, platform, author, current_post_id)
        pub_fut = ex.submit(_fetch_public_reply_history, platform, author, current_reply_id)
        sum_fut = ex.submit(_fetch_author_summary, platform, author, 7)
        same_post_disengage, dm_lines = dm_fut.result()
        pub_lines = pub_fut.result()
        summary_line, recent_replied = sum_fut.result()

    if not dm_lines and not pub_lines and not summary_line:
        return same_post_disengage, ""

    parts = [f"## Prior history with @{author}"]
    parts.append(
        "Soft context from past interactions across DM and public-reply rails. "
        "Use this to gauge tone, avoid repeating yourself, and notice if they "
        "have already declined or warmed up to a topic. Does NOT auto-block; "
        "you still decide reply or skip based on the current thread."
    )
    if summary_line:
        parts.append("")
        parts.append(summary_line)
    if recent_replied >= RECENT_CAP_THRESHOLD:
        parts.append(
            f"\nCAUTION: we've already replied to @{author} {recent_replied} times in the "
            f"last {RECENT_CAP_WINDOW_HOURS}h. Repeating the same correction again reads as "
            "one-note bot behavior. Only reply again if they asked a direct question or raised "
            "a genuinely new point; otherwise let this one go or shift to a different angle."
        )
    if dm_lines:
        parts.append("\n### DM threads on other posts")
        parts.append("\n".join(dm_lines))
    if pub_lines:
        parts.append("\n### Our prior public replies to this person")
        parts.append("\n".join(pub_lines))

    return same_post_disengage, "\n".join(parts)


if __name__ == "__main__":
    # Manual smoke-test:
    #   python3 counterparty_history.py reddit Secret_Theme3192
    #   python3 counterparty_history.py x someuser
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("platform")
    ap.add_argument("author")
    ap.add_argument("--post-id", type=int, default=None)
    ap.add_argument("--reply-id", type=int, default=None)
    args = ap.parse_args()

    disengage, block = get_counterparty_history_block(
        args.platform, args.author,
        current_post_id=args.post_id,
        current_reply_id=args.reply_id,
    )
    print(f"same_post_disengage: {disengage}")
    print("---")
    print(block or "(empty block — no history found)")
