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
PER_USER_MSG_CAP = 2500
PER_ASSISTANT_MSG_CAP = 700
PER_SESSION_CAP = 14_000
DEFAULT_TOTAL_BUDGET = 80_000

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
                    turns.append("USER: " + text[:PER_USER_MSG_CAP])
                elif d.get("type") == "assistant" and role == "assistant":
                    text = _text_blocks(msg.get("content"))
                    if not text:
                        continue
                    text = text.strip()
                    if len(text) < 40:  # skip one-liner tool narration
                        continue
                    turns.append("ASSISTANT: " + text[:PER_ASSISTANT_MSG_CAP])
    except OSError:
        return None

    if human_count < MIN_HUMAN_MSGS:
        return None
    if cwd and any(m in cwd for m in EXCLUDED_CWD_MARKERS):
        return None

    body = "\n\n".join(turns)
    if len(body) > PER_SESSION_CAP:
        # keep the head and the tail; the middle is elided
        half = PER_SESSION_CAP // 2
        body = body[:half] + "\n\n[... middle of session elided ...]\n\n" + body[-half:]
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
                "required": ["action", "text", "why", "source_session", "source_date"],
                "properties": {
                    "action": {"type": "string", "enum": ["add", "revise"]},
                    "revises_line": {"type": ["integer", "null"]},
                    "text": {"type": "string"},
                    "why": {"type": "string"},
                    "quote": {"type": "string"},
                    "source_session": {"type": "string"},
                    "source_date": {"type": "string"},
                },
            },
        }
    },
}


def build_prompt(sessions: list[dict], pending_props: list[dict] | None = None) -> str:
    corpus_lines = read_corpus_lines()
    corpus_block = (
        "\n".join(f"[{i + 1}] {ln}" for i, ln in enumerate(corpus_lines))
        if corpus_lines
        else "(the corpus is currently empty)"
    )
    considered = [
        {"status": r.get("status"), "text": r.get("text", "")} for r in read_ledger()[-60:]
    ] + [{"status": "pending", "text": p.get("text", "")} for p in (pending_props or [])]
    considered_block = (
        "\n".join(f"- ({r['status']}) {r['text'][:200]}" for r in considered)
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

You are the daily context miner for a personal "context corpus": a numbered list of durable, one-line memories distilled from the user's own Claude conversations. Each line is an insight worth keeping and potentially worth sharing with the world: a specific experience, a hard-won conclusion, a non-obvious observation, or a concrete data point.

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
right answer is 0 or 1. An empty proposals list is a valid, common answer. For each proposal:
- action: "add" for a new line, or "revise" when a new learning supersedes an existing corpus line (set revises_line to that line's number).
- text: the corpus line itself. One sentence, self-contained, max ~300 chars, written in the user's plainspoken voice. No hashtags, no em dashes.
- why: one sentence on why this is worth keeping (what makes it non-obvious or shareable).
- quote: a short supporting excerpt from the transcript (redact any secrets).
- source_session: the 8-char session id from the transcript header.
- source_date: the session date (YYYY-MM-DD).

Submit the result object now."""


# -------------------------------------------------------------------- queue


def run_gather(ns) -> int:
    sessions, dropped = gather_sessions(ns.days, ns.budget)
    if not sessions:
        print(json.dumps({"ok": False, "reason": "no_eligible_sessions", "days": ns.days}))
        return 1
    prompt = build_prompt(sessions)
    total_chars = len(prompt)
    print(
        f"[gather] {len(sessions)} sessions in window ({ns.days}d), "
        f"{dropped} dropped over budget, prompt {total_chars:,} chars",
        file=sys.stderr,
    )
    for s in sessions:
        print(
            f"[gather]   {s['session_id'][:8]} {s['first_ts'][:16]} "
            f"human_msgs={s['human_msgs']} chars={len(s['body']):,} cwd={s['cwd']}",
            file=sys.stderr,
        )

    if ns.dry_run:
        out = state_dir() / "last_prompt.txt"
        out.write_text(prompt)
        print(f"[gather] dry-run: prompt written to {out}, nothing enqueued", file=sys.stderr)
        return 0

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
    print(f"[gather] enqueuing context-mining job (timeout {ns.timeout}s)...", file=sys.stderr)
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
        return 79

    if proc.returncode == 79:
        print(
            "[gather] queue timed out (rc=79): no worker claimed the job. Is Claude "
            "Desktop open and the s4l-worker task firing?",
            file=sys.stderr,
        )
        return 79
    if proc.returncode != 0:
        print(f"[gather] provider failed rc={proc.returncode}", file=sys.stderr)
        sys.stderr.write((proc.stderr or "")[-2000:] + "\n")
        return proc.returncode

    try:
        envelope = json.loads(proc.stdout[proc.stdout.index("{"):])
        obj = envelope.get("structured_output")
        if obj is None:
            obj = json.loads(envelope.get("result") or "{}")
    except Exception as e:
        print(f"[gather] could not parse result envelope: {e}", file=sys.stderr)
        sys.stderr.write((proc.stdout or "")[-2000:] + "\n")
        return 1

    proposals = obj.get("proposals") or []
    stamped = []
    for p in proposals:
        pid = "cm-" + hashlib.sha1(
            (p.get("text", "") + p.get("source_session", "")).encode()
        ).hexdigest()[:8]
        p["id"] = pid
        p["mined_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stamped.append(p)
    pending_path().write_text(json.dumps({"proposals": stamped}, indent=2, ensure_ascii=False))
    print(
        f"[gather] job done: {len(stamped)} proposals -> {pending_path()}", file=sys.stderr
    )
    cmd_review(None)
    return 0


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
            head += f"  REVISES [{n}]: {old[:120]}"
        else:
            head += "  ADD"
        print(head)
        print(f"  text : {p.get('text', '')}")
        print(f"  why  : {p.get('why', '')}")
        if p.get("quote"):
            print(f"  quote: {p['quote'][:220]}")
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
            if p.get("action") == "revise" and p.get("revises_line"):
                n = p["revises_line"]
                if 0 < n <= len(corpus_lines):
                    corpus_lines[n - 1] = p["text"]
                else:
                    corpus_lines.append(p["text"])
            else:
                corpus_lines.append(p["text"])
        append_ledger(
            {
                "id": pid,
                "status": status,
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

    ns = ap.parse_args()
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
