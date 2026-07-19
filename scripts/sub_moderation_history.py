#!/usr/bin/env python3
"""Per-subreddit moderation history from the review_events ledger.

This is the READ path of the strike feedback loop. The WRITE path
(platform_strike_events.py, 2026-07-18) stores every confirmed removal of our
posts as a platform_removed event whose reject_note carries the live thread's
moderation context: removed_by_category, mod-rewritten flair, and the mod
team's stated reason ("Posts must directly relate to JavaScript", "This
content appears to be AI generated", ...). Account bans land as
platform_banned events.

This module surfaces those stored verdicts for ONE sub so the thread-drafting
prompt (run-reddit-threads.sh) can inject them at the rules-check step. The
drafting agent's historical miss mode is judging its own post on-topic when
the mods disagree; a past verdict from the same mods is the calibration the
rules page alone cannot provide. This matters most when a 30-day quarantine
(pick_thread_target.load_thread_blocked_subs) expires and the sub becomes
eligible again: the retry sees exactly why the sub removed us last time.

Usage:
    python3 scripts/sub_moderation_history.py javascript          # formatted block
    python3 scripts/sub_moderation_history.py r/rust --json
    python3 scripts/sub_moderation_history.py rust --max 5

Prints NOTHING (exit 0) when the sub has no history, so shell callers can
interpolate the output directly into a prompt.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, load_env  # noqa: E402

FETCH_LIMIT = 1000  # review-events GET hard cap


def _norm_sub(sub: str) -> str:
    s = (sub or "").strip()
    m = re.search(r"/r/([^/\s]+)", s)
    if m:
        s = m.group(1)
    return s.strip().strip("/").lower()


def history_for_sub(sub: str, max_events: int = 8) -> list[dict]:
    """Return up to max_events moderation events for this sub, newest first.

    Each item: {"decision", "reject_note", "thread_url", "created_at",
    "project"}. Matches by thread_url path (/r/<sub>/) with a reject_note
    "in r/<sub>" fallback for ban events whose thread_url is the sub root.
    Empty list on no history OR on API failure (callers degrade gracefully;
    a missing history block never blocks a post attempt).
    """
    slug = _norm_sub(sub)
    if not slug:
        return []
    try:
        resp = api_get("/api/v1/review-events", {
            "platform": "reddit",
            "unprocessed": "false",
            "limit": str(FETCH_LIMIT),
        })
    except Exception:
        return []
    events = ((resp or {}).get("data") or {}).get("events") or []
    pat = re.compile(rf"(reddit\.com/r/|(^|\s)in r/){re.escape(slug)}(/|\b)",
                     re.IGNORECASE)
    hits = []
    for e in events:
        if not str(e.get("decision") or "").startswith("platform_"):
            continue
        hay = f"{e.get('thread_url') or ''} {e.get('reject_note') or ''}"
        if not pat.search(hay):
            continue
        hits.append({
            "decision": e.get("decision"),
            "reject_note": e.get("reject_note"),
            "thread_url": e.get("thread_url"),
            "created_at": str(e.get("created_at") or ""),
            "project": e.get("project"),
        })
    hits.sort(key=lambda h: h["created_at"], reverse=True)
    return hits[:max_events]


def format_block(sub: str, events: list[dict]) -> str:
    """Render events as a prompt-ready block. Empty string when no events."""
    if not events:
        return ""
    slug = _norm_sub(sub)
    lines = [
        f"## PAST MODERATION VERDICTS in r/{slug} (hard evidence, ledger-sourced)",
        "Mods of this sub previously removed our posts or banned the account."
        " Treat each stated reason below as the mods' definition of off-topic,"
        " which OVERRIDES your own on-topic judgment. If your planned post"
        " matches any removed pattern, ABORT (set abort_reason) instead of"
        " posting a variation of it.",
    ]
    for e in events:
        when = e["created_at"][:10]
        note = re.sub(r"\s+", " ", e.get("reject_note") or "").strip()
        lines.append(f"- [{when}] {e['decision']}: {note[:600]}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sub", help="subreddit name or r/x form")
    ap.add_argument("--max", type=int, default=8, help="max events (default 8)")
    ap.add_argument("--json", action="store_true", help="raw JSON instead of prompt block")
    args = ap.parse_args()
    load_env()
    events = history_for_sub(args.sub, args.max)
    if args.json:
        print(json.dumps(events, indent=2, default=str))
    else:
        block = format_block(args.sub, events)
        if block:
            print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
