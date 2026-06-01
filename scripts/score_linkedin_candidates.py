#!/usr/bin/env python3
"""
score_linkedin_candidates.py

Reads a JSON array of LinkedIn SERP candidates (from stdin or --file),
computes engagement velocity + LinkedIn-tuned virality score, and upserts
into linkedin_candidates. Also expires + prunes old rows.

Why this exists, vs Twitter's score_twitter_candidates.py:

Twitter's pipeline runs every 20 min and uses a two-phase delta-momentum
gate (T0 scan, sleep 5 min, T1 rescan, score = delta engagement / 5 min).
LinkedIn is ad-hoc and we cannot afford the 5-min wait per cycle, so the
single-shot substitute is *engagement velocity since post creation*:

    velocity = (reactions + 2*comments + 3*reposts) / max(age_hours, 0.5)

Comments weighted higher than reposts than reactions because comments
signal a live conversation a reply can join. The 0.5-hour floor stops
brand-new posts from infinity-spiking.

The full virality score layers in author follower reach + age decay so a
trending post from a sub-50K-follower practitioner outranks a stale
influencer post with the same raw velocity.

Input JSON shape (one element per candidate, scraped via the
mcp__linkedin-agent walk in run-linkedin.sh Phase B):

    [
      {
        "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:...",
        "activity_id": "1234567890123456789",
        "all_urns": ["1234567890123456789", "..."],
        "author_name": "First Last",
        "author_profile_url": "https://www.linkedin.com/in/SLUG/",
        "author_followers": 12345,
        "post_text": "first 500 chars",
        "age_hours": 6.5,
        "reactions": 42,
        "comments": 7,
        "reposts": 3,
        "search_topic": "AI agents in production",
        "search_query": "ai agents production",
        "matched_project": "fazm",
        "language": "en",
        "serp_quality_score": 7.5
      }
    ]

Usage:
    python3 scripts/score_linkedin_candidates.py --batch-id <id> < candidates.json
    python3 scripts/score_linkedin_candidates.py --file /tmp/c.json --batch-id <id>
    python3 scripts/score_linkedin_candidates.py --expire-only

Pair with: top_linkedin_queries.py, top_dud_linkedin_queries.py,
log_linkedin_search_attempts.py.
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post
try:
    from account_resolver import resolve as _resolve_account
except Exception:
    def _resolve_account(_platform):  # type: ignore[unused-arg]
        return None


# Engagement weights. Comments worth more than reposts worth more than
# reactions because comments are the strongest "this thread is alive"
# signal for an outbound reply.
W_REACTIONS = 1.0
W_COMMENTS = 2.0
W_REPOSTS = 3.0

# Floor on age_hours so freshly-posted (<30 min old) posts cannot
# infinity-spike the velocity score. 0.5 = 30 min.
AGE_FLOOR_HOURS = 0.5

# Maximum age we'll consider. Posts older than this are too cold —
# the conversation has moved on, our reply lands in a graveyard. Mirrors
# Twitter's 18h ceiling, scaled up because LinkedIn threads stay live
# longer (multi-day).
MAX_AGE_HOURS = 96.0  # 4 days

# Freshness gate. We flip stale pending rows to 'expired' so they stop
# burning judgment tokens, but we NEVER delete rows. Per user instruction
# 2026-05-08, terminal rows are kept forever for analytics.
EXPIRE_PENDING_AFTER_HOURS = 96.0  # match MAX_AGE_HOURS


def calculate_velocity_score(cand):
    """Return (velocity, virality, age_hours_clamped).

    velocity is the raw weighted-engagement-per-hour signal. virality
    layers in follower reach + age decay so the candidate-picker can
    rank across a SERP regardless of absolute size.
    """
    reactions = int(cand.get("reactions", 0) or 0)
    comments = int(cand.get("comments", 0) or 0)
    reposts = int(cand.get("reposts", 0) or 0)
    followers = int(cand.get("author_followers", 0) or 0)

    age_hours = float(cand.get("age_hours", 0) or 0)
    if age_hours < AGE_FLOOR_HOURS:
        age_hours = AGE_FLOOR_HOURS

    weighted_eng = (
        W_REACTIONS * reactions
        + W_COMMENTS * comments
        + W_REPOSTS * reposts
    )
    velocity = weighted_eng / age_hours

    # Author reach multiplier. LinkedIn-specific tuning: practitioner
    # accounts (5K-50K followers) are the sweet spot for outbound
    # replies — they have audience but aren't influencer-saturated, so
    # our reply has a real chance of being seen.
    if followers <= 0:
        # Unknown follower count: don't penalize, just don't reward.
        reach_mult = 0.8
    elif followers < 500:
        reach_mult = 0.4
    elif followers < 2000:
        reach_mult = 0.7
    elif followers < 5000:
        reach_mult = 0.95
    elif followers < 50000:
        reach_mult = 1.0  # sweet spot
    elif followers < 200000:
        reach_mult = 1.2
    elif followers < 500000:
        reach_mult = 1.0
    else:
        reach_mult = 0.85  # mega accounts: lower hit rate, drowned out

    # Age decay. Half-life 24h on LinkedIn (vs 6h on Twitter): threads
    # stay live longer. ln(2)/24 ≈ 0.0289.
    # 12h = 71%, 24h = 50%, 48h = 25%, 96h = 6%.
    age_decay = math.exp(-0.0289 * age_hours)

    # Discussion-quality bonus: comments-to-reactions ratio. High ratio
    # (>10%) means it's an actual conversation, not a one-way like dump.
    if reactions > 0:
        disc_ratio = comments / reactions
    else:
        disc_ratio = 0
    disc_bonus = min(disc_ratio * 5, 1.0)  # up to +1.0x

    virality = velocity * reach_mult * age_decay * (1.0 + disc_bonus)

    return round(velocity, 2), round(virality, 2), round(age_hours, 2)


def _normalize_post_url(url):
    """Normalize a LinkedIn post URL to the canonical /feed/update/<urn> form,
    preserving the URN namespace (activity vs share vs ugcPost).

    Reality check (verified 2026-05-01 with Andreas Mautsch's "Apple Container"
    post): activity / share / ugcPost URNs for the same logical post are
    DIFFERENT numeric IDs and LinkedIn does NOT auto-redirect across them.
    /feed/update/urn:li:activity:<share_id>/ returns "Post not found" if the
    numeric is actually a share ID. So we MUST keep the original namespace,
    not collapse to activity. See linkedin_url.py docstring for context.

    Inputs accepted:
      * /feed/update/urn:li:activity:NUMERIC/
      * /feed/update/urn:li:share:NUMERIC/
      * /feed/update/urn:li:ugcPost:NUMERIC/
      * /posts/SLUG-activity-NUMERIC-RANDOM (3-dot-menu copy-link form)
      * /posts/SLUG-share-NUMERIC-RANDOM
      * /posts/SLUG-ugcPost-NUMERIC-RANDOM
    """
    if not url:
        return None
    m = re.search(r"urn:li:(activity|share|ugcPost):(\d{16,19})", url)
    if m:
        return f"https://www.linkedin.com/feed/update/urn:li:{m.group(1)}:{m.group(2)}/"
    # Slug form from "Copy link to post" 3-dot menu. The URN type is
    # encoded in the slug as -activity-NUM-, -share-NUM-, or -ugcPost-NUM-.
    m = re.search(r"-(activity|share|ugcPost)-(\d{16,19})\b", url, re.IGNORECASE)
    if m:
        # Normalize the type token's case (LinkedIn always emits ugcPost
        # camel-cased; activity/share lowercase).
        urn_type = m.group(1)
        if urn_type.lower() == "ugcpost":
            urn_type = "ugcPost"
        else:
            urn_type = urn_type.lower()
        return f"https://www.linkedin.com/feed/update/urn:li:{urn_type}:{m.group(2)}/"
    return url.strip().rstrip("/") + "/"


def _parse_age_hours(cand):
    """Pull age_hours out of the candidate, falling back to post_posted_at.

    Phase B's scrape generally writes age_hours directly (parsed from the
    relative timestamp string LinkedIn renders, e.g. "5h", "2d"). If the
    LLM instead wrote an ISO timestamp, derive age from it.
    """
    raw = cand.get("age_hours")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    posted_at = cand.get("post_posted_at") or cand.get("posted_at")
    if posted_at:
        try:
            dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        except (ValueError, TypeError):
            pass
    return None


def _fetch_posted_urls():
    """Return the set of normalized LinkedIn thread URLs we've already posted
    on, scoped per-account when an account name is configured (falls back to
    unscoped for legacy single-account behavior).

    Migrated 2026-06-01 from a direct `SELECT thread_url FROM posts` to the
    s4l.ai HTTP API (GET /api/v1/posts/thread-urls).
    """
    _li_name = _resolve_account("linkedin")
    resp = api_get(
        "/api/v1/posts/thread-urls",
        query={"platform": "linkedin", "our_account": _li_name},
    )
    urls = (resp.get("data") or {}).get("thread_urls") or []
    posted = set()
    for u in urls:
        norm = _normalize_post_url(u)
        if norm:
            posted.add(norm)
    return posted


def upsert_candidates(candidates, batch_id=None):
    """Score and upsert LinkedIn candidates. Returns (inserted, skipped, errors).

    Migrated 2026-06-01 from direct psycopg2 INSERT...ON CONFLICT to the s4l.ai
    HTTP API (POST /api/v1/linkedin-candidates, which mirrors the upsert
    server-side). The dedup-against-posted query moved to
    GET /api/v1/posts/thread-urls. Scoring stays client-side (pure Python).
    """
    # Dedupe against already-posted LinkedIn threads (the engaged-id check
    # in run-linkedin.sh covers URN-level dedup, but this catches URL-level
    # dupes too in case someone hand-feeds candidates).
    posted_urls = _fetch_posted_urls()

    inserted = skipped = errors = 0

    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        post_url = _normalize_post_url(cand.get("post_url"))
        if not post_url:
            errors += 1
            continue

        # Skip URLs we already posted on
        if post_url in posted_urls:
            skipped += 1
            continue

        age_hours = _parse_age_hours(cand)
        if age_hours is None:
            # Unknown age = treat as cold so it ranks below known-fresh,
            # but don't auto-reject (LinkedIn relative timestamps fail to
            # parse on long-tail formats like "1mo").
            age_hours = MAX_AGE_HOURS

        if age_hours > MAX_AGE_HOURS:
            skipped += 1
            continue

        cand["age_hours"] = age_hours
        velocity, virality, age_clamped = calculate_velocity_score(cand)

        # Resolve post_posted_at if not provided (we can derive from age)
        post_posted_at = cand.get("post_posted_at") or cand.get("posted_at")
        if not post_posted_at and age_hours is not None:
            try:
                from datetime import timedelta
                post_posted_at = (
                    datetime.now(timezone.utc) - timedelta(hours=age_hours)
                ).isoformat()
            except Exception:
                post_posted_at = None

        all_urns = cand.get("all_urns") or []
        if isinstance(all_urns, list):
            all_urns_str = ",".join(str(u) for u in all_urns if u)
        else:
            all_urns_str = str(all_urns)

        payload = {
            "post_url": post_url,
            "activity_id": cand.get("activity_id") or None,
            "all_urns": all_urns_str or None,
            "author_name": cand.get("author_name") or None,
            "author_profile_url": cand.get("author_profile_url") or None,
            "author_followers": int(cand.get("author_followers") or 0) or None,
            "post_text": (cand.get("post_text") or "") or None,
            "post_posted_at": post_posted_at,
            "age_hours": age_clamped,
            "reactions": int(cand.get("reactions") or 0),
            "comments": int(cand.get("comments") or 0),
            "reposts": int(cand.get("reposts") or 0),
            "engagement_velocity": velocity,   # raw
            "velocity_score": virality,        # post-multiplier
            "serp_quality_score": (
                float(cand["serp_quality_score"])
                if cand.get("serp_quality_score") is not None else None
            ),
            "search_topic": cand.get("search_topic") or None,
            "search_query": cand.get("search_query") or None,
            "matched_project": cand.get("matched_project") or None,
            "language": cand.get("language") or "en",
            "batch_id": batch_id,
        }

        try:
            # The endpoint mirrors the ON CONFLICT (post_url) DO UPDATE upsert:
            # COALESCE-bumps discovery fields, overwrites engagement metrics,
            # and preserves terminal statuses ('posted'/'skipped') while
            # resetting everything else to 'pending'.
            api_post("/api/v1/linkedin-candidates", payload)
            inserted += 1
        except SystemExit as e:
            print(f"  Error inserting {post_url}: {e}", file=sys.stderr)
            errors += 1
            continue

    expire_and_prune()
    return inserted, skipped, errors


def expire_and_prune(_conn=None):
    """Flip stale pending rows to 'expired'. We do NOT prune terminal rows
    by age (per user instruction 2026-05-08); every linkedin_candidates row
    is kept forever so we can audit skip reasons, engagement dynamics, and
    project routing across time. Function name kept for caller compatibility.

    Migrated 2026-06-01 to POST /api/v1/linkedin-candidates/expire-stale.
    The optional _conn arg is ignored (legacy signature compatibility).
    """
    api_post(
        "/api/v1/linkedin-candidates/expire-stale",
        {"hours": int(EXPIRE_PENDING_AFTER_HOURS)},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Read JSON from a file instead of stdin")
    parser.add_argument("--batch-id", help="Tag this batch on every row")
    parser.add_argument("--expire-only", action="store_true",
                        help="Only run expire/prune, no scoring or insert")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress final stdout summary line")
    args = parser.parse_args()

    if args.expire_only:
        expire_and_prune()
        if not args.quiet:
            print("Expired/pruned old linkedin_candidates")
        return 0

    if args.file:
        with open(args.file) as f:
            data = json.load(f)
    else:
        raw = sys.stdin.read().strip()
        if not raw:
            print("score_linkedin_candidates: empty stdin, nothing to score",
                  file=sys.stderr)
            return 0
        data = json.loads(raw)

    if not isinstance(data, list):
        data = [data]

    inserted, skipped, errors = upsert_candidates(data, batch_id=args.batch_id)
    if not args.quiet:
        print(
            f"score_linkedin_candidates: upserted={inserted} "
            f"skipped={skipped} errors={errors} batch={args.batch_id}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
