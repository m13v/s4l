#!/usr/bin/env python3
"""
reddit_query_bank.py — programmatic discover-phase query bank for the Reddit cycle.

Reddit analog of scripts/qualified_query_bank.py (Twitter). Before 2026-06-01 the
Reddit discover phase spent a full Claude LLM session just to pick query phrasings
and fire `reddit_tools.py search` Bash calls in OPAQUE mode (Claude never even saw
the results). That LLM call added ~zero value: query selection + search execution
are both deterministic. This module replaces the "Claude picks queries" half so
discover can run fully in Python, matching Twitter (scan = deterministic Python,
Claude only drafts).

Where the queries come from (in priority order):
  1. PROVEN queries — /api/v1/search-topics/ranked?platform=reddit&project=X.
     On Reddit, the harvested `search_topic` IS the raw query string that was run
     (see post_reddit._discover_iteration harvest: search_topic = payload["query"]),
     so the ranked-topics route already returns proven query phrasings with their
     clicks / posts / composite. A row qualifies if it has produced at least one
     posted candidate (posts > 0) OR at least one real click (clicks_total > 0).
     Ranked clicks-first, then composite (clicks*100 + comments + upvotes).
  2. CONFIG seeds — config.json `search_topics` for the project (via
     project_topics.topics_for_project). These give cold-start + coverage for
     projects/angles that have not converted yet. Appended after proven queries,
     deduped by normalized core so a seed that already converted isn't run twice.

Output (stdout, --json or default): a JSON list shaped like
    [{"project": "...", "query": "...", "source": "proven|seed",
      "clicks": <int>, "posts": <int>, "composite": <int>}, ...]
ranked strongest-first. `_discover_iteration` consumes the `query` field, caps to
SAPS_REDDIT_MAX_SEARCHES, and runs each via reddit_tools.cmd_search.

Usage:
    python3 scripts/reddit_query_bank.py --project fazm
    python3 scripts/reddit_query_bank.py --project Podlog --limit 6 --json
    python3 scripts/reddit_query_bank.py --project fazm --no-seeds   # proven only
    python3 scripts/reddit_query_bank.py --all                       # per-project sizes
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402

try:
    from project_topics import topics_for_project
except Exception:  # pragma: no cover - defensive; bank still works proven-only
    topics_for_project = None


def normalize(q: str) -> str:
    """Collapse a query to a comparable core for dedup. Reddit queries carry no
    per-cycle operators (no since:/min_faves), so this is just lowercase +
    punctuation/whitespace normalization."""
    q = (q or "").lower()
    q = re.sub(r'["()]', "", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def fetch_proven(project, window_days=30, limit=40):
    """Proven reddit query phrasings from /api/v1/search-topics/ranked.

    Returns bank rows for every ranked search_topic that has converted
    (posts > 0) or driven a click (clicks_total > 0). Ranked clicks-first then
    composite, mirroring the route's own ordering. NO direct-DB fallback (same
    convention as qualified_query_bank.py)."""
    q = {"platform": "reddit", "window_days": int(window_days), "limit": int(limit)}
    if project:
        q["project"] = project
    try:
        resp = api_get("/api/v1/search-topics/ranked", q)
    except SystemExit as e:
        print(f"reddit_query_bank: search-topics/ranked fetch failed for "
              f"{project!r}: {e}", file=sys.stderr)
        return []
    rows = ((resp or {}).get("data") or {}).get("rows") or []
    bank = []
    for r in rows:
        topic = (r.get("search_topic") or "").strip()
        if not topic:
            continue
        clicks = int(r.get("clicks_total") or 0)
        posts = int(r.get("posts") or 0)
        composite = int(r.get("composite_score") or 0)
        if posts <= 0 and clicks <= 0:
            continue  # never converted, never clicked — not "proven"
        bank.append({
            "project": project,
            "query": topic,
            "source": "proven",
            "clicks": clicks,
            "posts": posts,
            "composite": composite,
        })
    bank.sort(key=lambda b: (b["clicks"], b["composite"], b["posts"]), reverse=True)
    return bank


def seeds_from_config(project):
    """config.json `search_topics` for the project as bank rows (source=seed,
    zero stats so they sort below proven queries)."""
    if not topics_for_project:
        return []
    try:
        seeds = list(topics_for_project(project or "") or [])
    except Exception as e:
        print(f"reddit_query_bank: topics_for_project failed for {project!r}: {e}",
              file=sys.stderr)
        return []
    out = []
    for s in seeds:
        s = (s or "").strip()
        if not s:
            continue
        out.append({
            "project": project,
            "query": s,
            "source": "seed",
            "clicks": 0,
            "posts": 0,
            "composite": 0,
        })
    return out


def build_bank(project, limit=None, include_seeds=True, window_days=30):
    """Proven queries first, then config seeds not already covered (deduped by
    normalized core). Capped to `limit` if given."""
    proven = fetch_proven(project, window_days=window_days)
    seen = {normalize(b["query"]) for b in proven}
    bank = list(proven)
    if include_seeds:
        for s in seeds_from_config(project):
            core = normalize(s["query"])
            if not core or core in seen:
                continue
            seen.add(core)
            bank.append(s)
    if limit:
        bank = bank[: int(limit)]
    return bank


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="Project name (config.json casing).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the bank to the top-N strongest queries.")
    ap.add_argument("--window-days", type=int, default=30,
                    help="Lookback window for proven-query stats (default 30).")
    ap.add_argument("--no-seeds", action="store_true",
                    help="Proven queries only; skip the config.json seed tail.")
    ap.add_argument("--json", action="store_true",
                    help="Force JSON output (default is already JSON).")
    ap.add_argument("--all", action="store_true",
                    help="Debug: print proven-bank size per project from config.json.")
    args = ap.parse_args()

    if args.all:
        # Lazy import config so --project path has no dependency on it.
        try:
            import config_loader  # type: ignore
            projects = [p.get("name") for p in (config_loader.load() or {}).get("projects", [])]
        except Exception:
            cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "config.json")
            with open(cfg_path) as f:
                projects = [p.get("name") for p in (json.load(f) or {}).get("projects", [])]
        out = []
        for name in filter(None, projects):
            proven = fetch_proven(name)
            out.append({"project": name, "proven": len(proven),
                        "seeds": len(seeds_from_config(name))})
        json.dump(out, sys.stdout, indent=2)
        print()
        return 0

    if not args.project:
        print("reddit_query_bank: --project required (or --all)", file=sys.stderr)
        return 2

    bank = build_bank(args.project, limit=args.limit,
                      include_seeds=not args.no_seeds,
                      window_days=args.window_days)
    proven_n = sum(1 for b in bank if b["source"] == "proven")
    seed_n = len(bank) - proven_n
    json.dump(bank, sys.stdout)
    print()
    print(f"reddit_query_bank: {proven_n} proven + {seed_n} seed = {len(bank)} "
          f"queries for project={args.project!r}"
          f"{' (limit=' + str(args.limit) + ')' if args.limit else ''}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
