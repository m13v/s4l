#!/usr/bin/env python3
"""Extract all user-authored messages sent on a given date across every Claude
conversation transcript on this machine.

Two storage sources are walked (they do NOT overlap on disk):

  1. claude_code — ~/.claude/projects/<encoded-cwd>/*.jsonl
     Covers the Claude Code CLI (terminal) AND the Claude Code tab in the
     desktop app; both write here, keyed by the real project cwd. Scoped to the
     social-autoposter workspace by default (see WORKSPACE_REPOS /
     INCLUDE_ALL_CLAUDE_CODE_PROJECTS).

  2. cowork — ~/Library/Application Support/Claude/local-agent-mode-sessions/
              **/.claude/projects/**/*.jsonl
     Covers Cowork / local-agent-mode. Each Cowork session runs in a sandboxed
     HOME, so its transcript never lands in ~/.claude/projects. Cowork cannot be
     scoped by cwd (the cwd is the sandbox, e.g. /sessions/loving-laughing-ride),
     so ALL Cowork sessions for the date are included.

The plain chat tab in the desktop app stores conversations server-side
(claude.ai) with only a browser cache locally; there is no local transcript to
walk, so it is out of scope here.

Output: two Markdown files grouped by session, with timestamps + content.

Usage:
  python3 extract_user_messages_today.py            # today (UTC)
  python3 extract_user_messages_today.py 2026-04-21 # a specific UTC date
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()

# --- Source 1: Claude Code (CLI + desktop Code tab) ---------------------------
CLAUDE_CODE_PROJECTS_ROOT = HOME / ".claude" / "projects"
# Repos whose Claude Code transcripts we care about. Flip
# INCLUDE_ALL_CLAUDE_CODE_PROJECTS = True to walk every project instead.
WORKSPACE_REPOS = [HOME / "social-autoposter"]
INCLUDE_ALL_CLAUDE_CODE_PROJECTS = False

# --- Source 2: Cowork / local agent mode (desktop app, sandboxed HOMEs) -------
COWORK_SESSIONS_ROOT = (
    HOME / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
)

OUT_DIR = Path(__file__).resolve().parent


def _encode_cwd(path: Path) -> str:
    """Claude Code encodes a project cwd into its projects/ folder name by
    replacing every '/' with '-'."""
    return str(path).replace("/", "-")


def resolve_date(argv: list[str]) -> str:
    if len(argv) > 1 and argv[1].strip():
        d = argv[1].strip()
        # validate shape; raises if malformed
        datetime.strptime(d, "%Y-%m-%d")
        return d
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def iter_claude_code_session_files():
    """Yield .jsonl transcripts from the Claude Code projects root."""
    if not CLAUDE_CODE_PROJECTS_ROOT.is_dir():
        return
    if INCLUDE_ALL_CLAUDE_CODE_PROJECTS:
        for p in sorted(CLAUDE_CODE_PROJECTS_ROOT.glob("*/*.jsonl")):
            yield p
        return
    for repo in WORKSPACE_REPOS:
        proj_dir = CLAUDE_CODE_PROJECTS_ROOT / _encode_cwd(repo)
        if proj_dir.is_dir():
            for p in sorted(proj_dir.glob("*.jsonl")):
                yield p


def iter_cowork_session_files():
    """Yield .jsonl transcripts from every Cowork sandbox HOME."""
    if not COWORK_SESSIONS_ROOT.is_dir():
        return
    # **/.claude/projects/**/*.jsonl catches every account/workspace/session.
    for p in sorted(COWORK_SESSIONS_ROOT.glob("**/.claude/projects/**/*.jsonl")):
        yield p


def content_to_text(content) -> str | None:
    """Return the textual user input for a 'user' entry, or None if this is a
    tool_result / non-user payload that should be skipped."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # A list is either a tool_result block (skip) or a list of text blocks.
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                return None  # this is a tool response, not a user message
            if btype == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
        if texts:
            return "\n".join(texts)
    return None


def classify(text: str) -> str:
    """Tag a user-role message by origin:
    HUMAN       - you typed it in the terminal / chat composer
    COMMAND     - slash-command invocation (<command-name>...)
    TASK_NOTIF  - background Task tool wake-up (<task-notification>)
    CMD_STDOUT  - output of a ! bang-command echoed back (<local-command-stdout>)
    SCHED_WAKE  - autonomous loop / scheduled wake-up sentinel
    SYS_REMIND  - pure <system-reminder> block injected by the harness
    HOOK        - user-prompt-submit-hook injection
    """
    stripped = text.lstrip()
    if stripped.startswith("<task-notification>"):
        return "TASK_NOTIF"
    if stripped.startswith("<command-name>") or stripped.startswith("<command-message>"):
        return "COMMAND"
    if stripped.startswith("<local-command-stdout>") or stripped.startswith("<local-command-stderr>"):
        return "CMD_STDOUT"
    if "<<autonomous-loop" in stripped or stripped.startswith("<loop-"):
        return "SCHED_WAKE"
    if stripped.startswith("<user-prompt-submit-hook>"):
        return "HOOK"
    # A message that is ONLY system-reminder blocks (nothing else) is harness-injected.
    if stripped.startswith("<system-reminder>"):
        without = re.sub(r"<system-reminder>.*?</system-reminder>", "", stripped, flags=re.DOTALL).strip()
        if not without:
            return "SYS_REMIND"
    return "HUMAN"


def extract_from_file(path: Path, source: str, date: str):
    msgs = []
    session_meta = {
        "session_id": path.stem,
        "source": source,
        "path": str(path),
        "cwd": None,
        "first_ts": None,
        "last_ts": None,
    }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = d.get("timestamp")
                if ts and session_meta["first_ts"] is None:
                    session_meta["first_ts"] = ts
                if ts:
                    session_meta["last_ts"] = ts

                if session_meta["cwd"] is None:
                    cwd = d.get("cwd")
                    if cwd:
                        session_meta["cwd"] = cwd

                if d.get("type") != "user":
                    continue
                msg = d.get("message") or {}
                if msg.get("role") != "user":
                    continue
                if not ts or not ts.startswith(date):
                    continue

                text = content_to_text(msg.get("content"))
                if text is None:
                    continue
                text = text.strip()
                if not text:
                    continue

                msgs.append(
                    {
                        "timestamp": ts,
                        "promptId": d.get("promptId"),
                        "parentUuid": d.get("parentUuid"),
                        "isSidechain": d.get("isSidechain", False),
                        "kind": classify(text),
                        "text": text,
                    }
                )
    except OSError:
        return session_meta, []

    return session_meta, msgs


def collect_sessions(date: str):
    """Walk both sources, returning a list of (meta, msgs) for sessions that had
    user activity on `date`, deduped by (source, session_id)."""
    seen: set[tuple[str, str]] = set()
    sessions = []
    sources = (
        ("claude_code", iter_claude_code_session_files()),
        ("cowork", iter_cowork_session_files()),
    )
    for source, files in sources:
        for path in files:
            key = (source, path.stem)
            if key in seen:
                continue
            seen.add(key)
            meta, msgs = extract_from_file(path, source, date)
            if not msgs:
                continue
            msgs.sort(key=lambda m: m["timestamp"])
            sessions.append((meta, msgs))
    # sort sessions by earliest message
    sessions.sort(key=lambda s: s[1][0]["timestamp"])
    return sessions


def main():
    date = resolve_date(sys.argv)
    out_full = OUT_DIR / f"claude_user_messages_{date}.md"
    out_trimmed = OUT_DIR / f"claude_user_messages_{date}.interactive.md"

    sessions = collect_sessions(date)
    total_msgs = sum(len(msgs) for _m, msgs in sessions)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # tallies
    kind_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    source_session_counts: dict[str, int] = {}
    for meta, msgs in sessions:
        source_session_counts[meta["source"]] = source_session_counts.get(meta["source"], 0) + 1
        for m in msgs:
            kind_counts[m["kind"]] = kind_counts.get(m["kind"], 0) + 1
            source_counts[meta["source"]] = source_counts.get(meta["source"], 0) + 1

    scope = "ALL projects" if INCLUDE_ALL_CLAUDE_CODE_PROJECTS else ", ".join(str(r) for r in WORKSPACE_REPOS)

    lines: list[str] = []
    lines.append(f"# User messages for {date}")
    lines.append("")
    lines.append("Sources walked:")
    lines.append(f"- `claude_code` — `~/.claude/projects/` (CLI + desktop Code tab), scope: {scope}")
    lines.append(f"- `cowork` — `~/Library/Application Support/Claude/local-agent-mode-sessions/` (all sessions; not project-scopable)")
    lines.append("")
    lines.append(f"- Sessions with activity on date: **{len(sessions)}**"
                 + (f" ({', '.join(f'{s}={n}' for s, n in sorted(source_session_counts.items()))})" if source_session_counts else ""))
    lines.append(f"- Total user-role messages (excluding tool results): **{total_msgs}**")
    if source_counts:
        for s, n in sorted(source_counts.items()):
            lines.append(f"  - source `{s}`: {n}")
    for k in ("HUMAN", "COMMAND", "TASK_NOTIF", "CMD_STDOUT", "SCHED_WAKE", "SYS_REMIND", "HOOK"):
        if k in kind_counts:
            lines.append(f"  - kind `{k}`: {kind_counts[k]}")
    lines.append(f"- Generated: {generated_at}")
    lines.append("")
    lines.append("Each section below is one session. Messages are in chronological order.")
    lines.append("Tool-result blocks (role=user but produced by the harness) are excluded.")
    lines.append("")
    lines.append("Message **kind** tags:")
    lines.append("- `HUMAN` — typed by you (or fed non-interactively as the top-level prompt to `claude -p`)")
    lines.append("- `COMMAND` — slash-command invocation wrapper (`<command-name>...`)")
    lines.append("- `TASK_NOTIF` — background Task tool wake-up event")
    lines.append("- `CMD_STDOUT` — bang-command stdout/stderr echoed back into the conversation")
    lines.append("- `SCHED_WAKE` — autonomous loop or scheduled wake-up sentinel")
    lines.append("- `SYS_REMIND` — pure `<system-reminder>` injection (harness, not you)")
    lines.append("- `HOOK` — user-prompt-submit-hook injection")
    lines.append("")
    lines.append("To see only what you actually typed: grep for `kind=HUMAN` in this file.")
    lines.append("")
    lines.append("---")
    lines.append("")

    for idx, (meta, msgs) in enumerate(sessions, 1):
        lines.append(f"## Session {idx}: `{meta['session_id']}` (source: {meta['source']})")
        lines.append("")
        lines.append(f"- File: `{meta['path']}`")
        if meta.get("cwd"):
            lines.append(f"- cwd: `{meta['cwd']}`")
        lines.append(f"- First entry: `{meta['first_ts']}`")
        lines.append(f"- Last entry: `{meta['last_ts']}`")
        lines.append(f"- User-role messages on date: **{len(msgs)}**")
        kc: dict[str, int] = {}
        for m in msgs:
            kc[m["kind"]] = kc.get(m["kind"], 0) + 1
        lines.append(f"- Breakdown: {', '.join(f'{k}={v}' for k, v in sorted(kc.items()))}")
        lines.append("")

        for i, m in enumerate(msgs, 1):
            sc = " (sidechain)" if m.get("isSidechain") else ""
            lines.append(f"### [{i}] {m['timestamp']} — kind={m['kind']}{sc}")
            if m.get("promptId"):
                lines.append(f"`promptId={m['promptId']}`")
            lines.append("")
            lines.append("```")
            lines.append(m["text"])
            lines.append("```")
            lines.append("")

        lines.append("---")
        lines.append("")

    out_full.parent.mkdir(parents=True, exist_ok=True)
    out_full.write_text("\n".join(lines), encoding="utf-8")

    print(f"wrote {out_full}")
    print(f"sessions: {len(sessions)} ({', '.join(f'{s}={n}' for s, n in sorted(source_session_counts.items())) or 'none'})")
    print(f"user messages: {total_msgs}")

    # --- Trimmed output: interactive human messages only ---
    # Heuristic: a claude_code session is "interactive" if it has >=2 distinct
    # HUMAN promptIds. Sessions with a single HUMAN promptId are almost always
    # `claude -p` script fires from skill/run-*.sh. Cowork sessions carry no
    # promptId and have no script-fire concept, so they are kept whenever they
    # contain any HUMAN message. Inside kept sessions we still drop non-HUMAN kinds.
    interactive_sessions = []
    trimmed_total = 0
    for meta, msgs in sessions:
        human_only = [m for m in msgs if m["kind"] == "HUMAN"]
        if not human_only:
            continue
        if meta["source"] == "claude_code":
            human_prompt_ids = {m["promptId"] for m in human_only if m.get("promptId")}
            if len(human_prompt_ids) < 2:
                continue
        # cowork: keep as long as there's a HUMAN message
        interactive_sessions.append((meta, human_only))
        trimmed_total += len(human_only)

    tl: list[str] = []
    tl.append(f"# Interactive human messages for {date}")
    tl.append("")
    tl.append(f"- Sources: `claude_code` (scope: {scope}) + `cowork` (all)")
    tl.append(f"- Filter: claude_code sessions need ≥2 distinct HUMAN `promptId`s (proxy for interactive);")
    tl.append(f"  cowork sessions are kept whenever they contain any HUMAN message (no script-fires there)")
    tl.append(f"- Kept sessions: **{len(interactive_sessions)}** / {len(sessions)}")
    tl.append(f"- Kept messages: **{trimmed_total}** / {total_msgs}")
    tl.append(f"- Generated: {generated_at}")
    tl.append("")
    tl.append("Note: `claude -p` non-interactive script invocations (skill/run-*.sh) fire a single")
    tl.append("templated prompt per session, so they have exactly one HUMAN promptId and are")
    tl.append("filtered out here. Any session that kept you in a back-and-forth conversation survived.")
    tl.append("")
    tl.append("---")
    tl.append("")
    for idx, (meta, msgs) in enumerate(interactive_sessions, 1):
        tl.append(f"## Session {idx}: `{meta['session_id']}` (source: {meta['source']})")
        tl.append("")
        if meta.get("cwd"):
            tl.append(f"- cwd: `{meta['cwd']}`")
        tl.append(f"- First entry: `{meta['first_ts']}`")
        tl.append(f"- Last entry: `{meta['last_ts']}`")
        tl.append(f"- HUMAN messages on date: **{len(msgs)}**")
        tl.append("")
        for i, m in enumerate(msgs, 1):
            tl.append(f"### [{i}] {m['timestamp']}")
            tl.append("")
            tl.append("```")
            tl.append(m["text"])
            tl.append("```")
            tl.append("")
        tl.append("---")
        tl.append("")

    out_trimmed.write_text("\n".join(tl), encoding="utf-8")
    print(f"wrote {out_trimmed}")
    print(f"interactive sessions: {len(interactive_sessions)}")
    print(f"interactive HUMAN messages: {trimmed_total}")


if __name__ == "__main__":
    main()
