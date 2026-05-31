#!/usr/bin/env python3
"""
qualified_query_bank.py — programmatic Phase 1 query bank for the Twitter cycle.

EXPERIMENT (2026-05-29, flag TWITTER_PHASE1_QUERY_BANK=1): instead of asking
Claude to draft one fresh query per picked project every cycle, we replay the
project's *historically qualified* queries — every distinct query phrasing that
has ever produced a posted reply with at least one like OR at least one
(non-bot) link click. Topic is ignored as a gate: we run the FULL qualified set
for the picked project regardless of which search_topic the picker chose.

Why this exists: ~95% of LLM-drafted queries produce zero posts, and a tiny
qualified tail (≈2-30 per project) carries all the engaged output. Re-drafting
that tail with an LLM every cycle is pure cost. The freshness window inside
twitter_scan.scan() means replaying a fixed query each cycle still only surfaces
NEW tweets, so there's no downside to running the proven set deterministically.

Output (stdout): a JSON list shaped exactly like the lean Phase 1 $QUERIES_TMP
that run-twitter-cycle.sh feeds to twitter_scan.scan():

    [{"project": "...", "query": "...", "search_topic": "...",
      "likes": <int>, "clicks": <int>, "posts": <int>}, ...]

Qualification (per distinct NORMALIZED query core, operators like since:/
min_faves: stripped for grouping):
  - a core qualifies if ANY posted candidate it produced has likes>0 OR clicks>0
  - the emitted `query` is the best-performing RAW variant of that core
    (max clicks, then max likes), so a working min_faves:N operator is kept
  - `search_topic` is the most common topic among that core's posted candidates
    (purely for end-to-end attribution; not used as a gate)

Usage:
    python3 scripts/qualified_query_bank.py --project fazm
    python3 scripts/qualified_query_bank.py --project Runner --limit 20
    python3 scripts/qualified_query_bank.py --project fazm --min-likes 2
    python3 scripts/qualified_query_bank.py --all   # debug: counts per project
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


# Default: also include the invent pipeline's proven supply set (queries
# invent_topics.py drafted + supply-tested that surfaced fresh tweets but
# never produced a posted candidate, so the bank's JOIN to twitter_candidates
# can't see them). Disable with --no-invented for debugging.
#
# Floor=1 is intentional and NOT the same as invent's SUPPLY_FLOOR=3:
#   - invent's SUPPLY_FLOOR=3 = per-TOPIC stop condition (sum across the
#     topic's 5 queries must hit 3 for the invent loop to halt early).
#   - INVENT_MIN_SUPPLY=1 = per-QUERY bank-inclusion gate ("any query that
#     surfaced at least one fresh tweet deserves at least one cycle shot").
# Conflating the two silently filters out single-tweet winners — the user
# explicitly wants every non-zero-supply query reused, and zero-supply
# queries persisted (which they are) but not reused.
INVENT_MIN_SUPPLY = 1
INVENT_FETCH_LIMIT = 200


def normalize(q: str) -> str:
    """Strip per-cycle operators so phrasings that differ only by freshness/
    min_faves collapse to one core. Mirrors the analysis normalization."""
    q = (q or "").lower()
    for pat in (
        r"\bsince:\S+", r"\buntil:\S+",
        r"\bsince_time:\S+", r"\buntil_time:\S+",
        r"\bmin_faves:\d+", r"\bmin_retweets:\d+", r"\bmin_replies:\d+",
        r"\b-?filter:\S+", r"\blang:\S+",
    ):
        q = re.sub(pat, "", q)
    q = re.sub(r'[()"]', "", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _fetch_rows(project=None):
    """One row per posted candidate of a project, with likes + non-bot clicks.

    Migrated 2026-05-30 off direct DB (db.get_conn) onto the HTTP lane:
    GET /api/v1/twitter-search-attempts/qualified-rows[?project=...]. The route
    mirrors the legacy JOIN exactly, including the cross-route guard below, and
    returns one dict per posted candidate: {project_name, query, topic, likes,
    clicks}. There is intentionally NO direct-DB fallback.

    Legacy joins (now server-side): candidate(status=posted) -> search_attempt
    (for the raw query + topic) -> post (upvotes = likes) -> non-bot click count
    via post_links / post_link_clicks. search_attempt_id is required, so
    candidates posted before that column existed are excluded (their query can't
    be attributed).

    Cross-route guard (2026-05-29): a query only qualifies for the project
    that ISSUED it. The prep step re-routes a candidate to a different
    project when the thread fits it better (e.g. a broad invented Podlog
    query with "codebase" surfaces a Claude Code thread that gets routed to
    fazm). When that happens posts.project_name follows the new project while
    a.project_name stays the origin. Without `p.project_name = a.project_name`
    the origin query would "qualify" into its own bank on a conversion it
    actually routed away, then get replayed for the wrong product forever.
    NULL post project is treated as same-project so legacy rows written
    before project_name was stamped are not dropped.
    """
    query = {"project": project} if project else None
    resp = api_get("/api/v1/twitter-search-attempts/qualified-rows", query)
    data = (resp or {}).get("data") or {}
    return list(data.get("rows") or [])


def build_bank(project, min_likes=1, min_clicks=1, limit=None):
    rows = _fetch_rows(project)
    # group by normalized core
    cores = defaultdict(lambda: {
        "raw_variants": defaultdict(lambda: {"likes": 0, "clicks": 0}),
        "topics": defaultdict(int),
        "likes": 0, "clicks": 0, "posts": 0,
    })
    for row in rows:
        query = row.get("query") or ""
        topic = row.get("topic") or ""
        likes = int(row.get("likes") or 0)
        clicks = int(row.get("clicks") or 0)
        core = normalize(query)
        if not core:
            continue
        c = cores[core]
        c["posts"] += 1
        c["likes"] += likes
        c["clicks"] += clicks
        c["raw_variants"][query]["likes"] += likes
        c["raw_variants"][query]["clicks"] += clicks
        if topic:
            c["topics"][topic] += 1

    bank = []
    for core, c in cores.items():
        qualifies = (c["likes"] >= min_likes) or (c["clicks"] >= min_clicks)
        if not qualifies:
            continue
        # best raw variant: max clicks, then max likes
        best_raw = max(
            c["raw_variants"].items(),
            key=lambda kv: (kv[1]["clicks"], kv[1]["likes"]),
        )[0]
        topic = max(c["topics"].items(), key=lambda kv: kv[1])[0] if c["topics"] else ""
        bank.append({
            "project": project,
            "query": best_raw,
            "search_topic": topic,
            "likes": c["likes"],
            "clicks": c["clicks"],
            "posts": c["posts"],
        })

    # rank by clicks desc, then likes desc — so --limit keeps the strongest
    bank.sort(key=lambda b: (b["clicks"], b["likes"], b["posts"]), reverse=True)
    if limit:
        bank = bank[:limit]
    return bank


def fetch_invented_queries(project: str, min_supply: int = INVENT_MIN_SUPPLY,
                           limit: int = INVENT_FETCH_LIMIT) -> list[dict]:
    """Fetch invent_topics.py's proven-supply queries for a project via the
    /api/v1/twitter-search-attempts/invented-queries route. NOT a direct DB
    read — keeps the invent pipeline's persistence behind the API the same
    way log_twitter_search_attempts.py does on the write side.

    Returns bank-shaped rows (likes/clicks/posts=0, plus supply/attempts).
    Drops any whose normalized core already exists in `existing_cores` (caller
    handles dedup against the posted-engagement bank).
    """
    try:
        resp = api_get(
            "/api/v1/twitter-search-attempts/invented-queries",
            {"project": project, "min_supply": min_supply, "limit": limit},
        )
    except SystemExit as e:
        print(f"qualified_query_bank: invented-queries fetch failed for "
              f"{project!r}: {e}", file=sys.stderr)
        return []
    data = (resp or {}).get("data") or {}
    return list(data.get("queries") or [])


def merge_invented(bank: list[dict], invented: list[dict]) -> list[dict]:
    """Append invented queries to the bank, skipping any whose normalized core
    already appears in the posted-engagement bank (proven > unproven; same
    core won't surface twice). Invented entries land at the end — they sort
    naturally below proven ones because clicks/likes/posts are 0."""
    existing_cores = {normalize(b["query"]) for b in bank}
    appended = []
    for inv in invented:
        core = normalize(inv.get("query", ""))
        if not core or core in existing_cores:
            continue
        existing_cores.add(core)
        appended.append(inv)
    return bank + appended


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="Project name (config.json casing).")
    ap.add_argument("--min-likes", type=int, default=1,
                    help="A query core qualifies if its posts have >= this many total likes.")
    ap.add_argument("--min-clicks", type=int, default=1,
                    help="...OR >= this many total non-bot clicks.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the bank to the top-N strongest queries (safety budget).")
    ap.add_argument("--all", action="store_true",
                    help="Debug: print per-project bank sizes instead of one project's queries.")
    ap.add_argument("--from-projects-json", action="store_true",
                    help="Read the picked-projects JSON array (objects with a 'name' "
                         "field, i.e. run-twitter-cycle.sh's PROJECTS_JSON) on stdin and "
                         "emit the COMBINED bank for every project, shaped like the lean "
                         "Phase 1 $QUERIES_TMP. This is the cycle integration entrypoint.")
    ap.add_argument("--no-invented", action="store_true",
                    help="Skip the invented-queries merge (proven-engagement only). "
                         "Useful for debugging the posted-candidates path in isolation.")
    ap.add_argument("--invent-min-supply", type=int, default=INVENT_MIN_SUPPLY,
                    help=f"Min sum(tweets_found) for an invented query to enter the "
                         f"bank tail (default {INVENT_MIN_SUPPLY}, matches "
                         f"invent_topics.py SUPPLY_FLOOR).")
    args = ap.parse_args()

    if args.from_projects_json:
        try:
            projects = json.loads(sys.stdin.read() or "[]")
        except json.JSONDecodeError as e:
            print(f"qualified_query_bank: bad PROJECTS_JSON on stdin: {e}", file=sys.stderr)
            json.dump([], sys.stdout)
            print()
            return 1
        combined = []
        for p in projects:
            name = (p or {}).get("name") if isinstance(p, dict) else None
            if not name:
                continue
            bank = build_bank(name, args.min_likes, args.min_clicks, args.limit)
            proven_size = len(bank)
            if not args.no_invented:
                invented = fetch_invented_queries(name, args.invent_min_supply)
                bank = merge_invented(bank, invented)
            combined.extend(bank)
            invent_added = len(bank) - proven_size
            print(f"qualified_query_bank: project={name!r} -> {proven_size} proven "
                  f"+ {invent_added} invented = {len(bank)} queries",
                  file=sys.stderr)
        json.dump(combined, sys.stdout)
        print()
        print(f"qualified_query_bank: combined bank = {len(combined)} queries across "
              f"{len(projects)} project(s)", file=sys.stderr)
        return 0

    if args.all:
        rows = _fetch_rows(conn, None)
        per = defaultdict(list)
        for r in rows:
            per[r[0]].append(r)
        out = []
        for proj in sorted(per):
            bank = build_bank(conn, proj, args.min_likes, args.min_clicks, args.limit)
            out.append({"project": proj, "bank_size": len(bank)})
        json.dump(out, sys.stdout, indent=2)
        print()
        return 0

    if not args.project:
        print("qualified_query_bank: --project required (or --all)", file=sys.stderr)
        return 2

    bank = build_bank(conn, args.project, args.min_likes, args.min_clicks, args.limit)
    proven_size = len(bank)
    if not args.no_invented:
        invented = fetch_invented_queries(args.project, args.invent_min_supply)
        bank = merge_invented(bank, invented)
    invent_added = len(bank) - proven_size
    json.dump(bank, sys.stdout)
    print()
    print(f"qualified_query_bank: {proven_size} proven + {invent_added} invented = "
          f"{len(bank)} queries for project={args.project!r} "
          f"(min_likes={args.min_likes} OR min_clicks={args.min_clicks}, "
          f"invent_min_supply={args.invent_min_supply}"
          f"{', limit=' + str(args.limit) if args.limit else ''})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
