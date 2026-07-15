#!/usr/bin/env python3
"""engagement_signal_feedback.py — turn downstream reply engagement into two
durable, whitelist-enforced learned_preferences_global entries:

1. Warm-signal authors: repeat, constructive, low-risk authors (matching the
   same repeat_constructive_author shape reply_risk_digest.py already tags)
   get promoted into `audience_prefer` so future judging treats them as a
   positive signal instead of getting rediscovered from scratch every day.

2. Style performance: engagement_style values with enough sample size are
   ranked by child-reply continuation rate (the strongest observed signal
   for "this reply kept the thread going"); the current leader is recorded
   as a `draft_style_notes` entry so the drafting model actually defaults to
   the pattern that's been working, not just whatever the digest narrates in
   a one-off email.

Both write through learned_preferences.apply_mutations(), the SAME
whitelist-enforced, flock+backup+atomic-replace writer the human review-card
feedback loop uses (see scripts/learned_preferences.py). This script is a
second, independent caller of that writer, not a new writer: the whitelist
enforcement lives in apply_mutations() itself, so nothing here can touch
config.json outside audience_prefer / draft_style_notes.

All DB reads go through the s4l.ai HTTP API (http_api), never direct SQL, so
this script works unmodified on shipped customer installs, not just this
operator box. Risk-scoring reuses reply_risk_digest._skip_reason_risk_score
by import (read-only; that file is intentionally not modified here) instead
of forking a second copy of the skip-risk heuristics.

Usage:
    python3 scripts/engagement_signal_feedback.py --dry-run
    python3 scripts/engagement_signal_feedback.py --platform x --days 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = REPO_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from db import load_env  # noqa: E402
from http_api import api_get  # noqa: E402
from learned_preferences import apply_mutations, get_global_block  # noqa: E402
from reply_risk_digest import _skip_reason_risk_score  # noqa: E402

DEFAULT_DAYS = 30
MIN_STYLE_SAMPLE = 10
WARM_MIN_LAST_30D = 4
WARM_MIN_CHILD_REPLIES = 1
MAX_WARM_PROMOTIONS_PER_RUN = 3
ENTRY_CHAR_LIMIT = 200  # mirrors learned_preferences.MAX_ENTRY_CHARS


def fetch_window_rows(platform: str, days: int, limit_pages: int = 20) -> list[dict]:
    """Page through /api/v1/replies for the lookback window, id-cursor style
    (mirrors enrich_reply_parents.py's fetch_work loop). Capped at
    limit_pages * 500 rows as a sane upper bound for a 30d window."""
    since = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    out: list[dict] = []
    before_id = None
    for _ in range(limit_pages):
        query = {"platform": platform, "since": since, "order_by": "id", "limit": "500"}
        if before_id:
            query["before_id"] = str(before_id)
        resp = api_get("/api/v1/replies", query=query)
        rows = (resp.get("data") or {}).get("replies") or []
        if not rows:
            break
        out.extend(rows)
        min_id = min(r["id"] for r in rows)
        if before_id is not None and min_id >= before_id:
            break
        before_id = min_id
        if len(rows) < 500:
            break
    return out


def compute_style_stats(rows: list[dict]) -> list[dict]:
    by_style: dict[str, dict] = defaultdict(lambda: {"n": 0, "child_replies": 0, "upvotes": 0})
    for r in rows:
        if r.get("status") != "replied":
            continue
        style = r.get("engagement_style")
        if not style:
            continue
        s = by_style[style]
        s["n"] += 1
        s["child_replies"] += int(r.get("comments_count") or 0)
        s["upvotes"] += int(r.get("upvotes") or 0)

    ranked = []
    for style, s in by_style.items():
        if s["n"] < MIN_STYLE_SAMPLE:
            continue
        ranked.append({
            "style": style,
            "n": s["n"],
            "child_reply_rate": s["child_replies"] / s["n"],
            "upvote_rate": s["upvotes"] / s["n"],
        })
    ranked.sort(key=lambda x: x["child_reply_rate"], reverse=True)
    return ranked


def compute_warm_authors(rows: list[dict]) -> list[dict]:
    by_author: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "replied": 0, "riskish_skips": 0, "child_replies": 0,
        "upvotes": 0, "projects": Counter(),
    })
    for r in rows:
        handle = (r.get("their_author") or "").strip()
        if not handle:
            continue
        a = by_author[handle]
        a["n"] += 1
        if r.get("status") == "replied":
            a["replied"] += 1
        if _skip_reason_risk_score(r.get("skip_reason") or "") >= 4:
            a["riskish_skips"] += 1
        a["child_replies"] += int(r.get("comments_count") or 0)
        a["upvotes"] += int(r.get("upvotes") or 0)
        if r.get("project_name"):
            a["projects"][r["project_name"]] += 1

    warm = []
    for handle, a in by_author.items():
        if a["n"] < WARM_MIN_LAST_30D:
            continue
        if a["riskish_skips"] > 0:
            continue
        if a["child_replies"] < WARM_MIN_CHILD_REPLIES:
            continue
        top_project = a["projects"].most_common(1)[0][0] if a["projects"] else None
        warm.append({
            "handle": handle,
            "last_30d": a["n"],
            "replied": a["replied"],
            "child_replies": a["child_replies"],
            "upvotes": a["upvotes"],
            "project_name": top_project,
        })
    warm.sort(key=lambda x: (x["child_replies"], x["last_30d"]), reverse=True)
    return warm


def pick_default_project(cfg: dict, fallback_candidates: list[dict]) -> str | None:
    for c in fallback_candidates:
        if c.get("project_name"):
            return c["project_name"]
    projects = cfg.get("projects") or []
    return projects[0].get("name") if projects else None


def build_warm_entry(w: dict) -> str:
    entry = (
        f"@{w['handle']} — repeat constructive practitioner "
        f"({w['replied']}/{w['last_30d']} replied in 30d, {w['child_replies']} child "
        f"replies, 0 risk skips); warm to more engaged/product-relevant replies."
    )
    return entry[:ENTRY_CHAR_LIMIT]


def build_style_note(top: dict) -> str:
    note = (
        f"engagement_style='{top['style']}' shows the best child-reply continuation rate "
        f"({top['child_reply_rate']:.2f}/reply, n={top['n']}); prefer it for technical-"
        "correction replies (concrete metric, name the exact failure mode)."
    )
    if len(note) > ENTRY_CHAR_LIMIT:
        note = note[: ENTRY_CHAR_LIMIT - 1].rsplit(" ", 1)[0] + "…"
    return note


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", default="x")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_env()

    rows = fetch_window_rows(args.platform, args.days)
    print(f"[engagement_signal_feedback] fetched {len(rows)} rows over last {args.days}d ({args.platform})", file=sys.stderr)

    style_ranked = compute_style_stats(rows)
    warm_authors = compute_warm_authors(rows)[:MAX_WARM_PROMOTIONS_PER_RUN]

    cfg_path_val = None
    try:
        from learned_preferences import config_path
        cfg_path_val = config_path()
        cfg = json.loads(Path(cfg_path_val).read_text())
    except Exception as e:
        print(f"[engagement_signal_feedback] cannot read config: {e}", file=sys.stderr)
        return 1

    block = get_global_block(cfg)
    existing_prefer = block.get("audience_prefer") or []
    existing_notes = block.get("draft_style_notes") or []

    audience_prefer_add = []
    for w in warm_authors:
        handle_lower = w["handle"].lower()
        already = any(f"@{handle_lower}" in e.lower() for e in existing_prefer)
        if already:
            continue
        audience_prefer_add.append(build_warm_entry(w))

    draft_style_notes_add = []
    if style_ranked:
        top = style_ranked[0]
        already_noted = any(f"engagement_style='{top['style']}'" in n for n in existing_notes)
        if not already_noted:
            draft_style_notes_add.append(build_style_note(top))

    print("[engagement_signal_feedback] style ranking:", file=sys.stderr)
    for s in style_ranked:
        print(f"    {s['style']}: n={s['n']} child_reply_rate={s['child_reply_rate']:.2f} upvote_rate={s['upvote_rate']:.2f}", file=sys.stderr)
    print(f"[engagement_signal_feedback] warm candidates this run: {[w['handle'] for w in warm_authors]}", file=sys.stderr)
    print(f"[engagement_signal_feedback] audience_prefer additions: {audience_prefer_add}", file=sys.stderr)
    print(f"[engagement_signal_feedback] draft_style_notes additions: {draft_style_notes_add}", file=sys.stderr)

    if not audience_prefer_add and not draft_style_notes_add:
        print("[engagement_signal_feedback] nothing new to promote", file=sys.stderr)
        return 0

    if args.dry_run:
        print("[engagement_signal_feedback] --dry-run: not writing", file=sys.stderr)
        return 0

    project_name = pick_default_project(cfg, warm_authors)
    if not project_name:
        print("[engagement_signal_feedback] no project available to validate write; aborting", file=sys.stderr)
        return 1

    plan = {"changes": {}, "rationale": "engagement_signal_feedback.py: downstream reply engagement (child-reply/upvote rates)"}
    if audience_prefer_add:
        plan["changes"]["audience_prefer"] = {"add": audience_prefer_add}
    if draft_style_notes_add:
        plan["changes"]["draft_style_notes"] = {"add": draft_style_notes_add}

    result = apply_mutations(project_name, plan, cfg_path=cfg_path_val)
    print(f"[engagement_signal_feedback] apply_mutations result: {json.dumps(result)}", file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
