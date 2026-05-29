#!/usr/bin/env python3
"""
invent_mcp_server.py — MCP stdio server exposing topic + query lookup tools
to the invent_topics.py Claude session.

WHY
---
Before this server, invent_topics.py spawned a fresh `claude -p` for every
topic proposal — Claude saw only the top-12-per-bucket ledger slice and had
NO way to verify a topic against the full universe before proposing it.
Dupes were caught AFTER the session ended, then a brand-new session retried
with a longer avoid-list. Lots of wasted Claude calls.

This MCP server gives Claude **in-session tools** to:
  - search the FULL active topic universe by substring (no truncation)
  - read per-topic funnel stats (attempts / supply / candidates / posts /
    clicks / verdict) for any topic Claude is considering
  - submit the topic itself, with Jaccard dedup running ON THE SERVER so a
    near-dupe returns a tool-call error Claude can react to (try a different
    angle) instead of silently dying outside the session
  - the same shape for queries: search distinct query history, read per-
    query performance, see invented-but-not-posted winners

ARCHITECTURE
------------
Pure stdio MCP server (`mcp.server.fastmcp.FastMCP`). All persistence flows
through the social-autoposter-website /api/v1/* routes — no direct DB.
Mirrors the same routes invent_topics.py already uses, so the two paths
stay consistent.

USAGE
-----
The server is launched as a subprocess by `claude -p --mcp-config <cfg>`.
The cfg file points stdio at:
    python3 /Users/matthewdi/social-autoposter/scripts/invent_mcp_server.py
The session inherits the calling user's identity (via http_api's installation
header), so writes are correctly attributed.
"""
import os
import re
import sys
from typing import Optional

# Make the sibling scripts/ directory importable so we can reuse http_api
# (the same client log_twitter_search_attempts.py + invent_topics.py use).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http_api import api_get, api_post  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402


# --- Constants mirrored from invent_topics.py -------------------------------
# Kept in lockstep with invent_topics.SIMILARITY_THRESHOLD so the in-session
# dedup verdict matches what the standalone post-hoc gate would say. If you
# change one, change the other.
SIMILARITY_THRESHOLD = 0.6

# Topic-funnel window the prompt-time ledger uses (same default).
WINDOW_DAYS = 30


# --- Cheap text helpers (ported, not imported, so this file is self-contained
#     and the MCP server doesn't pull in the full invent module). ------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _normalize_query(q: str) -> str:
    """Strip per-cycle operators so since:/min_faves:/lang: collapse to one core.
    Mirrors invent_topics.normalize_query."""
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


# --- MCP server --------------------------------------------------------------

mcp = FastMCP("invent-tools")


# === TOPIC tools ===========================================================

@mcp.tool()
def search_topics(project: str, q: str = "", limit: int = 200) -> dict:
    """Search the FULL active topic universe for a project.

    Use this BEFORE proposing a new topic to verify nothing similar already
    exists. Returns matching active topics for the project. If `q` is empty,
    returns the full list (capped at `limit`). If `q` is a substring, only
    topics containing it (case-insensitive) are returned.

    Args:
        project: project name (config.json casing, e.g. 'fazm')
        q: substring filter, case-insensitive. Empty means "all".
        limit: cap on returned rows (default 200, max 1000)

    Returns: { "count": int, "topics": [{"topic": str, "source": str,
                "status": str, "created_at": str}, ...] }
    """
    limit = max(1, min(int(limit or 200), 1000))
    try:
        resp = api_get("/api/v1/project-search-topics",
                       {"project": project, "status": "active"})
    except SystemExit as e:
        return {"error": f"api_get failed: {e}"}
    rows = ((resp or {}).get("data") or {}).get("topics") or []
    q_norm = (q or "").strip().lower()
    out = []
    for r in rows:
        topic = (r.get("topic") or "").strip()
        if not topic:
            continue
        if q_norm and q_norm not in topic.lower():
            continue
        out.append({
            "topic": topic,
            "source": r.get("source"),
            "status": r.get("status"),
            "created_at": r.get("created_at"),
        })
        if len(out) >= limit:
            break
    return {"count": len(out), "topics": out}


@mcp.tool()
def get_topic_stats(project: str, topic: str) -> dict:
    """Read the topic-funnel row for one topic.

    Use this when a topic from search_topics looks adjacent to what you want
    to propose — read its performance to decide: STRONG/DECENT (good, propose
    an ADJACENT angle), WEAK (avoid this neighborhood), DUD (no Twitter
    supply, don't paraphrase).

    Args:
        project: project name
        topic: exact topic string (case-insensitive match against the funnel)

    Returns: { "found": bool, "stats": {...funnel row...} or null }
        Funnel fields include: attempts_n, tweets_found_total, candidates_n,
        posted_n, likes_total, clicks_total, views_total, clicks_per_post,
        verdict ('strong'|'decent'|'weak'|'dud'|'untried').
    """
    try:
        resp = api_get("/api/v1/topic-funnel",
                       {"project": project, "window_days": str(WINDOW_DAYS),
                        "platform": "twitter"})
    except SystemExit as e:
        return {"error": f"api_get failed: {e}"}
    rows = ((resp or {}).get("data") or {}).get("rows") or []
    needle = (topic or "").strip().lower()
    for r in rows:
        if (r.get("search_topic") or "").strip().lower() == needle:
            return {"found": True, "stats": r}
    return {"found": False, "stats": None}


@mcp.tool()
def submit_topic(project: str, topic: str, rationale: str = "") -> dict:
    """Submit a new topic for the project. Runs Jaccard dedup against the
    full active universe FIRST — if it's a near-dupe (sim >= 0.6) of any
    existing topic, the submission is REJECTED and the offending neighbor +
    similarity score are returned. React by proposing a different angle.

    On success, the topic is written to project_search_topics with
    source='invented', status='active'.

    Args:
        project: project name
        topic: the topic phrase, 2-6 words, lowercase, no Twitter operators
        rationale: ≤ 30 words explaining the gap this fills

    Returns: { "ok": bool, "topic": str, "neighbor": str|None,
               "similarity": float, "error": str|None }
    """
    topic_norm = (topic or "").strip().lower()
    if not topic_norm:
        return {"ok": False, "error": "topic is empty"}

    # Fetch the universe and run Jaccard dedup ON THE SERVER so Claude sees
    # the verdict as a tool-call result rather than dying outside the session.
    try:
        resp = api_get("/api/v1/project-search-topics",
                       {"project": project, "status": "active"})
    except SystemExit as e:
        return {"ok": False, "error": f"universe fetch failed: {e}"}
    universe = [(r.get("topic") or "").strip().lower()
                for r in ((resp or {}).get("data") or {}).get("topics") or []
                if r.get("topic")]
    universe_set = set(universe)

    # Exact-match dupe → reject immediately.
    if topic_norm in universe_set:
        return {"ok": False, "topic": topic_norm,
                "neighbor": topic_norm, "similarity": 1.0,
                "error": "exact_dupe"}

    # Near-dupe via Jaccard against every universe entry.
    best_neighbor: Optional[str] = None
    best_sim = 0.0
    for u in universe:
        s = _jaccard(topic_norm, u)
        if s > best_sim:
            best_sim = s
            best_neighbor = u
    if best_neighbor is not None and best_sim >= SIMILARITY_THRESHOLD:
        return {"ok": False, "topic": topic_norm,
                "neighbor": best_neighbor, "similarity": round(best_sim, 3),
                "error": "near_dupe"}

    # Honor invent_topics.py --dry-run. The parent process exports
    # INVENT_DRY_RUN=1 before spawning claude -p, which inherits into this
    # MCP server. We still want the dedup verdict and the "ok" path to fire
    # so Claude's session flow is identical to prod — we just skip the POST.
    if os.environ.get("INVENT_DRY_RUN") == "1":
        return {"ok": True, "topic": topic_norm,
                "neighbor": best_neighbor or "",
                "similarity": round(best_sim, 3),
                "dry_run": True}

    # Non-dupe → commit via API.
    try:
        api_post("/api/v1/project-search-topics", body={
            "project": project,
            "topic": topic_norm,
            "source": "invented",
            "status": "active",
            "notes": (rationale or "")[:512] or None,
        })
    except SystemExit as e:
        return {"ok": False, "error": f"commit failed: {e}"}
    return {"ok": True, "topic": topic_norm,
            "neighbor": best_neighbor or "",
            "similarity": round(best_sim, 3)}


# === QUERY tools ===========================================================

@mcp.tool()
def search_queries(project: str, q: str = "", limit: int = 200) -> dict:
    """Search the FULL distinct-query history for a project (every query ever
    drafted, cycle or invent). Use BEFORE drafting a new query to confirm
    it's not a re-phrasing of one already tried.

    Args:
        project: project name
        q: substring filter, case-insensitive
        limit: cap on returned rows (default 200, max 1000)

    Returns: { "count": int, "queries": [{"query": str, "core": str}, ...] }
        `core` is the normalized form (operators stripped) for dedup compares.
    """
    limit = max(1, min(int(limit or 200), 1000))
    # Request the full distinct set from the route (its own 5000 cap is fine)
    # so the substring filter runs against ALL queries, not just the first
    # alphabetical slice. Otherwise queries starting with 'w' get cut off
    # when limit*5 = 50 and the project has hundreds of cores.
    try:
        resp = api_get("/api/v1/twitter-search-attempts/distinct-queries",
                       {"project": project, "limit": "5000"})
    except SystemExit as e:
        return {"error": f"api_get failed: {e}"}
    queries = ((resp or {}).get("data") or {}).get("queries") or []
    q_norm = (q or "").strip().lower()
    out = []
    for raw in queries:
        if q_norm and q_norm not in raw.lower():
            continue
        out.append({"query": raw, "core": _normalize_query(raw)})
        if len(out) >= limit:
            break
    return {"count": len(out), "queries": out}


@mcp.tool()
def get_query_stats(project: str, query: str) -> dict:
    """Read performance stats for one query string.

    Looks across BOTH the cycle's posted-engagement bank (top-queries: posts,
    likes, clicks, virality) AND the invent supply test (invented-queries:
    fresh tweets surfaced, even if never posted). Empty fields mean "this
    query never appeared in that lane".

    Args:
        project: project name
        query: exact query string (matched case-insensitively)

    Returns: { "found": bool,
               "cycle": {posts, likes, clicks, virality, ...} | None,
               "invent": {supply, attempts} | None }
    """
    needle = (query or "").strip().lower()
    if not needle:
        return {"found": False, "error": "query is empty"}

    # Cycle side: top-queries returns the posted-engagement winners.
    cycle_row = None
    try:
        resp = api_get("/api/v1/twitter-search-attempts/top-queries",
                       {"project": project, "limit": "500"})
        for r in ((resp or {}).get("data") or {}).get("rows") or []:
            if (r.get("query") or "").strip().lower() == needle:
                cycle_row = r
                break
    except SystemExit as e:
        cycle_row = {"error": f"top-queries fetch failed: {e}"}

    # Invent side: invented-queries returns supply-test winners.
    invent_row = None
    try:
        resp = api_get("/api/v1/twitter-search-attempts/invented-queries",
                       {"project": project, "min_supply": "0", "limit": "500"})
        for r in ((resp or {}).get("data") or {}).get("queries") or []:
            if (r.get("query") or "").strip().lower() == needle:
                invent_row = {"supply": r.get("supply", 0),
                              "attempts": r.get("attempts", 0)}
                break
    except SystemExit as e:
        invent_row = {"error": f"invented-queries fetch failed: {e}"}

    return {
        "found": cycle_row is not None or invent_row is not None,
        "cycle": cycle_row,
        "invent": invent_row,
    }


# --- entrypoint -------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
