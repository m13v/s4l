#!/usr/bin/env python3
"""Shared virality primitives for the X and Reddit pipelines (2026-07-18).

One home for the pieces both platforms share, so the Reddit bar can't drift
from the X bar again:

  - age_decay():        the exponential half-life decay both scorers use.
                        X keeps its 6h half-life (tweets die in hours);
                        Reddit uses a 7-DAY half-life (threads stay
                        actionable ~7 days, per operator observation).
  - calculate_reddit_virality_score(): the Reddit composite predictor,
                        stamped into reddit_candidates.virality_score at
                        discover time by post_reddit.py. The X equivalent
                        stays in score_twitter_candidates.py (its signals
                        are tweet-specific: retweets, bookmarks, follower
                        reach) but imports age_decay from here.
  - fetch_virality_bar(): the rolling percentile bar fetch, shared by
                        run-twitter-cycle.sh (CLI mode) and post_reddit.py
                        (import mode). Talks to
                        /api/v1/{platform}-candidates/virality-threshold,
                        which resolves the REAL percentile server-side from
                        the install's posting_mode; the pctile passed here
                        only matters as the fail-open default. Returns None
                        (bar OFF) on cold start / thin pool / fetch failure,
                        so every caller fails open by construction.

CLI:
  python3 virality.py bar --platform twitter|reddit \
      [--pctile 0.99] [--min-sample 200] [--hours 24]
  Prints the active threshold ("%.4f") to stdout, or NOTHING when the bar is
  off. Always exits 0: the bar is an optimization, never a cycle-breaker.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

TWITTER_HALF_LIFE_HOURS = 6.0
# Reddit threads keep drawing comments/upvotes for about a week, vs hours on
# X. Within the 24h candidate-queue lifetime this decay is nearly flat (~10%
# at 24h), which is intentional: on Reddit, age mostly enters through the
# velocity denominator, not the decay.
REDDIT_HALF_LIFE_HOURS = 168.0


def age_decay(age_hours, half_life_hours):
    """Exponential decay: 1.0 at age 0, 0.5 at one half-life."""
    return math.exp(-(math.log(2.0) / float(half_life_hours)) * max(float(age_hours), 0.0))


def calculate_reddit_virality_score(thread):
    """Score a Reddit thread's viral potential. Higher = better reply target.

    Mirrors the SHAPE of the X scorer (velocity x bonuses x age decay) with
    Reddit-native signals:
      1. Velocity: (upvotes + 4*comments) / age_hours. Comments weighted 4x,
         echoing the retired ripen composite (delta_up + 4*delta_comments):
         a comment is a far stronger visibility signal for OUR reply than an
         upvote.
      2. Discussion bonus: comment:upvote ratio, capped at +1x. High ratio =
         active conversation our comment can join, not a drive-by upvote pile.
      3. Age decay: 7-day half-life (REDDIT_HALF_LIFE_HOURS).
    No reach term: Reddit has no author-follower analog (subreddit subscriber
    count would be the proxy, but search results don't carry it; revisit if
    we start fetching it).

    `thread` needs: score (upvotes), num_comments, age_hours.
    Returns (score, velocity), both rounded, mirroring the X scorer's shape.
    """
    upvotes = int(thread.get("score") or 0)
    comments = int(thread.get("num_comments") or 0)
    age_hours = float(thread.get("age_hours") or 1.0)
    if age_hours < 0.1:
        age_hours = 0.1

    velocity = (upvotes + 4.0 * comments) / age_hours
    discussion_bonus = min((comments / upvotes) * 2.0, 1.0) if upvotes > 0 else 0.0
    score = velocity * (1.0 + discussion_bonus) * age_decay(age_hours, REDDIT_HALF_LIFE_HOURS)

    return round(score, 2), round(velocity, 2)


# Per-platform fetch defaults. Env overrides (S4L_TWITTER_VIRALITY_*,
# S4L_REDDIT_VIRALITY_*) beat these; explicit args beat env.
_BAR_DEFAULTS = {
    "twitter": {"pctile": 0.99, "min_sample": 200, "hours": 24},
    "reddit": {"pctile": 0.99, "min_sample": 200, "hours": 24},
}


def fetch_virality_bar(platform, pctile=None, min_sample=None, hours=None):
    """Fetch the rolling virality bar for `platform`. Float when ACTIVE, else None.

    None means bar OFF this cycle: cold start (sample_count below min_sample),
    endpoint missing/erroring, or network failure. Callers must treat None as
    "no filtering" (fail-open) — the bar throttles quality, it never gates the
    pipeline's ability to run.
    """
    if platform not in _BAR_DEFAULTS:
        raise ValueError(f"unknown platform {platform!r}")
    d = _BAR_DEFAULTS[platform]
    env = f"S4L_{platform.upper()}_VIRALITY"
    if pctile is None:
        pctile = float(os.environ.get(f"{env}_PCTILE", d["pctile"]))
    if min_sample is None:
        min_sample = int(os.environ.get(f"{env}_MIN_SAMPLE", d["min_sample"]))
    if hours is None:
        hours = int(os.environ.get(f"{env}_HOURS", d["hours"]))
    try:
        from http_api import api_get
        r = api_get(
            f"/api/v1/{platform}-candidates/virality-threshold",
            {"pctile": pctile, "hours": hours},
        )
        data = (r or {}).get("data") or {}
        thr = data.get("threshold")
        n = int(data.get("sample_count") or 0)
        if thr is not None and n >= int(min_sample):
            return float(thr)
    except BaseException as e:
        print(f"[virality] bar fetch failed (bar OFF this cycle): {e}", file=sys.stderr)
    return None


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_bar = sub.add_parser("bar", help="print active virality bar, or nothing when OFF")
    p_bar.add_argument("--platform", required=True, choices=sorted(_BAR_DEFAULTS))
    p_bar.add_argument("--pctile", type=float, default=None)
    p_bar.add_argument("--min-sample", type=int, default=None)
    p_bar.add_argument("--hours", type=int, default=None)
    args = parser.parse_args(argv)

    if args.cmd == "bar":
        thr = fetch_virality_bar(
            args.platform,
            pctile=args.pctile,
            min_sample=args.min_sample,
            hours=args.hours,
        )
        if thr is not None:
            print(f"{thr:.4f}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(_main())
