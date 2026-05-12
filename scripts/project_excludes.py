#!/usr/bin/env python3
"""project_excludes.py — HTTP-backed exclude list (2026-05-12 migration).

Self-improving per-project exclusion list. Claude proposes specific keywords
during Phase 2b-prep when it rejects an off-topic candidate; those keywords
get appended as `-term` to all future search queries for that project after
they clear an activation gate (>=2 distinct batches).

All reads and writes now route through /api/v1/project-excludes on the
social-autoposter-website API. Direct SQL is GONE; the only Python state
this module owns is the local reserved-keyword check (which reads
config.json on disk).

CLI usage
---------
    # List active excludes for a project (JSON to stdout):
    python3 scripts/project_excludes.py active --platform twitter --project Vipassana

    # Active excludes split by kind (reddit):
    python3 scripts/project_excludes.py active-split --platform reddit --project studyly

    # Propose a new exclude (used by log_twitter_skips.py / post_reddit.py):
    python3 scripts/project_excludes.py propose \
        --platform reddit --project studyly --term subreddit:bestofredditorupdates \
        --candidate-id 10196 --batch-id rdtcycle-20260512-163303 \
        --reason 'off-topic drama subreddit'

    # Stamp last_used_at when terms get appended to a live query:
    python3 scripts/project_excludes.py mark-used \
        --platform reddit --project studyly --terms subreddit:foo subreddit:bar

    # Decay: prune terms unused in 60 days with <3 batches.
    python3 scripts/project_excludes.py decay [--days 60]

Module API
----------
    from project_excludes import active_excludes, active_excludes_by_kind, \
        propose, mark_used, decay

Activation gate
---------------
A term is APPLIED to live queries only when array_length(batch_ids,1) >= 2,
so one false-rejection can't mute the searches. The proposal IS recorded
on first emission so we can audit "Claude proposed this once but never again".

False-negative guards: structural validation (term shape, allowed kinds per
platform) is enforced server-side. Reserved-keyword check is enforced LOCALLY
before we hit the network, because config.json lives on the client.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http_api import api_get, api_post, api_delete


CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")
ACTIVATION_BATCH_FLOOR = 2          # term must appear in this many distinct batches before applying
DECAY_DAYS_DEFAULT = 60             # prune unused terms older than this with <3 distinct batches
TERM_MIN_LEN = 3
# Bare-keyword form (Twitter): "cricket", "kohli". Kept for back-compat.
TERM_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,30}$")
# Reddit-only typed form: "subreddit:bestofredditorupdates" (sub bans) or
# "keyword:foo" (explicit keyword ban). The 2026-05-11 reddit wiring writes
# subreddit: rows; keyword: is kept as a future-proof typed-keyword path so
# reddit and twitter never collide on the same row even if they share a name.
TYPED_TERM_RE = re.compile(r"^(subreddit|keyword):[a-z0-9][a-z0-9_\-]{1,40}$")

# Per-platform allowed term kinds. Twitter stays bare-keyword-only (legacy
# behavior unchanged); reddit accepts subreddit: and keyword: typed forms only,
# so an accidentally-bare term ("anki") can never silently kill a core seed.
ALLOWED_KINDS = {
    "twitter": {"bare"},
    "reddit": {"subreddit", "keyword"},
}


def parse_term(term):
    """Return (kind, value) for a normalized term.

    - Bare "cricket"                    -> ("bare", "cricket")           [twitter form]
    - "subreddit:bestofredditorupdates" -> ("subreddit", "bestofredditorupdates")
    - "keyword:powerpoint"              -> ("keyword", "powerpoint")
    Returns (None, None) for unrecognized shapes.
    """
    if not isinstance(term, str):
        return None, None
    t = term.strip().lower()
    if ":" in t:
        kind, _, val = t.partition(":")
        kind = kind.strip()
        val = val.strip()
        if kind in ("subreddit", "keyword") and val:
            return kind, val
        return None, None
    if TERM_RE.match(t):
        return "bare", t
    return None, None


def _load_reserved_terms_for_project(project_name):
    """Tokens we MUST NEVER let Claude exclude. Source: config.json search_topics for the project.

    `search_topics` entries can be Twitter-search-style strings with OR/parens/quotes;
    we split them into bare lowercase tokens so a query string like
    `"vipassana" OR "Goenka"` reserves both `vipassana` and `goenka`.
    """
    reserved = set()
    if not os.path.exists(CONFIG_PATH):
        return reserved
    try:
        cfg = json.load(open(CONFIG_PATH))
    except Exception:
        return reserved
    for p in cfg.get("projects", []):
        if p.get("name") != project_name:
            continue
        topics = p.get("search_topics") or []
        for t in topics:
            if not isinstance(t, str):
                continue
            for tok in re.split(r"[\s\(\)\"\'\|]+|\bOR\b|\bAND\b|\bNOT\b|min_faves:\d+|since:[\d\-]+|-filter:\w+", t):
                tok = tok.strip().lower()
                if tok and TERM_MIN_LEN <= len(tok) <= 32:
                    reserved.add(tok)
        # Also reserve the project name itself (case-insensitive single token).
        if isinstance(p.get("name"), str):
            reserved.add(p["name"].lower())
        break
    return reserved


def normalize_term(term):
    """Return a normalized term, or None if invalid."""
    if not isinstance(term, str):
        return None
    t = term.strip().lower().strip("\"'")
    if len(t) < TERM_MIN_LEN:
        return None
    if TYPED_TERM_RE.match(t):
        return t
    if TERM_RE.match(t):
        return t
    return None


def _kind_allowed_for_platform(kind, platform):
    """Gate which term kinds a given platform may write/read."""
    if not kind:
        return False
    allowed = ALLOWED_KINDS.get(platform)
    if not allowed:
        return False
    return kind in allowed


def active_excludes(platform, project, min_batches=ACTIVATION_BATCH_FLOOR):
    """Return the list of currently-active exclude terms for (platform, project).

    Only terms that have cleared the activation gate (>=ACTIVATION_BATCH_FLOOR
    distinct proposing batches) are returned. Order: longest-first so when
    the query drafter appends them, more-specific terms win lex-sort tooltips.
    """
    resp = api_get(
        "/api/v1/project-excludes",
        query={"platform": platform, "project": project, "min_batches": min_batches},
    )
    data = resp.get("data") if isinstance(resp, dict) else None
    if not data:
        return []
    return list(data.get("terms") or [])


def active_excludes_by_kind(platform, project):
    """Same as active_excludes() but split by kind for reddit callers."""
    terms = active_excludes(platform, project)
    out = {"subreddit": [], "keyword": [], "bare": []}
    for t in terms:
        kind, value = parse_term(t)
        if kind in out and value:
            out[kind].append(value)
    return out


def propose(platform, project, term, candidate_id=None, batch_id=None, reason=None):
    """UPSERT a single proposed exclude via HTTP.

    The reserved-keyword check runs LOCALLY (config.json is on the client).
    Structural validation (regex, platform-kind allowed) runs on both sides;
    the server is authoritative.

    outcome keys:
        ok (bool)         success
        term (str | None) normalized term (None if rejected by validation)
        action (str)      one of: 'inserted', 'bumped', 'duplicate_batch',
                          'rejected_invalid', 'rejected_reserved'
        active (bool)     whether the term is now ACTIVE (>=ACTIVATION_BATCH_FLOOR)
    """
    norm = normalize_term(term)
    if norm is None:
        return {"ok": False, "term": None, "action": "rejected_invalid", "active": False}

    kind, value = parse_term(norm)
    if not _kind_allowed_for_platform(kind, platform):
        return {"ok": False, "term": norm, "action": "rejected_invalid", "active": False}

    if kind in ("bare", "keyword"):
        reserved = _load_reserved_terms_for_project(project)
        check_val = value if kind == "keyword" else norm
        if check_val in reserved:
            return {"ok": False, "term": norm, "action": "rejected_reserved", "active": False}
        reserved_for_post = sorted(reserved)
    else:
        reserved_for_post = []

    body = {
        "platform": platform,
        "project": project,
        "term": norm,
        "reserved_terms": reserved_for_post,
    }
    if candidate_id is not None:
        body["candidate_id"] = int(candidate_id)
    if batch_id:
        body["batch_id"] = batch_id
    if reason:
        body["reason"] = reason[:500]
    resp = api_post("/api/v1/project-excludes", body)
    data = resp.get("data") if isinstance(resp, dict) else None
    if not data:
        return {"ok": False, "term": norm, "action": "rejected_invalid", "active": False}
    return {
        "ok": bool(data.get("ok")),
        "term": data.get("term") or norm,
        "action": data.get("action") or "unknown",
        "active": bool(data.get("active")),
    }


def mark_used(platform, project, terms):
    """Stamp last_used_at for each term we just appended to a query."""
    if not terms:
        return 0
    resp = api_post(
        "/api/v1/project-excludes/mark-used",
        {"platform": platform, "project": project, "terms": list(terms)},
    )
    data = resp.get("data") if isinstance(resp, dict) else None
    if not data:
        return 0
    return int(data.get("stamped") or 0)


def decay(days=DECAY_DAYS_DEFAULT, dry_run=False):
    """Prune terms with <3 distinct batches that haven't been used in `days`."""
    resp = api_delete(
        "/api/v1/project-excludes",
        query={"days": days, "dry_run": "true" if dry_run else None},
    )
    data = resp.get("data") if isinstance(resp, dict) else None
    if dry_run:
        rows = (data or {}).get("rows") or []
        return [
            {
                "platform": r.get("platform"),
                "project": r.get("project"),
                "term": r.get("term"),
                "batches": r.get("batches"),
                "last_used_at": r.get("last_used_at"),
            }
            for r in rows
        ]
    return int((data or {}).get("pruned_count") or 0)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("active", help="List active excludes for a project")
    a.add_argument("--platform", required=True)
    a.add_argument("--project", required=True)
    a.add_argument("--as-flags", action="store_true",
                   help="Print as space-joined `-term` flags instead of JSON list.")

    asplit = sub.add_parser("active-split",
                            help="Active excludes split by kind {subreddit, keyword, bare}. Reddit-friendly.")
    asplit.add_argument("--platform", required=True)
    asplit.add_argument("--project", required=True)

    p = sub.add_parser("propose", help="Propose a new exclude term")
    p.add_argument("--platform", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--term", required=True)
    p.add_argument("--candidate-id", type=int)
    p.add_argument("--batch-id")
    p.add_argument("--reason")

    m = sub.add_parser("mark-used", help="Stamp last_used_at on terms appended to a live query")
    m.add_argument("--platform", required=True)
    m.add_argument("--project", required=True)
    m.add_argument("--terms", nargs="+", required=True)

    d = sub.add_parser("decay", help="Prune unused unverified terms")
    d.add_argument("--days", type=int, default=DECAY_DAYS_DEFAULT)
    d.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.cmd == "active":
        terms = active_excludes(args.platform, args.project)
        if args.as_flags:
            sys.stdout.write(" ".join(f"-{t}" for t in terms))
            sys.stdout.write("\n")
        else:
            json.dump(terms, sys.stdout)
            sys.stdout.write("\n")
        return 0

    if args.cmd == "active-split":
        split = active_excludes_by_kind(args.platform, args.project)
        json.dump(split, sys.stdout)
        sys.stdout.write("\n")
        return 0

    if args.cmd == "propose":
        out = propose(
            args.platform, args.project, args.term,
            candidate_id=args.candidate_id,
            batch_id=args.batch_id,
            reason=args.reason,
        )
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
        return 0 if out["ok"] else 2

    if args.cmd == "mark-used":
        n = mark_used(args.platform, args.project, args.terms)
        print(f"mark_used: {n} rows stamped")
        return 0

    if args.cmd == "decay":
        if args.dry_run:
            rows = decay(days=args.days, dry_run=True)
            json.dump(rows, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            n = decay(days=args.days, dry_run=False)
            print(f"decay: {n} rows pruned (older than {args.days}d, <3 batches)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
