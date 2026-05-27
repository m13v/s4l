#!/usr/bin/env python3
"""Shared accessor for project_search_topics.

Single chokepoint for every runtime consumer of the per-project topic list
that used to live in config.json (projects[].search_topics[]). Reads from
the DB only — config.json is seed-only via scripts/seed_search_topics.py.

Why this module exists: 10+ scripts (pick_project, score_twitter_candidates,
scan_twitter_mentions_browser, scan_dm_candidates, post_reddit, post_github,
find_threads, project_excludes, seo/generate_keywords, run-linkedin.sh)
all needed the same per-project topic list. Replacing each `p.get("search_topics")`
with its own ad-hoc HTTP call would have produced 25 API hits per script
run (one per project, every cycle) with inconsistent error handling. This
helper does one network round-trip per project per process and caches the
result so the 10 consumers share work.

Public surface:

  topics_for_project(name) -> list[str]
      Returns the project's active topics (status='active'). Process-cached
      so repeated calls within one script run are free. Returns [] when the
      project has no active rows — that's a valid "this project just doesn't
      do topic-based matching" state for routing/filtering consumers. The
      picker (pick_search_topic.py) has its own zero-rows-is-error check
      layered on top.

      Raises TopicsError on actual API failure (network down, 5xx, auth
      mismatch). Callers should let it propagate so the cycle aborts loudly
      instead of degrading to a config.json fallback that doesn't exist
      anymore.

  clear_cache()
      Drop the process cache. Test-only; production scripts never need this.
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TopicsError(RuntimeError):
    """Raised when the topics API call itself fails (network, 5xx, auth).

    NOT raised on zero rows — that's a valid empty list. The picker
    layers its own "no universe" error on top via PickerError.
    """


_CACHE: Dict[str, List[str]] = {}


def topics_for_project(name: str) -> List[str]:
    """Active topics for one project (DB-only, process-cached)."""
    if not name:
        return []
    key = name.strip()
    if key in _CACHE:
        return _CACHE[key]
    try:
        from http_api import api_get
        resp = api_get(
            "/api/v1/project-search-topics",
            query={"project": key, "status": "active"},
        )
    except Exception as e:
        raise TopicsError(
            f"project-search-topics API unreachable for project={key!r}: {e}"
        ) from e
    data = (resp or {}).get("data") or {}
    rows = data.get("topics") or []
    seen = set()
    topics: List[str] = []
    for r in rows:
        t = (r.get("topic") or "").strip()
        if t and t not in seen:
            seen.add(t)
            topics.append(t)
    _CACHE[key] = topics
    return topics


def clear_cache() -> None:
    _CACHE.clear()


if __name__ == "__main__":
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    args = ap.parse_args()
    print(_json.dumps(topics_for_project(args.project), indent=2))
