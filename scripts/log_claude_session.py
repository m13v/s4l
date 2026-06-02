#!/usr/bin/env python3
"""Log a Claude Code session's cost into the claude_sessions table.

Reads the session transcript at ~/.claude/projects/<encoded-cwd>/<session_id>.jsonl,
sums per-model token usage from each assistant turn, applies a local pricing
table to compute total cost, and inserts one row.

Usage:
    python3 scripts/log_claude_session.py \\
        --session-id <uuid> \\
        --script run-linkedin \\
        [--started-at ISO8601] [--ended-at ISO8601]

Designed to be called by run_claude.sh after `claude -p --session-id $UUID` exits.
Idempotent: ON CONFLICT DO NOTHING on session_id.
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")

# Archive root for post-facto investigation. ~/.claude/projects/ is Claude
# Code's own scratch — it survives normal runs but is not under our control
# for retention or rotation, and the encoded-cwd subdirectory layout is
# annoying to navigate after the fact. We hardlink each finished session's
# transcript here so investigations of watchdog-killed phases are
# `tail skill/logs/claude-sessions/<date>/<HHMMSS>_<script>_<sid>.jsonl`
# instead of forensics across `~/.claude/projects/-/<sid>.jsonl` candidates.
ARCHIVE_ROOT = os.path.expanduser("~/social-autoposter/skill/logs/claude-sessions")


def find_transcript(session_id: str):
    """Locate the transcript .jsonl for a session id.

    Claude Code writes transcripts under `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`.
    The encoded-cwd depends on the working directory at invocation time:
    interactive runs land under `-Users-matthewdi-social-autoposter`, but
    launchd-fired runs (cwd=/) land under `-`. Glob across all project dirs.
    """
    matches = glob.glob(os.path.join(PROJECTS_ROOT, "*", f"{session_id}.jsonl"))
    return matches[0] if matches else None


def archive_transcript(transcript_path, session_id: str, script: str, started_iso):
    """Hardlink (or copy) the live transcript into ARCHIVE_ROOT.

    Best-effort: any failure returns None and the caller proceeds. The
    archive lives under <date>/<HHMMSS>_<script>_<session_id>.jsonl so
    investigators can navigate by day. Hardlink first (free, atomic, and
    keeps the archive in sync if claude appends final bytes between our
    archive call and parse_transcript); fall back to copy across volumes.
    Idempotent: returns the existing path if already archived.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        dt = None
        if started_iso:
            try:
                dt = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                dt = None
        if dt is None:
            try:
                dt = datetime.utcfromtimestamp(os.path.getmtime(transcript_path))
            except OSError:
                dt = datetime.utcnow()

        date_subdir = dt.strftime("%Y-%m-%d")
        time_part = dt.strftime("%H%M%S")
        safe_script = "".join(
            c if (c.isalnum() or c in ("-", "_")) else "_"
            for c in (script or "unknown")
        ) or "unknown"

        archive_dir = os.path.join(ARCHIVE_ROOT, date_subdir)
        os.makedirs(archive_dir, exist_ok=True)
        archive_path = os.path.join(
            archive_dir, f"{time_part}_{safe_script}_{session_id}.jsonl"
        )
        if os.path.exists(archive_path):
            return archive_path
        try:
            os.link(transcript_path, archive_path)
        except OSError:
            import shutil
            shutil.copy2(transcript_path, archive_path)
        return archive_path
    except Exception:
        return None

# USD per 1M tokens. Cache_5m / cache_1h are the WRITE rates (Anthropic charges
# a premium for caching writes); cache_read is the discounted re-read rate.
# Fallback (unknown model) uses Opus rates so we never underestimate.
PRICING = {
    "opus":   {"input": 15.0, "output": 75.0, "cache_5m": 18.75, "cache_1h": 30.0, "cache_read": 1.5},
    "sonnet": {"input": 3.0,  "output": 15.0, "cache_5m": 3.75,  "cache_1h": 6.0,  "cache_read": 0.3},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_5m": 1.25,  "cache_1h": 2.0,  "cache_read": 0.1},
}


def price_for_model(model_id: str) -> dict:
    m = (model_id or "").lower()
    if "opus" in m:
        return PRICING["opus"]
    if "sonnet" in m:
        return PRICING["sonnet"]
    if "haiku" in m:
        return PRICING["haiku"]
    return PRICING["opus"]


def cost_from_usage(model: str, usage: dict) -> float:
    p = price_for_model(model)
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_5m = (usage.get("cache_creation") or {}).get("ephemeral_5m_input_tokens", 0) or 0
    cache_1h = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0) or 0
    if not (cache_5m or cache_1h):
        cache_5m = usage.get("cache_creation_input_tokens", 0) or 0
    return (
        inp * p["input"]
        + out * p["output"]
        + cache_read * p["cache_read"]
        + cache_5m * p["cache_5m"]
        + cache_1h * p["cache_1h"]
    ) / 1_000_000


def _parse_subagent_transcript(path: str, meta: dict):
    """Parse a single agent-<id>.jsonl into a cost summary.

    Subagent transcripts have the same per-turn shape as the orchestrator
    (assistant turns with usage), but every event carries ``isSidechain:
    true``, an ``agentId``, and references back to the parent via
    ``sessionId`` (matches orchestrator's session id).
    """
    if not os.path.exists(path):
        return None
    by_model = {}
    first_ts = None
    last_ts = None
    turns = 0
    with open(path) as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = ev.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if ev.get("type") != "assistant":
                continue
            msg = ev.get("message") or {}
            usage = msg.get("usage") or {}
            if not usage:
                continue
            model = msg.get("model") or "unknown"
            entry = by_model.setdefault(model, {
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "cost_usd": 0.0,
            })
            inp = usage.get("input_tokens", 0) or 0
            out = usage.get("output_tokens", 0) or 0
            cr = usage.get("cache_read_input_tokens", 0) or 0
            cc = usage.get("cache_creation_input_tokens", 0) or 0
            entry["input_tokens"] += inp
            entry["output_tokens"] += out
            entry["cache_read_tokens"] += cr
            entry["cache_creation_tokens"] += cc
            entry["cost_usd"] += cost_from_usage(model, usage)
            turns += 1
    cost = sum(v["cost_usd"] for v in by_model.values())
    primary = None
    if by_model:
        primary = max(
            by_model.items(),
            key=lambda kv: (kv[1].get("output_tokens", 0), kv[1].get("input_tokens", 0)),
        )[0]
    return {
        "by_model": {
            m: {**v, "cost_usd": round(v["cost_usd"], 6)}
            for m, v in by_model.items()
        },
        "cost_usd": round(cost, 6),
        "turns": turns,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "model": primary or "unknown",
        "agent_type": (meta or {}).get("agentType"),
        "description": (meta or {}).get("description"),
    }


def _find_subagent_dir(orchestrator_path: str) -> str:
    """Given path .../<session_id>.jsonl, return .../<session_id>/subagents/."""
    if not orchestrator_path:
        return None
    base = orchestrator_path[:-len(".jsonl")] if orchestrator_path.endswith(".jsonl") else orchestrator_path
    candidate = os.path.join(base, "subagents")
    return candidate if os.path.isdir(candidate) else None


def parse_transcript(path: str):
    """Parse a Claude Code transcript .jsonl into per-session cost.

    Subagent (Agent tool) handling
    ------------------------------
    Claude Code SDK >= 2.1.x writes subagent transcripts to a SEPARATE
    sibling directory, NOT inline in the orchestrator's .jsonl. Layout:

        ~/.claude/projects/<encoded-cwd>/<orchestrator-session-id>.jsonl
        ~/.claude/projects/<encoded-cwd>/<orchestrator-session-id>/
                                                    subagents/
                                                        agent-<short-id>.jsonl
                                                        agent-<short-id>.meta.json

    The orchestrator's .jsonl only records the ``tool_use`` block (name
    "Agent", with ``subagent_type``/``description``/``prompt`` input) and
    the consolidated ``tool_result`` carrying the agent's final reply. The
    Agent's internal chain of assistant turns lives entirely in
    ``agent-<id>.jsonl`` with ``isSidechain: true`` on every event.

    This parser:
      * Sums orchestrator-only token usage and cost into by_model/totals
        (so total_cost_usd matches the parent thread only — subagent cost
        is broken out separately so the user can see exactly what the
        subagents added).
      * Counts ``Agent`` tool_use blocks in orchestrator turns -> the
        legacy ``task_call_count`` field (kept the name for back-compat
        even though the tool name is "Agent", not "Task").
      * Scans the sibling ``subagents/`` directory; for each
        agent-<id>.jsonl, parses it as an independent transcript and adds
        its cost to ``subagent_cost_usd``. Per-subagent details land in
        ``subagent_breakdown`` keyed by short agent id, with the
        agentType/description from the .meta.json sidecar.
      * As a defensive fallback, also detects legacy ``isSidechain: true``
        events inside the same .jsonl (older SDK versions may have used
        that layout). Currently zero hits across 14k+ historical sessions,
        but kept so we don't have to revisit when the SDK changes again.

    Historical note (2026-05-10): the prior parser version looked for
    tool_use name="Task" and inline isSidechain entries. Both miss the
    actual SDK layout. The corpus has 2041 Agent invocations (mostly in
    seo_generate_page sessions) whose cost was previously invisible.
    """
    if not os.path.exists(path):
        return None

    by_model = {}
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    first_ts = None
    last_ts = None

    # Subagent (sidechain) accounting. Keyed by chain-root uuid so chained
    # sidechain turns under the same Task() invocation aggregate together.
    # When parentUuid linkage is ambiguous we fall back to a single synthetic
    # group ("unknown") so the cost still gets counted.
    sidechain_groups = {}  # root_uuid -> {model: per-model dict, cost_usd, turns, first_ts, last_ts, description}
    # parentUuid -> root_uuid map, built as we walk the transcript. The first
    # sidechain turn we see introduces its uuid as a root candidate; later
    # turns chain by parentUuid.
    uuid_to_root = {}

    task_call_count = 0

    def _bump_model_bucket(bucket, model, usage):
        entry = bucket.setdefault(model, {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "cost_usd": 0.0,
        })
        inp = usage.get("input_tokens", 0) or 0
        out = usage.get("output_tokens", 0) or 0
        cr = usage.get("cache_read_input_tokens", 0) or 0
        cc = usage.get("cache_creation_input_tokens", 0) or 0
        entry["input_tokens"] += inp
        entry["output_tokens"] += out
        entry["cache_read_tokens"] += cr
        entry["cache_creation_tokens"] += cc
        entry["cost_usd"] += cost_from_usage(model, usage)
        return inp, out, cr, cc

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = ev.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts

            is_sidechain = bool(ev.get("isSidechain"))
            ev_uuid = ev.get("uuid")
            parent_uuid = ev.get("parentUuid")

            # Count subagent tool_use blocks (both legacy "Task" name and
            # current "Agent" name) in orchestrator turns. This gives us an
            # authoritative subagent-invocation count, independent of whether
            # the sibling subagents/ transcripts came through fully (a
            # watchdog SIGTERM mid-subagent can leave the tool_use stamped
            # but the sibling .jsonl never finished). 2026-05-10 the actual
            # SDK tool name is "Agent"; "Task" kept for forward-compat if
            # the SDK ever renames it again.
            msg = ev.get("message") or {}
            if not is_sidechain and ev.get("type") == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    for c in content:
                        if (isinstance(c, dict)
                                and c.get("type") == "tool_use"
                                and c.get("name") in ("Task", "Agent")):
                            task_call_count += 1

            if ev.get("type") != "assistant":
                continue
            usage = msg.get("usage") or {}
            model = msg.get("model") or "unknown"

            if not is_sidechain:
                # Orchestrator turn.
                inp, out, cr, cc = _bump_model_bucket(by_model, model, usage)
                totals["input"] += inp
                totals["output"] += out
                totals["cache_read"] += cr
                totals["cache_creation"] += cc
            else:
                # Sidechain (subagent) turn. Resolve to a chain root: if
                # parentUuid is already mapped to a root, attach there;
                # otherwise this is a new root.
                root = uuid_to_root.get(parent_uuid)
                if root is None:
                    root = ev_uuid or "unknown"
                if ev_uuid:
                    uuid_to_root[ev_uuid] = root

                grp = sidechain_groups.setdefault(root, {
                    "by_model": {},
                    "cost_usd": 0.0,
                    "turns": 0,
                    "first_ts": None,
                    "last_ts": None,
                    "root_uuid": root,
                })
                _bump_model_bucket(grp["by_model"], model, usage)
                grp["cost_usd"] += cost_from_usage(model, usage)
                grp["turns"] += 1
                if ts:
                    grp["first_ts"] = grp["first_ts"] or ts
                    grp["last_ts"] = ts

    if not by_model and not sidechain_groups:
        return None

    total_cost = sum(m["cost_usd"] for m in by_model.values())

    # Dominant model = the one that produced the most output tokens in this
    # session. Claude Code's transcript emits `"model": "<synthetic>"` on
    # interrupted/stopped events with zero usage; those shouldn't win just
    # because they sort alphabetically when all real candidates tie.
    real_models = {k: v for k, v in by_model.items() if not k.startswith("<")}
    pool = real_models or by_model
    if pool:
        primary_model = max(
            pool.items(),
            key=lambda kv: (kv[1].get("output_tokens", 0), kv[1].get("input_tokens", 0)),
        )[0]
    else:
        # Subagents-only session (no orchestrator turns logged — unusual but
        # possible if the orchestrator was SIGTERMed before its first
        # assistant turn yet a sidechain had already started). Fall back to
        # the dominant model across sidechains.
        all_models = {}
        for grp in sidechain_groups.values():
            for m, v in grp["by_model"].items():
                e = all_models.setdefault(m, {"output_tokens": 0, "input_tokens": 0})
                e["output_tokens"] += v.get("output_tokens", 0)
                e["input_tokens"] += v.get("input_tokens", 0)
        primary_model = max(
            all_models.items(),
            key=lambda kv: (kv[1].get("output_tokens", 0), kv[1].get("input_tokens", 0)),
        )[0] if all_models else "unknown"

    # Compact per-subagent breakdown for the subagent_breakdown jsonb column.
    # Two sources feed this map:
    #   1. Inline isSidechain entries (legacy SDK layout, ~0 hits today).
    #      Keyed by chain-root uuid.
    #   2. Sibling agent-<id>.jsonl files (current SDK layout, post-2.1.x).
    #      Keyed by short agent id (e.g. "ab24e352623c7d99b").
    subagent_breakdown = {}
    for root, grp in sidechain_groups.items():
        # Dominant model for this subagent group.
        bm = grp["by_model"]
        if bm:
            sg_model = max(
                bm.items(),
                key=lambda kv: (kv[1].get("output_tokens", 0), kv[1].get("input_tokens", 0)),
            )[0]
        else:
            sg_model = "unknown"
        subagent_breakdown[root] = {
            "source": "inline_sidechain",
            "cost_usd": round(grp["cost_usd"], 6),
            "turns": grp["turns"],
            "first_ts": grp["first_ts"],
            "last_ts": grp["last_ts"],
            "model": sg_model,
            "by_model": {
                m: {
                    "input_tokens": v["input_tokens"],
                    "output_tokens": v["output_tokens"],
                    "cache_read_tokens": v["cache_read_tokens"],
                    "cache_creation_tokens": v["cache_creation_tokens"],
                    "cost_usd": round(v["cost_usd"], 6),
                }
                for m, v in bm.items()
            },
        }
    subagent_cost_usd = sum(grp["cost_usd"] for grp in sidechain_groups.values())

    # ------- Scan sibling subagents/ directory for current-SDK transcripts -------
    sub_dir = _find_subagent_dir(path)
    if sub_dir:
        for agent_file in sorted(os.listdir(sub_dir)):
            if not agent_file.endswith(".jsonl"):
                continue
            short_id = agent_file[len("agent-"):-len(".jsonl")] if agent_file.startswith("agent-") else agent_file
            meta_path = os.path.join(sub_dir, agent_file.replace(".jsonl", ".meta.json"))
            meta = {}
            try:
                if os.path.exists(meta_path):
                    with open(meta_path) as mf:
                        meta = json.load(mf)
            except Exception:
                meta = {}
            sub = _parse_subagent_transcript(os.path.join(sub_dir, agent_file), meta)
            if not sub:
                continue
            subagent_breakdown[short_id] = {
                "source": "sibling_dir",
                "cost_usd": sub["cost_usd"],
                "turns": sub["turns"],
                "first_ts": sub["first_ts"],
                "last_ts": sub["last_ts"],
                "model": sub["model"],
                "agent_type": sub["agent_type"],
                "description": sub["description"],
                "by_model": sub["by_model"],
            }
            subagent_cost_usd += sub["cost_usd"]
    # Total distinct subagents (inline + sibling-dir) for the count column.
    subagent_count = len(subagent_breakdown)

    return {
        "by_model": by_model,
        "totals": totals,
        "total_cost_usd": total_cost,
        "primary_model": primary_model,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "task_call_count": task_call_count,
        "subagent_count": subagent_count,
        "subagent_cost_usd": round(subagent_cost_usd, 6),
        "subagent_breakdown": subagent_breakdown,
    }


_BACKFILL_TABLES = (
    "posts", "replies", "dms", "dm_messages",
    "seo_escalations", "seo_keywords", "seo_page_improvements", "gsc_queries",
)


def _persist_via_api(args, parsed, started, ended, duration_ms, orch_cost, cycle_id):
    """Upsert claude_sessions row + backfill model column via HTTP routes.

    Two calls:
      POST /api/v1/claude-sessions             -> upsert by session_id
      POST /api/v1/claude-sessions/backfill-model -> stamp model on activity rows
    """
    from http_api import api_post
    api_post(
        "/api/v1/claude-sessions",
        {
            "session_id": args.session_id,
            "script": args.script,
            "started_at": started,
            "ended_at": ended,
            "duration_ms": duration_ms,
            "total_cost_usd": round(parsed["total_cost_usd"], 6),
            "orchestrator_cost_usd": orch_cost,
            "input_tokens": parsed["totals"]["input"],
            "output_tokens": parsed["totals"]["output"],
            "cache_read_tokens": parsed["totals"]["cache_read"],
            "cache_creation_tokens": parsed["totals"]["cache_creation"],
            "model_breakdown": parsed["by_model"],
            "model": parsed["primary_model"],
            "cycle_id": cycle_id,
            "task_call_count": parsed.get("task_call_count", 0),
            "subagent_count": parsed.get("subagent_count", 0),
            "subagent_cost_usd": parsed.get("subagent_cost_usd", 0.0),
            "subagent_breakdown": parsed.get("subagent_breakdown") or None,
        },
    )

    resp = api_post(
        "/api/v1/claude-sessions/backfill-model",
        {
            "session_id": args.session_id,
            "model": parsed["primary_model"],
            "tables": list(_BACKFILL_TABLES),
        },
    )
    data = (resp or {}).get("data") or {}
    backfill_counts = data.get("backfilled") or {}
    for t in _BACKFILL_TABLES:
        backfill_counts.setdefault(t, 0)
    return backfill_counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--started-at", default=None,
                        help="ISO8601 timestamp; falls back to first transcript ts")
    parser.add_argument("--ended-at", default=None,
                        help="ISO8601 timestamp; falls back to last transcript ts")
    parser.add_argument("--orchestrator-cost-usd", default=None, type=float,
                        help="Native SDK cost (streamRes.total_cost_usd) for the "
                             "orchestrator session, captured from claude -p stdout. "
                             "Stored in claude_sessions.orchestrator_cost_usd. "
                             "Authoritative (matches Anthropic billing for the "
                             "orchestrator), but excludes Task subagent costs "
                             "(see anthropics/claude-code #43945). When omitted, "
                             "the column stays NULL and dashboards fall back to "
                             "total_cost_usd (manual transcript-derived estimate).")
    parser.add_argument("--cycle-id", default=None,
                        help="Optional per-cycle batch identifier (e.g. "
                             "'rdcycle-20260510-110005'). Lets get_run_cost.py / "
                             "the dashboard scope cost to ONE pipeline cycle "
                             "even when multiple cycles of the same script "
                             "(double-forked run-reddit-search.sh / "
                             "run-twitter-cycle.sh) overlap in wall-clock time. "
                             "Falls back to env SA_CYCLE_ID; NULL if unset.")
    args = parser.parse_args()

    # Allow callers (run_claude.sh, post_reddit.py spawning a child claude) to
    # propagate cycle_id via env without re-plumbing every call site. CLI flag
    # takes precedence so explicit overrides still work.
    cycle_id = args.cycle_id or os.environ.get("SA_CYCLE_ID") or None
    if cycle_id == "":
        cycle_id = None

    transcript = find_transcript(args.session_id)
    # Archive the transcript BEFORE parsing so even an empty/short session
    # leaves a forensics trail. This is the only path that runs reliably on
    # watchdog SIGTERM — once the wrapper's EXIT trap fires, log_claude_session
    # is the last chance to capture what claude was doing before death.
    archive_path = archive_transcript(
        transcript, args.session_id, args.script, args.started_at
    )
    parsed = parse_transcript(transcript) if transcript else None

    if parsed is None:
        print(json.dumps({
            "logged": False,
            "reason": "no-transcript-or-empty",
            "transcript": transcript,
            "archive_path": archive_path,
            "session_id": args.session_id,
        }))
        return

    started = args.started_at or parsed["first_ts"]
    ended = args.ended_at or parsed["last_ts"]
    duration_ms = None
    try:
        if started and ended:
            s = datetime.fromisoformat(started.replace("Z", "+00:00"))
            e = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            duration_ms = int((e - s).total_seconds() * 1000)
    except (ValueError, AttributeError):
        pass

    # Orchestrator cost: prefer the native SDK value passed via flag (from
    # streamRes.total_cost_usd in the caller); fall back to NULL so the column
    # only holds authoritative values. Manual transcript-derived estimate goes
    # into total_cost_usd unchanged.
    orch_cost = (
        round(args.orchestrator_cost_usd, 6)
        if args.orchestrator_cost_usd is not None
        else None
    )

    backfill_counts = _persist_via_api(args, parsed, started, ended, duration_ms,
                                       orch_cost, cycle_id)

    print(json.dumps({
        "logged": True,
        "session_id": args.session_id,
        "script": args.script,
        "cycle_id": cycle_id,
        "total_cost_usd": round(parsed["total_cost_usd"], 6),
        "orchestrator_cost_usd": orch_cost,
        "duration_ms": duration_ms,
        "model": parsed["primary_model"],
        "models": list(parsed["by_model"].keys()),
        "task_call_count": parsed.get("task_call_count", 0),
        "subagent_count": parsed.get("subagent_count", 0),
        "subagent_cost_usd": parsed.get("subagent_cost_usd", 0.0),
        "backfilled": backfill_counts,
        "archive_path": archive_path,
    }))


if __name__ == "__main__":
    main()
