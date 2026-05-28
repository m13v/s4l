#!/usr/bin/env python3
"""Topic-level funnel ledger — cross-cycle aggregated stats per
(project, search_topic) pair.

Closes the feedback gap surfaced 2026-05-28: today the picker reads
candidate-level stats from `top_search_topics`, but topics that were
ATTEMPTED but never produced a candidate row (because every returned
tweet hit the age gate) are invisible to that view, and to any
downstream caller (picker, invent_topics, dashboard). This script
joins twitter_search_attempts with twitter_candidates and posts to
produce a single funnel view that includes the attempted-but-failed
tail.

Output schema (one row per (project, search_topic) in window):

    {
      "project": "studyly",
      "search_topic": "bar exam prep ai",

      "attempts_n":            int,   # twitter_search_attempts rows in window
      "last_attempted_at":     str,   # ISO-8601 UTC, MAX(ran_at)
      "tweets_found_total":    int,   # SUM(tweets_found)
      "zero_supply_attempts":  int,   # attempts where tweets_found=0

      "candidates_n":          int,   # twitter_candidates rows
      "posted_n":              int,   # candidates with status='posted'
      "skipped_n":             int,   # rejected/expired/failed
      "first_candidate_at":    str,   # MIN(discovered_at)
      "last_candidate_at":     str,   # MAX(discovered_at)

      "views_total":           int,
      "likes_total":           int,
      "comments_total":        int,
      "clicks_total":          int,   # post_link_clicks where is_bot=false
      "clicks_per_post":       float, # clicks_total / NULLIF(posted_n, 0)

      "verdict": "dud" | "weak" | "decent" | "strong" | "untried",
    }

Verdict bucketing:
  - untried — attempts_n == 0
  - dud     — attempts_n >= 3 AND candidates_n == 0
  - weak    — posted_n >= 1 AND clicks_per_post < 0.3 (i.e. <30%
              click-through, weak conversion)
  - decent  — posted_n >= 1 AND clicks_per_post >= 0.3
  - strong  — clicks_per_post >= 1.0 (top decile, multiple clicks per post)

Writes:
  - JSON file at ~/social-autoposter/state/topic_ledger.json
  - Atomic write via temp + rename so readers never see partial data

Run by launchd com.m13v.social-topic-ledger every 15 min.

Readers:
  - scripts/invent_topics.py (lookup_topic_neighbors tool)
  - scripts/pick_search_topic.py (future: replace top_search_topics._load_signal)
  - dashboard surfaces (future)

CLI:
    python3 scripts/topic_ledger.py                    # default 30d window, all projects
    python3 scripts/topic_ledger.py --window-days 14
    python3 scripts/topic_ledger.py --project studyly  # filter to one project
    python3 scripts/topic_ledger.py --dry-run          # print to stdout, do not write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db as dbmod  # noqa: E402


LEDGER_PATH = Path(os.path.expanduser(
    "~/social-autoposter/state/topic_ledger.json"
))
DEFAULT_WINDOW_DAYS = 30
MIN_ATTEMPTS_FOR_DUD = 3
CLICKS_PER_POST_WEAK_THRESHOLD = 0.3
CLICKS_PER_POST_STRONG_THRESHOLD = 1.0


def _build_query(project: str | None, window_days: int) -> tuple[str, list]:
    """SQL: per (project, search_topic) FULL OUTER JOIN of attempts vs
    candidates with posts/clicks join on the candidate side.

    Mirrors top_search_topics._query_twitter's FULL OUTER JOIN pattern
    but extends it with MAX(a.ran_at) for last_attempted_at, MIN/MAX(
    c.discovered_at) for the candidate timeline, and emits comments_total
    + clicks_per_post derived in Python rather than SQL.

    Window is a SINGLE knob applied to both sides of the join so the
    cand vs attempt counts are over the same period.
    """
    where_proj_c = ""
    where_proj_a = ""
    params: list = [str(window_days)]
    if project:
        where_proj_c = "AND LOWER(c.matched_project) = LOWER(%s)"
        params.append(project)
    params.append(str(window_days))
    if project:
        where_proj_a = "AND LOWER(a.project_name) = LOWER(%s)"
        params.append(project)

    sql = f"""
        WITH cand_agg AS (
            SELECT c.search_topic AS search_topic,
                   c.matched_project AS project_name,
                   COUNT(DISTINCT c.id) AS candidates_n,
                   COUNT(DISTINCT c.id) FILTER (WHERE c.status='posted') AS posted_n,
                   COUNT(DISTINCT c.id) FILTER (WHERE c.status IN ('skipped','expired','failed')) AS skipped_n,
                   MIN(c.discovered_at) AS first_candidate_at,
                   MAX(c.discovered_at) AS last_candidate_at,
                   COALESCE(SUM(p.views)          FILTER (WHERE c.status='posted'), 0) AS views_total,
                   COALESCE(SUM(p.upvotes)        FILTER (WHERE c.status='posted'), 0) AS likes_total,
                   COALESCE(SUM(p.comments_count) FILTER (WHERE c.status='posted'), 0) AS comments_total,
                   COUNT(plc.id) FILTER (WHERE c.status='posted' AND plc.is_bot = false) AS clicks_total
              FROM twitter_candidates c
              LEFT JOIN posts            p   ON p.id = c.post_id
              LEFT JOIN post_links       pl  ON pl.post_id = c.post_id
              LEFT JOIN post_link_clicks plc ON plc.code = pl.code
             WHERE c.discovered_at > NOW() - (%s || ' days')::interval
               AND c.search_topic IS NOT NULL
               AND c.search_topic <> ''
               {where_proj_c}
             GROUP BY c.search_topic, c.matched_project
        ),
        attempt_agg AS (
            SELECT a.search_topic AS search_topic,
                   a.project_name AS project_name,
                   COUNT(*)::int AS attempts_n,
                   MAX(a.ran_at) AS last_attempted_at,
                   COALESCE(SUM(a.tweets_found), 0)::int AS tweets_found_total,
                   COUNT(*) FILTER (WHERE COALESCE(a.tweets_found, 0) = 0)::int AS zero_supply_attempts
              FROM twitter_search_attempts a
             WHERE a.ran_at > NOW() - (%s || ' days')::interval
               AND a.search_topic IS NOT NULL
               AND a.search_topic <> ''
               {where_proj_a}
             GROUP BY a.search_topic, a.project_name
        )
        SELECT COALESCE(c.project_name, a.project_name) AS project,
               COALESCE(c.search_topic, a.search_topic) AS search_topic,
               COALESCE(a.attempts_n, 0)              AS attempts_n,
               a.last_attempted_at                     AS last_attempted_at,
               COALESCE(a.tweets_found_total, 0)      AS tweets_found_total,
               COALESCE(a.zero_supply_attempts, 0)    AS zero_supply_attempts,
               COALESCE(c.candidates_n, 0)            AS candidates_n,
               COALESCE(c.posted_n, 0)                AS posted_n,
               COALESCE(c.skipped_n, 0)               AS skipped_n,
               c.first_candidate_at                   AS first_candidate_at,
               c.last_candidate_at                    AS last_candidate_at,
               COALESCE(c.views_total, 0)             AS views_total,
               COALESCE(c.likes_total, 0)             AS likes_total,
               COALESCE(c.comments_total, 0)          AS comments_total,
               COALESCE(c.clicks_total, 0)            AS clicks_total
          FROM cand_agg c
          FULL OUTER JOIN attempt_agg a
            ON c.search_topic = a.search_topic
           AND c.project_name = a.project_name
         ORDER BY project, clicks_total DESC, posted_n DESC, attempts_n DESC
    """
    return sql, params


def _verdict_for(row: dict) -> str:
    """Bucket each (project, topic) row into one of five outcomes.

    Designed to be cheap for invent_topics.py to consume — the
    propose-refine loop can branch on this single field instead of
    reasoning over the underlying counts.
    """
    attempts_n = row.get("attempts_n", 0)
    candidates_n = row.get("candidates_n", 0)
    posted_n = row.get("posted_n", 0)
    clicks_per_post = row.get("clicks_per_post") or 0.0

    if attempts_n == 0:
        return "untried"
    if attempts_n >= MIN_ATTEMPTS_FOR_DUD and candidates_n == 0:
        return "dud"
    if posted_n == 0:
        # Got candidates but none posted (drafter rejecting them).
        # Categorize as weak so the model knows to pivot the angle.
        return "weak"
    if clicks_per_post >= CLICKS_PER_POST_STRONG_THRESHOLD:
        return "strong"
    if clicks_per_post >= CLICKS_PER_POST_WEAK_THRESHOLD:
        return "decent"
    return "weak"


def _row_to_dict(r) -> dict:
    """Tuple from db.execute().fetchall() -> dict matching the schema."""
    out = {
        "project": r[0],
        "search_topic": r[1],
        "attempts_n": int(r[2] or 0),
        "last_attempted_at": r[3].isoformat() if r[3] else None,
        "tweets_found_total": int(r[4] or 0),
        "zero_supply_attempts": int(r[5] or 0),
        "candidates_n": int(r[6] or 0),
        "posted_n": int(r[7] or 0),
        "skipped_n": int(r[8] or 0),
        "first_candidate_at": r[9].isoformat() if r[9] else None,
        "last_candidate_at": r[10].isoformat() if r[10] else None,
        "views_total": int(r[11] or 0),
        "likes_total": int(r[12] or 0),
        "comments_total": int(r[13] or 0),
        "clicks_total": int(r[14] or 0),
    }
    # Derived fields. clicks_per_post is the headline conversion metric
    # the user specifically asked for instead of clicks_total — rate
    # matters more than raw count when comparing topics that posted
    # different numbers of replies.
    out["clicks_per_post"] = (
        round(out["clicks_total"] / out["posted_n"], 3)
        if out["posted_n"] > 0
        else None
    )
    out["verdict"] = _verdict_for(out)
    return out


def aggregate(project: str | None = None,
              window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    """Return all (project, search_topic) ledger rows in the window.

    Public entry point. Callers should treat the return value as
    read-only; the materialized file is the single source of truth.
    """
    dbmod.load_env()
    conn = dbmod.get_conn()
    try:
        sql, params = _build_query(project, window_days)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def _atomic_write_json(path: Path, payload) -> None:
    """Write payload as JSON to path via temp file + rename so readers
    never see a partial file. Creates parent dirs if needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.rename(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def write_ledger(rows: list[dict], path: Path = LEDGER_PATH,
                 window_days: int = DEFAULT_WINDOW_DAYS) -> None:
    """Persist the aggregated ledger to disk atomically."""
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": window_days,
        "row_count": len(rows),
        "rows": rows,
    }
    _atomic_write_json(path, payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--project", default=None,
                    help="Filter to one project (default: all)")
    ap.add_argument("--out", default=str(LEDGER_PATH),
                    help=f"Output JSON path (default: {LEDGER_PATH})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print to stdout instead of writing the materialized file")
    args = ap.parse_args()

    rows = aggregate(project=args.project, window_days=args.window_days)

    # Per-verdict + per-project summary to stderr so the launchd log
    # has a useful one-line digest without grepping the JSON.
    by_verdict: dict[str, int] = {}
    by_project: dict[str, int] = {}
    for r in rows:
        by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
        by_project[r["project"]] = by_project.get(r["project"], 0) + 1
    print(
        f"[topic_ledger] window={args.window_days}d rows={len(rows)} "
        f"projects={len(by_project)} verdicts="
        + ",".join(f"{k}:{v}" for k, v in sorted(by_verdict.items())),
        file=sys.stderr,
    )

    if args.dry_run:
        json.dump(rows, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return

    out_path = Path(args.out).expanduser()
    write_ledger(rows, path=out_path, window_days=args.window_days)
    print(f"[topic_ledger] wrote {len(rows)} rows -> {out_path}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
