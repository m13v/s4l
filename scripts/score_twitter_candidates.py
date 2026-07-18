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

# Best-effort dedicated logger for the follow-gate -> skill/logs/follow-gate.log.
# Guarded so a missing/older helper file can never break scoring (fail-open).
try:
    import follow_gate_log as _fgl  # noqa: E402
except Exception:
    _fgl = None
from twitter_account import resolve_handle as _resolve_twitter_handle  # noqa: E402
from project_topics import topics_for_project  # noqa: E402
from virality import age_decay as _shared_age_decay, TWITTER_HALF_LIFE_HOURS  # noqa: E402


# Freshness window (in hours) for the expire-stale gate that flips stale
# pending rows to status='expired'. Sourced from the FRESHNESS_HOURS env the
# cycle exports (run-twitter-cycle.sh) so the expiry ceiling is configured in
# ONE place. Falls back to 18 when unset (e.g. ad-hoc / --expire-only runs) to
# preserve the historical default.
#
# 2026-07-15: the gate now measures thread age (tweet_posted_at), not
# discovery age (discovered_at) — see EXPIRE_BASIS below. It used to be
# discovered_at; that was harmless while discovery was capped at 1h (the two
# stayed within ~1h of each other), but wrong once discovery widened to 6h
# alongside this ceiling (a discovered_at-based gate would then let real
# thread age reach ~12h before expiring).
EXPIRE_FRESHNESS_HOURS = int(os.environ.get("FRESHNESS_HOURS") or "18")

# Opt-in basis param for the expire-stale route (see
# ~/social-autoposter-website twitter-candidates/expire-stale): the route
# defaults to discovered_at for every other install, so this is passed
# explicitly rather than changing the route's default.
EXPIRE_BASIS = "tweet_posted_at"


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


# Weight on the additive reach-potential term in calculate_virality_score
# (2026-05-28). Tunable. Larger = a fresh high-follower thread with no
# engagement yet ranks higher relative to threads with demonstrated velocity.
# At 0.6, a freshly-posted tweet from a 50k-200k account scores ~4 on reach
# alone; a 200M account ~5.4; a sub-1k account stays near 0. Set to 0 to fall
# back to the pure multiplicative (engagement-only) score.
REACH_POTENTIAL_WEIGHT = 0.6


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
    # Shared with the Reddit scorer (scripts/virality.py, 2026-07-18); only
    # the half-life differs (6h here vs 7 days there).
    age_decay = _shared_age_decay(age_hours, TWITTER_HALF_LIFE_HOURS)

    # 7. Retweet ratio bonus
    rt_bonus = 1.0 + min(rt_ratio * 2, 1.0)  # up to 2x for high RT ratio

    # Engagement-driven score (multiplicative). This collapses to 0 for any
    # tweet with zero engagement, because velocity (= total_eng / age) gates the
    # entire product. That is correct for ranking *demonstrated* momentum.
    engagement_score = velocity * reach_mult * age_decay * rt_bonus * (1 + reply_bonus) * (1 + discussion_bonus)

    # Reach-potential term (ADDITIVE, 2026-05-28). The multiplicative score above
    # throws away the follower signal whenever engagement is 0: a freshly-posted
    # tweet from a 200M-follower account scored identically (0.0) to a 1-follower
    # nobody, because anything * 0 = 0. That is wrong as a *predictor* — catching
    # a fresh thread on a large account early is real option value (the account
    # reliably draws reach the thread just hasn't accumulated yet). We ADD (not
    # multiply) a reach term so the follower signal survives a zero-engagement
    # velocity. It is monotonic in followers (log10 growth dominates the
    # mega-account reach_mult dip) and decays on the SAME 6h half-life via
    # age_decay, so a stale big-account tweet that STILL has no engagement sinks
    # back toward zero (a real dud), while a fresh one ranks above a fresh nobody.
    # No cap, no cutoff: this only ever raises a score, never removes a candidate.
    reach_potential = math.log10(max(followers, 1)) * reach_mult * age_decay * REACH_POTENTIAL_WEIGHT

    score = engagement_score + reach_potential

    return round(score, 2), round(velocity, 2), round(rt_ratio, 3)


def match_project(tweet_text, search_topic, config):
    """Match a tweet to the best project based on topic and content."""
    projects = config.get("projects", [])

    # If search_topic maps to a specific project, use that
    topic_lower = (search_topic or "").lower()
    text_lower = (tweet_text or "").lower()

    for proj in projects:
        name = proj.get("name", "")
        topics = [t.lower() for t in topics_for_project(name)]
        # Direct topic match
        for t in topics:
            if t in topic_lower or t in text_lower:
                return name

    return None


def upsert_candidates(tweets, config, batch_id=None, attempts_map=None, scored_sidecar=None):
    """Score and upsert tweet candidates into DB.

    If batch_id is provided, also populates T0 engagement columns and tags
    the row with batch_id so Phase 2 of the cycle can re-poll only this batch.

    If attempts_map is provided (dict keyed by (query, project) -> attempt_id),
    stamps twitter_candidates.search_attempt_id so dashboard per-query stats
    can attribute each posted candidate to the exact discovering search,
    rather than fanning out (batch_id, project_name) across every query the
    batch ran for that project (2026-05-21 bug fix).

    If scored_sidecar is provided, writes per-query verdict tallies to that
    JSON path so run-twitter-cycle.sh can build the directional
    TRIED_QUERIES_JSON for the next retry attempt's prompt (2026-05-28
    retry-feedback loop). Shape:
        {query_string: {raw, kept_after_age, kept_after_skip}, ...}
    raw = tweets fed to upsert_candidates from the enrich step
    kept_after_age = tweets surviving the FRESHNESS_HOURS_DISCOVER cap
    kept_after_skip = tweets that made it through to api_post insert
    Never raises if the path isn't writable; the verdict step falls back to
    raw == kept_after_age (no all_aged_out distinction) when the sidecar is
    missing.

    Migrated 2026-05-18 to call the s4l.ai HTTP API:
      - dedup probe -> GET /api/v1/posts/thread-urls?platform=twitter
      - per-tweet upsert -> POST /api/v1/twitter-candidates
        (route handles the ON CONFLICT + peer-cycle race guard server-side)
      - freshness gate -> POST /api/v1/twitter-candidates/expire-stale
        (default 18h window; never deletes rows — status flip only)
    """
    attempts_map = attempts_map or {}
    # Per-query tally for the scored sidecar. We seed `raw` upfront so a query
    # whose every tweet was dropped (stale, fabricated, ceiling) still shows
    # up with raw>0, kept_after_age=0 -> all_aged_out verdict instead of
    # silently disappearing into the kept_after_skip=0 branch.
    sidecar = {}
    if scored_sidecar:
        for _t in tweets:
            _q = (_t.get("query") or "").strip()
            if not _q:
                continue
            ent = sidecar.setdefault(_q, {"raw": 0, "kept_after_age": 0, "kept_after_skip": 0})
            ent["raw"] += 1
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

    # Get already-SKIPPED (tweet_url, project) pairs for the per-project skip
    # gate. Claude explicitly rejected these threads for the matched project in
    # a prior cycle (status='skipped'); since the Phase 2b prompt now reserves
    # 'rejected' for permanent, thread-intrinsic reasons (transient cap / dedup
    # / cooldown deferrals are left pending, never skipped), every skipped row
    # is a genuine rejection safe to suppress from future scans permanently.
    # Per-project so a thread skipped as fazm stays eligible if a later scan
    # matches it to a different project. Fail-open: ok_on_404 + try/except so a
    # missing/unavailable endpoint behaves exactly like the pre-feature cycle
    # (no skip filtering) instead of crashing Phase 1.
    skipped_pairs = set()
    if _twitter_handle:
        try:
            _skip_resp = api_get(
                "/api/v1/twitter-candidates/skipped-urls",
                query={"our_account": _twitter_handle},
                ok_on_404=True,
            )
            if _skip_resp.get("_not_found"):
                # 404: endpoint not deployed yet. Explicit so a 0-pair gate is
                # never mistaken for "loaded the set, nothing matched".
                print(
                    f"[skip_gate] fail-open: skipped-urls endpoint 404 "
                    f"(not deployed) our_account={_twitter_handle}; "
                    f"skip filter inactive this cycle",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                for _pair in (_skip_resp.get("data") or {}).get("pairs") or []:
                    _su = (_pair.get("tweet_url") or "").strip()
                    if _su:
                        skipped_pairs.add((_su, _pair.get("project")))
        except SystemExit as _skip_err:
            # http_api raises SystemExit on terminal HTTP failure (e.g. a 429
            # rate-limit, which is a 4xx). Fail open: an empty set means the
            # gate is inert this cycle rather than crashing Phase 1. Logged
            # explicitly so an inert gate is distinguishable from a real
            # no-match (both otherwise show "already rejected for project: 0").
            skipped_pairs = set()
            print(
                f"[skip_gate] fail-open: skipped-urls fetch failed "
                f"({_skip_err}); skip filter inactive this cycle",
                file=sys.stderr,
                flush=True,
            )
    # Always emit the loaded size so every cycle self-documents whether the
    # gate had real data (N>0) or fell open (N=0). Pairs with N>0 is the
    # positive proof that the check ran against the live skipped set.
    print(
        f"[skip_gate] loaded {len(skipped_pairs)} skipped (url,project) pairs "
        f"for our_account={_twitter_handle or '(unresolved)'}",
        file=sys.stderr,
        flush=True,
    )

    # Skip threads whose author is someone we already follow. We don't need to
    # win over accounts already in our network — the comment buys no new reach.
    # The follow list is harvested out-of-band (scripts/harvest_twitter_following.py
    # scrapes x.com/<handle>/following) and stored server-side; we just read the
    # set here, scoped to our posting handle. Fail-open exactly like the skip gate
    # above: a missing endpoint / 429 / unresolved handle leaves the set empty so
    # the cycle behaves exactly as it did before this guardrail (2026-06-03).
    followed_handles = set()
    _follow_source = "unresolved"
    if _twitter_handle:
        try:
            _foll_resp = api_get(
                "/api/v1/followed-accounts",
                query={"platform": "twitter", "our_account": _twitter_handle},
                ok_on_404=True,
            )
            if _foll_resp.get("_not_found"):
                _follow_source = "404"
                print(
                    f"[follow_gate] fail-open: followed-accounts endpoint 404 "
                    f"(not deployed) our_account={_twitter_handle}; "
                    f"follow filter inactive this cycle",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                _follow_source = "ok"
                for _fh in (_foll_resp.get("data") or {}).get("handles") or []:
                    _fhs = (_fh or "").strip().lstrip("@").lower()
                    if _fhs:
                        followed_handles.add(_fhs)
        except SystemExit as _foll_err:
            _follow_source = "error"
            followed_handles = set()
            print(
                f"[follow_gate] fail-open: followed-accounts fetch failed "
                f"({_foll_err}); follow filter inactive this cycle",
                file=sys.stderr,
                flush=True,
            )
    print(
        f"[follow_gate] loaded {len(followed_handles)} followed handles "
        f"for our_account={_twitter_handle or '(unresolved)'}",
        file=sys.stderr,
        flush=True,
    )

    inserted = updated = skipped = 0
    skipped_fake_id = 0
    skipped_already_rejected = 0
    skipped_followed_author = 0

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

        # Skip threads authored by someone we already follow (guardrail
        # 2026-06-03). Same class as the posted-dedup above: an identity-based
        # global skip, independent of project, so it lives here (before age math
        # and scoring). followed_handles is the harvested set loaded once above
        # (empty => gate inert, fail-open). enrich_twitter_candidates.py has
        # already canonicalized tweet["handle"] to the author's screen_name.
        _cand_handle = (tweet.get("handle") or "").strip().lstrip("@").lower()
        if _cand_handle and _cand_handle in followed_handles:
            skipped += 1
            skipped_followed_author += 1
            print(
                f"[follow_gate] skip @{tweet.get('handle')} (followed) url={url}",
                file=sys.stderr,
                flush=True,
            )
            if _fgl:
                _fgl.record_skip(_twitter_handle, _cand_handle, url, batch_id)
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

        # Tally kept_after_age for the verdict sidecar BEFORE the ceiling-D
        # cap below. all_aged_out means "the freshness gate killed everything";
        # ceiling-D is a quality filter that fires after age. Keeping the
        # tallies separate prevents D-cycle queries from looking like aged-out
        # to the next retry's drafter.
        if scored_sidecar:
            _q_age = (tweet.get("query") or "").strip()
            if _q_age and _q_age in sidecar:
                sidecar[_q_age]["kept_after_age"] += 1

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

        # Skip threads Claude already explicitly rejected for THIS project
        # (status='skipped'). Per-project: a thread skipped as fazm can still be
        # picked if this scan matched it to a different project, so we key on
        # (url, project) rather than url alone. Done here (not at the posted
        # dedup above) because the project isn't resolved until this point.
        if (url, project) in skipped_pairs:
            skipped += 1
            skipped_already_rejected += 1
            print(
                f"  [skipped_already_rejected] {project}: {url}",
                file=sys.stderr,
            )
            continue

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
            # If omitted, the server stamps a per-install 'unknown:<install_id>'
            # sentinel (2026-07-18; it used to default to 'm13v_', silently
            # impersonating the repo owner). Callers should always pass it —
            # _resolve_twitter_handle() covers env -> config -> cookie mirror.
            "our_account": _twitter_handle or "",
            # Repost provenance (2026-06-04). The scan derives the author from
            # the status URL, so author_handle/tweet_url already point at the
            # ORIGINAL tweet; is_repost flags that it surfaced via a repost and
            # reposted_by names the account that reposted. Only sent when the
            # scan evaluated it (presence-detected server-side).
            "is_repost": bool(tweet.get("is_repost", False)),
            "reposted_by": tweet.get("reposted_by", "") or "",
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
            if scored_sidecar:
                _q_kept = (tweet.get("query") or "").strip()
                if _q_kept and _q_kept in sidecar:
                    sidecar[_q_kept]["kept_after_skip"] += 1
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

    # Expire old pending candidates past the freshness window. This is a
    # freshness GATE (status flip), not a delete — we keep the row forever
    # for analytics.
    api_post(
        "/api/v1/twitter-candidates/expire-stale",
        {"freshness_hours": EXPIRE_FRESHNESS_HOURS, "basis": EXPIRE_BASIS},
    )

    # NO PRUNING. We keep every twitter_candidates row forever (chosen, skipped,
    # expired) so we can audit project routing, skip reasons, growth dynamics,
    # and engagement curves over time. Per user instruction (2026-05-08): never
    # add DELETE-by-age back here, regardless of retention window.

    if _fgl:
        _fgl.record_cycle(_twitter_handle, len(followed_handles), _follow_source, len(tweets), skipped_followed_author, batch_id)
    print(f"Scored: {inserted} upserted, {skipped} skipped (already posted or fabricated ID: {skipped_fake_id}, already rejected for project: {skipped_already_rejected}, followed authors: {skipped_followed_author})")

    # Emit the verdict sidecar for the retry loop's directional feedback. Best
    # effort: never fatal if the path is unwritable, never overwrites the
    # cycle's other state.
    if scored_sidecar:
        try:
            with open(scored_sidecar, "w") as fh:
                json.dump(sidecar, fh)
            print(
                f"scored_sidecar: wrote {len(sidecar)} query verdicts -> {scored_sidecar}",
                file=sys.stderr,
            )
        except OSError as e:
            print(
                f"scored_sidecar: could not write {scored_sidecar}: {e}",
                file=sys.stderr,
            )

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
    parser.add_argument(
        "--scored-sidecar",
        help="Path to write per-query verdict tallies for the retry loop "
             "feedback (2026-05-28). Shape: {query: {raw, kept_after_age, "
             "kept_after_skip}, ...}. Consumed by run-twitter-cycle.sh to "
             "build the directional TRIED_QUERIES_JSON for the next attempt's "
             "drafter prompt.",
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
            {"freshness_hours": EXPIRE_FRESHNESS_HOURS, "basis": EXPIRE_BASIS},
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

    upsert_candidates(
        tweets,
        config,
        batch_id=args.batch_id,
        attempts_map=attempts_map,
        scored_sidecar=args.scored_sidecar,
    )


if __name__ == "__main__":
    main()
