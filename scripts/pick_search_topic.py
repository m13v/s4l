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

Three branches in priority order:

1. EXPLORE (~10%): uniform random over cold topics
   (universe minus the trusted top-N). Forces the cycle to genuinely
   try seeds the model has been ignoring. If no cold topics exist
   (every universe topic is already trusted), this degenerates to
   weighted use.

2. USE (~90%): weighted random sample over the trusted top-N by
   composite_score (clicks*100 + likes + views*0.001). A topic enters
   the trusted pool when it has posted_n >= 1 AND composite_score > 0
   within the window. The set rotates as performance shifts.

3. COLD_START fallback: when the trusted pool is empty for this
   project, uniform random over the entire universe. Once any topic
   gets a post + measurable engagement, the project flips to USE.

Output schema (single JSON object to stdout, one row per --project):

    {
      "mode": "use" | "explore" | "cold_start",
      "search_topic": str,
      "project": str,
      "platform": "twitter",
      "score": float,                    # composite_score; 0 for explore/cold
      "reference_topics": [              # trusted top-N (always populated)
        {"search_topic", "composite_score", "posts",
         "clicks_total", "posted_n", "skipped_n"},
        ...
      ],
      "universe_size": int,
      "trusted_n": int,
      "cold_n": int,
      "window_days": int,
      "picked_at": ISO-8601 UTC
    }
"""

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")

EXPLORE_RATE = 0.10
CURATED_TOP_N = 5
MIN_POSTED_N = 1
WINDOW_DAYS = 30


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
    failure (no DB, no rows yet, etc.) so the picker can fall through
    to cold_start without surfacing a hard error to the pipeline.
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


def _build_trusted_pool(universe, signal_rows):
    """Trusted = topics in universe with posted_n >= 1 AND composite_score > 0.

    The intersection guards against legacy topics that are still in the
    signal but no longer in config.json (someone removed them). Sorted
    by composite_score DESC then by posts DESC as a tiebreaker.
    """
    universe_set = {t for t in universe}
    pool = []
    for r in signal_rows:
        topic = r.get("search_topic")
        if not topic or topic not in universe_set:
            continue
        if (r.get("posted_n") or 0) < MIN_POSTED_N:
            continue
        if (r.get("composite_score") or 0) <= 0:
            continue
        pool.append(r)
    pool.sort(
        key=lambda r: (
            -(r.get("composite_score") or 0),
            -(r.get("posts") or 0),
        )
    )
    return pool[:CURATED_TOP_N]


def _ref_meta(r):
    """Strip the row down to the fields the prompt block surfaces."""
    return {
        "search_topic": r.get("search_topic"),
        "composite_score": round(float(r.get("composite_score") or 0), 2),
        "posts": int(r.get("posts") or 0),
        "clicks_total": int(r.get("clicks_total") or 0),
        "posted_n": int(r.get("posted_n") or 0),
        "skipped_n": int(r.get("skipped_n") or 0),
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
    trusted_pool = _build_trusted_pool(universe, signal)
    reference_topics = [_ref_meta(r) for r in trusted_pool]

    trusted_set = {r["search_topic"] for r in trusted_pool}
    cold_universe = [t for t in universe if t not in trusted_set]

    base = {
        "project": project_name,
        "platform": platform,
        "reference_topics": reference_topics,
        "universe_size": len(universe),
        "trusted_n": len(trusted_pool),
        "cold_n": len(cold_universe),
        "window_days": window_days,
        "picked_at": picked_at,
    }

    # Cold start: no trusted topics yet for this project, uniform random
    # over the universe. The project flips to use/explore once any
    # topic earns a posted_n>=1 with measurable engagement.
    if not trusted_pool:
        pick = rnd.choice(universe)
        return {**base, "mode": "cold_start", "search_topic": pick, "score": 0.0}

    # Explore: uniform random over cold topics. Degrades gracefully to
    # use if every universe topic is already trusted (cold pool empty).
    if cold_universe and rnd.random() < explore_rate:
        pick = rnd.choice(cold_universe)
        return {**base, "mode": "explore", "search_topic": pick, "score": 0.0}

    # Use: weighted random sample by composite_score over the trusted top-N.
    weights = [max(float(r.get("composite_score") or 0), 0.01) for r in trusted_pool]
    total = sum(weights) or 1.0
    needle = rnd.uniform(0.0, total)
    cum = 0.0
    chosen = trusted_pool[0]
    for r, w in zip(trusted_pool, weights):
        cum += w
        if needle <= cum:
            chosen = r
            break
    return {
        **base,
        "mode": "use",
        "search_topic": chosen.get("search_topic"),
        "score": round(float(chosen.get("composite_score") or 0), 2),
    }


def get_assigned_topic_prompt(assignment):
    """Compact prompt block built from a pick_topic_for_project() assignment.

    Mirrors get_assigned_style_prompt: tell Claude the topic has already
    been chosen by the picker (weighted by live click performance),
    instruct it not to swap, and surface the trusted top-N as reference
    context so the model knows why this topic was picked (or, on
    explore, what it's exploring around).
    """
    if not assignment:
        return "(no search_topics defined for this project)"

    mode = assignment.get("mode", "use")
    topic = assignment.get("search_topic") or ""
    refs = assignment.get("reference_topics") or []

    lines = [f"## Your assigned search topic: **{topic}**", ""]

    if mode == "use":
        lines.append(
            f"This topic was selected by the picker (weighted random "
            f"over the top {assignment.get('trusted_n', 0)} performers by "
            f"clicks-weighted composite score, last "
            f"{assignment.get('window_days', WINDOW_DAYS)}d). Draft ONE "
            f"Twitter advanced-search query that surfaces fresh tweets "
            f"about this exact topic. Do not substitute a different topic."
        )
    elif mode == "explore":
        lines.append(
            f"This topic is on the EXPLORE branch (~{int(EXPLORE_RATE*100)}% of "
            f"cycles): the picker chose a cold topic from the project's "
            f"search_topics[] that has not yet earned trusted performance "
            f"data. Draft ONE Twitter advanced-search query that surfaces "
            f"fresh tweets about this exact topic. The goal is to learn "
            f"whether it converts, not to fall back to a known-winning "
            f"topic. Do not substitute."
        )
    else:
        lines.append(
            f"This project has no posted history yet for any topic in its "
            f"search_topics[] (cold start). Draft ONE Twitter advanced-search "
            f"query that surfaces fresh tweets about this exact topic so we "
            f"can start collecting performance signal. Do not substitute."
        )

    lines.append("")
    lines.append(
        f"Universe: {assignment.get('universe_size', 0)} topics in "
        f"config.json. Trusted (posted_n>=1, composite>0): "
        f"{assignment.get('trusted_n', 0)}. Cold: "
        f"{assignment.get('cold_n', 0)}."
    )

    if refs:
        lines.append("")
        lines.append(f"### Trusted top {len(refs)} for context (do NOT pick from this list, your topic is already assigned)")
        for r in refs:
            lines.append(
                f"- **{r['search_topic']}** "
                f"(score {r['composite_score']:.1f}, "
                f"posts {r['posts']}, clicks {r['clicks_total']}, "
                f"posted_n {r['posted_n']}, skipped_n {r['skipped_n']})"
            )

    lines.append("")
    lines.append(
        "In the JSON you emit per tweet, set `search_topic` to exactly "
        f'"{topic}" (string match). The scoring pipeline will reject any '
        "row whose search_topic does not equal the assigned value."
    )
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
