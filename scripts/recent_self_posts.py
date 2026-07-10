#!/usr/bin/env python3
"""scripts/recent_self_posts.py — cross-cycle anti-repetition context.

Prints a prompt block listing OUR most recent posted replies on a platform
(across ALL projects), so a drafting session can see what this account
already sounds like and deliberately diverge. This is the cross-cycle
complement to author_history_block.py (which is per-author only): before
2026-07-10 the model never saw its own recent output across threads and
kept recycling the same openers and sentence skeletons cycle after cycle.

The block is explicitly NEGATIVE context ("do not sound like these"), the
opposite of the top_performers few-shots. Keep it that way: never add
engagement numbers or any "this one did well" framing here, or the model
will read it as examples to imitate.

Wired into (one callsite):
  - skill/run-twitter-cycle.sh  (Phase 2b-prep PREP_PROMPT)

CLI:
  python3 scripts/recent_self_posts.py --platform twitter --limit 20

Stdout is a ready-to-inject prompt block; EMPTY stdout when there are no
rows or on any failure (the cycle must never block on this context).
Stderr carries diagnostics only.
"""

import argparse
import os
import sys

REPO_DIR = os.path.expanduser("~/social-autoposter")
sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))
from http_api import api_get  # noqa: E402

# Truncation length per reply. Long enough to expose the opener + skeleton
# (the parts the model must avoid repeating), short enough that 20 rows stay
# a small fraction of the prompt.
SNIPPET_CHARS = 220


def _load_active_campaign_suffixes():
    """Best-effort list of active campaign suffix literals to strip.

    Same contract as author_history_block._load_active_campaign_suffixes:
    the block must never teach the model to echo a campaign suffix (the
    tool layer appends its own copy at post time). On failure returns [].
    """
    suffixes = []
    try:
        resp = api_get(
            "/api/v1/campaigns",
            query={"status": "active", "has_suffix": "true", "limit": 500},
        )
        rows = ((resp or {}).get("data") or {}).get("campaigns") or []
        for r in rows:
            s = (r.get("suffix") or "").strip()
            if s and s not in suffixes:
                suffixes.append(s)
    except Exception as e:
        print(f"[recent_self_posts] suffix load failed: {e!r}", file=sys.stderr)
    return suffixes


def _strip_suffixes(text, suffixes):
    """Trailing-only, idempotent strip (mirrors author_history_block)."""
    if not text or not suffixes:
        return text
    cleaned = text.rstrip()
    changed = True
    while changed:
        changed = False
        for sfx in suffixes:
            if sfx and cleaned.endswith(sfx):
                cleaned = cleaned[: -len(sfx)].rstrip()
                changed = True
    return cleaned


def _snippet(text, suffixes):
    """One-line, suffix-stripped, truncated rendering of a reply."""
    t = _strip_suffixes((text or "").strip(), suffixes)
    t = " ".join(t.split())  # collapse newlines/runs of whitespace
    if len(t) > SNIPPET_CHARS:
        t = t[: SNIPPET_CHARS - 1].rstrip() + "…"
    return t


def build_block(platform, limit):
    """Return the prompt block string, or "" when nothing to show."""
    resp = api_get(
        "/api/v1/posts",
        query={
            "platform": platform,
            "status": "active",
            "order_by": "posted_at",
            "order_dir": "desc",
            "limit": str(limit),
        },
    )
    rows = ((resp or {}).get("data") or {}).get("posts") or []
    suffixes = _load_active_campaign_suffixes()
    items = []
    for r in rows:
        snip = _snippet(r.get("our_content"), suffixes)
        if not snip:
            continue
        date = str(r.get("posted_at") or "")[:10]
        proj = r.get("project_name") or "(no project)"
        items.append(f"{len(items) + 1}. [{date} | {proj}] {snip}")
        if len(items) >= limit:
            break
    if not items:
        return ""
    header = (
        "## YOUR RECENT REPLIES (cross-cycle anti-repetition; NEGATIVE examples)\n"
        f"The {len(items)} most recent replies this account posted, all projects. "
        "This is what you ALREADY sound like. It is NOT a list to imitate. "
        "Hard rules for every draft this cycle:\n"
        "- Do NOT reuse any opener below (the first 6-8 words' shape counts, "
        "not just the exact words).\n"
        "- Do NOT reuse their sentence skeletons, rhetorical moves, or pet "
        "phrases (recurring words like 'actually', 'the real X', copula "
        "reframes 'X is the Y').\n"
        "- If a draft you are writing starts to echo any entry below, stop "
        "and rewrite it from a different entry point.\n"
    )
    return header + "\n".join(items)


def main():
    parser = argparse.ArgumentParser(
        description="Render the cross-cycle recent-self-replies prompt block")
    parser.add_argument("--platform", default="twitter")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        block = build_block(args.platform, max(1, min(args.limit, 50)))
    except Exception as e:
        print(f"[recent_self_posts] failed: {e!r}", file=sys.stderr)
        block = ""
    if block:
        print(block)


if __name__ == "__main__":
    main()
