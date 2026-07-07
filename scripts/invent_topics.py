#!/usr/bin/env python3
"""Standalone topic-invention job — runs OUTSIDE the post-comments cycle.

Architectural split (2026-05-28): in-cycle EXPLORE_INVENT was removed
from pick_search_topic.py. Topic invention is now a separate, deliberate
background job:

  - Picks ONE project per run using the same `pick_projects()` weighting
    the cycle uses (inverse-recent-share, dampens active projects).
  - Reads that project's per-topic funnel from
    GET /api/v1/topic-funnel?project=<name> — server-side aggregation,
    no local-file state (replaces ~/social-autoposter/state/topic_ledger.json
    from earlier draft of this job, 2026-05-28).
  - Asks Claude to propose ONE new search_topic per scan slot given the
    project's description, the COMPLETE existing universe, the strong/decent
    performers, the duds, and the untried tail.
  - Dedups post-hoc via token-Jaccard against the full universe; a dupe
    re-prompts with a grown avoid-list (up to DUPE_RETRIES_PER_SLOT).
  - POSTs survivors to /api/v1/project-search-topics with
    source='invented', status='active'.
  - POSTs an audit row to /api/v1/invented-topics-audit so invention
    quality is reviewable offline (no local file).

Queue-native (2026-07-06): EVERY Claude turn is a typed job on the local
claude-queue (scripts/claude_job.py, tags invent-topic / invent-queries),
drained by the Claude Desktop scheduled-task worker — on every install,
operator Mac included. There is no `claude -p` path and no provider env
switch in this pipeline; call_claude pins S4L_CLAUDE_PROVIDER=queue into
run_claude.sh. No worker firing => the run logs and skips (exit 79 from
the provider), same failure posture as the drafting pipeline. The old
in-session invent-tools MCP mode (scripts/invent_mcp_server.py) was
deleted with this change; its full-universe visibility moved into the
prompt and its server-side dedup moved back to validate_proposals().

Cadence: invoked by the skill/run-draft-and-publish.sh kicker on every
install (state-file gate, S4L_INVENT_EVERY_HOURS). The operator-only
launchd job com.m13v.social-invent-topics is retired.
Project budget: one per run (n=1). Knob is PROJECTS_PER_RUN below.

CLI:
    python3 scripts/invent_topics.py                       # pick a project, invent, commit
    python3 scripts/invent_topics.py --project studyly     # force a specific project
    python3 scripts/invent_topics.py --dry-run             # log plan, do not commit or audit
    python3 scripts/invent_topics.py --proposals 5         # ask Claude for N proposals
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http_api import api_get, api_post  # noqa: E402
from pick_project import load_config, pick_projects  # noqa: E402


PROJECTS_PER_RUN = 1
DEFAULT_PROPOSALS = 1    # topics invented per loop iteration (one-topic-at-a-time)
SIMILARITY_THRESHOLD = 0.6  # Jaccard threshold above which we reject as near-dupe
WINDOW_DAYS = 30  # ledger window the picker reads from

# Retry-loop knobs. Each loop iteration invents ONE topic, drafts queries for
# it, supply-tests them, logs every attempt, and commits the topic. The loop
# stops as soon as a single topic clears the supply floor (sum of fresh tweets
# across its queries >= SUPPLY_FLOOR), or MAX_ATTEMPTS iterations are exhausted.
DEFAULT_TARGET = 1        # qualifying topics wanted per run (one is enough; supply is the real target)
DEFAULT_MAX_ATTEMPTS = 5  # hard cap on loop iterations per run (cost guard)

# Dupe handling is POST-HOC (validate_proposals) since the queue-native
# rewrite: a rejected near-dupe re-prompts with the grown avoid-list, at most
# this many times per scan slot before the run declares saturation. Each retry
# is a full queue job, so keep this small.
DUPE_RETRIES_PER_SLOT = 3

# Supply-test knobs (2026-05-28: invent loop now scans drafted queries before
# committing, mirroring the cycle's Phase 1 freshness gate).
QUERIES_PER_TOPIC = 5     # distinct queries drafted + scanned per invented topic
SUPPLY_FLOOR = 3          # min SUM of fresh tweets across a topic's queries to "qualify"
FRESHNESS_HOURS = 6       # freshness window each query is scanned at (matches discover)
CDP_PORT = 9555           # managed Chrome the twitter-harness drives
LOCK_TIMEOUT_SEC = 600    # how long the supply-test helper waits for twitter-browser lock

# Honor S4L_REPO_DIR (set by the MCP wrapper + the kicker on .mcpb installs)
# so the plugin's installed package resolves its own helpers; operator Macs
# fall back to the classic path.
_REPO_DIR = os.environ.get("S4L_REPO_DIR") or os.path.expanduser("~/social-autoposter")
_SUPPLY_TEST_SH = os.path.join(_REPO_DIR, "skill", "invent-supply-test.sh")
_LOG_ATTEMPTS_PY = os.path.join(_REPO_DIR, "scripts", "log_twitter_search_attempts.py")
_RUN_CLAUDE_SH = os.path.join(_REPO_DIR, "scripts", "run_claude.sh")


# --- Query normalization (copied verbatim from qualified_query_bank.normalize
#     so this module needs NO direct DB import — all reads go through the API).
#     Strips per-cycle operators so phrasings that differ only by freshness or
#     min_faves collapse to one core for dedup. -------------------------------

def normalize_query(q: str) -> str:
    """Strip per-cycle operators so two queries that differ only by
    since:/min_faves:/filter: collapse to the same core for dedup."""
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


# --- Ledger / universe loading via API --------------------------------------

def load_project_topics(project_name: str,
                        window_days: int = WINDOW_DAYS) -> list[dict]:
    """Fetch the per-topic funnel from /api/v1/topic-funnel for one project.

    Server-side aggregation (no direct DB access from this client).
    Returns rows already enriched with `verdict` and `clicks_per_post`.
    Hard-fails on network errors — invention without ledger signal
    would be uninformed and risks producing dupes-by-accident.
    """
    try:
        resp = api_get(
            "/api/v1/topic-funnel",
            query={
                "project": project_name,
                "window_days": str(window_days),
                "platform": "twitter",
            },
        )
    except Exception as exc:
        raise SystemExit(
            f"topic-funnel API failed for project={project_name!r}: {exc}"
        ) from exc
    data = (resp or {}).get("data") or {}
    rows = data.get("rows") or []
    # Server returns rows sorted by clicks_total DESC. Re-sort by verdict
    # for the prompt's table so strong/decent appear first regardless of
    # raw click counts (a strong topic with low absolute clicks is still
    # a quality signal we want to highlight).
    verdict_rank = {"strong": 0, "decent": 1, "weak": 2, "untried": 3, "dud": 4}
    rows.sort(key=lambda r: (
        verdict_rank.get(r.get("verdict", "untried"), 5),
        -(r.get("clicks_per_post") or 0),
        -(r.get("posted_n") or 0),
    ))
    return rows


def project_universe_strings(project_name: str) -> set[str]:
    """Full active universe for the project from project_search_topics.

    Read from /api/v1/project-search-topics for the freshest read at
    invent time. Lowercased for case-insensitive matching against
    proposals.
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


def _table_visible_rows(rows: list[dict], max_per_bucket: int = 12) -> list[dict]:
    """The exact rows _format_topic_table renders (top N per verdict bucket),
    so build_prompt can exclude them from the remaining-universe list."""
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(r.get("verdict", "untried"), []).append(r)
    out: list[dict] = []
    for verdict in ("strong", "decent", "weak", "dud", "untried"):
        out.extend(buckets.get(verdict, [])[:max_per_bucket])
    return out


def _format_universe_list(universe: set[str], shown_in_table: set[str]) -> str:
    """Complete plain list of every active topic NOT already detailed in the
    stats table, so the prompt carries FULL-universe visibility (this replaced
    the retired invent-tools search_topics/get_topic_stats session tools; the
    table alone truncates at 12 per bucket). Comma-separated: topics are short
    2-6 word strings, so even hundreds fit in a few KB of prompt."""
    rest = sorted(t for t in universe if t not in shown_in_table)
    if not rest:
        return ""
    return (
        f"\n## Remaining active topics not detailed above ({len(rest)} more — "
        "together with the table this is the COMPLETE universe; anything here "
        "or a paraphrase of it is a dupe)\n\n"
        + ", ".join(rest) + "\n"
    )


def build_prompt(
    project: dict,
    topics: list[dict],
    n_proposals: int,
    avoid_topics: set[str] | None = None,
    universe: set[str] | None = None,
) -> str:
    """Assemble the single text->JSON topic-invention prompt (one queue job).

    Flattened (2026-07-06, queue-native rewrite): no tools. The prompt carries
    the stats table (top 12 per bucket) PLUS the complete remaining topic-name
    list, so the model has full-universe visibility for both gap-finding and
    dupe avoidance. Dedup runs post-hoc in validate_proposals(); a rejected
    near-dupe re-prompts with the grown `avoid_topics` list.

    `avoid_topics` carries topics already proposed earlier in THIS run
    (committed or rejected). `universe` is the full working universe including
    topics minted earlier this run.
    """
    name = project.get("name", "")
    description = project.get("description", "")
    voice = project.get("voice_relationship", "")

    table = _format_topic_table(topics)
    shown_in_table = {
        (r.get("search_topic") or "").strip().lower()
        for r in _table_visible_rows(topics)
    }
    universe_block = _format_universe_list(universe or set(), shown_in_table)

    avoid_block = ""
    if avoid_topics:
        avoid_lines = "\n".join(f"- {t}" for t in sorted(avoid_topics))
        avoid_block = (
            "\n## Already proposed earlier this run — DO NOT repeat or paraphrase\n\n"
            "An earlier attempt this run already suggested the topics below "
            "(committed OR rejected as dupes). Stay clear of these:\n\n"
            f"{avoid_lines}\n"
        )

    return f"""You are inventing ONE new Twitter search_topic seed for project **{name}**.

A topic is a short concept phrase (2-6 words typically) used to draft Twitter search queries downstream. Good topics surface fresh, on-topic threads where our reply has product fit. Bad topics are too generic (noise), too narrow (zero supply), or paraphrases of existing topics.

## Project context
- Name: {name}
- Description: {description}
- Voice: {voice}

## Performance ledger (stats for top 12 per bucket; full universe has {len(topics)} topics)

{table}
{universe_block}
## Ledger bucket guide
- **STRONG** (clicks_per_post >= 1.0): audience converts. Invent ADJACENT angles, not paraphrases.
- **DECENT** (>= 0.3): solid signal. Same as strong, lower peak.
- **WEAK** (posted but few clicks): audience doesn't convert. AVOID this neighborhood.
- **DUD** (>=3 attempts, zero candidates): no Twitter supply. DON'T paraphrase — they fail at the source level.
- **UNTRIED** (no attempts in 30d): unknown. Read carefully before proposing nearby.
{avoid_block}
## Workflow (follow strictly)
1. Scan the full universe above (table + remaining-topics list) for genuinely uncovered angles.
2. For your candidate, check its nearest neighbors in the ledger: adjacent to STRONG/DECENT is good ground; adjacent to WEAK/DUD is poisoned ground; a paraphrase of ANY listed topic is a dupe and will be rejected.
3. Answer with exactly ONE proposal in the JSON envelope below. It will be dedup-checked (token-Jaccard) against the full universe after you answer, so self-check first: if you cannot phrase a real gap in genuinely different words, say so via the saturation envelope instead of forcing a paraphrase.

## Required final answer

Return STRICT JSON only, no prose around it:

```json
{{"proposals": [{{"topic": "<2-6 words, lowercase>", "rationale": "<≤30 words, why this gap>"}}]}}
```

If the universe is genuinely saturated and you cannot propose a non-dupe:

```json
{{"proposals": [], "reason": "short explanation, e.g. 'every angle adjacent to the strong topics is already covered'"}}
```

Strict topic constraints (dedup-enforced after you answer):
- Lowercase, 2-6 words
- No quotes inside topic strings
- NO Twitter operators (no `min_faves:`, `-filter:replies`, `since:` — those are added at query-draft time downstream)"""


# --- Claude invocation ------------------------------------------------------

# Path to the MCP server that exposes search/stats/submit tools for the
# in-session topic + query lookups. Kept beside this file so the launchd job
# always finds it.
_MCP_SERVER_PY = os.path.join(_REPO_DIR, "scripts", "invent_mcp_server.py")

# Tool names the topic round is allowed to call. Claude Code's --allowed-tools
# accepts the `mcp__<server-name>__<tool>` form for MCP-provided tools.
# `invent-tools` is the name we passed to FastMCP() in the server.
_TOPIC_TOOLS = [
    "mcp__invent-tools__search_topics",
    "mcp__invent-tools__get_topic_stats",
    "mcp__invent-tools__submit_topic",
]
# Query round may also probe history if it wants to (read-only).
_QUERY_TOOLS = [
    "mcp__invent-tools__search_queries",
    "mcp__invent-tools__get_query_stats",
]


def _write_mcp_config_file() -> str:
    """Write a temporary MCP config JSON pointing at our invent-tools server
    and return its path. claude -p --mcp-config <file> will spawn the server
    as a subprocess over stdio."""
    cfg = {
        "mcpServers": {
            "invent-tools": {
                "command": "/opt/homebrew/bin/python3.11",
                "args": [_MCP_SERVER_PY],
            }
        }
    }
    fd, path = tempfile.mkstemp(prefix="invent-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return path


def call_claude(prompt: str, timeout_sec: int = 300,
                allowed_tools: list[str] | None = None) -> str:
    """Run a single `claude -p` invocation and return its text output.

    When `allowed_tools` is non-empty the call goes through the invent-tools
    MCP server (search/stats/submit) and the model can call those tools
    in-session before producing its final response. The Jaccard dedup runs
    on the SERVER inside submit_topic, so a near-dupe surfaces as a
    tool-call error Claude can react to instead of a silent post-hoc kill.

    Inherits the global model from ~/.claude/settings.json per the
    project's "single source of truth" convention (do NOT hardcode
    --model here). The CLAUDE_MODEL env var, if set, is forwarded.
    """
    cmd = ["claude", "-p", "--output-format", "json"]
    model = os.environ.get("CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    # When the parent process is in dry-run mode it sets INVENT_DRY_RUN=1
    # in its own env at startup; claude -p inherits it, which propagates to
    # the MCP server, which short-circuits submit_topic without POSTing.
    mcp_cfg_path = None
    if allowed_tools:
        mcp_cfg_path = _write_mcp_config_file()
        # --strict-mcp-config: ignore any user-level MCP config so test runs
        # never pick up the operator's personal MCP servers by accident.
        # --allowed-tools: explicit allow-list so Claude can't reach for any
        # other tool (Read/Bash/etc) it might infer from its default kit.
        cmd += ["--mcp-config", mcp_cfg_path,
                "--strict-mcp-config",
                "--allowed-tools", ",".join(allowed_tools)]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
    finally:
        if mcp_cfg_path:
            try:
                os.remove(mcp_cfg_path)
            except OSError:
                pass
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


def extract_submitted_topic(claude_text: str) -> dict | None:
    """Parse Claude's final envelope from the tool-using topic session.

    Looks for the LAST JSON object in the response containing a
    `submitted_topic` field. Returns:
      - {"topic": "...", "rationale": "..."} on a successful submission
      - {"topic": None, "reason": "..."} on a saturated session
      - None if the envelope is missing or malformed (caller treats as bailout)
    """
    text = (claude_text or "").strip()
    if not text:
        return None
    # Walk all fenced JSON blocks first, then any bare {...} blocks; take the
    # LAST that contains "submitted_topic" so trailing prose from the tool
    # iteration doesn't shadow the final answer.
    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(m.group(1))
    # Fallback: scan balanced braces for any unfenced JSON
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(text[start:i + 1])
                    start = -1
    last_ok: dict | None = None
    for blob in candidates:
        try:
            env = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(env, dict) or "submitted_topic" not in env:
            continue
        topic_val = env.get("submitted_topic")
        if topic_val is None:
            last_ok = {"topic": None,
                       "reason": (env.get("reason") or "").strip()}
        else:
            topic = (str(topic_val) or "").strip().lower()
            if not topic:
                continue
            last_ok = {"topic": topic,
                       "rationale": (env.get("rationale") or "").strip()}
    return last_ok


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


# --- Commit + audit (both via API) ------------------------------------------

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


def write_audit(payload: dict, dry_run: bool = False) -> None:
    """POST one audit row to /api/v1/invented-topics-audit.

    No local file: persistence is server-side via the API. Failures
    are logged but never raise — losing one audit row is preferable
    to crashing the invocation that just successfully committed topics.
    """
    if dry_run:
        print(f"[dry-run] would write audit row (project={payload.get('project')!r})",
              file=sys.stderr)
        return
    try:
        api_post("/api/v1/invented-topics-audit", body=payload)
    except SystemExit as exc:
        print(f"[invent_topics] audit POST failed: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"[invent_topics] audit POST exception: {exc}", file=sys.stderr)


# --- Harness liveness -------------------------------------------------------

def harness_alive(port: int = CDP_PORT, timeout: float = 2.0) -> bool:
    """Cheap TCP probe of the managed Chrome's CDP port.

    The supply test is meaningless if the browser the harness drives isn't up:
    every scan would return 0 and we'd commit real topics as false 'duds'. So
    the loop checks this BEFORE spending Claude tokens on query drafting, and
    treats a mid-run drop as 'untested' (abort, retry next hour) rather than
    'zero supply'.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


# --- Query drafting (Claude) ------------------------------------------------

# Per-project ledger cache so build_query_prompt doesn't fetch top-queries +
# dud-queries + invented-queries from scratch on every topic in a single run.
_QUERY_LEDGER_CACHE: dict[str, str] = {}


def _build_query_ledger(project_name: str) -> str:
    """Fetch the per-query performance ledger for the project and format it
    as a markdown table bucketed STRONG / DECENT / WEAK / INVENTED / DUD.

    Sources:
      - /api/v1/twitter-search-attempts/top-queries  → posted-engagement winners
      - /api/v1/twitter-search-attempts/invented-queries → supply-only winners
      - /api/v1/twitter-search-attempts/dud-queries  → zero-supply queries
        (avoid paraphrasing — they fail at the source, not the framing)

    Cached per project for the lifetime of the process so multiple topics in
    one run share one fetch. Returns "" on fetch failure (best-effort
    enrichment; the prompt still works without it).
    """
    if project_name in _QUERY_LEDGER_CACHE:
        return _QUERY_LEDGER_CACHE[project_name]

    try:
        top_resp = api_get("/api/v1/twitter-search-attempts/top-queries",
                           {"project": project_name, "limit": "50"})
        invent_resp = api_get("/api/v1/twitter-search-attempts/invented-queries",
                              {"project": project_name, "min_supply": "1",
                               "limit": "30"})
        dud_resp = api_get("/api/v1/twitter-search-attempts/dud-queries",
                           {"project": project_name, "limit": "20"})
    except SystemExit as exc:
        print(f"[invent_topics] query-ledger fetch failed: {exc}",
              file=sys.stderr)
        _QUERY_LEDGER_CACHE[project_name] = ""
        return ""

    top_rows = ((top_resp or {}).get("data") or {}).get("rows") or []
    invent_rows = ((invent_resp or {}).get("data") or {}).get("queries") or []
    dud_rows = ((dud_resp or {}).get("data") or {}).get("rows") or []

    # Bucket top rows by clicks_per_post — same verdict the topic ledger uses.
    strong, decent, weak = [], [], []
    for r in top_rows:
        posts = r.get("posts") or r.get("posted_n") or 0
        clicks = r.get("clicks_total") or 0
        if posts <= 0:
            continue
        cpp = clicks / posts
        if cpp >= 1.0:
            strong.append(r)
        elif cpp >= 0.3:
            decent.append(r)
        else:
            weak.append(r)

    def _fmt_top(r: dict) -> str:
        q = (r.get("query") or "")[:110]
        return (f"- `{q}` posts {r.get('posts', r.get('posted_n', 0))}, "
                f"likes {r.get('likes_total', 0)}, "
                f"clicks {r.get('clicks_total', 0)}")

    parts: list[str] = []
    if strong:
        parts.append(f"\n### STRONG queries ({len(strong)} total, "
                     f"showing top {min(len(strong), 10)}; clicks_per_post >= 1.0)")
        for r in strong[:10]:
            parts.append(_fmt_top(r))
    if decent:
        parts.append(f"\n### DECENT queries ({len(decent)} total, "
                     f"showing top {min(len(decent), 10)}; clicks_per_post >= 0.3)")
        for r in decent[:10]:
            parts.append(_fmt_top(r))
    if weak:
        parts.append(f"\n### WEAK queries ({len(weak)} total, "
                     f"showing top {min(len(weak), 6)}; posted but low engagement)")
        for r in weak[:6]:
            parts.append(_fmt_top(r))
    if invent_rows:
        parts.append(f"\n### INVENTED queries ({len(invent_rows)} total; "
                     f"surfaced supply but no posts yet)")
        for r in invent_rows[:10]:
            q = (r.get("query") or "")[:110]
            parts.append(f"- `{q}` supply {r.get('supply', 0)}, "
                         f"attempts {r.get('attempts', 0)}")
    if dud_rows:
        parts.append(f"\n### DUD queries ({len(dud_rows)} total, "
                     f"showing top {min(len(dud_rows), 10)}) — DO NOT paraphrase")
        for r in dud_rows[:10]:
            q = (r.get("query") or "")[:110]
            parts.append(f"- `{q}` attempts {r.get('attempts', 0)}")

    out = "\n".join(parts) if parts else ""
    _QUERY_LEDGER_CACHE[project_name] = out
    return out


def build_query_prompt(
    project: dict,
    topic: str,
    n_queries: int,
    avoid_queries: set[str] | None = None,
) -> str:
    """Prompt Claude to draft N distinct X/Twitter advanced-search queries for
    one invented topic. Includes a per-query performance ledger (STRONG /
    DECENT / WEAK / INVENTED / DUD with posts/clicks/likes/supply stats) so
    the model can pattern-match against what's working for this project
    instead of drafting in the dark. `avoid_queries` carries cores already
    drafted/tried this run so a refill steers away from them."""
    name = project.get("name", "")
    description = project.get("description", "")
    excludes = project.get("excludes_for_search") or project.get("excludes") or []
    excludes_block = ""
    if isinstance(excludes, list) and excludes:
        excludes_block = (
            "\n## Mandatory exclude terms for this project\n\n"
            "Append these as `-term` to EVERY query (they filter known noise):\n"
            f"{' '.join('-' + str(e) for e in excludes)}\n"
        )

    avoid_block = ""
    if avoid_queries:
        avoid_lines = "\n".join(f"- {q}" for q in sorted(avoid_queries))
        avoid_block = (
            "\n## Already tried — do NOT repeat or trivially re-phrase these\n\n"
            f"{avoid_lines}\n"
        )

    ledger = _build_query_ledger(name)
    ledger_block = ""
    if ledger:
        ledger_block = (
            "\n## Per-query performance ledger for this project\n\n"
            "The queries below have been tried; use their performance to "
            "shape your N drafts. Mimic the operator structure of STRONG/"
            "DECENT queries when the angle fits the topic; pattern-match "
            "INVENTED queries that already surfaced supply; AVOID the "
            "phrasings in WEAK and DUD.\n"
            f"{ledger}\n"
        )

    return f"""You are drafting X (Twitter) advanced-search queries to find FRESH threads where project **{name}** could reply with product fit.

## Project
- Name: {name}
- Description: {description}

## Topic to cover
**{topic}**

Draft **exactly {n_queries}** DISTINCT search queries that probe this topic from different angles, so together they maximize the chance of surfacing fresh, on-topic tweets.
{excludes_block}{avoid_block}{ledger_block}
## Query rules
- Each query targets the topic above but varies the angle/phrasing/breadth so the {n_queries} don't overlap.
- You MAY use X operators: `min_faves:N`, `OR` (inside parentheses), quoted phrases, `-excludeterm`, `lang:en`.
- Do NOT include `since:`, `until:`, `since_time:`, or `until_time:` — the freshness window ({FRESHNESS_HOURS}h) is applied automatically downstream.
- Mix breadth: at least one broad query (few/no operators) and at least one tighter query (e.g. `min_faves:5` or a quoted phrase) so we measure supply at multiple precision levels.
- Keep each query realistic — phrasing real users would actually tweet, not keyword salad.

## Output format
Return STRICT JSON only, no prose:

```json
{{"queries": ["query one", "query two", "... exactly {n_queries} total ..."]}}
```"""


def extract_queries(claude_text: str, n_expected: int) -> list[str]:
    """Pull the queries[] array out of Claude's output (fenced or bare JSON)."""
    text = (claude_text or "").strip()
    if not text:
        return []
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = m.group(1) if m else None
    if candidate is None:
        start = text.find("{")
        candidate = text[start:] if start >= 0 else None
    if not candidate:
        return []
    env = None
    try:
        env = json.loads(candidate)
    except json.JSONDecodeError:
        # Trim trailing prose to the matching brace.
        depth = 0
        for i, ch in enumerate(candidate):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        env = json.loads(candidate[: i + 1])
                    except json.JSONDecodeError:
                        env = None
                    break
    if not isinstance(env, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for q in env.get("queries") or []:
        if not isinstance(q, str):
            continue
        q = q.strip()
        core = normalize_query(q)
        if not q or not core or core in seen:
            continue
        seen.add(core)
        out.append(q)
    return out[:n_expected] if n_expected else out


# --- Query dedup against history (via API) ----------------------------------

def load_existing_query_cores(project_name: str) -> set[str]:
    """Normalized cores of every query ever attempted for this project.

    Reads /api/v1/twitter-search-attempts/distinct-queries (no direct DB).
    Returns an empty set on API failure so a transient read error degrades to
    'no dedup' rather than crashing the invocation (we'd rather re-test a
    duplicate query than skip inventing entirely)."""
    try:
        resp = api_get(
            "/api/v1/twitter-search-attempts/distinct-queries",
            query={"project": project_name},
        )
    except SystemExit as exc:
        print(f"[invent_topics] distinct-queries read failed for "
              f"{project_name!r}: {exc} (proceeding without query dedup)",
              file=sys.stderr)
        return set()
    queries = ((resp or {}).get("data") or {}).get("queries") or []
    return {normalize_query(q) for q in queries if q}


def dedup_queries(
    drafted: list[str],
    existing_cores: set[str],
) -> tuple[list[str], list[str]]:
    """Split drafted queries into (new, already_tried) by normalized core."""
    new: list[str] = []
    dupes: list[str] = []
    seen = set(existing_cores)
    for q in drafted:
        core = normalize_query(q)
        if core in seen:
            dupes.append(q)
        else:
            new.append(q)
            seen.add(core)
    return new, dupes


# --- Supply test (browser-harness via lock helper) --------------------------

def supply_test(
    project_name: str,
    topic: str,
    queries: list[str],
    freshness_hours: int = FRESHNESS_HOURS,
    lock_timeout: int = LOCK_TIMEOUT_SEC,
) -> tuple[bool, list[dict]]:
    """Scan each query at `freshness_hours` via the lock+harness helper.

    Returns (tested, results) where results is
    [{"query": q, "tweets_found": n}, ...] in the SAME order as `queries`.

    tested=False means the helper produced NO scan records (lock timeout, or
    the browser went down) — the caller must NOT treat that as zero supply.
    """
    if not queries:
        return True, []
    qpayload = [
        {"project": project_name, "query": q, "search_topic": topic}
        for q in queries
    ]
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="invent-queries-", delete=False
    ) as qf:
        json.dump(qpayload, qf)
        queries_path = qf.name
    scan_out = tempfile.NamedTemporaryFile(
        suffix=".jsonl", prefix="invent-scan-", delete=False
    ).name

    try:
        subprocess.run(
            ["bash", _SUPPLY_TEST_SH, queries_path, scan_out,
             str(freshness_hours), str(lock_timeout)],
            check=False,
            timeout=lock_timeout + 600,  # helper's own lock wait + scan headroom
        )
    except subprocess.TimeoutExpired:
        print(f"[invent_topics] supply-test helper timed out for topic "
              f"{topic!r}", file=sys.stderr)
        _safe_unlink(queries_path)
        _safe_unlink(scan_out)
        return False, []

    # Parse per-query scan records. scan() writes one record per call even on
    # zero tweets, so an empty file means the scan loop never ran (untested).
    found_by_core: dict[str, int] = {}
    records = 0
    try:
        with open(scan_out) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                records += 1
                core = normalize_query(rec.get("query", ""))
                found_by_core[core] = len(rec.get("tweets") or [])
    except OSError:
        records = 0

    _safe_unlink(queries_path)
    _safe_unlink(scan_out)

    if records == 0:
        return False, []

    results = [
        {"query": q, "tweets_found": int(found_by_core.get(normalize_query(q), 0))}
        for q in queries
    ]
    return True, results


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# --- Attempt logging (via log_twitter_search_attempts.py -> route) ----------

def log_attempts(
    project_name: str,
    topic: str,
    results: list[dict],
    batch_id: str,
    dry_run: bool = False,
) -> None:
    """Log every scanned query (dud + hit) to twitter_search_attempts via the
    existing logger script, which POSTs to /api/v1/twitter-search-attempts.

    All attempts are logged on purpose — duds are the anti-list signal and the
    topic's supply record, per the user's 'log all attempts' rule."""
    if not results:
        return
    rows = [
        {
            "query": r["query"],
            "project": project_name,
            "tweets_found": r["tweets_found"],
            "search_topic": topic,
        }
        for r in results
    ]
    if dry_run:
        print(f"[dry-run] would log {len(rows)} attempts for topic {topic!r} "
              f"(batch={batch_id})", file=sys.stderr)
        return
    try:
        subprocess.run(
            ["python3", _LOG_ATTEMPTS_PY, "--batch-id", batch_id, "--kind", "invent"],
            input=json.dumps(rows),
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(f"[invent_topics] log_attempts timed out for topic {topic!r}",
              file=sys.stderr)


# --- Per-topic pipeline: draft -> dedup -> scan -> log ----------------------

def process_topic(
    project: dict,
    topic: str,
    existing_query_cores: set[str],
    batch_id: str,
    dry_run: bool = False,
) -> dict:
    """Run the full draft->dedup->supply-test->log pipeline for one topic.

    Returns a result dict with: queries_drafted, queries_tested, attempts
    (list of {query, tweets_found}), supply_total, tested (bool), qualifies
    (bool). `tested=False` signals the browser was unavailable — the caller
    should abort the run rather than treat the topic as a dud.
    """
    project_name = project.get("name", "")

    # 1. Draft queries.
    qprompt = build_query_prompt(project, topic, QUERIES_PER_TOPIC)
    raw = call_claude(qprompt)
    drafted = extract_queries(raw, QUERIES_PER_TOPIC)
    print(f"  [{topic}] drafted {len(drafted)} queries", file=sys.stderr)

    # 2. Dedup against history; refill ONCE if dedup drops us below target.
    new_q, dupes = dedup_queries(drafted, existing_query_cores)
    if dupes:
        print(f"  [{topic}] dropped {len(dupes)} already-tried queries",
              file=sys.stderr)
    if len(new_q) < QUERIES_PER_TOPIC and drafted:
        tried_cores = existing_query_cores | {normalize_query(q) for q in drafted}
        refill_prompt = build_query_prompt(
            project, topic, QUERIES_PER_TOPIC,
            avoid_queries={normalize_query(q) for q in drafted},
        )
        refill_raw = call_claude(refill_prompt)
        refill = extract_queries(refill_raw, QUERIES_PER_TOPIC)
        more_new, _ = dedup_queries(refill, tried_cores)
        for q in more_new:
            if len(new_q) >= QUERIES_PER_TOPIC:
                break
            new_q.append(q)
        print(f"  [{topic}] refill added {min(len(more_new), QUERIES_PER_TOPIC)} "
              f"queries (now {len(new_q)})", file=sys.stderr)

    queries = new_q[:QUERIES_PER_TOPIC]

    # 3. Supply-test.
    if dry_run:
        # No browser work in dry-run; report the plan only.
        print(f"  [dry-run] [{topic}] would scan {len(queries)} queries at "
              f"{FRESHNESS_HOURS}h", file=sys.stderr)
        return {
            "topic": topic, "queries_drafted": len(drafted),
            "queries_tested": len(queries), "attempts": [],
            "supply_total": 0, "tested": True, "qualifies": False,
            "queries": queries,
        }

    tested, results = supply_test(project_name, topic, queries)
    if not tested:
        return {
            "topic": topic, "queries_drafted": len(drafted),
            "queries_tested": len(queries), "attempts": [],
            "supply_total": 0, "tested": False, "qualifies": False,
            "queries": queries,
        }

    supply_total = sum(r["tweets_found"] for r in results)
    qualifies = supply_total >= SUPPLY_FLOOR
    for r in results:
        print(f"    scan q={r['query'][:48]!r} -> {r['tweets_found']} fresh",
              file=sys.stderr)
    print(f"  [{topic}] supply_total={supply_total} "
          f"(floor={SUPPLY_FLOOR}) qualifies={qualifies}", file=sys.stderr)

    # 4. Log all attempts (dud + hit).
    log_attempts(project_name, topic, results, batch_id, dry_run=dry_run)

    return {
        "topic": topic, "queries_drafted": len(drafted),
        "queries_tested": len(queries), "attempts": results,
        "supply_total": supply_total, "tested": True, "qualifies": qualifies,
        "queries": queries,
    }


# --- Main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default=None,
                    help="Force a specific project (skips pick_projects)")
    ap.add_argument("--proposals", type=int, default=DEFAULT_PROPOSALS,
                    help="How many candidate topics to ask Claude for per attempt")
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET,
                    help="Loop until this many NEW non-dupe topics are committed")
    ap.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
                    help="Hard cap on Claude calls per run (cost guard)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan; do not commit to project_search_topics")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS,
                    help="Ledger window passed to /api/v1/topic-funnel")
    args = ap.parse_args()

    # Propagate dry-run into the spawned claude -p / MCP server subprocess
    # tree so submit_topic short-circuits without POSTing during smoke tests.
    if args.dry_run:
        os.environ["INVENT_DRY_RUN"] = "1"

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

    # --- Load the ledger via API ---
    topics_for_project = load_project_topics(project_name, args.window_days)
    print(f"[invent_topics] ledger rows for {project_name}: {len(topics_for_project)}",
          file=sys.stderr)

    # --- Read fresh universe via API ---
    universe = project_universe_strings(project_name)
    universe_size_before = len(universe)
    print(f"[invent_topics] active universe size: {universe_size_before}",
          file=sys.stderr)

    # --- Probe the managed Chrome BEFORE spending Claude tokens. A down
    #     browser would make every supply scan return 0 and we'd commit real
    #     topics as false 'duds'. Dry-run skips the probe (no scans run). ----
    if not args.dry_run and not harness_alive():
        print(f"[invent_topics] managed Chrome CDP port {CDP_PORT} is not "
              f"answering; skipping this run (no tokens spent).",
              file=sys.stderr)
        return

    # --- Query-dedup corpus: every distinct query ever tried for this project,
    #     normalized to cores. Loaded once; process_topic dedups against it and
    #     we fold each tested topic's queries back in so a later attempt this
    #     run won't re-draft the same cores. -------------------------------
    existing_query_cores = load_existing_query_cores(project_name)
    print(f"[invent_topics] existing query cores for {project_name}: "
          f"{len(existing_query_cores)}", file=sys.stderr)

    # --- batch_id ties every attempt logged this run together in
    #     twitter_search_attempts, mirroring the cycle's batch convention. ---
    batch_id = f"invent-{project_name}-{int(time.time())}"

    # --- Retry loop: invent ONE topic, draft + dedup + supply-test its
    #     queries, log ALL attempts (dud + hit), ALWAYS commit the topic (even
    #     at 0 supply — the topic is a real concept; its supply record lives in
    #     twitter_search_attempts), and count it toward TARGET only if its
    #     queries cleared SUPPLY_FLOOR. Stop when TARGET qualifying topics land
    #     or MAX_ATTEMPTS iterations run out. A mid-run browser drop ('tested'
    #     False) aborts the run rather than committing false duds. -----------
    # working_universe grows as we commit so later attempts dedupe against
    # both the original universe AND topics minted earlier this run.
    working_universe = set(universe)
    # avoid_topics carries every proposal seen so far back into the next prompt
    # as an explicit do-not-repeat list, so each retry explores new ground.
    avoid_topics: set[str] = set()

    processed: list[dict] = []     # one entry per topic we supply-tested
    all_rejected: list[dict] = []  # filled by submit_topic dupe errors reported by Claude
    total_proposals_parsed = 0     # kept for audit compatibility; counts successful submits
    last_raw = ""
    attempts_used = 0       # SCANS performed (the only thing that ticks up to max_attempts)
    claude_calls = 0        # ALL Claude sessions (tool calls hidden inside each session)
    dupe_retries_total = 0  # kept for audit compatibility; always 0 in MCP-session mode
    aborted_untested = False
    saturated_bailout = False

    def n_qualifying() -> int:
        return sum(1 for p in processed if p.get("qualifies"))

    while attempts_used < args.max_attempts:
        if n_qualifying() >= args.target:
            break

        # --- ONE tool-using Claude session per scan slot. Inside this session
        #     Claude can call search_topics / get_topic_stats to explore, and
        #     submit_topic to commit. submit_topic runs Jaccard dedup on the
        #     server, so near-dupes surface as in-session tool errors Claude
        #     reacts to — no post-hoc kill, no dupe-retry-doesn't-count
        #     bookkeeping needed in Python. The session ends when Claude
        #     emits the final JSON envelope with `submitted_topic`. -----------
        prompt = build_prompt(project, topics_for_project, args.proposals,
                              avoid_topics=avoid_topics)
        last_raw = call_claude(prompt, allowed_tools=_TOPIC_TOOLS)
        claude_calls += 1

        envelope = extract_submitted_topic(last_raw)
        if envelope is None:
            print(f"[invent_topics] session returned no parseable envelope; "
                  f"raw head: {(last_raw or '')[:200]!r}", file=sys.stderr)
            saturated_bailout = True
            break

        if envelope.get("topic") is None:
            # Claude self-reported saturation (tried N submits, all dupes).
            reason = envelope.get("reason") or "(no reason given)"
            print(f"[invent_topics] saturated: claude session reports "
                  f"no non-dupe available — {reason}",
                  file=sys.stderr)
            saturated_bailout = True
            break

        topic = envelope["topic"]
        rationale = envelope.get("rationale", "")
        total_proposals_parsed += 1
        avoid_topics.add(topic)
        working_universe.add(topic)
        print(f"[invent_topics] scan {attempts_used+1}/{args.max_attempts}: "
              f"submitted {topic!r} (qualifying so far={n_qualifying()}/{args.target})",
              file=sys.stderr)
        print(f"  rationale: {rationale[:120]}", file=sys.stderr)

        # The topic is ALREADY in project_search_topics via the submit_topic
        # tool — do not re-commit. Now draft queries + supply-test.
        result = process_topic(project, topic, existing_query_cores,
                               batch_id, dry_run=args.dry_run)

        # A False 'tested' means the browser dropped mid-run; abort the run
        # and let the next hourly retry it. The topic stays committed (it's a
        # real concept either way), but we don't fabricate a supply verdict.
        if not result.get("tested", False):
            print(f"[invent_topics] supply test UNTESTED for {topic!r} "
                  f"(browser unavailable); aborting run.", file=sys.stderr)
            aborted_untested = True
            break

        # This was a real supply test — count it against max_attempts.
        attempts_used += 1

        # Fold this topic's tested query cores into the dedup corpus.
        for q in result.get("queries", []):
            existing_query_cores.add(normalize_query(q))

        processed.append({
            "topic": topic,
            "rationale": rationale,
            **result,
            "committed": True,  # submit_topic already wrote it
            "attempt": attempts_used,
        })

        print(f"  supply={result['supply_total']} qualifies={result['qualifies']}",
              file=sys.stderr)

        if n_qualifying() >= args.target:
            break

    target_met = n_qualifying() >= args.target

    # --- Audit row (via API; no local file) ---
    elapsed = round(time.time() - t0, 2)
    audit_payload = {
        "ts": started_at,
        "elapsed_sec": elapsed,
        "project": project_name,
        "pick_method": pick_method,
        "batch_id": batch_id,
        "ledger_rows_for_project": len(topics_for_project),
        "universe_size_before": universe_size_before,
        "proposals_requested": args.proposals,
        "target": args.target,
        "max_attempts": args.max_attempts,
        "attempts_used": attempts_used,
        "claude_calls": claude_calls,
        "dupe_retries_total": dupe_retries_total,
        "saturated_bailout": saturated_bailout,
        "target_met": target_met,
        "aborted_untested": aborted_untested,
        "proposals_parsed": total_proposals_parsed,
        "supply_floor": SUPPLY_FLOOR,
        "queries_per_topic": QUERIES_PER_TOPIC,
        "freshness_hours": FRESHNESS_HOURS,
        "processed": [
            {
                "topic": p["topic"],
                "committed": p.get("committed"),
                "qualifies": p.get("qualifies"),
                "supply_total": p.get("supply_total"),
                "queries_drafted": p.get("queries_drafted"),
                "queries_tested": p.get("queries_tested"),
                "attempt": p.get("attempt"),
                "neighbor": p.get("neighbor"),
                "similarity": p.get("similarity"),
                "attempts": p.get("attempts"),
            }
            for p in processed
        ],
        "rejected": all_rejected,
        "dry_run": args.dry_run,
        "raw_response_head": (last_raw or "")[:500],
    }
    write_audit(audit_payload, dry_run=args.dry_run)

    n_qual = n_qualifying()
    n_committed_topics = sum(1 for p in processed if p.get("committed"))
    print(f"[invent_topics] done. project={project_name!r} "
          f"scans={attempts_used}/{args.max_attempts} "
          f"claude_calls={claude_calls} dupe_retries={dupe_retries_total} "
          f"target={args.target} target_met={target_met} "
          f"saturated_bailout={saturated_bailout} "
          f"aborted_untested={aborted_untested} "
          f"proposals={total_proposals_parsed} "
          f"topics_committed={n_committed_topics} qualifying={n_qual} "
          f"rejected={len(all_rejected)} elapsed={elapsed}s",
          file=sys.stderr)

    # --- Surface this run in the dashboard's Status > Job History tab via
    #     log_run.py + run_monitor.log. Skip on dry-run so smoke tests don't
    #     leak fake rows into the dashboard. ----------------------------------
    if not args.dry_run:
        _emit_run_monitor_row(
            project_name=project_name,
            processed=processed,
            attempts_used=attempts_used,
            aborted_untested=aborted_untested,
            saturated_bailout=saturated_bailout,
            elapsed_sec=elapsed,
        )


def _emit_run_monitor_row(
    project_name: str,
    processed: list[dict],
    attempts_used: int,
    aborted_untested: bool,
    saturated_bailout: bool,
    elapsed_sec: float,
) -> None:
    """Call scripts/log_run.py so the invent run lands in run_monitor.log and
    the dashboard's Status > Job History tab surfaces it under the
    'Invent Topics' filter pill. Best-effort: any failure is logged and
    swallowed — we never want a dashboard-write hiccup to mask the actual
    run output."""
    # Per-topic query counts (parallel array to topic_names; joined with '+').
    qpt = [int(p.get("queries_tested", 0)) for p in processed]
    queries_total = sum(qpt)
    queries_w_supply = sum(
        1
        for p in processed
        for attempt in (p.get("attempts") or [])
        if (attempt.get("tweets_found") or 0) > 0
    )
    topics_invented = len(processed)
    n_qual = sum(1 for p in processed if p.get("qualifies"))
    # Skipped = topics tested but didn't qualify. Failed = 1 iff the run
    # aborted partway (browser drop); harmless 0 otherwise.
    skipped = max(topics_invented - n_qual, 0)
    failed = 1 if aborted_untested else 0

    # topic_names: parallel-to-qpt array of the actual topics committed this
    # run. Encode each name so it can't break the run_monitor.log segment
    # parser: replace spaces with '+', strip the four chars that have
    # structural meaning in the log line (',', ';', '=', '|'). Decoded
    # client-side in server.js. Empty list = no topics committed = no per-
    # topic pills get rendered (e.g. saturated runs).
    def _encode_topic_name(t: str) -> str:
        encoded = (t or "").strip().replace(" ", "+")
        for ch in (",", ";", "=", "|"):
            encoded = encoded.replace(ch, "")
        return encoded

    topic_names = [_encode_topic_name(p.get("topic", "")) for p in processed]
    topic_names = [t for t in topic_names if t]
    topic_names_segment = (
        f",topic_names={';'.join(topic_names)}" if topic_names else ""
    )

    invent_kv = ",".join([
        f"project={project_name}",
        f"topics={topics_invented}",
        f"queries={queries_total}",
        f"queries_w_supply={queries_w_supply}",
        f"qpt={'+'.join(str(x) for x in qpt) if qpt else '0'}",
    ]) + topic_names_segment
    log_run_py = os.path.join(_REPO_DIR, "scripts", "log_run.py")
    cmd = [
        "/opt/homebrew/bin/python3.11", log_run_py,
        "--script", "invent_topics",
        "--posted", str(topics_invented),
        "--skipped", str(skipped),
        "--failed", str(failed),
        "--cost", "0",  # claude -p inherits cost tracking; not surfaced per-call
        "--elapsed", str(int(elapsed_sec)),
        "--invent", invent_kv,
    ]
    try:
        subprocess.run(cmd, check=False, timeout=30,
                       capture_output=True, text=True)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[invent_topics] log_run.py emit failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
