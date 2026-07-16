#!/usr/bin/env python3
"""Sweep platform moderation strikes into the review_events ledger.

Two platform-derived decision types (2026-07-15 design, see
migrations/2026-07-15-review-platform-events.sql in the website repo):

  platform_removed  A post of ours flipped to status='removed'/'deleted'
                    (moderator removal, automod filter, or platform spam
                    filter). One event per post. Less severe: the lesson
                    is the CONTENT pattern, not the venue.
  platform_banned   Our account is blocked from a community (config.json
                    subreddit_bans.comment_blocked, reason
                    'account_blocked_in_sub'). One event per (sub, account).
                    Severe: strong evidence against the venue. Emitted with
                    project=NULL (the ban is account-level, not tied to one
                    project); the digest folds these into every project's
                    prompt like overall feedback.

Idempotency: event_uuid is a uuid5 of the source fact (post id / sub+account),
NOT salted per install or per run, so re-runs AND concurrent sweeps from
sibling installs of the same fleet all collapse into one ledger row via the
API's ON CONFLICT (event_uuid) DO NOTHING.

Scoping: only posts whose project_name appears in the LOCAL config.json are
swept. This file ships in the release, so a customer install must never
mint events for other installs' posts; the local project list is exactly
the boundary of what this box is allowed to learn from. (posts GET is
deliberately fleet-wide, so own_install=true would drop the operator's
pre-install-stamping rows; the project filter is the correct scope.)

Recency gate: only posts DETECTED removed recently (status_checked_at within
--window-days, default 14) and bans ADDED within the same window. Removal
detection was dead 2026-05-28 to 2026-07-14, so without the gate the first
sweep would flood the digest with months of ancient history. Only rows with
deletion_detect_count >= 2 qualify (the same two-strike confirmation the
strike-alert email rail requires).

Invoked automatically at the start of every feedback_digest.py run (skipped
on --dry-run), or manually:

    python3 scripts/platform_strike_events.py [--window-days N] [--dry-run]

stderr marker (load-bearing shape, keep stable):
    [platform_strike_events] swept=... removed_events=... ban_events=... inserted=... duplicates=...
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http_api import api_get, api_post, load_env  # noqa: E402

CONFIG_PATH = os.environ.get(
    "S4L_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"),
)
# Stable namespace for deterministic event uuids. Never change this value:
# a new namespace would re-mint every historical fact as a "new" event.
EVENT_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://s4l.ai/platform-strike-events")
POST_BATCH = 100  # review-events POST MAX_BATCH
FETCH_LIMIT = 500  # posts GET hard cap; newest-first covers all fresh flips
MAX_NOTE = 1900  # route clips reject_note at 2000; stay under
MAX_DRAFT = 9500


def log(msg: str) -> None:
    print(f"[platform_strike_events] {msg}", file=sys.stderr, flush=True)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_ts(raw) -> datetime.datetime | None:
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _load_config() -> dict:
    try:
        return json.loads(Path(CONFIG_PATH).read_text())
    except Exception:
        return {}


def _venue(post: dict) -> str | None:
    """Human-readable venue for the note: subreddit for reddit, repo for
    github, otherwise nothing (twitter has no venue concept)."""
    url = post.get("thread_url") or post.get("our_url") or ""
    m = re.search(r"reddit\.com/r/([^/]+)/", url)
    if m:
        return f"r/{m.group(1)}"
    m = re.search(r"github\.com/([^/]+/[^/#?]+)", url)
    if m:
        return m.group(1)
    return None


def _removed_event(post: dict) -> dict:
    venue = _venue(post)
    is_own_thread = bool(post.get("thread_url")) and post.get("thread_url") == post.get("our_url")
    kind = "top-level post" if is_own_thread else "comment/reply"
    posted = str(post.get("posted_at") or "")[:10]
    detected = str(post.get("status_checked_at") or "")[:10]
    note = (
        f"Platform moderation strike: our {post.get('platform')} {kind}"
        f"{' in ' + venue if venue else ''} is now status='{post.get('status')}'"
        f" (confirmed on {post.get('deletion_detect_count')} consecutive scans)."
        f" Posted {posted}, removal detected {detected},"
        f" account {post.get('our_account')}."
        f" Machine-derived signal, not a human card decision: a moderator or"
        f" platform filter judged this content unwelcome in that venue."
    )
    return {
        "event_uuid": str(uuid.uuid5(EVENT_NS, f"platform_removed:post:{post['id']}")),
        "decision": "platform_removed",
        "platform": post.get("platform") or "reddit",
        "project": post.get("project_name"),
        "thread_url": (post.get("our_url") or post.get("thread_url") or "")[:1000] or None,
        "thread_author": post.get("thread_author"),
        "draft_text": (post.get("our_content") or "")[:MAX_DRAFT] or None,
        "reject_note": note[:MAX_NOTE],
        "batch_id": "platform_strike_sweep",
    }


def _ban_event(entry: dict) -> dict:
    sub = str(entry.get("sub") or "").strip().lower()
    account = str(entry.get("account") or "").strip()
    added = str(entry.get("added_at") or "")[:10]
    note = (
        f"Community ban: our reddit account {account or '(unknown)'} is blocked"
        f" from participating in r/{sub} (reason: {entry.get('reason')},"
        f" noticed {added or 'unknown date'})."
        f" Machine-derived signal: the venue itself rejected the account,"
        f" the strongest platform-side evidence against this venue/content pairing."
    )
    return {
        "event_uuid": str(uuid.uuid5(EVENT_NS, f"platform_banned:reddit:{sub}:{account.lower()}")),
        "decision": "platform_banned",
        "platform": "reddit",
        "project": entry.get("noticed_by_project") or None,
        "thread_url": f"https://www.reddit.com/r/{sub}/" if sub else None,
        "reject_note": note[:MAX_NOTE],
        "batch_id": "platform_strike_sweep",
    }


def collect_events(window_days: int) -> tuple[list[dict], int, int]:
    """Returns (events, removed_count, ban_count)."""
    cfg = _load_config()
    local_projects = {p.get("name") for p in (cfg.get("projects") or []) if p.get("name")}
    cutoff = _now() - datetime.timedelta(days=window_days)

    resp = api_get("/api/v1/posts", {
        "statuses": "removed,deleted",
        "order_by": "id",
        "order_dir": "desc",
        "limit": str(FETCH_LIMIT),
    })
    posts = ((resp or {}).get("data") or {}).get("posts") or []

    removed_events = []
    for p in posts:
        if p.get("project_name") not in local_projects:
            continue
        if int(p.get("deletion_detect_count") or 0) < 2:
            continue
        detected = _parse_ts(p.get("status_checked_at"))
        if detected is None or detected < cutoff:
            continue
        removed_events.append(_removed_event(p))

    ban_events = []
    for entry in ((cfg.get("subreddit_bans") or {}).get("comment_blocked") or []):
        if entry.get("reason") != "account_blocked_in_sub":
            continue
        added = _parse_ts(entry.get("added_at"))
        if added is None or added < cutoff:
            continue
        ban_events.append(_ban_event(entry))

    return removed_events + ban_events, len(removed_events), len(ban_events)


def sweep(window_days: int = 14, dry_run: bool = False) -> dict:
    events, n_removed, n_banned = collect_events(window_days)
    inserted = 0
    duplicates = 0
    if dry_run:
        for ev in events:
            print(json.dumps(ev, indent=2))
    else:
        for i in range(0, len(events), POST_BATCH):
            resp = api_post("/api/v1/review-events", {"events": events[i:i + POST_BATCH]})
            data = (resp or {}).get("data") or {}
            inserted += int(data.get("inserted") or 0)
            duplicates += int(data.get("duplicates") or 0)
    log(
        f"swept={len(events)} removed_events={n_removed} ban_events={n_banned}"
        f" inserted={inserted} duplicates={duplicates}"
        f"{' (dry run, nothing posted)' if dry_run else ''}"
    )
    return {"swept": len(events), "removed": n_removed, "banned": n_banned,
            "inserted": inserted, "duplicates": duplicates}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window-days", type=int,
                    default=int(os.environ.get("S4L_PLATFORM_STRIKE_WINDOW_DAYS", "14")),
                    help="sweep strikes detected within the last N days")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the events instead of posting them")
    args = ap.parse_args()
    load_env()
    sweep(args.window_days, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
