#!/usr/bin/env python3
"""
context_mining.py -- PROTOTYPE: mine the user's own Claude conversation
transcripts for insights worth adding to a personal context corpus, using the
existing claude_job.py file queue as the LLM lane (design agreed 2026-07-31).

Flow (test-run shape; toggle/watermark/cards come later):
  gather  -> deterministically collect recent human-interactive sessions,
             compact each FULL transcript (user prose kept, tool plumbing
             dropped), inline the current corpus (numbered lines) plus the
             already-considered ledger, enqueue ONE pure text->JSON job via
             run_claude.sh (tag: context-mining), block for the result, and
             write proposals to pending.json.
  review  -> print pending proposals.
  approve -> apply a proposal to corpus.md (append or replace a line) and
             record it in the considered ledger.
  skip    -> record the proposal as skipped so it is never re-pitched.

State lives under <state_dir>/context-mining/ where state_dir is
$S4L_STATE_DIR or ~/.social-autoposter-mcp (same convention as the queue):
  corpus.md         the corpus; every non-blank line is one numbered memory
  pending.json      proposals awaiting a human decision
  considered.jsonl  append-only ledger of every approve/skip decision

Sources walked (same two stores as extract_user_messages_today.py):
  ~/.claude/projects/*/*.jsonl                      Claude Code (CLI + desktop)
  ~/Library/Application Support/Claude/
      local-agent-mode-sessions/**/.claude/projects/**/*.jsonl   Cowork

Eligibility: a session must contain >= MIN_HUMAN_MSGS user messages classified
HUMAN (typed prose, not command echoes / task notifications / reminders), and
its cwd must not be an S4L worker sandbox. S4L's own automation transcripts are
never mined -- that would be a feedback loop.

Usage:
  python3 scripts/context_mining.py gather --days 3
  python3 scripts/context_mining.py gather --days 3 --dry-run   # build, no enqueue
  python3 scripts/context_mining.py review
  python3 scripts/context_mining.py approve cm-1a2b3c4d [id...]
  python3 scripts/context_mining.py skip cm-1a2b3c4d [id...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parent.parent

CLAUDE_CODE_PROJECTS_ROOT = HOME / ".claude" / "projects"
COWORK_SESSIONS_ROOT = (
    HOME / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
)


def state_dir() -> Path:
    base = os.environ.get("S4L_STATE_DIR") or str(HOME / ".social-autoposter-mcp")
    d = Path(base) / "context-mining"
    d.mkdir(parents=True, exist_ok=True)
    return d


def corpus_path() -> Path:
    return state_dir() / "corpus.md"


def pending_path() -> Path:
    return state_dir() / "pending.json"


def ledger_path() -> Path:
    return state_dir() / "considered.jsonl"


# ---------------------------------------------------------------- gathering

MIN_HUMAN_MSGS = 2
# No per-message or per-session caps (2026-11-17): transcripts go in whole.
# The budgets below only decide how many sessions share ONE job; a window that
# does not fit is split across more jobs, never trimmed.
DEFAULT_TOTAL_BUDGET = 400_000  # chars per job (~100k tokens); uncapped sessions need room

# cwd substrings that mark a session as S4L's own automation, never mined
EXCLUDED_CWD_MARKERS = (".s4l-worker", ".saps-worker")


def _classify(text: str) -> str:
    """HUMAN vs harness-injected user-role messages (same taxonomy as
    extract_user_messages_today.py)."""
    stripped = text.lstrip()
    if stripped.startswith("<task-notification>"):
        return "TASK_NOTIF"
    if stripped.startswith("<command-name>") or stripped.startswith("<command-message>"):
        return "COMMAND"
    if stripped.startswith("<local-command-stdout>") or stripped.startswith(
        "<local-command-stderr>"
    ):
        return "CMD_STDOUT"
    if "<<autonomous-loop" in stripped or stripped.startswith("<loop-"):
        return "SCHED_WAKE"
    if stripped.startswith("<user-prompt-submit-hook>"):
        return "HOOK"
    if stripped.startswith("<system-reminder>"):
        without = re.sub(
            r"<system-reminder>.*?</system-reminder>", "", stripped, flags=re.DOTALL
        ).strip()
        if not without:
            return "SYS_REMIND"
    return "HUMAN"


def _strip_reminders(text: str) -> str:
    return re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()


def _text_blocks(content) -> str | None:
    """Textual payload of a message; None for tool_result plumbing."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                return None
            if btype == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
        if texts:
            return "\n".join(texts)
    return None


def _iter_transcript_files():
    if CLAUDE_CODE_PROJECTS_ROOT.is_dir():
        for p in CLAUDE_CODE_PROJECTS_ROOT.glob("*/*.jsonl"):
            yield "claude_code", p
    if COWORK_SESSIONS_ROOT.is_dir():
        for p in COWORK_SESSIONS_ROOT.glob("**/.claude/projects/**/*.jsonl"):
            yield "cowork", p


def _compact_session(path: Path) -> dict | None:
    """Read one full transcript and compact it to the conversation arc:
    HUMAN user messages (near-full) + assistant prose (trimmed). Returns None
    if the session is not human-interactive or is S4L automation."""
    cwd = None
    first_ts = last_ts = None
    human_count = 0
    turns: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = d.get("timestamp")
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                if cwd is None and d.get("cwd"):
                    cwd = d["cwd"]
                if d.get("isSidechain"):
                    continue
                msg = d.get("message") or {}
                role = msg.get("role")
                if d.get("type") == "user" and role == "user":
                    text = _text_blocks(msg.get("content"))
                    if text is None:
                        continue
                    text = text.strip()
                    if not text or _classify(text) != "HUMAN":
                        continue
                    text = _strip_reminders(text)
                    if not text:
                        continue
                    human_count += 1
                    turns.append("USER: " + text)
                elif d.get("type") == "assistant" and role == "assistant":
                    text = _text_blocks(msg.get("content"))
                    if not text:
                        continue
                    text = text.strip()
                    if len(text) < 40:  # skip one-liner tool narration
                        continue
                    turns.append("ASSISTANT: " + text)
    except OSError:
        return None

    if human_count < MIN_HUMAN_MSGS:
        return None
    if cwd and any(m in cwd for m in EXCLUDED_CWD_MARKERS):
        return None

    body = "\n\n".join(turns)
    return {
        "session_id": path.stem,
        "path": str(path),
        "cwd": cwd or "?",
        "first_ts": first_ts or "?",
        "last_ts": last_ts or "?",
        "human_msgs": human_count,
        "body": body,
    }


def gather_sessions(days: int) -> list[dict]:
    """ALL eligible human sessions within the window, most recent first."""
    cutoff = time.time() - days * 86400
    candidates = []
    for _source, p in _iter_transcript_files():
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        candidates.append((mtime, p))
    candidates.sort(reverse=True)

    kept: list[dict] = []
    seen_ids: set[str] = set()
    for _mtime, p in candidates:
        if p.stem in seen_ids:
            continue
        sess = _compact_session(p)
        if sess is None:
            continue
        seen_ids.add(p.stem)
        kept.append(sess)
    return kept


def chunk_by_budget(sessions: list[dict], budget: int) -> list[list[dict]]:
    """Split into batches whose combined body size fits the budget. A batch
    always takes at least one session, so oversized sessions still ship."""
    batches: list[list[dict]] = []
    cur: list[dict] = []
    used = 0
    for s in sessions:
        cost = len(s["body"])
        if cur and used + cost > budget:
            batches.append(cur)
            cur, used = [], 0
        cur.append(s)
        used += cost
    if cur:
        batches.append(cur)
    return batches


# ------------------------------------------------------------------- corpus


def read_corpus_lines() -> list[str]:
    if not corpus_path().exists():
        return []
    return [ln.rstrip("\n") for ln in corpus_path().read_text().splitlines() if ln.strip()]


def write_corpus_lines(lines: list[str]) -> None:
    corpus_path().write_text("\n".join(lines) + ("\n" if lines else ""))


def read_ledger() -> list[dict]:
    if not ledger_path().exists():
        return []
    out = []
    for ln in ledger_path().read_text().splitlines():
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def append_ledger(rec: dict) -> None:
    with ledger_path().open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------- prompt

SCHEMA = {
    "type": "object",
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "action",
                    "title",
                    "story",
                    "conclusion",
                    "source_session",
                    "source_date",
                ],
                "properties": {
                    "action": {"type": "string", "enum": ["add", "revise"]},
                    "revises_line": {"type": ["integer", "null"]},
                    "title": {"type": "string"},
                    "story": {"type": "string"},
                    "conclusion": {"type": "string"},
                    "source_session": {"type": "string"},
                    "source_date": {"type": "string"},
                },
            },
        }
    },
}


def corpus_entry(p: dict) -> str:
    """Render one approved proposal as a single corpus line (numbering is
    line-based, so the entry must not contain newlines)."""
    story = " ".join((p.get("story") or "").split())
    concl = " ".join((p.get("conclusion") or "").split())
    title = " ".join((p.get("title") or "").split())
    return f"{title}: {story} Conclusion: {concl}"


def build_prompt(sessions: list[dict], pending_props: list[dict] | None = None) -> str:
    corpus_lines = read_corpus_lines()
    corpus_block = (
        "\n".join(f"[{i + 1}] {ln}" for i, ln in enumerate(corpus_lines))
        if corpus_lines
        else "(the corpus is currently empty)"
    )
    def _gist(r: dict) -> str:
        # works for both the old one-line shape (text) and the story shape
        return r.get("text") or f"{r.get('title', '')}: {r.get('conclusion', '')}"

    considered = [
        {"status": r.get("status"), "gist": _gist(r)} for r in read_ledger()[-60:]
    ] + [{"status": "pending", "gist": _gist(p)} for p in (pending_props or [])]
    considered_block = (
        "\n".join(f"- ({r['status']}) {r['gist']}" for r in considered)
        if considered
        else "(nothing has been considered yet)"
    )
    transcript_parts = []
    for s in sessions:
        transcript_parts.append(
            f"### Session {s['session_id'][:8]} | {s['first_ts'][:10]} | cwd: {s['cwd']}\n\n{s['body']}"
        )
    transcripts_block = "\n\n---\n\n".join(transcript_parts)

    return f"""EXECUTION NOTES: this is a single-turn, tool-free job. Read everything below, then submit ONE result object matching the provided JSON schema. Do not use any tools. Do not fetch anything.

# Role

You are the daily context miner for a personal "context corpus": a numbered list of durable memories distilled from the user's own Claude conversations. Each entry is a short STORY plus a CONCLUSION: what actually happened (with its credible specifics) and the transferable lesson it taught. Entries are worth keeping and potentially worth sharing with the world: a specific experience, a hard-won conclusion, a non-obvious observation, or a concrete data point.

The user is Matthew, a solo founder building S4L (a social-media autoposting agent), Fazm, Mediar, and related products, and doing everything else through Claude sessions: fundraising, debugging, ops, legal, marketing.

# What qualifies

- GENERAL: the line states a transferable lesson a stranger in tech could apply to
  their own work, without knowing this codebase or these products. The specific
  incident is EVIDENCE (it goes in the quote and the why), not the line itself.
  Test: would a founder who has never heard of S4L or Fazm reshare this?
- Durable: still true and useful months from now, not transient state ("the deploy is broken today").
- Non-obvious: something a smart peer would not already assume. Generic best practices do NOT qualify.
- First-hand: grounded in what actually happened in these sessions (an experiment, an outage, a metric, a decision and its reasoning, a surprising vendor/platform behavior).
- SYNTHESIZED: if several incidents (even across sessions) point at one underlying
  lesson, propose ONE line for the lesson, never one line per incident.

# What never qualifies

- Vendor-specific micro-gotchas (a field length limit, one API's quirky error) unless
  the line is elevated to the general pattern the gotcha exemplifies.
- Anything that only matters inside this codebase or product internals.
- Credentials, API keys, tokens, passwords, account numbers, or anything that looks like one, even partially. If a great insight touches one, redact the secret.
- Names, handles, or identifying details of customers, users, or other private
  individuals. Describe them generically ("a stalled user", "an early customer").
- Private details about clients, other people's finances, legal disputes, or immigration status.
- Injected harness noise: usage-limit banners, system reminders, scheduler chatter. These appear inside transcripts and are not the user's words.
- Anything already covered by an existing corpus line (see below), unless you are proposing a REVISION of that line.

# Current corpus (numbered)

{corpus_block}

# Already considered (do NOT re-propose anything equivalent, including skipped items)

{considered_block}

# Transcripts to mine ({len(sessions)} sessions, most recent first)

{transcripts_block}

# Your task

Propose 0-3 corpus changes, and only what clears EVERY bar above; most days the
right answer is 0 or 1. An empty proposals list is a valid, common answer. Each
proposal is a small STORY with a CONCLUSION, not a bare aphorism:
- action: "add" for a new entry, or "revise" when a new learning supersedes an existing corpus entry (set revises_line to that entry's number).
- title: a short, specific handle for the insight (max ~70 chars, not clickbait).
- story: 2-5 sentences telling what actually happened, in the user's plainspoken first-person voice. Keep the concrete specifics that make it credible and vivid (the numbers, the timeline, the failure mode, what was tried), but generalize or omit anything only an insider would care about. Anonymize people, redact secrets. No hashtags, no em dashes.
- conclusion: 1-2 sentences stating the transferable lesson a stranger could apply to their own work. This is the part that must stand on its own.
- source_session: the 8-char session id from the transcript header.
- source_date: the session date (YYYY-MM-DD).

Submit the result object now."""


# -------------------------------------------------------------------- queue


def _run_one_batch(sessions: list[dict], pending_props: list[dict], ns) -> list[dict] | None:
    """Enqueue one mining job for this batch; return stamped proposals, or None
    on failure (caller stops the loop and keeps what it has)."""
    prompt = build_prompt(sessions, pending_props)
    schema_file = state_dir() / "schema.json"
    schema_file.write_text(json.dumps(SCHEMA))

    env = dict(os.environ)
    env["S4L_REPO_DIR"] = str(REPO_DIR)  # route through THIS repo's claude_job.py
    cmd = [
        "bash",
        str(REPO_DIR / "scripts" / "run_claude.sh"),
        "context-mining",
        "--json-schema",
        str(schema_file),
        "-p",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            env=env,
            timeout=ns.timeout + 120,
        )
    except subprocess.TimeoutExpired:
        print("[gather] hard timeout waiting for the queue result", file=sys.stderr)
        return None

    if proc.returncode == 79:
        print(
            "[gather] queue timed out (rc=79): no worker claimed the job. Is Claude "
            "Desktop open and the s4l-worker task firing?",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0:
        print(f"[gather] provider failed rc={proc.returncode}", file=sys.stderr)
        sys.stderr.write((proc.stderr or "")[-2000:] + "\n")
        return None

    try:
        envelope = json.loads(proc.stdout[proc.stdout.index("{"):])
        obj = envelope.get("structured_output")
        if obj is None:
            obj = json.loads(envelope.get("result") or "{}")
    except Exception as e:
        print(f"[gather] could not parse result envelope: {e}", file=sys.stderr)
        sys.stderr.write((proc.stdout or "")[-2000:] + "\n")
        return None

    stamped = []
    for p in obj.get("proposals") or []:
        key = (p.get("title") or p.get("text") or "") + p.get("source_session", "")
        pid = "cm-" + hashlib.sha1(key.encode()).hexdigest()[:8]
        p["id"] = pid
        p["mined_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stamped.append(p)
    return stamped


def run_gather(ns) -> int:
    sessions = gather_sessions(ns.days)
    if not sessions:
        print(json.dumps({"ok": False, "reason": "no_eligible_sessions", "days": ns.days}))
        return 1
    batches = chunk_by_budget(sessions, ns.budget)
    total_chars = sum(len(s["body"]) for s in sessions)
    print(
        f"[gather] {len(sessions)} sessions in window ({ns.days}d), "
        f"{total_chars:,} chars -> {len(batches)} batches (budget {ns.budget:,})",
        file=sys.stderr,
    )

    if ns.dry_run:
        out = state_dir() / "last_prompt.txt"
        out.write_text(build_prompt(batches[0], []))
        for i, b in enumerate(batches, 1):
            ids = ", ".join(s["session_id"][:8] for s in b)
            print(f"[gather]   batch {i}/{len(batches)}: {len(b)} sessions ({ids})", file=sys.stderr)
        print(f"[gather] dry-run: batch-1 prompt written to {out}, nothing enqueued", file=sys.stderr)
        return 0

    # carry over any still-pending proposals so batches dedup against them too
    all_props: list[dict] = _load_pending()
    known_ids = {p["id"] for p in all_props}
    failed = 0
    for i, batch in enumerate(batches, 1):
        ids = ", ".join(s["session_id"][:8] for s in batch)
        print(
            f"[gather] batch {i}/{len(batches)}: {len(batch)} sessions "
            f"({sum(len(s['body']) for s in batch):,} chars) [{ids}]",
            file=sys.stderr,
        )
        stamped = _run_one_batch(batch, all_props, ns)
        if stamped is None:
            failed += 1
            print(f"[gather] batch {i} failed; stopping loop, keeping prior results", file=sys.stderr)
            break
        fresh = [p for p in stamped if p["id"] not in known_ids]
        known_ids.update(p["id"] for p in fresh)
        all_props.extend(fresh)
        pending_path().write_text(
            json.dumps({"proposals": all_props}, indent=2, ensure_ascii=False)
        )
        print(f"[gather] batch {i} done: +{len(fresh)} proposals ({len(all_props)} total)", file=sys.stderr)

    print(
        f"[gather] run complete: {len(batches) - failed}/{len(batches)} batches, "
        f"{len(all_props)} pending proposals -> {pending_path()}",
        file=sys.stderr,
    )
    cmd_review(None)
    return 0 if not failed else 1


# ------------------------------------------------------------------- review


def _load_pending() -> list[dict]:
    if not pending_path().exists():
        return []
    try:
        return json.loads(pending_path().read_text()).get("proposals", [])
    except Exception:
        return []


def cmd_review(_ns) -> int:
    props = _load_pending()
    if not props:
        print("no pending proposals")
        return 0
    corpus_lines = read_corpus_lines()
    for p in props:
        print("=" * 72)
        head = p["id"]
        if p.get("action") == "revise" and p.get("revises_line"):
            n = p["revises_line"]
            old = corpus_lines[n - 1] if 0 < n <= len(corpus_lines) else "(missing line)"
            head += f"  REVISES [{n}]: {old}"
        else:
            head += "  ADD"
        print(head)
        if p.get("title"):
            print(f"  title: {p['title']}")
            print(f"  story: {p.get('story', '')}")
            print(f"  concl: {p.get('conclusion', '')}")
        else:  # legacy one-line shape
            print(f"  text : {p.get('text', '')}")
            print(f"  why  : {p.get('why', '')}")
        print(f"  src  : {p.get('source_session', '?')} @ {p.get('source_date', '?')}")
    print("=" * 72)
    print(f"{len(props)} pending. approve/skip with: context_mining.py approve <id...>")
    return 0


def _decide(ids: list[str], status: str) -> int:
    props = _load_pending()
    if not props:
        print("no pending proposals")
        return 1
    if ids == ["all"]:
        ids = [p["id"] for p in props]
    by_id = {p["id"]: p for p in props}
    corpus_lines = read_corpus_lines()
    done = []
    for pid in ids:
        p = by_id.get(pid)
        if not p:
            print(f"unknown id: {pid}")
            continue
        if status == "approved":
            entry = corpus_entry(p) if p.get("title") else p.get("text", "")
            if p.get("action") == "revise" and p.get("revises_line"):
                n = p["revises_line"]
                if 0 < n <= len(corpus_lines):
                    corpus_lines[n - 1] = entry
                else:
                    corpus_lines.append(entry)
            else:
                corpus_lines.append(entry)
        append_ledger(
            {
                "id": pid,
                "status": status,
                "title": p.get("title", ""),
                "conclusion": p.get("conclusion", ""),
                "text": p.get("text", ""),
                "source_session": p.get("source_session"),
                "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        done.append(pid)
    if status == "approved":
        write_corpus_lines(corpus_lines)
    remaining = [p for p in props if p["id"] not in set(done)]
    pending_path().write_text(
        json.dumps({"proposals": remaining}, indent=2, ensure_ascii=False)
    )
    print(f"{status}: {', '.join(done) if done else 'nothing'}; {len(remaining)} still pending")
    if status == "approved":
        print(f"corpus now has {len(corpus_lines)} lines: {corpus_path()}")
    return 0


# --------------------------------------------------------- recent-work digest
#
# 2026-11-17: the original (Feb 2026) autoposter started every run from the
# user's recent Claude work (prompt-db), then searched for threads where that
# work fit. That was lost in the Mar 9 config-driven refactor. This brings it
# back for the personal_brand lane only, behind the `recent_work_context`
# flag in mode.json (s4l_mode.get_recent_work, DEFAULT OFF):
#
#   digest   one tool-free queue job over the last ~2 days of human sessions
#            -> recent_work.json: shareable work items + X search queries.
#            `--if-stale-hours N --background` is what the cycle calls every
#            run: no-op unless the flag is on, the digest is stale, no refresh
#            is in flight, and the last attempt was not in the past hour.
#   queries  read PROJECTS_JSON on stdin, emit extra Phase 1 queries (bank
#            shape) for the persona project only; [] when off or stale.
#   show     print the current digest.
#
# Consumers: skill/run-twitter-cycle.sh (Phase 1 queries + refresh kick) and
# scripts/draft_prompt_core.py (recent_work_block, persona lane only).

DIGEST_DAYS = 2
DIGEST_BUDGET = 400_000  # chars per job (~100k tokens)
DIGEST_MAX_AGE_HOURS = 48       # older than this: not used at all
DIGEST_REFRESH_HOURS = 6        # cycle kicks a background refresh past this
DIGEST_RETRY_COOLDOWN_S = 3600  # min gap between attempts (success or not)
MAX_RECENT_WORK_QUERIES = 4  # per cycle; X search is budgeted (twitter_scan.py)
RECENT_WORK_TOPIC_PREFIX = "recent work: "

DIGEST_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "what", "specifics", "angle", "queries"],
                "properties": {
                    "title": {"type": "string"},
                    "what": {"type": "string"},
                    "specifics": {"type": "string"},
                    "angle": {"type": "string"},
                    "queries": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


def recent_work_enabled() -> bool:
    try:
        import s4l_mode  # noqa: WPS433 (lives next to this file)
        return s4l_mode.get_recent_work()
    except Exception:
        return False


def digest_path() -> Path:
    return state_dir() / "recent_work.json"


def _digest_attempt_path() -> Path:
    return state_dir() / "recent_work.attempt.json"


def _digest_lock_path() -> Path:
    return state_dir() / "recent_work.lock"


def load_digest(max_age_hours: float = DIGEST_MAX_AGE_HOURS) -> dict | None:
    """The current digest, or None when missing, unreadable, or too old."""
    try:
        d = json.loads(digest_path().read_text())
        ts = datetime.fromisoformat(d["generated_at"])
    except Exception:
        return None
    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    if age_h > max_age_hours:
        return None
    d["age_hours"] = round(age_h, 1)
    return d


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def build_digest_prompt(sessions: list[dict]) -> str:
    parts = [
        f"### Session {s['session_id'][:8]} | {s['last_ts'][:16]} | cwd: {s['cwd']}\n\n{s['body']}"
        for s in sessions
    ]
    transcripts = "\n\n---\n\n".join(parts)
    return f"""EXECUTION NOTES: this is a single-turn, tool-free job. Read everything below, then submit ONE result object matching the provided JSON schema: {{"items": [...]}}. Do not use any tools. Do not fetch anything. An empty items list is a valid answer.

# Role

You write the "recent work" digest for a personal-brand X account. The account holder is a solo founder who does almost all of their work through Claude sessions. Below are their own sessions from the last {DIGEST_DAYS} days (their messages near-verbatim, assistant replies trimmed). The digest has two consumers:
1. A thread finder that runs your search queries on X to find LIVE public conversations where this person's recent hands-on experience is genuinely relevant.
2. A reply drafter that grounds replies in what they actually did this week (real specifics, not invented ones).

# Pick 0-6 work items

An item is something they actually built, debugged, measured, decided, or learned in these sessions that a stranger in tech would find interesting. Prefer items with concrete specifics: numbers, timelines, failure modes, what was tried, what worked. Merge sessions about the same thing into one item. Skip trivial chores, pure ops noise, and anything only meaningful inside their codebase.

NEVER include (drop the item, or describe it generically):
- credentials, keys, tokens, account numbers, or anything resembling one
- names, handles, or identifying details of customers, users, clients, investors, or other private people
- fundraising, legal, immigration, personal finance, or health details
- security incidents in the founder's own products or accounts (leaked or abused keys, exposed endpoints, revoked tokens, breaches, abuse flags, billing spikes from abuse) and anything that reveals a still-exploitable weakness; drop these items entirely, do not genericize them
- client work that is not clearly the founder's own public product
- harness noise (usage banners, reminders, scheduler chatter)

# Fields

- title: short specific handle (max ~60 chars).
- what: 1-2 sentences, plain first person, what they did and why.
- specifics: the concrete details a reply can cite truthfully (numbers, tools, the exact failure, the outcome). Only facts stated in the transcripts.
- angle: one sentence on the opinion or lesson they could credibly share in a public thread about this.
- queries: 2-3 X search queries that find recent public posts by OTHER people discussing this problem space (not this founder's products by name). These run against only the last ~6 hours of X, so they must be BROAD:
  - Each query combines at most TWO concepts. Never AND three groups together; "(a OR b) (c OR d) (e OR f)" returns nothing.
  - When the work involved a specific named thing people tweet about (a benchmark, model, tool, library, company, launch), make one query just that name, e.g. "AutomationBench lang:en". Named things find the best threads.
  - Use quotes for multi-word phrases and OR for variants. End each with lang:en. No since:, no min_faves:, no from:.
  Good: "\\"agent memory\\" benchmark lang:en", "\\"claude code\\" subagents lang:en", "SWE-bench lang:en". Bad: "\\"CLAUDE.md\\" (memory OR context) (eval OR benchmark) lang:en" (three concepts, zero results).

# Transcripts ({len(sessions)} sessions, most recent first)

{transcripts}

Submit the result object now."""


def _run_digest_job(prompt: str, timeout: int) -> dict | None:
    schema_file = state_dir() / "recent_work.schema.json"
    schema_file.write_text(json.dumps(DIGEST_SCHEMA))
    env = dict(os.environ)
    env["S4L_REPO_DIR"] = str(REPO_DIR)
    cmd = ["bash", str(REPO_DIR / "scripts" / "run_claude.sh"), "recent-work",
           "--json-schema", str(schema_file), "-p"]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              env=env, timeout=timeout + 120)
    except subprocess.TimeoutExpired:
        print("[digest] hard timeout waiting for the queue result", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"[digest] provider failed rc={proc.returncode}", file=sys.stderr)
        sys.stderr.write((proc.stderr or "")[-2000:] + "\n")
        return None
    try:
        envelope = json.loads(proc.stdout[proc.stdout.index("{"):])
        obj = envelope.get("structured_output")
        if obj is None:
            obj = json.loads(envelope.get("result") or "{}")
        return obj if isinstance(obj, dict) else None
    except Exception as e:
        print(f"[digest] could not parse result envelope: {e}", file=sys.stderr)
        return None


def run_digest(ns) -> int:
    if not recent_work_enabled() and not ns.force:
        if ns.if_stale_hours is None:  # stay silent on the per-cycle kick
            print("[digest] recent_work_context flag is off; nothing to do", file=sys.stderr)
        return 0

    if ns.if_stale_hours is not None:
        if load_digest(max_age_hours=ns.if_stale_hours) is not None:
            return 0
        try:
            last = json.loads(_digest_attempt_path().read_text()).get("at", 0)
        except Exception:
            last = 0
        if time.time() - last < DIGEST_RETRY_COOLDOWN_S:
            return 0

    lock = _digest_lock_path()
    try:
        held = int(lock.read_text().strip() or 0)
    except Exception:
        held = 0
    if held and _pid_alive(held):
        print(f"[digest] refresh already in flight (pid {held})", file=sys.stderr)
        return 0

    if ns.background:
        _digest_attempt_path().write_text(json.dumps({"at": time.time()}))
        log = open(state_dir() / "recent_work.log", "a")
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "digest",
             "--days", str(ns.days), "--timeout", str(ns.timeout)]
            + (["--force"] if ns.force else []),
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        print(f"[digest] background refresh started (pid {child.pid})", file=sys.stderr)
        return 0

    lock.write_text(str(os.getpid()))
    _digest_attempt_path().write_text(json.dumps({"at": time.time()}))
    try:
        sessions = gather_sessions(ns.days)
        if not sessions:
            print("[digest] no eligible human sessions in window", file=sys.stderr)
            return 1
        # Every session goes in whole. A window bigger than one job's budget
        # is split across jobs and the items are merged, so nothing is trimmed
        # and nothing is dropped for being older than the newest few sessions.
        batches = chunk_by_budget(sessions, DIGEST_BUDGET)
        print(f"[digest] {datetime.now().isoformat(timespec='seconds')} "
              f"{len(sessions)} sessions in {ns.days}d, "
              f"{sum(len(s['body']) for s in sessions):,} chars -> {len(batches)} job(s)",
              file=sys.stderr)
        if ns.dry_run:
            out = state_dir() / "recent_work.last_prompt.txt"
            prompt = build_digest_prompt(batches[0])
            out.write_text(prompt)
            for n, b in enumerate(batches, 1):
                print(f"[digest]   job {n}/{len(batches)}: {len(b)} sessions, "
                      f"{sum(len(s['body']) for s in b):,} chars", file=sys.stderr)
            print(f"[digest] dry-run: job-1 prompt ({len(prompt):,} chars) -> {out}", file=sys.stderr)
            return 0
        items: list[dict] = []
        seen_titles: set[str] = set()
        digested: list[dict] = []
        for n, batch in enumerate(batches, 1):
            obj = _run_digest_job(build_digest_prompt(batch), ns.timeout)
            if obj is None:
                print(f"[digest] job {n}/{len(batches)} failed; continuing", file=sys.stderr)
                continue
            digested.extend(batch)
            for i in (obj.get("items") or []):
                if not (isinstance(i, dict) and i.get("title")):
                    continue
                key = " ".join(str(i["title"]).lower().split())
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                items.append(i)
        if not digested:
            return 1
        batch = digested
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window_days": ns.days,
            "sessions": [s["session_id"][:8] for s in batch],
            "items": items,
        }
        tmp = digest_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        tmp.replace(digest_path())
        print(f"[digest] wrote {len(items)} items -> {digest_path()}", file=sys.stderr)
        return 0
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def recent_work_queries(projects: list) -> list[dict]:
    """Extra Phase 1 queries (qualified_query_bank shape) for the persona
    project, or [] when the flag is off, the digest is stale, or no persona
    project is in this cycle's selection."""
    if not recent_work_enabled():
        return []
    digest = load_digest()
    if not digest:
        return []
    try:
        import s4l_mode  # noqa: WPS433
        persona = s4l_mode.persona_name()
    except Exception:
        persona = ""
    names = {(p or {}).get("name") for p in projects if isinstance(p, dict)}
    if not persona or persona not in names:
        return []
    # Interleave items (round-robin) into one deduped list, then serve a
    # rotating window of MAX_RECENT_WORK_QUERIES per cycle so EVERY digest
    # query runs, not just the first 8 (2026-11-17: 8 of 16 never ran).
    item_queries = [
        [(it["title"], q) for q in (it.get("queries") or []) if isinstance(q, str) and q.strip()]
        for it in digest.get("items") or []
    ]
    flat, seen = [], set()
    while any(item_queries):
        for qs in item_queries:
            if not qs:
                continue
            title, q = qs.pop(0)
            key = " ".join(q.lower().split())
            if key in seen:
                continue
            seen.add(key)
            flat.append((title, q.strip()))
    if not flat:
        return []
    window = _rotate_window(len(flat), digest.get("generated_at", ""))
    return [
        {
            "project": persona,
            "query": flat[i][1],
            "search_topic": (RECENT_WORK_TOPIC_PREFIX + flat[i][0])[:120],
            "likes": 0, "clicks": 0, "posts": 0,
        }
        for i in window
    ]


def _rotate_window(n: int, digest_id: str) -> list[int]:
    """Indices for this cycle's window and advance the cursor. The cursor
    resets whenever a new digest is generated."""
    path = state_dir() / "recent_work.cursor.json"
    try:
        cur = json.loads(path.read_text())
        start = int(cur.get("next", 0)) if cur.get("digest") == digest_id else 0
    except Exception:
        start = 0
    k = min(MAX_RECENT_WORK_QUERIES, n)
    idx = [(start + j) % n for j in range(k)]
    try:
        path.write_text(json.dumps({"digest": digest_id, "next": (start + k) % n}))
    except OSError:
        pass
    return idx


def recent_work_block() -> str:
    """Prompt block for persona-lane drafts, or '' when off / stale / empty."""
    if not recent_work_enabled():
        return ""
    digest = load_digest()
    items = (digest or {}).get("items") or []
    if not items:
        return ""
    lines = [
        "## RECENT WORK (what the account holder actually did in the last few days)",
        "First-hand, current, and true. A candidate whose `Search query` starts with "
        f"\"{RECENT_WORK_TOPIC_PREFIX.strip()}\" was found by a keyword search for the matching item "
        "below, so the keyword match is NOT evidence of fit. Use an item ONLY when the thread "
        "itself is about that item's subject (the same problem, tool, or claim), so the story "
        "answers what the author actually said. If the thread only shares a keyword (a "
        "changelog, a launch, a product pitch, a different problem), REJECT the candidate as "
        "off-topic instead of bending the story to fit; the reviewer rejects those every time. "
        "Tell each story at most ONCE: if your recent replies already used an item's anecdote, "
        "do not reuse it. Cite only the specifics listed here; do not embellish numbers or "
        "invent outcomes. Never name private people or clients.",
        "",
    ]
    for it in items:
        lines.append(f"- {it.get('title', '').strip()}")
        for key in ("what", "specifics", "angle"):
            val = " ".join(str(it.get(key) or "").split())
            if val:
                lines.append(f"  {key}: {val}")
    return "\n".join(lines) + "\n"


def cmd_queries(ns) -> int:
    try:
        projects = json.loads(sys.stdin.read() or "[]")
    except json.JSONDecodeError:
        projects = []
    qs = recent_work_queries(projects if isinstance(projects, list) else [])
    json.dump(qs, sys.stdout, ensure_ascii=False)
    print()
    return 0


def cmd_show(_ns) -> int:
    d = load_digest(max_age_hours=10**6)
    print(f"flag recent_work_context: {'on' if recent_work_enabled() else 'off'}")
    if not d:
        print("no digest yet")
        return 0
    print(f"generated {d['generated_at']} ({d['age_hours']}h ago, used while <{DIGEST_MAX_AGE_HOURS}h)")
    print(recent_work_block() or "(block empty: flag off, stale, or no items)")
    for it in d.get("items") or []:
        for q in it.get("queries") or []:
            print(f"  query: {q}")
    return 0


# ---------------------------------------------------------------------- CLI


def main() -> int:
    ap = argparse.ArgumentParser(description="mine Claude transcripts into a context corpus")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gather", help="collect sessions, enqueue one mining job, save proposals")
    g.add_argument("--days", type=int, default=3)
    g.add_argument("--budget", type=int, default=DEFAULT_TOTAL_BUDGET)
    g.add_argument("--timeout", type=int, default=1800)
    g.add_argument("--dry-run", action="store_true", help="build the prompt, don't enqueue")
    g.set_defaults(func=run_gather)

    r = sub.add_parser("review", help="print pending proposals")
    r.set_defaults(func=cmd_review)

    a = sub.add_parser("approve", help="apply proposal(s) to the corpus")
    a.add_argument("ids", nargs="+")
    a.set_defaults(func=lambda ns: _decide(ns.ids, "approved"))

    s = sub.add_parser("skip", help="reject proposal(s), never re-pitched")
    s.add_argument("ids", nargs="+")
    s.set_defaults(func=lambda ns: _decide(ns.ids, "skipped"))

    dg = sub.add_parser("digest", help="refresh the recent-work digest (flag-gated)")
    dg.add_argument("--days", type=int, default=DIGEST_DAYS)
    dg.add_argument("--timeout", type=int, default=1800)
    dg.add_argument("--if-stale-hours", type=float, default=None,
                    help="no-op if the digest is younger than this, or an attempt ran in the last hour")
    dg.add_argument("--background", action="store_true", help="detach and return immediately")
    dg.add_argument("--force", action="store_true", help="run even with the flag off (testing)")
    dg.add_argument("--dry-run", action="store_true", help="build the prompt, don't enqueue")
    dg.set_defaults(func=run_digest)

    qq = sub.add_parser("queries", help="PROJECTS_JSON on stdin -> recent-work Phase 1 queries")
    qq.set_defaults(func=cmd_queries)

    sh = sub.add_parser("show", help="print the recent-work digest and block")
    sh.set_defaults(func=cmd_show)

    ns = ap.parse_args()
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
