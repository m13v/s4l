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
import db as dbmod

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


def parse_transcript(path: str):
    """Parse a Claude Code transcript .jsonl into per-session cost.

    Subagent (Task tool) handling
    -----------------------------
    Claude Code embeds subagent turns into the SAME parent transcript .jsonl
    rather than writing a separate file. Subagent entries are flagged with
    ``"isSidechain": true`` at the top level of each event. Their token
    usage is otherwise structured identically to parent (orchestrator) turns.

    This parser:
      * Sums orchestrator-only token usage and cost into by_model/totals
        (so the headline total_cost_usd matches the parent thread, not
        parent+subagent — that's what `cost_from_usage` callers expect).
      * Counts Task tool_use blocks in orchestrator turns -> task_call_count.
        Each Task call is one subagent invocation from the parent's point
        of view (even though the subagent itself may chain through several
        assistant turns).
      * Tallies sidechain (subagent) turns into a separate bucket grouped
        by ``parentUuid`` chain root, producing a per-subagent breakdown
        suitable for the subagent_breakdown jsonb column.
      * Surfaces ``subagent_count`` (distinct sidechain roots) and
        ``subagent_cost_usd`` (sum across all sidechain turns).

    All four fields are 0/empty/None when the session has no subagent
    activity, which is the current steady state for every social-autoposter
    pipeline (verified 2026-05-10 across 3141 archived sessions: zero
    isSidechain events, zero Task tool_use calls).
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

            # Count Task tool_use blocks even in orchestrator turns so we
            # have an authoritative subagent-invocation count, independent
            # of whether subagent transcripts came through fully (a
            # watchdog SIGTERM mid-subagent can leave Task tool_use without
            # the corresponding sidechain turns).
            msg = ev.get("message") or {}
            if not is_sidechain and ev.get("type") == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Task":
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
    # Keys are stringified root uuids (or 'unknown'); values carry cost +
    # turn count + duration + dominant model so dashboards can show "Task #N
    # cost $X" without rescanning the transcript.
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

    return {
        "by_model": by_model,
        "totals": totals,
        "total_cost_usd": total_cost,
        "primary_model": primary_model,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "task_call_count": task_call_count,
        "subagent_count": len(sidechain_groups),
        "subagent_cost_usd": round(subagent_cost_usd, 6),
        "subagent_breakdown": subagent_breakdown,
    }


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

    dbmod.load_env()
    conn = dbmod.get_conn()
    subagent_breakdown_json = (
        json.dumps(parsed["subagent_breakdown"]) if parsed.get("subagent_breakdown") else None
    )
    conn.execute(
        """INSERT INTO claude_sessions (
            session_id, script, started_at, ended_at, duration_ms,
            total_cost_usd, orchestrator_cost_usd,
            input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens, model_breakdown, model,
            cycle_id,
            task_call_count, subagent_count, subagent_cost_usd, subagent_breakdown
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                  %s, %s, %s, %s::jsonb)
        ON CONFLICT (session_id) DO UPDATE SET
            ended_at = EXCLUDED.ended_at,
            duration_ms = EXCLUDED.duration_ms,
            total_cost_usd = EXCLUDED.total_cost_usd,
            orchestrator_cost_usd = COALESCE(EXCLUDED.orchestrator_cost_usd,
                                             claude_sessions.orchestrator_cost_usd),
            input_tokens = EXCLUDED.input_tokens,
            output_tokens = EXCLUDED.output_tokens,
            cache_read_tokens = EXCLUDED.cache_read_tokens,
            cache_creation_tokens = EXCLUDED.cache_creation_tokens,
            model_breakdown = EXCLUDED.model_breakdown,
            model = EXCLUDED.model,
            cycle_id = COALESCE(EXCLUDED.cycle_id, claude_sessions.cycle_id),
            task_call_count = EXCLUDED.task_call_count,
            subagent_count = EXCLUDED.subagent_count,
            subagent_cost_usd = EXCLUDED.subagent_cost_usd,
            subagent_breakdown = EXCLUDED.subagent_breakdown
        """,
        [
            args.session_id, args.script, started, ended, duration_ms,
            round(parsed["total_cost_usd"], 6),
            orch_cost,
            parsed["totals"]["input"], parsed["totals"]["output"],
            parsed["totals"]["cache_read"], parsed["totals"]["cache_creation"],
            json.dumps(parsed["by_model"]),
            parsed["primary_model"],
            cycle_id,
            parsed.get("task_call_count", 0),
            parsed.get("subagent_count", 0),
            parsed.get("subagent_cost_usd", 0.0),
            subagent_breakdown_json,
        ],
    )

    # Backfill dominant model onto any activity rows stamped with this session.
    # Only overwrites rows where model IS NULL so re-runs of log_claude_session
    # against the same session_id stay idempotent. Covers social tables
    # (posts/replies/dms/dm_messages) plus SEO pipeline tables that stamp
    # claude_session_id (seo_escalations, seo_keywords, seo_page_improvements,
    # gsc_queries).
    backfill_counts = {}
    for table in (
        "posts", "replies", "dms", "dm_messages",
        "seo_escalations", "seo_keywords", "seo_page_improvements", "gsc_queries",
    ):
        cur = conn.execute(
            f"UPDATE {table} SET model = %s "
            f"WHERE claude_session_id = %s AND model IS NULL",
            [parsed["primary_model"], args.session_id],
        )
        backfill_counts[table] = cur.rowcount
    conn.commit()
    conn.close()

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
