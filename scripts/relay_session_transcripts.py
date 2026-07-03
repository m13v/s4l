#!/usr/bin/env python3
"""relay_session_transcripts.py — ship Claude session transcripts to the relay.

Why
---
The Cloud Logging relay (bin/server.js /api/v1/installations/logs) carries
subprocess output and tool-call events, but NOT what the Claude sessions on a
user's box actually said/did: the scheduled queue-worker sessions (`claude -p`
runs under ~/.s4l-worker) and any Code-tab / CLI sessions in the s4l repos.
When Karol's setup stalled on 2026-07-03 the single most useful artifact (the
session transcript) only existed on his Mac. This script closes that gap: it
incrementally tails the session .jsonl transcripts Claude Code writes under
~/.claude/projects/<encoded-cwd>/<session-id>.jsonl, compacts each message to
a bounded record, and POSTs them through the SAME relay lane the pipeline logs
use (X-Installation auth, no GCP creds on the client). Query in Log Explorer:

    jsonPayload.install_id="<uuid>" AND jsonPayload.context:"transcript:"

Privacy scope: ONLY transcripts whose encoded project dir looks s4l-related
(social-autoposter repos, the ~/.s4l-worker scheduled-task dir) are relayed.
An operator/dev Mac has many unrelated personal sessions under
~/.claude/projects; those never match and are never read. Override the match
with S4L_TRANSCRIPT_DIR_RE (a Python regex over the encoded dir name).

Design
------
- Durable per-file byte offsets in ~/.social-autoposter-mcp/transcript-relay-
  state.json, so each run ships only NEW lines (safe to run every few minutes).
- Only complete lines are consumed; a partial trailing line (session mid-write)
  waits for the next run.
- Message VALUES are truncated hard (text 1500 chars, tool_result 400) and the
  whole relay line is capped, so a pathological session can't flood the lane.
- Global per-run line cap (--max-lines); the remainder ships on the next run.
- Best-effort everywhere: a malformed record, unreadable file, or POST failure
  never raises out of main(); offsets only advance for lines actually accepted.

Called every 5 minutes by the MCP server (mcp/src/index.ts) while Claude
Desktop is open. Also runnable by hand:

    python3 scripts/relay_session_transcripts.py --dry-run
    python3 scripts/relay_session_transcripts.py --max-lines 200
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")
STATE_DIR = os.path.expanduser(
    os.environ.get("S4L_STATE_DIR", "~/.social-autoposter-mcp")
)
STATE_PATH = os.path.join(STATE_DIR, "transcript-relay-state.json")
LOCK_PATH = os.path.join(STATE_DIR, "transcript-relay.lock")

# Cloud Run relay host (NOT the Vercel API host) — same split as telemetry.ts.
LOG_BASE = (
    os.environ.get("AUTOPOSTER_LOG_BASE") or "https://app.s4l.ai"
).rstrip("/")

# Which encoded project dirs are in scope. The encoded name is the session cwd
# with "/" -> "-" (e.g. "-Users-karolzdebel--s4l-worker",
# "-Users-x-social-autoposter"). Everything else on the box is out of scope.
DIR_RE = re.compile(
    os.environ.get("S4L_TRANSCRIPT_DIR_RE") or r"(social-autoposter|s4l-worker|-s4l\b)",
    re.IGNORECASE,
)

MAX_FILE_AGE_DAYS = 14  # ignore transcripts older than this (state stays lean)
MAX_TEXT = 1500         # per-message text excerpt
MAX_TOOL_RESULT = 400   # per tool_result excerpt
MAX_LINE = 7500         # relay caps at 8192; leave headroom for the envelope
POST_BATCH = 200        # relay accepts 1-200 lines per POST


def _install_header() -> str | None:
    """Mint the X-Installation header via identity.py (same lane as telemetry)."""
    ident = os.path.join(os.path.dirname(os.path.abspath(__file__)), "identity.py")
    if not os.path.exists(ident):
        return None
    try:
        out = subprocess.run(
            [sys.executable, ident, "header"],
            capture_output=True, text=True, timeout=15,
        )
        header = (out.stdout or "").strip()
        return header if out.returncode == 0 and header else None
    except Exception:
        return None


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def _content_parts(content) -> tuple[str, list[str], str]:
    """Flatten a message content field -> (text, tool_use names, tool_result excerpt)."""
    texts: list[str] = []
    tools: list[str] = []
    tool_result = ""
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for blk in content:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text" and isinstance(blk.get("text"), str):
                texts.append(blk["text"])
            elif btype == "thinking":
                # Thinking blocks are internal; note presence, don't ship content.
                tools.append("(thinking)")
            elif btype == "tool_use":
                name = blk.get("name")
                if isinstance(name, str) and name:
                    tools.append(name)
            elif btype == "tool_result":
                inner = blk.get("content")
                if isinstance(inner, str):
                    tool_result = inner
                elif isinstance(inner, list):
                    tr_texts = [
                        b.get("text") for b in inner
                        if isinstance(b, dict) and isinstance(b.get("text"), str)
                    ]
                    tool_result = "\n".join(t for t in tr_texts if t)
    return "\n".join(t for t in texts if t), tools, tool_result


def _compact(rec: dict) -> dict | None:
    """One transcript JSONL record -> one bounded relay record (or None to skip)."""
    rtype = rec.get("type")
    if rtype == "summary":
        title = rec.get("summary")
        return {"t": "summary", "text": str(title)[:300]} if title else None
    if rtype not in ("user", "assistant", "system"):
        return None  # progress/queue noise etc.
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    role = msg.get("role") or rtype
    text, tools, tool_result = _content_parts(msg.get("content"))
    out: dict = {"t": role}
    if text:
        out["text"] = text[:MAX_TEXT]
    if tools:
        out["tools"] = tools[:20]
    if tool_result:
        out["tool_result"] = tool_result[:MAX_TOOL_RESULT]
    model = msg.get("model")
    if isinstance(model, str) and model:
        out["model"] = model
    ts = rec.get("timestamp")
    if isinstance(ts, str) and ts:
        out["ts"] = ts
    if not (out.get("text") or out.get("tools") or out.get("tool_result")):
        return None  # empty envelope (e.g. bare system record)
    return out


def _post(lines: list[dict], header: str) -> bool:
    body = json.dumps({"lines": lines}).encode("utf-8")
    req = urllib.request.Request(
        f"{LOG_BASE}/api/v1/installations/logs",
        data=body,
        headers={"X-Installation": header, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[transcript-relay] POST failed: {e}", file=sys.stderr)
        return False


def _candidate_files() -> list[str]:
    cutoff = time.time() - MAX_FILE_AGE_DAYS * 86400
    out = []
    for path in glob.glob(os.path.join(PROJECTS_ROOT, "*", "*.jsonl")):
        proj_dir = os.path.basename(os.path.dirname(path))
        if not DIR_RE.search(proj_dir):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        out.append(path)
    # Oldest-modified first so a busy box drains its backlog in order.
    out.sort(key=lambda p: os.path.getmtime(p))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-lines", type=int, default=600,
                    help="Global cap on relay lines shipped this run (default 600); "
                         "the remainder ships on the next run.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the relay lines instead of POSTing; offsets are NOT advanced.")
    args = ap.parse_args()

    # Single-flight: overlapping runs (boot + interval) must not double-ship.
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[transcript-relay] another run holds the lock; exiting", file=sys.stderr)
        return 0

    header = None
    if not args.dry_run:
        header = _install_header()
        if not header:
            print("[transcript-relay] no installation identity yet; exiting", file=sys.stderr)
            return 0

    state = _load_state()
    files = _candidate_files()
    budget = max(1, args.max_lines)
    shipped = 0
    files_touched = 0
    pending: list[dict] = []
    # (path, new_offset) applied only after the batch containing its lines ships.
    offset_updates: dict[str, int] = {}

    def flush() -> bool:
        nonlocal shipped
        if not pending:
            return True
        if args.dry_run:
            for ln in pending:
                print(json.dumps(ln, ensure_ascii=False))
        else:
            if not _post(list(pending), header):
                return False
            for pth, off in offset_updates.items():
                st = state.get(pth) or {}
                st["offset"] = off
                state[pth] = st
            _save_state(state)
        shipped += len(pending)
        pending.clear()
        offset_updates.clear()
        return True

    for path in files:
        if budget - shipped - len(pending) <= 0:
            break
        session_id = os.path.splitext(os.path.basename(path))[0]
        proj_dir = os.path.basename(os.path.dirname(path))
        st = state.get(path) or {}
        offset = int(st.get("offset") or 0)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size < offset:
            offset = 0  # truncated/rewritten; start over
        if size == offset:
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read(4 * 1024 * 1024)  # 4MB per file per run is plenty
        except OSError:
            continue
        # Consume only complete lines; the tail waits for the next run.
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            continue
        consumed = chunk[: last_nl + 1]
        emitted_any = False
        # split() on data ending in \n yields a trailing empty artifact; drop it
        # so every remaining element accounts for exactly len(raw)+1 bytes.
        for raw in consumed.split(b"\n")[:-1]:
            if budget - shipped - len(pending) <= 0:
                # Out of budget mid-file: offset stays at the last consumed line.
                break
            offset += len(raw) + 1
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
            compact = _compact(rec)
            if not compact:
                continue
            compact["dir"] = proj_dir[:80]
            line = json.dumps(compact, ensure_ascii=False)
            pending.append({
                "ts": compact.get("ts") or None,
                "stream": "stdout",
                "line": line[:MAX_LINE],
                "context": f"transcript:{session_id}",
            })
            emitted_any = True
            if len(pending) >= POST_BATCH:
                offset_updates[path] = offset
                if not flush():
                    return 0  # POST failing: stop; offsets already saved per-batch
        offset_updates[path] = offset
        if emitted_any or offset != int(st.get("offset") or 0):
            files_touched += 1

    if not flush():
        return 0
    if args.dry_run and offset_updates:
        # dry-run never persists offsets, but surface what WOULD advance.
        print(f"[transcript-relay] dry-run: would advance {len(offset_updates)} offset(s)",
              file=sys.stderr)
    elif not args.dry_run and offset_updates:
        for pth, off in offset_updates.items():
            st = state.get(pth) or {}
            st["offset"] = off
            state[pth] = st
        _save_state(state)

    print(f"[transcript-relay] shipped={shipped} files={files_touched} "
          f"candidates={len(files)}"
          + (" [dry-run]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # best-effort lane: never crash the caller
        print(f"[transcript-relay] fatal (suppressed): {e}", file=sys.stderr)
        raise SystemExit(0)
