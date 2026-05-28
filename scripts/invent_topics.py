#!/usr/bin/env python3
"""Standalone topic-invention job — runs OUTSIDE the post-comments cycle.

Architectural split (2026-05-28): in-cycle EXPLORE_INVENT was removed
from pick_search_topic.py. Topic invention is now a separate, deliberate
background job:

  - Picks ONE project per run using the same `pick_projects()` weighting
    the cycle uses (inverse-recent-share, dampens active projects).
  - Loads that project's per-topic ledger
    (~/social-autoposter/state/topic_ledger.json).
  - Asks Claude to propose 3-5 NEW search_topic candidates given the
    project's description, the existing universe, the strong/decent
    performers, the duds, and the untried tail.
  - For each proposal, computes the closest existing neighbor in the
    project's universe via token-Jaccard similarity (cheap, no
    embeddings needed at our scale).
  - Drops proposals that are exact-match dupes or near-dupes
    (Jaccard >= SIMILARITY_THRESHOLD against any existing topic).
  - POSTs survivors to /api/v1/project-search-topics with
    source='invented', status='active'.
  - Writes an audit row to ~/social-autoposter/state/invented_topics_audit.jsonl
    (one JSON line per invocation) capturing project, proposals,
    rejections, commits — so we can review invention quality offline.

Cadence: hourly via launchd com.m13v.social-invent-topics.
Project budget: one per run (n=1). Knob is PROJECTS_PER_RUN below.

CLI:
    python3 scripts/invent_topics.py                       # pick a project, invent, commit
    python3 scripts/invent_topics.py --project studyly     # force a specific project
    python3 scripts/invent_topics.py --dry-run             # print plan, do not commit
    python3 scripts/invent_topics.py --proposals 5         # ask Claude for N proposals
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http_api import api_get, api_post  # noqa: E402
from pick_project import load_config, pick_projects  # noqa: E402


LEDGER_PATH = Path(os.path.expanduser(
    "~/social-autoposter/state/topic_ledger.json"
))
AUDIT_PATH = Path(os.path.expanduser(
    "~/social-autoposter/state/invented_topics_audit.jsonl"
))
PROJECTS_PER_RUN = 1
DEFAULT_PROPOSALS = 4
SIMILARITY_THRESHOLD = 0.6  # Jaccard threshold above which we reject as near-dupe
WINDOW_DAYS = 30  # ledger window the picker reads from


# --- Tokenization for cheap similarity --------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Lowercased word-token set used for Jaccard similarity.

    Strips punctuation and case. Stopword removal is intentionally
    minimal — for short topic strings even small filler words carry
    enough signal to differentiate genuine paraphrases.
    """
    return set(_TOKEN_RE.findall((text or "").lower()))


def _jaccard(a: str, b: str) -> float:
    """Token Jaccard similarity in [0, 1]. Empty inputs return 0.

    Cheap, deterministic, no embedding cost. Good enough at our scale
    (<200 topics per project) to catch obvious paraphrases like
    'voice coding agent' vs 'voice AI coding agent' without false
    positives across genuinely different concepts.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union)


# --- Ledger loading ---------------------------------------------------------

def load_ledger() -> list[dict]:
    """Read the materialized ledger written by scripts/topic_ledger.py.

    Hard-fails if the file is missing — readers must NOT silently
    degrade to candidate-only signal (that's the bug the ledger
    exists to fix). The cron schedule guarantees the file is at most
    15 min stale; staler than that means the ledger job has broken
    and we'd rather abort than make uninformed inventions.
    """
    if not LEDGER_PATH.exists():
        raise SystemExit(
            f"topic_ledger.json missing at {LEDGER_PATH}. "
            f"Run scripts/topic_ledger.py first."
        )
    with open(LEDGER_PATH) as fh:
        data = json.load(fh)
    rows = data.get("rows") or []
    return rows


def project_topics(ledger: list[dict], project_name: str) -> list[dict]:
    """Slice the ledger to one project, sorted strong -> weak."""
    rows = [r for r in ledger if r.get("project") == project_name]
    verdict_rank = {"strong": 0, "decent": 1, "weak": 2, "untried": 3, "dud": 4}
    rows.sort(key=lambda r: (
        verdict_rank.get(r.get("verdict", "untried"), 5),
        -(r.get("clicks_per_post") or 0),
        -(r.get("posted_n") or 0),
    ))
    return rows


def project_universe_strings(project_name: str) -> set[str]:
    """Full active universe for the project from project_search_topics.

    Read directly from the API so we have the freshest read at invent
    time — the ledger uses a 30d window but the universe is the
    authoritative dedupe target. Lowercased for case-insensitive
    matching against proposals.
    """
    try:
        resp = api_get(
            "/api/v1/project-search-topics",
            query={"project": project_name, "status": "active"},
        )
    except Exception as exc:
        raise SystemExit(
            f"could not fetch active universe for project={project_name!r}: {exc}"
        ) from exc
    rows = ((resp or {}).get("data") or {}).get("topics") or []
    return {(r.get("topic") or "").strip().lower() for r in rows if r.get("topic")}


# --- Prompt building --------------------------------------------------------

def _format_topic_table(rows: list[dict], max_per_bucket: int = 12) -> str:
    """Compact markdown table of topics grouped by verdict.

    Caps each bucket at max_per_bucket so the prompt stays bounded
    even when the project has hundreds of untried seeds. The visible
    slice is intentionally sorted by clicks_per_post DESC within each
    bucket so the model sees what's actually converting.
    """
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(r.get("verdict", "untried"), []).append(r)

    parts: list[str] = []
    for verdict in ("strong", "decent", "weak", "dud", "untried"):
        bucket = buckets.get(verdict, [])
        if not bucket:
            continue
        parts.append(f"\n### {verdict.upper()} ({len(bucket)} total, showing top {min(len(bucket), max_per_bucket)})")
        for r in bucket[:max_per_bucket]:
            cpp = r.get("clicks_per_post")
            cpp_str = f"{cpp:.2f}" if cpp is not None else "—"
            parts.append(
                f"- **{r['search_topic']}** "
                f"(attempts {r['attempts_n']}, "
                f"candidates {r['candidates_n']}, "
                f"posted {r['posted_n']}, "
                f"clicks/post {cpp_str}, "
                f"supply {r['tweets_found_total']})"
            )
        if len(bucket) > max_per_bucket:
            parts.append(f"  …and {len(bucket) - max_per_bucket} more in this bucket.")
    return "\n".join(parts)


def build_prompt(project: dict, topics: list[dict], n_proposals: int) -> str:
    """Assemble the single Claude prompt for the proposal step."""
    name = project.get("name", "")
    description = project.get("description", "")
    voice = project.get("voice_relationship", "")

    table = _format_topic_table(topics)

    return f"""You are inventing new Twitter search_topic seeds for project **{name}**.

A topic is a short concept phrase (2-6 words typically) that we use to draft Twitter search queries. Good topics: surface fresh, on-topic threads where our reply has product fit. Bad topics: too generic (returns noise), too narrow (zero supply), or too similar to an existing topic that already covers it.

## Project context

- Name: {name}
- Description: {description}
- Voice: {voice}

## Existing topic ledger (last 30 days)

The full universe currently has {len(topics)} topics in five performance buckets.

{table}

## Your task

Propose **exactly {n_proposals}** NEW topic phrases to add to the universe. Each must:

1. Be in the project's domain (see description above)
2. NOT be an exact match or near-paraphrase of any existing topic in the universe
3. Probe a GAP — fill an angle the current list doesn't cover. Examples of good gaps: adjacent verticals, longer-tail phrases, fresh angles on the value prop, specific competitor names not yet in the list, timely/seasonal angles
4. Be plausible as something real users tweet about (i.e. would yield results in Twitter advanced search)

## How to use the ledger to inform your inventions

- **STRONG topics** (clicks_per_post >= 1.0): the audience around these is converting. Propose ADJACENT angles in the same neighborhood, not paraphrases.
- **DECENT topics** (clicks_per_post >= 0.3): also good signal. Same as strong, just less peak.
- **WEAK topics** (posted but few/no clicks): audience doesn't convert. AVOID this neighborhood; if you must propose nearby, change the angle entirely.
- **DUD topics** (>=3 attempts, zero candidates): no Twitter supply. DON'T propose paraphrases of these — they fail at the source level, not the framing.
- **UNTRIED topics** (zero attempts in 30d): may already cover what you'd invent. Read these carefully before proposing.

## Output format

Return STRICT JSON only, no prose before or after:

```json
{{
  "proposals": [
    {{
      "topic": "the new topic phrase, lowercase, 2-6 words",
      "rationale": "one sentence explaining which gap this fills and why it's distinct from existing universe topics"
    }},
    ... exactly {n_proposals} entries ...
  ]
}}
```

Strict requirements:
- Lowercase topic strings (the picker normalizes anyway, but be consistent)
- No quotes inside topic strings
- The topic field MUST be a phrase, NOT a full Twitter query (no `min_faves:`, no `-filter:replies`, no `since:` operators — those are added at query-draft time downstream)
- Each rationale ≤ 30 words"""


# --- Claude invocation ------------------------------------------------------

def call_claude(prompt: str, timeout_sec: int = 300) -> str:
    """Run a single `claude -p` invocation and return its text output.

    Inherits the global model from ~/.claude/settings.json per the
    project's "single source of truth" convention (do NOT hardcode
    --model here). The CLAUDE_MODEL env var, if set, is forwarded.
    """
    cmd = ["claude", "-p", "--output-format", "json"]
    model = os.environ.get("CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"claude -p exited {proc.returncode}: {proc.stderr[:500]}"
        )
    # claude -p --output-format json wraps the model's text in
    # {"result": "...", ...}. Extract the result string.
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"could not parse claude envelope: {exc}\nstdout head: {proc.stdout[:500]}"
        ) from exc
    return envelope.get("result") or ""


# --- Output parsing ---------------------------------------------------------

def extract_proposals(claude_text: str) -> list[dict]:
    """Pull the proposals[] array out of Claude's output.

    Defensive: handles plain JSON, JSON in fenced code blocks, and the
    occasional preamble. Returns [] on any parse failure; the caller
    audit-logs the raw text so we can debug offline.
    """
    text = (claude_text or "").strip()
    if not text:
        return []
    # Try fenced JSON first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = m.group(1) if m else None
    if candidate is None:
        # Try the first balanced top-level {...} block
        start = text.find("{")
        if start >= 0:
            candidate = text[start:]
    if not candidate:
        return []
    try:
        env = json.loads(candidate)
    except json.JSONDecodeError:
        # Trailing prose can break json.loads; try to find the matching brace
        try:
            depth = 0
            for i, ch in enumerate(candidate):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        env = json.loads(candidate[: i + 1])
                        break
            else:
                return []
        except Exception:
            return []
    props = env.get("proposals") or []
    cleaned: list[dict] = []
    for p in props:
        if not isinstance(p, dict):
            continue
        topic = (p.get("topic") or "").strip().lower()
        rationale = (p.get("rationale") or "").strip()
        if not topic:
            continue
        cleaned.append({"topic": topic, "rationale": rationale})
    return cleaned


# --- Validation -------------------------------------------------------------

def find_closest_neighbor(
    proposal: str,
    universe: set[str],
) -> tuple[str | None, float]:
    """Return the closest universe topic by Jaccard similarity.

    Used both to reject near-dupes (Jaccard >= SIMILARITY_THRESHOLD)
    and to attach context to each proposal in the audit log.
    """
    best_topic: str | None = None
    best_sim = 0.0
    for u in universe:
        sim = _jaccard(proposal, u)
        if sim > best_sim:
            best_sim = sim
            best_topic = u
    return best_topic, best_sim


def validate_proposals(
    proposals: list[dict],
    universe: set[str],
) -> tuple[list[dict], list[dict]]:
    """Split proposals into (committed_ok, rejected) by dedupe rule.

    A proposal is rejected when:
      - Its lowercased topic exactly matches an existing universe entry
      - Its highest Jaccard similarity to any universe entry meets or
        exceeds SIMILARITY_THRESHOLD (near-dupe)

    The reason for rejection plus the offending neighbor is attached
    to each rejected entry for audit-logging.
    """
    committed: list[dict] = []
    rejected: list[dict] = []
    # Build a working set of "already committed this run" so two
    # near-duplicate proposals in the same batch don't both land.
    working_universe = set(universe)

    for prop in proposals:
        topic = prop["topic"]
        if topic in working_universe:
            rejected.append({
                **prop,
                "reject_reason": "exact_dupe",
                "neighbor": topic,
                "similarity": 1.0,
            })
            continue
        neighbor, sim = find_closest_neighbor(topic, working_universe)
        if neighbor is not None and sim >= SIMILARITY_THRESHOLD:
            rejected.append({
                **prop,
                "reject_reason": "near_dupe",
                "neighbor": neighbor,
                "similarity": round(sim, 3),
            })
            continue
        committed.append({
            **prop,
            "neighbor": neighbor,
            "similarity": round(sim, 3) if neighbor else 0.0,
        })
        working_universe.add(topic)

    return committed, rejected


# --- Commit + audit ---------------------------------------------------------

def commit_topic(project_name: str, topic: str, dry_run: bool = False) -> bool:
    """POST a new topic to project_search_topics with source='invented'.

    Returns True on success. Idempotent in the API layer: re-POSTing
    an existing (project, topic) pair is a no-op upstream. We still
    rely on the local validation step to dedupe so the audit log is
    accurate about which proposals were genuinely new.
    """
    if dry_run:
        print(f"[dry-run] would commit project={project_name!r} topic={topic!r}",
              file=sys.stderr)
        return True
    try:
        api_post(
            "/api/v1/project-search-topics",
            body={
                "project": project_name,
                "topic": topic,
                "source": "invented",
                "status": "active",
            },
        )
        return True
    except SystemExit as exc:
        print(f"[invent_topics] commit FAILED project={project_name!r} "
              f"topic={topic!r} error={exc}", file=sys.stderr)
        return False


def write_audit(payload: dict) -> None:
    """Append one JSON line per invocation to the audit log.

    The audit row captures inputs (project, proposals, universe size)
    and outputs (committed, rejected) so invention quality is
    reviewable offline without re-running. Atomic append; file
    grows monotonically — no rotation yet (cheap to add later).
    """
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_PATH, "a") as fh:
        fh.write(json.dumps(payload, separators=(",", ":")) + "\n")


# --- Main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default=None,
                    help="Force a specific project (skips pick_projects)")
    ap.add_argument("--proposals", type=int, default=DEFAULT_PROPOSALS,
                    help="How many candidate topics to ask Claude for")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan; do not commit to project_search_topics")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS,
                    help="Ledger window (must match topic_ledger.py)")
    args = ap.parse_args()

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.time()

    # --- Pick a project (same code path as the cycle) ---
    config = load_config()
    if args.project:
        forced = None
        for p in config.get("projects", []):
            if p.get("name", "").lower() == args.project.lower():
                forced = p
                break
        if not forced:
            raise SystemExit(f"unknown project {args.project!r}")
        project = forced
        pick_method = "forced"
    else:
        picks = pick_projects(config, platform="twitter", n=PROJECTS_PER_RUN)
        if not picks:
            raise SystemExit("pick_projects returned no eligible project")
        project = picks[0]
        pick_method = "weighted"

    project_name = project.get("name", "")
    print(f"[invent_topics] project={project_name!r} (pick_method={pick_method})",
          file=sys.stderr)

    # --- Load the ledger + slice to this project ---
    ledger = load_ledger()
    topics_for_project = project_topics(ledger, project_name)
    print(f"[invent_topics] ledger rows for {project_name}: {len(topics_for_project)}",
          file=sys.stderr)

    # --- Read fresh universe for dedupe ---
    universe = project_universe_strings(project_name)
    print(f"[invent_topics] active universe size: {len(universe)}",
          file=sys.stderr)

    # --- Build prompt + call Claude ---
    prompt = build_prompt(project, topics_for_project, args.proposals)
    raw = call_claude(prompt)
    proposals = extract_proposals(raw)
    print(f"[invent_topics] proposals parsed: {len(proposals)}",
          file=sys.stderr)
    for p in proposals:
        print(f"  proposal: {p['topic']!r} — {p['rationale'][:80]}",
              file=sys.stderr)

    # --- Validate against the universe ---
    committed_plan, rejected = validate_proposals(proposals, universe)
    print(f"[invent_topics] valid: {len(committed_plan)} | rejected: {len(rejected)}",
          file=sys.stderr)
    for r in rejected:
        print(f"  reject ({r['reject_reason']} sim={r['similarity']}): "
              f"{r['topic']!r} ~ {r['neighbor']!r}",
              file=sys.stderr)

    # --- Commit survivors ---
    committed_actually: list[dict] = []
    for c in committed_plan:
        ok = commit_topic(project_name, c["topic"], dry_run=args.dry_run)
        if ok:
            committed_actually.append({**c, "committed": True})
            print(f"  committed: {c['topic']!r} (neighbor={c['neighbor']!r}, "
                  f"sim={c['similarity']})", file=sys.stderr)
        else:
            committed_actually.append({**c, "committed": False})

    # --- Audit row ---
    elapsed = round(time.time() - t0, 2)
    audit_row = {
        "ts": started_at,
        "elapsed_sec": elapsed,
        "project": project_name,
        "pick_method": pick_method,
        "ledger_rows_for_project": len(topics_for_project),
        "universe_size_before": len(universe),
        "proposals_requested": args.proposals,
        "proposals_parsed": len(proposals),
        "committed": committed_actually,
        "rejected": rejected,
        "dry_run": args.dry_run,
        "raw_response_head": (raw or "")[:500],
    }
    write_audit(audit_row)

    n_new = sum(1 for c in committed_actually if c.get("committed"))
    print(f"[invent_topics] done. project={project_name!r} "
          f"proposals={len(proposals)} committed={n_new} "
          f"rejected={len(rejected)} elapsed={elapsed}s",
          file=sys.stderr)


if __name__ == "__main__":
    main()
