#!/usr/bin/env python3
"""project_excludes.py

Self-improving per-project exclusion list. Claude proposes specific keywords
during Phase 2b-prep when it rejects an off-topic candidate; those keywords
get appended as `-term` to all future search queries for that project after
they clear an activation gate (>=2 distinct batches).

CLI usage
---------
    # List active excludes for a project (JSON to stdout):
    python3 scripts/project_excludes.py active --platform twitter --project Vipassana

    # Propose a new exclude (used by log_twitter_skips.py):
    python3 scripts/project_excludes.py propose \
        --platform twitter --project Vipassana --term cricket \
        --candidate-id 10196 --batch-id twcycle-20260508-163303 \
        --reason 'cricket franchise Sanjiv Goenka, not S.N. Goenka'

    # Decay: prune terms unused in 60 days with <3 batches.
    python3 scripts/project_excludes.py decay [--days 60]

Module API
----------
    from project_excludes import active_excludes, propose, decay

Activation gate
---------------
A term is APPLIED to live queries only when array_length(batch_ids,1) >= 2,
so one false-rejection can't mute the searches. The proposal IS recorded
on first emission so we can audit "Claude proposed this once but never again".

False-negative guards (enforced at propose time, NOT during query rendering):
  - term must be >=3 chars, ascii-letters/digits/hyphen, single token
  - term must NOT match (case-insensitive) any token of the project's
    `search_topics` list in config.json (the reserved core list). The check
    splits search_topics phrases on whitespace AND the OR/AND/parentheses
    chars, so 'vipassana' is reserved even when search_topics has
    '"vipassana" OR "Goenka"'.
  - term is normalized: lowercase, stripped of surrounding quotes/whitespace.

Schema
------
project_search_excludes (
    platform          TEXT,
    project           TEXT,
    term              TEXT,
    proposals         INTEGER,        -- count, includes duplicates from same batch
    batch_ids         TEXT[],         -- DISTINCT batches that proposed it
    candidate_ids     INTEGER[],      -- every candidate (kept for audit)
    sample_reason     TEXT,           -- first reason recorded, for debugging
    first_proposed_at TIMESTAMPTZ,
    last_proposed_at  TIMESTAMPTZ,
    last_used_at      TIMESTAMPTZ,    -- bumped by mark_used() when appended to a query
    PRIMARY KEY (platform, project, term)
);
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


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

    - Bare "cricket"               -> ("bare", "cricket")           [twitter form]
    - "subreddit:bestofredditorupdates" -> ("subreddit", "bestofredditorupdates")
    - "keyword:powerpoint"         -> ("keyword", "powerpoint")
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
    """Return a normalized term, or None if invalid.

    Accepts:
      - bare-keyword form (twitter legacy): "cricket"
      - typed reddit form: "subreddit:bestofredditorupdates" or "keyword:powerpoint"

    Normalizes by lower-casing and stripping surrounding quotes/whitespace.
    Empty / too-short / wrong-shape inputs return None.
    """
    if not isinstance(term, str):
        return None
    t = term.strip().lower().strip("\"'")
    if len(t) < TERM_MIN_LEN:
        return None
    # Try typed first so "subreddit:foo" doesn't get mis-rejected by TERM_RE.
    if TYPED_TERM_RE.match(t):
        return t
    if TERM_RE.match(t):
        return t
    return None


def _kind_allowed_for_platform(kind, platform):
    """Gate which term kinds a given platform may write/read.

    Prevents twitter from accidentally getting a `subreddit:` row and reddit
    from getting a bare keyword that might accidentally mute a core seed.
    """
    if not kind:
        return False
    allowed = ALLOWED_KINDS.get(platform)
    if not allowed:
        return False
    return kind in allowed


def active_excludes(platform, project):
    """Return the list of currently-active exclude terms for (platform, project).

    Only terms that have cleared the activation gate (>=ACTIVATION_BATCH_FLOOR
    distinct proposing batches) are returned. Order: longest-first so when
    the query drafter appends them, more-specific terms win lex-sort tooltips.
    """
    conn = dbmod.get_conn()
    try:
        rows = conn.execute(
            """
            SELECT term
            FROM project_search_excludes
            WHERE platform=%s
              AND project=%s
              AND COALESCE(array_length(batch_ids, 1), 0) >= %s
            ORDER BY LENGTH(term) DESC, term ASC
            """,
            [platform, project, ACTIVATION_BATCH_FLOOR],
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def active_excludes_by_kind(platform, project):
    """Same as active_excludes() but split by kind for reddit callers.

    Returns a dict {"subreddit": [...], "keyword": [...], "bare": [...]}.
    Empty lists for absent kinds. Order within each list is longest-first (the
    same lex order as active_excludes() — keeps audit output stable).
    """
    terms = active_excludes(platform, project)
    out = {"subreddit": [], "keyword": [], "bare": []}
    for t in terms:
        kind, value = parse_term(t)
        if kind in out and value:
            out[kind].append(value)
    return out


def propose(platform, project, term, candidate_id=None, batch_id=None, reason=None):
    """UPSERT a single proposed exclude. Returns dict with the outcome.

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
        # e.g. trying to write a bare "anki" to reddit, or "subreddit:foo" to
        # twitter. Reject so platforms can't cross-contaminate.
        return {"ok": False, "term": norm, "action": "rejected_invalid", "active": False}

    # Reserved-keyword check only applies to bare + typed-keyword terms.
    # "subreddit:foo" never mutes a search topic since reddit search treats
    # the sub name as a separate scope from the query terms.
    if kind in ("bare", "keyword"):
        reserved = _load_reserved_terms_for_project(project)
        check_val = value if kind == "keyword" else norm
        if check_val in reserved:
            return {"ok": False, "term": norm, "action": "rejected_reserved", "active": False}

    conn = dbmod.get_conn()
    try:
        # Read current row (if any) so we can decide whether this batch_id is new.
        existing = conn.execute(
            "SELECT batch_ids, candidate_ids FROM project_search_excludes "
            "WHERE platform=%s AND project=%s AND term=%s",
            [platform, project, norm],
        ).fetchone()

        if existing is None:
            cur = conn.execute(
                """
                INSERT INTO project_search_excludes
                  (platform, project, term, proposals,
                   batch_ids, candidate_ids, sample_reason)
                VALUES (%s, %s, %s, 1, %s, %s, %s)
                ON CONFLICT (platform, project, term) DO NOTHING
                """,
                [
                    platform, project, norm,
                    [batch_id] if batch_id else [],
                    [candidate_id] if candidate_id is not None else [],
                    (reason or "")[:500] or None,
                ],
            )
            conn.commit()
            action = "inserted"
            new_batches = 1 if batch_id else 0
        else:
            current_batches, current_cands = existing
            current_batches = list(current_batches or [])
            current_cands = list(current_cands or [])

            new_batch = bool(batch_id and batch_id not in current_batches)

            if new_batch:
                action = "bumped"
                conn.execute(
                    """
                    UPDATE project_search_excludes
                       SET proposals     = proposals + 1,
                           batch_ids     = array_append(batch_ids, %s),
                           candidate_ids = CASE WHEN %s IS NULL THEN candidate_ids
                                                ELSE array_append(candidate_ids, %s) END,
                           last_proposed_at = NOW()
                     WHERE platform=%s AND project=%s AND term=%s
                    """,
                    [batch_id, candidate_id, candidate_id, platform, project, norm],
                )
            else:
                action = "duplicate_batch"
                # Same batch re-proposing: bump count + append candidate, leave batch_ids alone.
                conn.execute(
                    """
                    UPDATE project_search_excludes
                       SET proposals     = proposals + 1,
                           candidate_ids = CASE WHEN %s IS NULL THEN candidate_ids
                                                ELSE array_append(candidate_ids, %s) END,
                           last_proposed_at = NOW()
                     WHERE platform=%s AND project=%s AND term=%s
                    """,
                    [candidate_id, candidate_id, platform, project, norm],
                )
            conn.commit()
            new_batches = len(current_batches) + (1 if new_batch else 0)

        active = new_batches >= ACTIVATION_BATCH_FLOOR
        return {"ok": True, "term": norm, "action": action, "active": active}
    finally:
        conn.close()


def mark_used(platform, project, terms):
    """Stamp last_used_at for each term we just appended to a query."""
    if not terms:
        return 0
    conn = dbmod.get_conn()
    try:
        cur = conn.execute(
            """
            UPDATE project_search_excludes
               SET last_used_at = NOW()
             WHERE platform=%s AND project=%s AND term = ANY(%s)
            """,
            [platform, project, list(terms)],
        )
        conn.commit()
        return getattr(cur, "rowcount", 0) or 0
    finally:
        conn.close()


def decay(days=DECAY_DAYS_DEFAULT, dry_run=False):
    """Prune terms with <3 distinct batches that haven't been used in `days`."""
    conn = dbmod.get_conn()
    try:
        if dry_run:
            rows = conn.execute(
                """
                SELECT platform, project, term,
                       COALESCE(array_length(batch_ids,1),0),
                       last_used_at
                FROM project_search_excludes
                WHERE COALESCE(array_length(batch_ids,1),0) < 3
                  AND (last_used_at IS NULL OR last_used_at < NOW() - (%s || ' days')::interval)
                  AND last_proposed_at < NOW() - (%s || ' days')::interval
                ORDER BY last_proposed_at ASC
                """,
                [str(days), str(days)],
            ).fetchall()
            return [
                {"platform": r[0], "project": r[1], "term": r[2],
                 "batches": r[3], "last_used_at": str(r[4]) if r[4] else None}
                for r in rows
            ]
        cur = conn.execute(
            """
            DELETE FROM project_search_excludes
            WHERE COALESCE(array_length(batch_ids,1),0) < 3
              AND (last_used_at IS NULL OR last_used_at < NOW() - (%s || ' days')::interval)
              AND last_proposed_at < NOW() - (%s || ' days')::interval
            """,
            [str(days), str(days)],
        )
        conn.commit()
        return getattr(cur, "rowcount", 0) or 0
    finally:
        conn.close()


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
