#!/usr/bin/env python3
"""One-off backfill: fix cross-route attribution on Twitter rows (2026-05-29).

Context: the Phase 2b prep step re-routes a candidate to a better-fitting
project than the Phase 1 query that surfaced it (PROJECT ROUTING rule in
run-twitter-cycle.sh). When that happens posts.project_name follows the new
project, but twitter_candidates.matched_project stays the origin (the
mark_posted API route does not rewrite it) and the origin search_topic gets
copied onto the new project's post. That mis-files conversions for analytics
that read the candidate row (top_search_topics -> pick_search_topic) and the
post's topic.

The live pipeline is already made robust to this via consumer-side guards
(qualified_query_bank._fetch_rows and top_search_topics._query_twitter both
require the post's project to match), but the historical rows are still wrong.
This script corrects them in place:

  1. twitter_candidates: re-point matched_project -> posts.project_name for
     every posted Twitter candidate whose post landed on a different project,
     and clear that candidate's search_topic (the origin topic does not belong
     to the routed project; the origin query is still recoverable via
     search_attempt_id -> twitter_search_attempts.project_name/search_topic).

  2. posts: clear search_topic (NULL) on cross-routed Twitter posts that still
     carry the origin project's topic, so posts.search_topic never names a
     topic foreign to posts.project_name.

NOT touched: posts whose search_topic is simply not in the *active* universe
but whose project matches the candidate (paused/renamed topics) — those are
correct attributions and must be left alone.

Usage:
    python3 scripts/backfill_crossroute_attribution.py            # dry-run
    python3 scripts/backfill_crossroute_attribution.py --apply    # execute
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402


CANDIDATE_SEL = """
    SELECT c.id, c.matched_project AS origin_proj, p.project_name AS post_proj,
           c.search_topic
      FROM twitter_candidates c
      JOIN posts p ON p.id = c.post_id
     WHERE p.platform = 'twitter'
       AND c.matched_project IS NOT NULL
       AND p.project_name   IS NOT NULL
       AND lower(c.matched_project) <> lower(p.project_name)
"""

POST_SEL = """
    SELECT p.id, p.project_name, p.search_topic
      FROM posts p
      JOIN twitter_candidates c ON c.post_id = p.id
     WHERE p.platform = 'twitter'
       AND COALESCE(p.search_topic, '') <> ''
       AND p.project_name IS NOT NULL
       AND c.matched_project IS NOT NULL
       AND lower(c.matched_project) <> lower(p.project_name)
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Execute the UPDATEs. Without this flag, dry-run only.")
    args = ap.parse_args()

    db.load_env()
    conn = db.get_conn()

    cand_rows = conn.execute(CANDIDATE_SEL).fetchall()
    post_rows = conn.execute(POST_SEL).fetchall()

    print(f"[backfill] candidate rows to re-point matched_project + clear "
          f"search_topic: {len(cand_rows)}")
    for r in cand_rows[:10]:
        print(f"  cand {r[0]}: matched_project {r[1]!r} -> {r[2]!r} "
              f"(clear search_topic {r[3]!r})")
    if len(cand_rows) > 10:
        print(f"  ...and {len(cand_rows) - 10} more")

    print(f"[backfill] posts to clear search_topic (cross-routed): {len(post_rows)}")
    for r in post_rows[:10]:
        print(f"  post {r[0]} (project {r[1]!r}): clear search_topic {r[2]!r}")
    if len(post_rows) > 10:
        print(f"  ...and {len(post_rows) - 10} more")

    if not args.apply:
        print("\n[backfill] DRY-RUN — re-run with --apply to execute.")
        return 0

    # ORDER MATTERS: clear posts.search_topic FIRST, while the candidate row
    # still carries the origin matched_project that makes the cross-route
    # condition true. The candidate UPDATE below rewrites matched_project to
    # the post's project, after which `c.matched_project <> p.project_name`
    # would be false and this posts UPDATE would match nothing.
    post_cur = conn.execute("""
        UPDATE posts p
           SET search_topic = NULL
          FROM twitter_candidates c
         WHERE c.post_id = p.id
           AND p.platform = 'twitter'
           AND COALESCE(p.search_topic, '') <> ''
           AND p.project_name IS NOT NULL
           AND c.matched_project IS NOT NULL
           AND lower(c.matched_project) <> lower(p.project_name)
    """)
    post_n = post_cur.rowcount

    # twitter_candidates: matched_project <- posts.project_name, search_topic <- NULL
    cand_cur = conn.execute("""
        UPDATE twitter_candidates c
           SET matched_project = p.project_name,
               search_topic    = NULL
          FROM posts p
         WHERE p.id = c.post_id
           AND p.platform = 'twitter'
           AND c.matched_project IS NOT NULL
           AND p.project_name   IS NOT NULL
           AND lower(c.matched_project) <> lower(p.project_name)
    """)
    cand_n = cand_cur.rowcount

    conn.commit()
    print(f"\n[backfill] APPLIED. twitter_candidates updated: {cand_n}; "
          f"posts updated: {post_n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
