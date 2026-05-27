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
from twitter_account import resolve_handle as _resolve_twitter_handle  # noqa: E402


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


def upsert_candidates(tweets, config, batch_id=None, attempts_map=None):
    """Score and upsert tweet candidates into DB.

    If batch_id is provided, also populates T0 engagement columns and tags
    the row with batch_id so Phase 2 of the cycle can re-poll only this batch.

    If attempts_map is provided (dict keyed by (query, project) -> attempt_id),
    stamps twitter_candidates.search_attempt_id so dashboard per-query stats
    can attribute each posted candidate to the exact discovering search,
    rather than fanning out (batch_id, project_name) across every query the
    batch ran for that project (2026-05-21 bug fix).

    Migrated 2026-05-18 to call the s4l.ai HTTP API:
      - dedup probe -> GET /api/v1/posts/thread-urls?platform=twitter
      - per-tweet upsert -> POST /api/v1/twitter-candidates
        (route handles the ON CONFLICT + peer-cycle race guard server-side)
      - freshness gate -> POST /api/v1/twitter-candidates/expire-stale
        (default 18h window; never deletes rows — status flip only)
    """
    attempts_map = attempts_map or {}
    # Get already-posted thread URLs for dedup. Scope per-account so the mk0r
    # VM running as @matt_diak doesn't skip a tweet that @m13v_ posted on
    # (or vice versa). Falls back to unscoped when the resolver can't pin a
    # handle, which preserves the legacy single-machine behavior.
    _twitter_handle = _resolve_twitter_handle()
    _probe_query = {"platform": "twitter"}
    if _twitter_handle:
        _probe_query["our_account"] = _twitter_handle
    posted_resp = api_get("/api/v1/posts/thread-urls", query=_probe_query)
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

        tweet["age_hours"] = age_hours
        tweet["author_followers"] = tweet.get("author_followers", 0)

        # Hard age cutoff (2026-05-27): defense-in-depth against X's Latest tab
        # silently degrading to "best available" results when our `since_time:`
        # operator yields a sparse window. The pre-search hook
        # (~/.claude/hooks/twitter-search-since-rewrite.py) injects
        # `since_time:<now - FRESHNESS_HOURS_DISCOVER>` into every cycle query,
        # and the harness scrape opens &f=live (Latest tab). In theory those
        # two together cap age at the variant's freshness window. In practice
        # x.com/search?f=live ignores `since_time:` on low-yield queries and
        # falls back to whatever stale tweets it has. Without this cutoff,
        # those stale rows land in twitter_candidates with virality ~0 (the
        # 6h half-life decay floors them), survive into the post_twitter
        # draft prompt, and get chosen when all candidates score near zero.
        # We hard-drop here so they never reach the API, the draft prompt,
        # or any per-row token spend. Reads the same env var the hook reads,
        # so the cutoff matches the variant's window (1h for C/D, 6h for A/B).
        # Falls back to 6h for non-cycle callers (legacy paths). Discovered
        # 2026-05-27 after batches twcycle-20260527-134432 (Mediar) and
        # twcycle-20260527-135430 (paperback-expert) posted under 49-77h-old
        # threads that bypassed both layers.
        try:
            _freshness_cap = int(os.environ.get("FRESHNESS_HOURS_DISCOVER") or "6")
        except ValueError:
            _freshness_cap = 6
        if age_hours > _freshness_cap:
            skipped += 1
            print(
                f"[stale_age_skip] age_hours={age_hours:.1f} cap={_freshness_cap}h "
                f"variant={os.environ.get('TWITTER_CYCLE_VARIANT') or ''} "
                f"url={url}",
                file=sys.stderr,
                flush=True,
            )
            continue

        # Variant D (2026-05-25): 2k-view ceiling cap on parent thread.
        # Bucket analysis on 250+ mature posts showed view-share collapses
        # from ~4% on 500-2k-view threads to ~0.1% on >10k-view threads —
        # our reply is invisible to the audience of large threads. D drops
        # any candidate whose T0 views exceed 2000; A/B/C let everything
        # through unchanged. Comparing posted-quality (views/likes per
        # surviving candidate) between D and C isolates the ceiling effect.
        # No DB row written for rejects: the dashboard already groups by
        # cycle_variant and the stderr marker below captures opportunity
        # cost for later log-based analysis.
        _ceiling_variant = os.environ.get("TWITTER_CYCLE_VARIANT") or ""
        _ceiling_views = tweet.get("views", 0) or 0
        if _ceiling_variant == "D" and _ceiling_views > 2000:
            skipped += 1
            print(
                f"[ceiling_d_skip] views_t0={_ceiling_views} "
                f"likes={tweet.get('likes', 0)} replies={tweet.get('replies', 0)} "
                f"age_hours={age_hours:.2f} url={url}",
                file=sys.stderr,
                flush=True,
            )
            continue

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
            "discovery_batch_id": batch_id,
            "cycle_variant": os.environ.get("TWITTER_CYCLE_VARIANT") or None,
            # Stamp the machine's Twitter handle so the (tweet_url, our_account)
            # composite unique gives each account its own candidate row.
            # Without this, account A's 'posted' status on tweet X would lock
            # account B out of the same tweet (ON CONFLICT preserved 'posted').
            # Defaults server-side to 'm13v_' if omitted; new callers should
            # always pass it explicitly.
            "our_account": _twitter_handle or "",
        }
        # Stamp the exact discovering search_attempt when the scanner gave us
        # the literal query that surfaced this tweet AND the log script wrote
        # an attempts map. Dashboard SQL prefers this column over the legacy
        # (batch_id, project_name) fanout, which credits dud queries with
        # posts they never surfaced.
        _q = (tweet.get("query") or "").strip()
        if _q and attempts_map:
            attempt_id = attempts_map.get((_q, project))
            if attempt_id is None:
                attempt_id = attempts_map.get((_q, None))
            if attempt_id is not None:
                body["search_attempt_id"] = int(attempt_id)
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
            try:
                _tweet_iso = body.get("tweet_posted_at") or body.get("tweet_created_at") or ""
                _disc_iso = body.get("discovered_at") or body.get("created_at") or ""
                _url = body.get("tweet_url") or body.get("url") or ""
                _age_h = ""
                if _tweet_iso and _disc_iso:
                    from datetime import datetime as _dt
                    try:
                        _t = _dt.fromisoformat(_tweet_iso.replace("Z", "+00:00"))
                        _d = _dt.fromisoformat(_disc_iso.replace("Z", "+00:00"))
                        _age_h = f"{(_d.timestamp() - _t.timestamp()) / 3600:.2f}"
                    except Exception:
                        _age_h = ""
                print(
                    f"[twitter_discovery] batch_id={batch_id} "
                    f"discovery_batch_id={batch_id} "
                    f"cycle_variant={os.environ.get('TWITTER_CYCLE_VARIANT') or ''} "
                    f"tweet_age_hours={_age_h} discovered_at={_disc_iso} url={_url}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception:
                pass
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

    print(f"Scored: {inserted} upserted, {skipped} skipped (already posted or fabricated ID: {skipped_fake_id})")
    return inserted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Read tweets from JSON file instead of stdin")
    parser.add_argument("--expire-only", action="store_true", help="Only expire stale pending rows (status flip; no row deletion)")
    parser.add_argument("--batch-id", help="Tag these candidates with a batch id and populate T0 columns")
    parser.add_argument(
        "--attempts",
        help="Path to JSON list [{query, project, attempt_id}, ...] from "
             "log_twitter_search_attempts.py --attempts-out. When provided, "
             "stamps twitter_candidates.search_attempt_id per tweet so the "
             "dashboard can attribute posts to the exact discovering query.",
    )
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

    attempts_map = {}
    if args.attempts and os.path.exists(args.attempts):
        try:
            with open(args.attempts) as f:
                rows = json.load(f)
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                q = (r.get("query") or "").strip()
                aid = r.get("attempt_id")
                if not q or aid is None:
                    continue
                proj = r.get("project") or None
                attempts_map[(q, proj)] = int(aid)
            print(
                f"score_twitter_candidates: loaded {len(attempts_map)} "
                f"(query, project) -> attempt_id entries from {args.attempts}",
                file=sys.stderr,
            )
        except (OSError, ValueError) as e:
            print(
                f"score_twitter_candidates: could not read attempts map "
                f"{args.attempts}: {e}",
                file=sys.stderr,
            )

    upsert_candidates(tweets, config, batch_id=args.batch_id, attempts_map=attempts_map)


if __name__ == "__main__":
    main()
