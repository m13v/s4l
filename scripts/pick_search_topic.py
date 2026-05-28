#!/usr/bin/env python3
"""Programmatically pick ONE search_topic per project per cycle.

Mirrors the engagement_styles.pick_style_for_post() pattern so that
search_topic gets the same treatment: force-picked in Python, stamped on
the candidate row, then propagated to posts. This replaces the legacy
"show Claude the entire search_topics[] array and let it improvise"
flow, which made end-to-end attribution noisy because the same tweet
could be tagged with different topics on re-discovery.

Universe: project_search_topics table via /api/v1/project-search-topics
(install-scoped, status='active' only). config.json is seed-only and is
NEVER consulted at pick time. Run scripts/seed_search_topics.py once
per install to mirror config.json into the DB; from then on,
paused/excluded topics and invented winners live in the DB and the
picker honors them. If the DB is unreachable or the project has zero
active topics, the picker raises PickerError and the cycle aborts
loudly — there is no config.json fallback.

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

   base(score>0)  = log_e(score + 1) + 1.0
   base(score==0) = COLD_TOPIC_WEIGHT (= 0.15)

   2026-05-27: base weight is then adjusted by two layered factors
   that read from twitter_search_attempts (via the supply join in
   top_search_topics._query_twitter) so the picker distinguishes
   topics that were tried-and-failed from topics that were never
   tried at all. Math lives in _compute_weight; concretely:

     - attempts_n == 0           → return base unchanged
     - 0 supply across N tries   → base * SUPPLY_DEAD_WEIGHT (0.3x)
     - else                      → base * conversion (posts/attempt)

   Floor at base*DEAD_FLOOR_FRACTION so no topic ever locks out
   entirely; we always keep a small retest probability in case X's
   firehose or our criteria shift.

2. EXPLORE_INVENT (~10%): the picker returns search_topic=None and
   mode='explore_invent'. The downstream prompt then asks Claude to
   INVENT a brand-new topic for this project given the stats of every
   existing topic (so it sees what's working, what's saturated, and
   what gaps to probe). The invented topic gets stamped onto every
   candidate row from this cycle for analytics, and the candidate-scoring
   step (score_twitter_candidates.py::auto_promote_invented_topics) then
   upserts it into project_search_topics with source='invented',
   status='active' so the next USE cycle can re-select it via the normal
   weighted-random path. The existing _compute_weight gates (conversion
   penalty, supply-dead penalty) act as the quality filter: a FIT_FAIL
   invention naturally drops to the floor weight without any manual
   curation.

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


class PickerError(RuntimeError):
    """Raised when the picker cannot get a valid universe from the DB.

    Callers (run-twitter-cycle.sh's heredoc, CLI main) must treat this
    as a hard stop: do NOT silently degrade to free-form picking or
    config.json reads. The DB is the only source of truth for what's
    eligible (paused/excluded/invented topics all live there).
    """


class UniverseExhaustedError(RuntimeError):
    """Raised when `exclude_topics` has filtered the universe to empty.

    Happens mid-cycle when the retry loop has already tried every active
    topic for this project. Callers should catch this distinctly from
    PickerError and stop the retry loop gracefully (log
    `universe_exhausted:1` as the cycle's failure reason and proceed to
    Phase 2 with whatever candidates accumulated). There is no invent
    fallback here by design (2026-05-28): invention is owned by the
    standalone `invent_topics.py` job, not this in-cycle picker.
    """


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

# 2026-05-27 conversion + supply gates (closes the "S4L empty-batch" gap:
# a topic with N attempts and 0 posts used to weight identically to a
# brand-new topic — see top_search_topics._query_twitter for the upstream
# join that makes this signal visible).
#
# DEAD_FLOOR_FRACTION: even the worst-performing topic keeps this fraction
# of its base weight, so we always retest occasionally in case supply
# recovers. Tune up to retest more often, down to lock duds out harder.
DEAD_FLOOR_FRACTION = 0.02
# SUPPLY_DEAD_WEIGHT: when X returns 0 tweets across many attempts, the
# topic isn't necessarily a bad fit — supply is dead. Apply a mild fixed
# penalty rather than the heavy conversion math (which would drive the
# weight to ~0 even though the fault is partly external).
SUPPLY_DEAD_WEIGHT = 0.3
# Need at least this many attempts before calling a topic supply-dead.
# A single dry attempt isn't evidence; 3 in a row across a 30d window is.
MIN_ATTEMPTS_FOR_SUPPLY_VERDICT = 3


def _compute_weight(r):
    """Final weighted-sampling weight for one pool row.

    Three layered factors, applied in order:

    1. base = log(composite_score+1)+1 (positive performance), or
              COLD_TOPIC_WEIGHT (=0.15) when never scored. Log-smoothing
              compresses the right-skewed composite distribution so the
              top performer lands around 20-30%, not 90%+.

    2. supply gate: if attempts_n >= MIN_ATTEMPTS_FOR_SUPPLY_VERDICT and
       tweets_found_total == 0, X is just not returning tweets for this
       topic. Apply a mild 0.3x rather than the heavy conversion math —
       supply failure is partly outside our control, so the topic stays
       on the bench for occasional retest.

    3. conversion: posts per attempt, Laplace-smoothed
       (posted_n + 1) / (attempts_n + 1). Capped at 1.0 so we don't BOOST
       above base. Heavy penalty for topics that surface supply but
       never convert to posts (the "got 10 attempts, 50 tweets, 0 posts"
       FIT failure — our prompts/criteria aren't working for this topic).

    Untried topics (attempts_n == 0) skip both #2 and #3 and return base
    unchanged, so cold topics still get full exploration weight.

    A floor of base*DEAD_FLOOR_FRACTION ensures no topic drops to zero
    weight: we always want some chance to retest a stale dud in case
    supply/fit changes (X firehose shifts, project description evolves).
    """
    score = float(r.get("composite_score") or 0)
    posted_n = int(r.get("posted_n") or 0)
    attempts_n = int(r.get("attempts_n") or 0)
    tweets_found_total = int(r.get("tweets_found_total") or 0)

    if score > 0:
        base = math.log(score + 1.0) + 1.0
    else:
        base = COLD_TOPIC_WEIGHT

    if attempts_n == 0:
        return base

    if (
        tweets_found_total == 0
        and attempts_n >= MIN_ATTEMPTS_FOR_SUPPLY_VERDICT
    ):
        return max(base * DEAD_FLOOR_FRACTION, base * SUPPLY_DEAD_WEIGHT)

    conversion = min(1.0, (posted_n + 1.0) / (attempts_n + 1.0))
    return max(base * DEAD_FLOOR_FRACTION, base * conversion)


def _load_universe(project_name):
    """Return the project's active search topics (unique, ordered).

    GET /api/v1/project-search-topics?project=X&status=active. Each
    install sees its own rows plus the legacy null-install bucket (same
    null-claim pattern as posts/replies). Only 'active' rows are used
    so paused/excluded topics drop out without any local config.

    Raises PickerError on API failure or zero rows. There is NO
    config.json fallback by design (per 2026-05-27): a misconfigured
    install must fail loud rather than silently posting against a stale
    seed list. Cold-start procedure: run scripts/seed_search_topics.py
    once per install to mirror config.json into the DB, then the
    picker has a universe to work with.
    """
    try:
        from http_api import api_get
        resp = api_get(
            "/api/v1/project-search-topics",
            query={"project": project_name, "status": "active"},
        )
    except Exception as e:
        raise PickerError(
            f"project-search-topics API unreachable for project="
            f"{project_name!r}: {e}"
        ) from e
    data = (resp or {}).get("data") or {}
    rows = data.get("topics") or []
    seen = set()
    out = []
    source_map = {}  # topic -> source (first occurrence wins)
    source_counts = {"seed": 0, "invented": 0, "manual": 0}
    for r in rows:
        t = (r.get("topic") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        src = (r.get("source") or "").strip()
        source_map[t] = src or "seed"
        if src in source_counts:
            source_counts[src] += 1
    if not out:
        raise PickerError(
            f"no active search topics for project={project_name!r} in "
            f"project_search_topics. Seed via scripts/seed_search_topics.py "
            f"or activate at least one row."
        )
    # Grep-able marker so cycle logs show the new universe source explicitly.
    # active= is the count the picker actually uses; seed/invented/manual
    # split surfaces auto-promoted topics vs the original seed pool so
    # invention activity is visible without a DB query.
    sys.stderr.write(
        f"[pick_search_topic] universe_source=db project={project_name!r} "
        f"active={len(out)} seed={source_counts['seed']} "
        f"invented={source_counts['invented']} "
        f"manual={source_counts['manual']}\n"
    )
    return out, source_map


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


def _build_pool(universe, signal_rows, source_map=None):
    """Pool = full DB universe (project_search_topics, status='active') with
    scores attached.

    Every active topic is eligible, scored or not. Unscored topics get
    composite_score=0 and rely on COLD_TOPIC_WEIGHT to remain in play.

    The pre-picker era wrote entire query strings into search_topic
    (e.g. `("foo" OR "bar") min_faves:50 since:...`); those would pollute
    the pool, so universe membership (not top_search_topics) is the
    source of truth. Invented topics promoted by
    scripts/promote_invented_topics.py carry source='invented' in
    project_search_topics and are eligible the same way seeds are. The
    optional `source_map` is a topic -> source dict so per-row source
    can be surfaced to the trace without a second API call.

    Returns the pool sorted by composite_score DESC.
    """
    signal_map = {
        r.get("search_topic"): r
        for r in signal_rows
        if r.get("search_topic")
    }
    source_map = source_map or {}

    pool = []
    for topic in universe:
        r = signal_map.get(topic, {})
        score = float(r.get("composite_score") or 0)
        pool.append({
            "search_topic": topic,
            "source": source_map.get(topic, "seed"),
            "composite_score": score,
            "posts": int(r.get("posts") or 0),
            "clicks_total": int(r.get("clicks_total") or 0),
            "posted_n": int(r.get("posted_n") or 0),
            "skipped_n": int(r.get("skipped_n") or 0),
            "attempts_n": int(r.get("attempts_n") or 0),
            "tweets_found_total": int(r.get("tweets_found_total") or 0),
            "zero_supply_attempts": int(r.get("zero_supply_attempts") or 0),
        })

    pool.sort(key=lambda r: (-r["composite_score"], -r["posts"]))
    return pool


def _ref_meta(r, weight_pct):
    """Strip the pool row down to the fields the prompt block surfaces.

    attempts_n / tweets_found_total are surfaced so Claude can see the
    fit-vs-supply story in the explore_invent branch ("topic X had 10
    attempts and 50 tweets_found but zero posts → fit failure, propose a
    different angle on the same audience").
    """
    return {
        "search_topic": r["search_topic"],
        "composite_score": round(r["composite_score"], 2),
        "posts": r["posts"],
        "clicks_total": r["clicks_total"],
        "posted_n": r["posted_n"],
        "skipped_n": r["skipped_n"],
        "attempts_n": r.get("attempts_n", 0),
        "tweets_found_total": r.get("tweets_found_total", 0),
        "zero_supply_attempts": r.get("zero_supply_attempts", 0),
        "weight_pct": round(weight_pct, 2),
    }


def _verdict_for_row(r):
    """Same FIT_FAIL / SUPPLY_DEAD classification used in the prompt
    block, returned as a flat string for trace-log consumers (greppable
    after the fact)."""
    attempts_n = int(r.get("attempts_n") or 0)
    tweets_found_total = int(r.get("tweets_found_total") or 0)
    posted_n = int(r.get("posted_n") or 0)
    if attempts_n >= MIN_ATTEMPTS_FOR_SUPPLY_VERDICT and tweets_found_total == 0:
        return "SUPPLY_DEAD"
    if attempts_n >= 3 and posted_n == 0 and tweets_found_total > 0:
        return "FIT_FAIL"
    return None


def _emit_trace(assignment, pool, weight_pcts, chosen_idx):
    """Write a single JSON line to stderr capturing the entire pick
    decision: project, mode, picked topic + weight%, and the full pool
    with weights/stats/verdicts. Grep-friendly tag `[pick_search_topic]`
    so cycle logs (skill/logs/twitter-cycle-*.log, which capture stderr
    of the bash pipeline) carry the full audit trail without the prompt
    needing to.

    Failures here are swallowed so a logging hiccup never breaks the
    actual pick.
    """
    try:
        pool_entries = [
            {
                "topic": r["search_topic"],
                "source": r.get("source", "seed"),
                "weight_pct": round(weight_pcts[i], 2),
                "score": round(r["composite_score"], 2),
                "posts": r["posts"],
                "clicks": r["clicks_total"],
                "posted_n": r["posted_n"],
                "skipped_n": r["skipped_n"],
                "attempts": r.get("attempts_n", 0),
                "supply": r.get("tweets_found_total", 0),
                "verdict": _verdict_for_row(r),
                "chosen": (chosen_idx is not None and i == chosen_idx),
            }
            for i, r in enumerate(pool)
        ]
        # Compact time-series snapshot of every invented topic in the
        # active pool — answers "is Laravel Horizon's score growing?"
        # straight from the cycle log without a DB query. Picked flag
        # is included so post-hoc you can also answer "was an invented
        # topic ever drawn?" by greping `"invented_in_pool".*"picked":\s*true`.
        invented_in_pool = [
            {
                "topic": e["topic"],
                "weight_pct": e["weight_pct"],
                "score": e["score"],
                "posts": e["posts"],
                "clicks": e["clicks"],
                "supply": e["supply"],
                "picked": e["chosen"],
            }
            for e in pool_entries
            if e["source"] == "invented"
        ]
        trace = {
            "project": assignment.get("project"),
            "platform": assignment.get("platform"),
            "mode": assignment.get("mode"),
            "picked": assignment.get("search_topic"),
            "picked_weight_pct": assignment.get("picked_weight_pct"),
            "universe_size": assignment.get("universe_size"),
            "scored_n": assignment.get("scored_n"),
            "cold_n": assignment.get("cold_n"),
            "window_days": assignment.get("window_days"),
            "picked_at": assignment.get("picked_at"),
            "invented_in_pool": invented_in_pool,
            "pool": pool_entries,
        }
        sys.stderr.write("[pick_search_topic] " + json.dumps(trace) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def pick_topic_for_project(project_name, platform="twitter",
                           window_days=WINDOW_DAYS,
                           exclude_topics=None,
                           rng=None):
    """Pick ONE search_topic for this project on this platform.

    Returns the assignment dict described in the module docstring.
    Raises PickerError when the DB universe lookup fails or the project
    has zero active topics. Raises UniverseExhaustedError when
    `exclude_topics` filters the universe to empty.

    `exclude_topics` is an optional iterable of topic strings to drop from
    the universe before sampling (case-insensitive, whitespace-trimmed).
    Used by `run-twitter-cycle.sh`'s Phase 1 retry loop to force a fresh
    topic on each scan attempt so the model isn't pinned to one assigned
    topic across all retries. When the exclusion list empties the universe,
    we raise `UniverseExhaustedError` and the shell breaks the retry loop
    cleanly — no invent fallback. Invention is the standalone
    `invent_topics.py` job's responsibility (2026-05-28 architectural
    split); this picker is pure use-mode selection over the universe.
    """
    rnd = rng or random
    universe, source_map = _load_universe(project_name)
    picked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    signal = _load_signal(project_name, platform, window_days)

    excluded_set = {
        (t or "").strip().lower()
        for t in (exclude_topics or [])
        if t and isinstance(t, str)
    }
    if excluded_set:
        filtered_universe = [
            t for t in universe
            if (t or "").strip().lower() not in excluded_set
        ]
        if not filtered_universe:
            # All topics in the universe were already tried this cycle.
            # Hard stop. The shell's retry loop catches this and exits
            # Phase 1 with whatever candidates accumulated; the cycle's
            # log_run summary surfaces `universe_exhausted:1` so the
            # dashboard distinguishes this from empty_batch / phase1_no_tweets.
            sys.stderr.write(
                f"[pick_search_topic] universe_exhausted project={project_name!r} "
                f"excluded={len(excluded_set)} active={len(universe)}\n"
            )
            raise UniverseExhaustedError(
                f"project={project_name!r} exhausted: all "
                f"{len(universe)} active topics already tried this cycle "
                f"(excluded={len(excluded_set)})"
            )
        sys.stderr.write(
            f"[pick_search_topic] excluded={len(excluded_set)} "
            f"remaining_universe={len(filtered_universe)} project={project_name!r}\n"
        )
        universe = filtered_universe

    pool = _build_pool(universe, signal, source_map=source_map)

    weights = [_compute_weight(r) for r in pool]
    weight_total = sum(weights) or 1.0
    weight_pcts = [w / weight_total * 100.0 for w in weights]

    scored_n = sum(1 for r in pool if r["composite_score"] > 0)
    cold_n = sum(1 for r in pool if r["composite_score"] <= 0)

    reference_topics = [
        _ref_meta(pool[i], weight_pcts[i])
        for i in range(min(REFERENCE_TOP_N, len(pool)))
    ]

    # USE: weighted random over the (possibly filtered) pool. This is now
    # the only path — EXPLORE_INVENT was removed 2026-05-28 in favor of
    # the standalone invent_topics.py job that writes new topics directly
    # into project_search_topics.
    needle = rnd.uniform(0.0, weight_total)
    cum = 0.0
    chosen_idx = 0
    for i, w in enumerate(weights):
        cum += w
        if needle <= cum:
            chosen_idx = i
            break
    chosen = pool[chosen_idx]

    assignment = {
        "project": project_name,
        "platform": platform,
        "reference_topics": reference_topics,
        "universe_size": len(universe),
        "scored_n": scored_n,
        "cold_n": cold_n,
        "pool_size": len(pool),
        "window_days": window_days,
        "picked_at": picked_at,
        "mode": "use",
        "search_topic": chosen["search_topic"],
        "score": round(chosen["composite_score"], 2),
        "picked_weight_pct": round(weight_pcts[chosen_idx], 2),
    }
    _emit_trace(assignment, pool, weight_pcts, chosen_idx=chosen_idx)
    return assignment


def _format_pool_table(refs):
    """Render the pool stats as a compact markdown table for the prompt.

    Single-purpose post-2026-05-28: the picker only has one mode (use),
    so the table is always rendered as "context for an already-assigned
    topic". The `mode` param was removed when explore_invent was deleted.
    """
    if not refs:
        return "(no stats yet for any topic in this project)"
    lines = []
    header = "### Pool stats (your topic is already assigned, this is context only)"
    lines.append(header)
    for r in refs:
        attempts_n = r.get("attempts_n", 0)
        tweets_found_total = r.get("tweets_found_total", 0)
        verdict = ""
        if attempts_n >= MIN_ATTEMPTS_FOR_SUPPLY_VERDICT and tweets_found_total == 0:
            verdict = " [SUPPLY_DEAD]"
        elif attempts_n >= 3 and r["posted_n"] == 0 and tweets_found_total > 0:
            verdict = " [FIT_FAIL]"
        lines.append(
            f"- **{r['search_topic']}** "
            f"(weight {r['weight_pct']:.2f}%, "
            f"score {r['composite_score']:.1f}, "
            f"posts {r['posts']}, clicks {r['clicks_total']}, "
            f"posted_n {r['posted_n']}, skipped_n {r['skipped_n']}, "
            f"attempts {attempts_n}, supply {tweets_found_total}){verdict}"
        )
    lines.append(
        "  ([SUPPLY_DEAD] = ≥3 attempts and 0 tweets returned; X isn't surfacing "
        "anything for this topic. [FIT_FAIL] = ≥3 attempts and tweets found but "
        "0 posted; the topic surfaces noise we keep rejecting.)"
    )
    return "\n".join(lines)


def get_assigned_topic_prompt(assignment):
    """Compact prompt block built from a pick_topic_for_project() assignment.

    Single-mode: the picker always returns a use-mode assignment now
    (2026-05-28 explore_invent removal). Invention is owned by the
    standalone `invent_topics.py` job.
    """
    if not assignment:
        return "(no search_topics defined for this project)"

    topic = assignment.get("search_topic") or ""

    # Programmatic pick is final; the model gets the topic and the
    # instruction, nothing else. The full pool with weights/verdicts is
    # emitted to the cycle log via the `[pick_search_topic]` trace line
    # in pick_topic_for_project, so any post-hoc tracing reads from the
    # log, not the prompt.
    lines = [
        f"## Your assigned search topic: **{topic}**",
        "",
        (
            f"Draft ONE Twitter advanced-search query that surfaces fresh "
            f"tweets about this exact topic. Do not substitute a different "
            f"topic."
        ),
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
    ap.add_argument("--exclude-topics", default="", help="JSON array of topic strings to drop from the universe before sampling (used by Phase 1 retry loop)")
    args = ap.parse_args()

    rng = random.Random(args.seed) if args.seed is not None else None
    excluded = []
    if args.exclude_topics:
        try:
            excluded = json.loads(args.exclude_topics) or []
            if not isinstance(excluded, list):
                excluded = []
        except json.JSONDecodeError:
            excluded = []
    try:
        assignment = pick_topic_for_project(
            args.project,
            platform=args.platform,
            window_days=args.window_days,
            explore_rate=args.explore_rate,
            exclude_topics=excluded,
            rng=rng,
        )
    except PickerError as e:
        sys.stderr.write(f"pick_search_topic: {e}\n")
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
