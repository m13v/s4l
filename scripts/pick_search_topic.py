#!/usr/bin/env python3
"""Programmatically pick ONE search_topic per project per cycle.

Mirrors the engagement_styles.pick_style_for_post() pattern so that
search_topic gets the same treatment: force-picked in Python, stamped on
the candidate row, then propagated to posts. This replaces the legacy
"show Claude the entire search_topics[] array and let it improvise"
flow, which made end-to-end attribution noisy because the same tweet
could be tagged with different topics on re-discovery.

Universe: projects[].search_topics in config.json (the eligibility list).
Performance signal: top_search_topics.query(project, "twitter", ...)
which aggregates from twitter_candidates -> posts -> post_link_clicks.

Two branches:

1. USE (~90%): weighted random sample over the FULL universe. Every
   topic in config.json is always eligible, including ones with no
   post history. Weights are LOG-SMOOTHED so the top performer lands
   around 20-30% (vs raw proportional which would give 75-95% to one
   dominant topic) and unscored topics get an explicit floor weight
   around 0.5-1% per topic (low but never zero). This way Claude Code
   (and any other underexplored topic) always has a real shot.

   weight(score>0)  = log_e(score + 1) + 1.0
   weight(score==0) = COLD_TOPIC_WEIGHT (= 0.15)

2. EXPLORE_INVENT (~10%): the picker returns search_topic=None and
   mode='explore_invent'. The downstream prompt then asks Claude to
   INVENT a brand-new topic for this project given the stats of every
   existing topic (so it sees what's working, what's saturated, and
   what gaps to probe). The invented topic gets stamped onto every
   candidate row from this cycle for analytics, but it does NOT
   automatically enter the eligible universe — promoting a winning
   invention is a manual config.json edit (intentional human gate so
   the universe stays curated). To see inventions that performed, look
   at top_search_topics for the project: any search_topic not in the
   project's config.json seed list is an invention from a past cycle.

Output schema (single JSON object to stdout, one row per --project):

    {
      "mode": "use" | "explore_invent",
      "search_topic": str | None,        # None when mode == explore_invent
      "project": str,
      "platform": "twitter",
      "score": float,                    # composite_score; 0 for explore_invent
      "reference_topics": [              # full pool, sorted by score DESC
        {"search_topic", "composite_score", "posts",
         "clicks_total", "posted_n", "skipped_n", "weight_pct"},
        ...
      ],
      "universe_size": int,
      "scored_n": int,                   # topics with composite > 0
      "cold_n": int,                     # topics with composite == 0
      "window_days": int,
      "picked_at": ISO-8601 UTC
    }
"""

import argparse
import json
import math
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")

EXPLORE_RATE = 0.10
WINDOW_DAYS = 30

# Log-smoothed weighting constants. See module docstring for the curve.
# COLD_TOPIC_WEIGHT is intentionally non-zero so any topic in config.json
# can be selected even with no history (the floor maps to ~0.5-1% in a
# typical 15-25 topic universe). Tune by editing this number directly.
COLD_TOPIC_WEIGHT = 0.15

# How many top entries from the full pool to surface to the prompt as
# context. We expose more than the old trusted-top-5 because the model
# can now genuinely weigh a long tail of underperformers when inventing.
REFERENCE_TOP_N = 10


def _smooth_weight(score):
    """Log-smoothed weight for weighted-random sampling.

    Raw composite_score is heavily right-skewed (one winner often has
    100x the score of the runner-up), so proportional sampling collapses
    onto a single topic. log(score+1)+1 compresses the curve so the top
    performer gets a meaningful but not-overwhelming share, and the
    rest of the pool stays in play. Unscored topics get COLD_TOPIC_WEIGHT
    directly so they always have a small but visible chance.
    """
    if score and score > 0:
        return math.log(score + 1.0) + 1.0
    return COLD_TOPIC_WEIGHT


def _load_universe(project_name):
    """Return the project's search_topics[] from config.json (unique, ordered)."""
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    for p in cfg.get("projects", []):
        if (p.get("name") or "").lower() == project_name.lower():
            seen = set()
            out = []
            for t in (p.get("search_topics") or []):
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
            return out
    return []


def _load_signal(project_name, platform, window_days):
    """Pull per-topic performance for this project from top_search_topics.

    Returns a list of dicts keyed by search_topic. Empty list on any
    failure (no DB, no rows yet, etc.) so the picker still works in
    pure cold-start mode.
    """
    try:
        from top_search_topics import query as _top_query
        rows = _top_query(
            project=project_name,
            platform=platform,
            window_days=window_days,
            limit=200,
        )
        return rows or []
    except Exception:
        return []


def _build_pool(universe, signal_rows):
    """Pool = full universe (config.json) with scores attached.

    Every topic in config.json is eligible, scored or not. Unscored topics
    get composite_score=0 and rely on COLD_TOPIC_WEIGHT to remain in play.

    DB-only topics (i.e. legacy free-form `search_topic` strings from
    before the picker existed, or future inventions from explore cycles)
    are NOT included here. The pre-picker era wrote entire query strings
    into search_topic (e.g. `("foo" OR "bar") min_faves:50 since:...`),
    so re-introducing them via top_search_topics would pollute the pool.
    Invented topics from the explore_invent branch are captured in
    twitter_candidates.search_topic for analytics; promotion into the
    eligible universe is a manual config.json edit (intentional gate).

    Returns the pool sorted by composite_score DESC.
    """
    signal_map = {
        r.get("search_topic"): r
        for r in signal_rows
        if r.get("search_topic")
    }

    pool = []
    for topic in universe:
        r = signal_map.get(topic, {})
        score = float(r.get("composite_score") or 0)
        pool.append({
            "search_topic": topic,
            "composite_score": score,
            "posts": int(r.get("posts") or 0),
            "clicks_total": int(r.get("clicks_total") or 0),
            "posted_n": int(r.get("posted_n") or 0),
            "skipped_n": int(r.get("skipped_n") or 0),
        })

    pool.sort(key=lambda r: (-r["composite_score"], -r["posts"]))
    return pool


def _ref_meta(r, weight_pct):
    """Strip the pool row down to the fields the prompt block surfaces."""
    return {
        "search_topic": r["search_topic"],
        "composite_score": round(r["composite_score"], 2),
        "posts": r["posts"],
        "clicks_total": r["clicks_total"],
        "posted_n": r["posted_n"],
        "skipped_n": r["skipped_n"],
        "weight_pct": round(weight_pct, 2),
    }


def pick_topic_for_project(project_name, platform="twitter",
                           window_days=WINDOW_DAYS,
                           explore_rate=EXPLORE_RATE,
                           rng=None):
    """Pick ONE search_topic for this project on this platform.

    Returns the assignment dict described in the module docstring, or
    None if the project has no search_topics[] in config.json (caller
    should handle this by falling back to free-form behavior or
    skipping the project).
    """
    rnd = rng or random
    universe = _load_universe(project_name)
    picked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not universe:
        return None

    signal = _load_signal(project_name, platform, window_days)
    pool = _build_pool(universe, signal)

    weights = [_smooth_weight(r["composite_score"]) for r in pool]
    weight_total = sum(weights) or 1.0
    weight_pcts = [w / weight_total * 100.0 for w in weights]

    scored_n = sum(1 for r in pool if r["composite_score"] > 0)
    cold_n = sum(1 for r in pool if r["composite_score"] <= 0)

    reference_topics = [
        _ref_meta(pool[i], weight_pcts[i])
        for i in range(min(REFERENCE_TOP_N, len(pool)))
    ]

    base = {
        "project": project_name,
        "platform": platform,
        "reference_topics": reference_topics,
        "universe_size": len(universe),
        "scored_n": scored_n,
        "cold_n": cold_n,
        "pool_size": len(pool),
        "window_days": window_days,
        "picked_at": picked_at,
    }

    # EXPLORE_INVENT: picker punts on selection, Claude invents a new
    # topic given the full stats. search_topic=None signals the prompt
    # branch downstream.
    if rnd.random() < explore_rate:
        return {
            **base,
            "mode": "explore_invent",
            "search_topic": None,
            "score": 0.0,
        }

    # USE: weighted random over the FULL pool (smoothed weights).
    needle = rnd.uniform(0.0, weight_total)
    cum = 0.0
    chosen_idx = 0
    for i, w in enumerate(weights):
        cum += w
        if needle <= cum:
            chosen_idx = i
            break
    chosen = pool[chosen_idx]

    return {
        **base,
        "mode": "use",
        "search_topic": chosen["search_topic"],
        "score": round(chosen["composite_score"], 2),
        "picked_weight_pct": round(weight_pcts[chosen_idx], 2),
    }


def _format_pool_table(refs, mode):
    """Render the pool stats as a compact markdown table for the prompt."""
    if not refs:
        return "(no stats yet for any topic in this project)"
    lines = []
    header = "### "
    if mode == "explore_invent":
        header += f"Full pool stats for context (do NOT pick from this list — invent something NEW)"
    else:
        header += f"Pool stats (your topic is already assigned, this is context only)"
    lines.append(header)
    for r in refs:
        lines.append(
            f"- **{r['search_topic']}** "
            f"(weight {r['weight_pct']:.2f}%, "
            f"score {r['composite_score']:.1f}, "
            f"posts {r['posts']}, clicks {r['clicks_total']}, "
            f"posted_n {r['posted_n']}, skipped_n {r['skipped_n']})"
        )
    return "\n".join(lines)


def get_assigned_topic_prompt(assignment):
    """Compact prompt block built from a pick_topic_for_project() assignment.

    Two branches mirror the picker's two modes:
    - use: tell Claude the topic is locked, don't substitute.
    - explore_invent: tell Claude to invent a brand-new topic given the
      stats of every existing topic for this project.
    """
    if not assignment:
        return "(no search_topics defined for this project)"

    mode = assignment.get("mode", "use")
    topic = assignment.get("search_topic") or ""
    refs = assignment.get("reference_topics") or []
    universe_size = assignment.get("universe_size", 0)
    scored_n = assignment.get("scored_n", 0)
    cold_n = assignment.get("cold_n", 0)
    window_days = assignment.get("window_days", WINDOW_DAYS)

    if mode == "explore_invent":
        lines = [
            "## Your assigned mode: **EXPLORE_INVENT (invent a new topic)**",
            "",
            (
                f"This cycle is on the EXPLORE branch (~{int(EXPLORE_RATE*100)}% "
                f"of cycles). Instead of picking from the existing pool, "
                f"you must INVENT one new search_topic for this project — "
                f"a concept that does NOT appear in the stats below and is "
                f"NOT a paraphrase of any topic in the stats below. The new "
                f"topic should be in the project's domain (see the "
                f"project's `description` field), but probe a gap the "
                f"current list doesn't cover."
            ),
            "",
            (
                f"How to use the stats: look at which topics earn clicks vs "
                f"which get high skipped_n (audience mismatch), and propose "
                f"a topic that aims at the audience the winners reach without "
                f"copying their wording. Inventions can be longer-tail "
                f"phrases ('AI coding agent for legacy codebases'), adjacent "
                f"verticals ('Cursor alternative for designers'), or fresh "
                f"angles on the project's value prop. They MUST be plausible "
                f"as a Twitter advanced-search query (i.e. real people "
                f"would tweet about it)."
            ),
            "",
            (
                f"Universe today: {universe_size} seeded topics in "
                f"config.json. {scored_n} have data (any posts/skips/clicks), "
                f"{cold_n} are unscored."
            ),
            "",
            _format_pool_table(refs, mode),
            "",
            (
                "Draft ONE Twitter advanced-search query targeting your "
                "INVENTED topic. In the JSON you emit per tweet, set "
                "`search_topic` to the EXACT invented topic name you "
                "chose (one consistent string for every tweet in this "
                "cycle). Future cycles will automatically pick up the "
                "invented topic via top_search_topics if it earns posts "
                "or clicks — no config.json edit needed."
            ),
        ]
        return "\n".join(lines)

    # Mode == "use"
    weight_pct = assignment.get("picked_weight_pct", 0.0)
    lines = [
        f"## Your assigned search topic: **{topic}**",
        "",
        (
            f"This topic was selected by the picker (weighted random over "
            f"the FULL universe; this topic carried "
            f"{weight_pct:.2f}% of the weight). Weights are log-smoothed "
            f"so the top performer lands around 20-30% while every cold "
            f"topic still has a small but real chance "
            f"(floor weight = {COLD_TOPIC_WEIGHT}). Composite score is "
            f"clicks*100 + likes + views*0.001 over the last "
            f"{window_days}d. Draft ONE Twitter advanced-search query "
            f"that surfaces fresh tweets about this exact topic. Do not "
            f"substitute a different topic."
        ),
        "",
        (
            f"Universe: {universe_size} seeded topics in config.json. "
            f"Pool: {assignment.get('pool_size', 0)} topics total "
            f"({scored_n} with data, {cold_n} cold)."
        ),
        "",
        _format_pool_table(refs, mode),
        "",
        (
            "In the JSON you emit per tweet, set `search_topic` to "
            f"exactly \"{topic}\" (string match). The scoring pipeline "
            "will reject any row whose search_topic does not equal the "
            "assigned value."
        ),
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True, help="Project name from config.json")
    ap.add_argument("--platform", default="twitter", help="Platform (default: twitter)")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--explore-rate", type=float, default=EXPLORE_RATE)
    ap.add_argument("--seed", type=int, default=None, help="Deterministic RNG seed for tests")
    ap.add_argument("--out", default=None, help="Optional path to also write the JSON to (mirrors styles.sh pattern)")
    ap.add_argument("--prompt", action="store_true", help="Print the prompt block to stdout instead of JSON")
    args = ap.parse_args()

    rng = random.Random(args.seed) if args.seed is not None else None
    assignment = pick_topic_for_project(
        args.project,
        platform=args.platform,
        window_days=args.window_days,
        explore_rate=args.explore_rate,
        rng=rng,
    )

    if assignment is None:
        sys.stderr.write(
            f"pick_search_topic: project '{args.project}' has no search_topics[] in config.json\n"
        )
        sys.exit(2)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(assignment, f)

    if args.prompt:
        print(get_assigned_topic_prompt(assignment))
    else:
        json.dump(assignment, sys.stdout)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
