#!/usr/bin/env python3
"""seed_search_queries.py — convert a project's seeded search TOPICS into a
cold-start QUERY bank (>=30 real X advanced-search strings) at setup time.

Why
---
The Twitter cycle's deterministic Phase 1 (scripts/qualified_query_bank.py)
replays a project's historically *qualified* queries — distinct X search
strings that already produced an engaged post. A brand-new project has zero
post history, so that bank is empty and the cycle falls back to ONE crude
query (the single picked topic + "-filter:replies"). That's the "only one
search query" cold-start symptom the user hit on chosenhq.

This script fixes the supply side: it reads the project's ACTIVE topics from
project_search_topics, fans each topic out into several distinct X queries via
the SAME Claude drafting prompt invent_topics.py uses (reused, not duplicated),
optionally supply-tests them against the live browser harness, and persists the
survivors into project_search_queries with source='seed'. The bank backfills
from these active rows when the proven+invented set is still thin, so a fresh
project runs ~30 queries on day one and the seed rows fade as real winners
accumulate.

Reuse
-----
All drafting / parsing / dedup / supply-test logic is imported from
invent_topics.py (build_query_prompt, extract_queries, call_claude,
normalize_query, load_existing_query_cores, dedup_queries, supply_test,
harness_alive). This file is only the orchestration + persistence layer.

Topics + queries are read/written through the website API (/api/v1/*) per the
"no direct SQL in pipeline Python" rule.

CLI:
    python3 scripts/seed_search_queries.py --project chosenhq
    python3 scripts/seed_search_queries.py --project fazm --target 30
    python3 scripts/seed_search_queries.py --project fazm --supply-test off
    python3 scripts/seed_search_queries.py --project fazm --dry-run
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http_api import api_get, api_post  # noqa: E402
from pick_project import load_config  # noqa: E402
from invent_topics import (  # noqa: E402
    CDP_PORT,
    FRESHNESS_HOURS,
    build_query_prompt,
    call_claude,
    dedup_queries,
    extract_queries,
    harness_alive,
    load_existing_query_cores,
    normalize_query,
    supply_test,
)

# How many real queries we want a fresh project to launch with. The user's
# target: "convert these search topics into at least 30 search queries
# altogether". 30 is enough to fan a typical 6-12 topic universe across
# multiple angles without exploding setup cost.
DEFAULT_TARGET = 30

# Per-topic draft cap so a project with very few topics doesn't ask Claude for
# 30 queries off one topic (they'd collapse into near-dupes). With >=4 topics
# the ceil(target/topics) math stays under this anyway.
MAX_PER_TOPIC = 8
MIN_PER_TOPIC = 2

# Query-draft Claude call timeout (one call per topic).
DRAFT_TIMEOUT_SEC = 240


def _load_active_topics(project: str) -> list[str]:
    """Active topics for a project from project_search_topics (DB universe)."""
    resp = api_get(
        "/api/v1/project-search-topics",
        {"project": project, "status": "active"},
    )
    data = (resp or {}).get("data") or {}
    rows = data.get("topics") or []
    out, seen = [], set()
    for r in rows:
        t = (r.get("topic") or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _load_existing_seed_cores(project: str) -> set[str]:
    """Normalized cores of seed queries already persisted for this project, so
    re-running the seeder is idempotent and never double-drafts the same core."""
    try:
        resp = api_get(
            "/api/v1/project-search-queries",
            {"project": project, "status": "all"},
        )
    except SystemExit as exc:
        print(f"[seed_search_queries] existing-seed read failed for "
              f"{project!r}: {exc} (proceeding without seed dedup)",
              file=sys.stderr)
        return set()
    data = (resp or {}).get("data") or {}
    rows = data.get("queries") or []
    return {normalize_query(r.get("query") or "") for r in rows if r.get("query")}


def _fetch_active_queries(project: str) -> list[dict]:
    """The project's current ACTIVE seed-query bank (query + topic), so a caller
    can show the user exactly what the cycle will fan out over. Best-effort:
    returns [] on read failure."""
    try:
        resp = api_get(
            "/api/v1/project-search-queries",
            {"project": project, "status": "active"},
        )
    except SystemExit:
        return []
    data = (resp or {}).get("data") or {}
    out = []
    for r in (data.get("queries") or []):
        q = (r.get("query") or "").strip()
        if q:
            out.append({"query": q, "topic": (r.get("topic") or "").strip()})
    return out


def _find_project(cfg: dict, name: str) -> dict | None:
    for p in cfg.get("projects", []):
        if (p.get("name") or "").strip().lower() == name.strip().lower():
            return p
    return None


def _persist(project: str, query: str, topic: str,
             supply_tested: bool, tweets_found, dry_run: bool) -> str:
    """POST one seed query; returns the action ('inserted'/'updated'/'fail')."""
    if dry_run:
        return "dry"
    try:
        resp = api_post(
            "/api/v1/project-search-queries",
            body={
                "project": project,
                "query": query,
                "topic": topic,
                "source": "seed",
                "status": "active",
                "supply_tested": supply_tested,
                "tweets_found": tweets_found,
            },
        )
    except SystemExit as e:
        print(f"[FAIL] {project}: {query!r}: {e}", file=sys.stderr)
        return "fail"
    data = (resp or {}).get("data") or resp or {}
    return data.get("action") or "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True,
                    help="Project name (config.json casing).")
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET,
                    help=f"Total queries to aim for (default {DEFAULT_TARGET}).")
    ap.add_argument("--supply-test", choices=["auto", "on", "off"],
                    default="auto",
                    help="auto (default): supply-test only if the harness is "
                         "up; on: require it; off: skip and seed all drafts.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Draft + (maybe) supply-test, but do NOT persist.")
    ap.add_argument("--emit-json", action="store_true",
                    help="After seeding, print the project's full ACTIVE seed-query "
                         "bank as JSON on a sentinel line (===QUERIES_JSON===) so a "
                         "caller (e.g. the MCP setup tool) can hand the queries back "
                         "to the user.")
    args = ap.parse_args()

    cfg = load_config()
    project_entry = _find_project(cfg, args.project)
    if not project_entry:
        print(f"seed_search_queries: project {args.project!r} not in config.json",
              file=sys.stderr)
        return 2
    # canonical name as stored
    project = (project_entry.get("name") or args.project).strip()

    topics = _load_active_topics(project)
    if not topics:
        print(f"seed_search_queries: no active topics for {project!r} — seed "
              f"topics first (scripts/seed_search_topics.py).", file=sys.stderr)
        return 3

    target = max(1, args.target)
    per_topic = math.ceil(target / len(topics))
    per_topic = max(MIN_PER_TOPIC, min(MAX_PER_TOPIC, per_topic))

    # Dedup against (a) every query EVER attempted for this project and (b) seed
    # queries already persisted. Accumulates as we go so topics don't overlap.
    avoid_cores = load_existing_query_cores(project) | _load_existing_seed_cores(project)

    print(f"seed_search_queries: project={project!r} topics={len(topics)} "
          f"target={target} per_topic={per_topic} "
          f"existing_cores={len(avoid_cores)}", file=sys.stderr)

    # --- Draft queries topic by topic ------------------------------------
    drafted: list[tuple[str, str]] = []  # (query, topic)
    for topic in topics:
        if len({normalize_query(q) for q, _ in drafted}) >= target:
            break
        prompt = build_query_prompt(
            project_entry, topic, per_topic, avoid_queries=avoid_cores
        )
        try:
            out = call_claude(prompt, timeout_sec=DRAFT_TIMEOUT_SEC)
        except SystemExit as e:
            print(f"[seed_search_queries] draft failed for topic {topic!r}: {e}",
                  file=sys.stderr)
            continue
        qs = extract_queries(out, per_topic)
        new_qs, dupes = dedup_queries(qs, avoid_cores)
        for q in new_qs:
            avoid_cores.add(normalize_query(q))
            drafted.append((q, topic))
        print(f"  topic={topic!r}: drafted={len(qs)} new={len(new_qs)} "
              f"dupes={len(dupes)}", file=sys.stderr)

    if not drafted:
        print("seed_search_queries: drafted 0 queries — nothing to seed.",
              file=sys.stderr)
        return 1

    # --- Decide whether to supply-test -----------------------------------
    do_supply = False
    if args.supply_test == "on":
        do_supply = True
    elif args.supply_test == "auto":
        do_supply = harness_alive(CDP_PORT)
    if args.supply_test == "on" and not harness_alive(CDP_PORT):
        print("seed_search_queries: --supply-test on but harness not reachable "
              f"on :{CDP_PORT}", file=sys.stderr)
        return 4

    supply_map: dict[str, int] = {}
    supply_ran = False
    if do_supply:
        # Group by topic so supply_test gets a coherent (topic, queries) batch.
        by_topic: dict[str, list[str]] = {}
        for q, t in drafted:
            by_topic.setdefault(t, []).append(q)
        for t, qs in by_topic.items():
            tested, results = supply_test(project, t, qs,
                                          freshness_hours=FRESHNESS_HOURS)
            if not tested:
                # Lock timeout / browser down: do NOT treat as zero supply.
                print(f"  supply-test topic={t!r}: not tested (harness "
                      f"unavailable) — keeping queries untested",
                      file=sys.stderr)
                continue
            supply_ran = True
            for r in results:
                supply_map[normalize_query(r.get("query") or "")] = int(
                    r.get("tweets_found") or 0
                )

    # --- Persist ----------------------------------------------------------
    # When supply ran, drop zero-supply queries (they surfaced nothing fresh),
    # but never let the drop take us below MIN_KEEP — a thin real bank beats an
    # empty one for cold start. When supply did NOT run, seed everything
    # untested (the bank still beats the 1-query fallback).
    MIN_KEEP = max(1, target // 2)
    to_seed: list[tuple[str, str, bool, object]] = []
    dropped_zero = 0
    for q, t in drafted:
        core = normalize_query(q)
        tested = supply_ran and core in supply_map
        tw = supply_map.get(core) if tested else None
        if supply_ran and tested and tw == 0:
            dropped_zero += 1
            continue
        to_seed.append((q, t, bool(tested), tw))

    if supply_ran and len(to_seed) < MIN_KEEP:
        # Too aggressive — restore the highest-supply zeros... actually all were
        # zero, so restore drafted order until MIN_KEEP. Keep them as tested=0.
        restored = 0
        have = {normalize_query(q) for q, _, _, _ in to_seed}
        for q, t in drafted:
            if restored and len(to_seed) >= MIN_KEEP:
                break
            core = normalize_query(q)
            if core in have:
                continue
            to_seed.append((q, t, True, supply_map.get(core, 0)))
            have.add(core)
            restored += 1
        print(f"  supply-test dropped too many; restored {restored} to meet "
              f"MIN_KEEP={MIN_KEEP}", file=sys.stderr)

    inserted = updated = failed = 0
    for q, t, tested, tw in to_seed:
        action = _persist(project, q, t, tested, tw, args.dry_run)
        if action == "inserted":
            inserted += 1
        elif action == "updated":
            updated += 1
        elif action == "fail":
            failed += 1

    # Machine-parseable summary line (consumed by mcp/src/index.ts setup hook).
    print(
        f"seed_search_queries: project={project} topics={len(topics)} "
        f"drafted={len(drafted)} supply_ran={int(supply_ran)} "
        f"dropped_zero={dropped_zero} seeded={inserted + updated} "
        f"inserted={inserted} updated={updated} failed={failed}"
        + (" [dry-run]" if args.dry_run else "")
    )

    # Hand the resulting bank back to the caller (MCP setup tool) so it can show
    # the user exactly which queries the cycle will run. Sentinel-delimited so it
    # survives alongside the human/stderr log noise. On --dry-run we report what
    # we drafted (nothing persisted yet); otherwise the live active bank.
    if args.emit_json:
        if args.dry_run:
            queries = [{"query": q, "topic": t} for q, t in drafted]
        else:
            queries = _fetch_active_queries(project)
        print("===QUERIES_JSON===")
        print(json.dumps({"project": project, "count": len(queries),
                          "queries": queries}))

    return 1 if (failed and not (inserted or updated)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
