#!/usr/bin/env python3
"""Shared accessor for project_search_topics.

Single chokepoint for every runtime consumer of the per-project topic list
that used to live in config.json (projects[].search_topics[]). The DB
(project_search_topics) is the living runtime universe (invent/decay/exclude
operate on it); config.json's search_topics[] is the human-authored SEED.

Self-healing seed-on-empty: if the DB has no active topics for a project that
DOES carry a search_topics[] seed in config.json, this module mirrors that seed
into the DB once (first-run bootstrap) and re-reads. That makes "add a project
to config.json" sufficient to make it run, with no separate manual
seed_search_topics.py step to forget. Before this, a fully-configured project
(weight, enabled, topics) could silently never run because the manual seed was
skipped (Capstacker 2026-06, Karol/pamba earlier). This is a SEED-ON-EMPTY, not
a live config.json fallback: it fires only for a project the DB has never heard
of, and once rows exist the living state owns the universe and it never runs
again — so it does not resurrect decayed/excluded topics.

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

import json
import os
import sys
from typing import Dict, List, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")


class TopicsError(RuntimeError):
    """Raised when the topics API call itself fails (network, 5xx, auth).

    NOT raised on zero rows — that's a valid empty list. The picker
    layers its own "no universe" error on top via PickerError.
    """


_CACHE: Dict[str, List[str]] = {}
_BOOTSTRAP_ATTEMPTED: Set[str] = set()


def _fetch_active_topics(key: str) -> List[str]:
    """Read active topics for one project from the DB. Raises TopicsError on a
    real API failure (network, 5xx, auth) so the cycle aborts loudly."""
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
    return topics


def _config_topics_for(name: str) -> List[str]:
    """The seed search_topics[] for one project from config.json (de-duped).
    Used only to bootstrap a project the DB has never seen — see
    _bootstrap_from_config."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        return []
    key = name.strip().lower()
    for p in cfg.get("projects", []):
        if (p.get("name") or "").strip().lower() == key:
            seen, out = set(), []
            for t in (p.get("search_topics") or []):
                t = (t or "").strip()
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
            return out
    return []


def _bootstrap_from_config(name: str) -> int:
    """One-time self-heal: mirror a project's config.json search_topics into the
    DB when it has zero active rows. Idempotent (the API upserts on
    install_id+project+topic), so a duplicate/concurrent run is harmless. Never
    raises: a seed failure is logged and treated as "no topics" so the read path
    is never worse than before, just loud instead of silent. Returns the number
    of topics POSTed. Honors SAPS_NO_TOPIC_AUTOSEED=1 for read-only contexts."""
    if os.environ.get("SAPS_NO_TOPIC_AUTOSEED") == "1":
        return 0
    topics = _config_topics_for(name)
    if not topics:
        return 0
    try:
        from http_api import api_post
    except Exception as e:
        sys.stderr.write(
            f"[project_topics] auto-seed unavailable project={name!r}: {e}\n"
        )
        return 0
    seeded = 0
    for topic in topics:
        try:
            api_post(
                "/api/v1/project-search-topics",
                body={"project": name, "topic": topic,
                      "source": "seed", "status": "active"},
            )
            seeded += 1
        except Exception as e:
            sys.stderr.write(
                f"[project_topics] auto-seed FAILED project={name!r} "
                f"topic={topic!r}: {e}\n"
            )
    if seeded:
        sys.stderr.write(
            f"[project_topics] auto-seeded {seeded}/{len(topics)} topic(s) for "
            f"project={name!r} from config.json (first-run bootstrap)\n"
        )
    return seeded


def topics_for_project(name: str) -> List[str]:
    """Active topics for one project (DB-backed, process-cached, self-healing).

    On the first read where the DB has no active topics but config.json carries
    a search_topics[] seed, the seed is mirrored into the DB once and re-read,
    so adding a project to config.json is enough to make it run. After that the
    DB is the single living source of truth."""
    if not name:
        return []
    key = name.strip()
    if key in _CACHE:
        return _CACHE[key]
    topics = _fetch_active_topics(key)
    if not topics and key not in _BOOTSTRAP_ATTEMPTED:
        _BOOTSTRAP_ATTEMPTED.add(key)
        if _bootstrap_from_config(key) > 0:
            topics = _fetch_active_topics(key)
    _CACHE[key] = topics
    return topics


def clear_cache() -> None:
    _CACHE.clear()
    _BOOTSTRAP_ATTEMPTED.clear()


if __name__ == "__main__":
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    args = ap.parse_args()
    print(_json.dumps(topics_for_project(args.project), indent=2))
