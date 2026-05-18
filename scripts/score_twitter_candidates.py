#!/usr/bin/env python3
"""
score_twitter_candidates.py

Reads raw tweet data (JSON from stdin or file), calculates virality scores,
and upserts into the twitter_candidates table.

Also expires stale pending candidates by flipping status to 'expired'.
NO PRUNING: rows are kept forever for analytics (skip-reason audit, engagement
dynamics, project routing review). Per user instruction 2026-05-08, do not
re-introduce DELETE-by-age here under any retention window.

Can be called standalone or piped from the scanner:
    echo '[{...}]' | python3 scripts/score_twitter_candidates.py
    python3 scripts/score_twitter_candidates.py --file /tmp/tweets.json
    python3 scripts/score_twitter_candidates.py --expire-only
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402


# Real Twitter snowflake IDs are 18-19 digit numbers with full entropy in the
# low bits (sequence counter + worker/datacenter ID = bottom 22 bits ≈ bottom
# 7 decimal digits). An ID ending in 6+ zeros is statistically impossible
# unless the sequence counter, worker ID, and datacenter ID were all exactly 0
# at submission AND the timestamp aligned with a power-of-two ms boundary —
# combined probability ≈ 0. Observed 2026-05-16 (batch twcycle-20260516-080005):
# the harness scan model fabricates IDs by templating a high-digit prefix and
# zero-padding, e.g. 2055588000000000000, 2055590000000000000 (sequential by 1).
# fxtwitter rejects these at T1 ("truncated/invalid status ID and loads no
# tweet"). Drop them at score time so we don't burn draft tokens or candidate
# rows on phantom URLs.
_SNOWFLAKE_OK = re.compile(r"/status/(\d{15,19})(?:[/?#]|$)")
_TRAILING_ZEROS_FAKE = re.compile(r"0{6,}$")


def looks_like_fabricated_tweet_url(url: str) -> bool:
    """True if the URL's snowflake suffix is the model's fabrication signature.

    Returns True for:
      - URLs without a parseable /status/<digits> segment
      - URLs whose snowflake ID is outside the plausible 15-19 digit range
      - URLs whose snowflake ID ends in 6 or more zeros (template signature)
    """
    if not url:
        return True
    m = _SNOWFLAKE_OK.search(url)
    if not m:
        return True
    sid = m.group(1)
    if _TRAILING_ZEROS_FAKE.search(sid):
        return True
    return False


def calculate_virality_score(tweet):
    """
    Score a tweet's viral potential. Higher = better candidate to reply to.

    Signals (from research + production tuning):
    1. Engagement velocity (eng/hour) - strongest predictor
    2. Retweet ratio > 0.3 = strong viral signal
    3. Reply count is weighted heavily (discussion = visibility for our reply)
    4. Reply-to-like ratio (discussion quality vs one-way broadcast)
    5. Author followers 5K+ sweet spot, big names not penalized
    6. Age penalty: exponential decay with 6h half-life (softer than before)
    """
    likes = tweet.get("likes", 0)
    retweets = tweet.get("retweets", 0)
    replies = tweet.get("replies", 0)
    bookmarks = tweet.get("bookmarks", 0)
    views = tweet.get("views", 0)
    followers = tweet.get("author_followers", 0)

    total_eng = likes + retweets + replies + bookmarks

    # Age in hours
    age_hours = tweet.get("age_hours", 1)
    if age_hours < 0.1:
        age_hours = 0.1

    # 1. Engagement velocity (most important)
    velocity = total_eng / age_hours

    # 2. Retweet ratio (reshare intent)
    rt_ratio = retweets / total_eng if total_eng > 0 else 0

    # 3. Reply activity bonus (active discussion = more visibility for our reply)
    # 15 replies = +1x, 30 = +2x, 60+ = +4x cap
    reply_bonus = min(replies / 15, 4.0)

    # 4. Discussion quality (reply:like ratio). High ratio = real discussion.
    # 0.05 ratio = +0.5x, 0.1+ = +1.0x cap
    discussion_ratio = replies / likes if likes > 0 else 0
    discussion_bonus = min(discussion_ratio * 10, 1.0)

    # 5. Author reach multiplier
    # Sweet spot: 5K+ followers. Big names (KentBeck-class) get full credit,
    # since brand value outweighs the "too competitive" concern.
    if followers < 1000:
        reach_mult = 0.3
    elif followers < 5000:
        reach_mult = 0.6
    elif followers < 50000:
        reach_mult = 1.0
    elif followers < 200000:
        reach_mult = 1.4
    elif followers < 500000:
        reach_mult = 1.3
    else:
        reach_mult = 1.1  # mega accounts still worth it for brand exposure

    # 6. Age decay: half-life of 6 hours (softened from 3h)
    # 3h = 71%, 6h = 50%, 12h = 25%, 18h = 12.5%
    age_decay = math.exp(-0.1155 * age_hours)  # ln(2)/6

    # 7. Retweet ratio bonus
    rt_bonus = 1.0 + min(rt_ratio * 2, 1.0)  # up to 2x for high RT ratio

    # Combine
    score = velocity * reach_mult * age_decay * rt_bonus * (1 + reply_bonus) * (1 + discussion_bonus)

    return round(score, 2), round(velocity, 2), round(rt_ratio, 3)


def match_project(tweet_text, search_topic, config):
    """Match a tweet to the best project based on topic and content."""
    projects = config.get("projects", [])

    # If search_topic maps to a specific project, use that
    topic_lower = (search_topic or "").lower()
    text_lower = (tweet_text or "").lower()

    for proj in projects:
        name = proj.get("name", "")
        topics = [t.lower() for t in proj.get("search_topics", [])]
        # Direct topic match
        for t in topics:
            if t in topic_lower or t in text_lower:
                return name

    return None


def upsert_candidates(tweets, config, batch_id=None):
    """Score and upsert tweet candidates into DB.

    If batch_id is provided, also populates T0 engagement columns and tags
    the row with batch_id so Phase 2 of the cycle can re-poll only this batch.

    Migrated 2026-05-18 to call the s4l.ai HTTP API:
      - dedup probe -> GET /api/v1/posts/thread-urls?platform=twitter
      - per-tweet upsert -> POST /api/v1/twitter-candidates
        (route handles the ON CONFLICT + peer-cycle race guard server-side)
      - freshness gate -> POST /api/v1/twitter-candidates/expire-stale
        (default 18h window; never deletes rows — status flip only)
    """
    # Get already-posted thread URLs for dedup
    posted_resp = api_get("/api/v1/posts/thread-urls", query={"platform": "twitter"})
    posted = set((posted_resp.get("data") or {}).get("thread_urls") or [])

    inserted = updated = skipped = 0
    skipped_fake_id = 0

    for tweet in tweets:
        url = (tweet.get("tweet_url") or tweet.get("tweetUrl") or "").strip()
        if not url:
            continue

        # Reject hallucinated snowflake IDs (see looks_like_fabricated_tweet_url
        # docstring). Counted separately so the failure mode is visible in the
        # pipeline log; rolled into `skipped` total for backwards-compat metrics.
        if looks_like_fabricated_tweet_url(url):
            skipped += 1
            skipped_fake_id += 1
            print(f"  Drop fabricated snowflake: {url}", file=sys.stderr)
            continue

        # Skip if we already posted on this thread
        if url in posted:
            skipped += 1
            continue

        # Calculate age
        dt_str = tweet.get("datetime", "")
        if dt_str:
            try:
                posted_at = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - posted_at).total_seconds() / 3600
            except ValueError:
                posted_at = None
                age_hours = 24  # unknown age, penalize
        else:
            posted_at = None
            age_hours = 24

        # Skip tweets older than the cycle's Phase 0 freshness wall (6h, set by
        # FRESHNESS_HOURS in skill/run-twitter-cycle.sh). Anything older than that
        # gets hard-expired by the next cycle's Phase 0 before it can be posted,
        # so admitting it just wastes a draft and a candidate row. Keep this
        # value in sync with FRESHNESS_HOURS in run-twitter-cycle.sh.
        if age_hours > 6:
            skipped += 1
            continue

        tweet["age_hours"] = age_hours
        tweet["author_followers"] = tweet.get("author_followers", 0)

        score, velocity, rt_ratio = calculate_virality_score(tweet)

        # Use LLM-assigned project if available, fall back to keyword matching
        project = tweet.get("matched_project") or match_project(
            tweet.get("text", ""),
            tweet.get("search_topic", ""),
            config,
        )

        body = {
            "tweet_url": url,
            "author_handle": tweet.get("handle", ""),
            "author_followers": tweet.get("author_followers", 0),
            "tweet_text": tweet.get("text", "") or "",
            "tweet_posted_at": posted_at.isoformat() if posted_at else None,
            "likes": tweet.get("likes", 0),
            "retweets": tweet.get("retweets", 0),
            "replies": tweet.get("replies", 0),
            "views": tweet.get("views", 0),
            "bookmarks": tweet.get("bookmarks", 0),
            "engagement_velocity": velocity,
            "retweet_ratio": rt_ratio,
            "virality_score": score,
            "search_topic": tweet.get("search_topic", ""),
            "matched_project": project,
            "batch_id": batch_id,
        }
        # T0 columns only stamped when this row was discovered inside a cycle
        # batch, mirroring the conditional in the original SQL.
        if batch_id:
            body["likes_t0"] = tweet.get("likes", 0)
            body["retweets_t0"] = tweet.get("retweets", 0)
            body["replies_t0"] = tweet.get("replies", 0)
            body["views_t0"] = tweet.get("views", 0)
            body["bookmarks_t0"] = tweet.get("bookmarks", 0)

        try:
            api_post("/api/v1/twitter-candidates", body)
            inserted += 1
        except SystemExit as e:
            # http_api raises SystemExit on terminal failure. Keep iterating;
            # the cycle should not die because one URL hit a 4xx validation
            # edge case.
            print(f"  Error inserting {url}: {e}", file=sys.stderr)
            continue

    # Expire old pending candidates (> 18h). This is a freshness GATE
    # (status flip), not a delete — we keep the row forever for analytics.
    api_post("/api/v1/twitter-candidates/expire-stale", {"freshness_hours": 18})

    # NO PRUNING. We keep every twitter_candidates row forever (chosen, skipped,
    # expired) so we can audit project routing, skip reasons, growth dynamics,
    # and engagement curves over time. Per user instruction (2026-05-08): never
    # add DELETE-by-age back here, regardless of retention window.

    print(f"Scored: {inserted} upserted, {skipped} skipped (already posted, too old, or fabricated ID: {skipped_fake_id})")
    return inserted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Read tweets from JSON file instead of stdin")
    parser.add_argument("--expire-only", action="store_true", help="Only expire stale pending rows (status flip; no row deletion)")
    parser.add_argument("--batch-id", help="Tag these candidates with a batch id and populate T0 columns")
    args = parser.parse_args()

    config_path = os.path.expanduser("~/social-autoposter/config.json")
    config = {}
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)

    if args.expire_only:
        # Freshness gate only. NO PRUNING — see note in upsert_candidates().
        # Server-side route runs the same UPDATE atomically; client just kicks
        # it off and prints the count.
        resp = api_post(
            "/api/v1/twitter-candidates/expire-stale",
            {"freshness_hours": 18},
        )
        expired = (resp.get("data") or {}).get("expired_count", 0)
        print(f"Expired {expired} old pending candidates (no row deletion)")
        return

    if args.file:
        with open(args.file) as f:
            tweets = json.load(f)
    else:
        tweets = json.load(sys.stdin)

    if not isinstance(tweets, list):
        tweets = [tweets]

    upsert_candidates(tweets, config, batch_id=args.batch_id)


if __name__ == "__main__":
    main()
