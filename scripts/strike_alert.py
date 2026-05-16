#!/usr/bin/env python3
"""Strike escalation rail.

Background scan that emails i@m13v.com whenever a previously-active post
flips to status='deleted' or status='removed'. We do not want a comment
disappearing without us hearing about it, e.g. the antiwork/gumroad block
on 2026-05-01 was found via inbound notification email, not via our own
pipeline.

Idempotency: posts.strike_email_sent_at TIMESTAMPTZ. NULL = not yet
emailed. Set to NOW() after a successful send. Historical strikes were
backfilled to a non-NULL value at column creation so we only alert NEW
strikes from then forward.

Usage:
    # default sweep (used by launchd plist)
    python3 scripts/strike_alert.py --sweep

    # target a single post (manual re-fire / smoke test)
    python3 scripts/strike_alert.py --post-id 22200

    # see what would be sent without sending
    python3 scripts/strike_alert.py --sweep --dry-run

    # cap the batch (sanity gate against a wide-spread moderation event)
    python3 scripts/strike_alert.py --sweep --limit 10

Patterned after seo/escalate.py: same Gmail token, same dash-scrubbing,
same recipient default (NOTIFICATION_EMAIL env override). Independent
from update_stats.py so a Python error in the sweeper cannot break the
stats refresh.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import db as dbmod

GMAIL_TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
GMAIL_SCOPES = ["https://mail.google.com/"]
NOTIFICATION_EMAIL = os.environ.get("NOTIFICATION_EMAIL", "i@m13v.com")
DEFAULT_LIMIT = 25


def _scrub_dashes(s):
    if not s:
        return s
    return s.replace("—", ",").replace("–", ",")


def _gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


_REPO_STATE_CACHE = {}


def _github_repo_state(thread_url):
    """Return one of:

      - 'repo_gone'        : parent repo 404s (owner deleted the whole repo)
      - 'feature_disabled' : repo is live but the feature this comment lived
                             on is turned off (e.g. /issues/123 on a repo
                             with has_issues=false, or /discussions/N on a
                             repo with has_discussions=false). ALL issues
                             or ALL discussions vanish at once; this is not
                             a moderation strike against our comment.
      - 'live'             : repo is alive AND the relevant feature is on.
                             Our comment is gone in isolation, true strike.
      - 'unknown'          : network error, non-github URL, etc.

    Distinguishes moderation strikes (our content was hidden/deleted on a
    live, fully-featured repo) from collateral damage (owner restructured
    the project). Cached per-process; the gh-api fetch is one round-trip
    per repo per sweep."""
    if not thread_url:
        return "unknown"
    parts = urlparse(thread_url).path.strip("/").split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return "unknown"
    owner, repo = parts[0], parts[1]
    # which sub-feature did this URL live on? issues / discussions / other.
    feature = None
    if len(parts) >= 3:
        if parts[2] == "issues":
            feature = "issues"
        elif parts[2] == "discussions":
            feature = "discussions"
    key = f"{owner}/{repo}".lower()
    if key in _REPO_STATE_CACHE:
        cached = _REPO_STATE_CACHE[key]
    else:
        try:
            proc = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}"],
                capture_output=True, text=True, timeout=20,
            )
        except Exception:
            _REPO_STATE_CACHE[key] = {"state": "unknown"}
            return "unknown"
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout or "{}")
            except Exception:
                data = {}
            cached = {
                "state": "live",
                "has_issues": bool(data.get("has_issues", True)),
                "has_discussions": bool(data.get("has_discussions", True)),
            }
        else:
            err = ((proc.stderr or "") + (proc.stdout or "")).lower()
            if "not found" in err or "http 404" in err:
                cached = {"state": "repo_gone"}
            else:
                cached = {"state": "unknown"}
        _REPO_STATE_CACHE[key] = cached

    base = cached.get("state", "unknown")
    if base != "live":
        return base
    # Repo is alive; check whether the specific feature the URL points at is on.
    if feature == "issues" and not cached.get("has_issues", True):
        return "feature_disabled"
    if feature == "discussions" and not cached.get("has_discussions", True):
        return "feature_disabled"
    return "live"


def _owner_strike_count(db, owner, days=90):
    """How many of our posts under this owner have been moderated in the
    last `days` days, excluding posts whose entire parent repo is now 404
    (repo-gone is not a moderation strike). Mirrors the same filtering used
    by github_tools._dynamic_owner_blocklist so the email body and the
    search-time blocklist stay in sync."""
    if not owner:
        return (0, 0)
    cur = db.execute(
        "SELECT thread_url FROM posts "
        "WHERE platform='github' "
        "  AND posted_at > NOW() - INTERVAL %s "
        "  AND lower(thread_url) LIKE %s "
        "  AND (status='deleted' OR COALESCE(deletion_detect_count, 0) > 0)",
        [f"{int(days)} days", f"https://github.com/{owner.lower()}/%"],
    )
    raw_count = 0
    live_count = 0
    for r in cur.fetchall():
        url = r[0] if not hasattr(r, "get") else r["thread_url"]
        raw_count += 1
        state = _github_repo_state(url)
        # repo_gone + feature_disabled are both "owner restructured" cases,
        # not moderation. Don't count them against the owner.
        if state not in ("repo_gone", "feature_disabled"):
            live_count += 1
    return (live_count, raw_count)


def _format_subject(post, repo_state=None):
    platform = post["platform"] or "?"
    status = post["status"] or "?"
    tag = "STRIKE"
    if platform == "github" and repo_state == "repo_gone":
        # Owner nuked the whole repo. Not a moderation strike against us.
        status = "repo-deleted"
        tag = "STRIKE-REPOGONE"
    elif platform == "github" and repo_state == "feature_disabled":
        # Repo is alive but Issues/Discussions feature was disabled. Our
        # comment vanished as collateral, not a moderation action.
        status = "feature-disabled"
        tag = "STRIKE-FEATURE-OFF"
    project = post["project_name"] or "(no project)"
    title = (post["thread_title"] or "")[:60]
    return _scrub_dashes(
        f"[{tag} #{post['id']}] {platform} {status}: {project} / {title}"
    )


def _format_body(db, post, repo_state=None):
    platform = post["platform"] or "?"
    status = post["status"] or "?"
    project = post["project_name"] or "(no project)"
    account = post["our_account"] or "?"
    posted_at = post["posted_at"].isoformat() if post["posted_at"] else "?"
    checked_at = (
        post["status_checked_at"].isoformat() if post["status_checked_at"] else "?"
    )
    thread_url = post["thread_url"] or "?"
    our_url = post["our_url"] or "(no comment URL)"
    title = post["thread_title"] or "(no title)"
    content = (post["our_content"] or "(no content)").strip()
    content_preview = content[:600] + ("..." if len(content) > 600 else "")
    style = post["engagement_style"] or "(none)"
    detect_count = post["deletion_detect_count"] or 0

    owner_block = ""
    repo_block = ""
    if platform == "github" and thread_url:
        if repo_state == "repo_gone":
            repo_block = (
                "Repo state: GONE (parent repo returns 404). "
                "Owner nuked the whole repo, this is not a moderation strike "
                "against our comment specifically.\n"
            )
        elif repo_state == "feature_disabled":
            repo_block = (
                "Repo state: live but FEATURE DISABLED (has_issues=false or "
                "has_discussions=false on the repo). The entire issues/"
                "discussions surface was turned off by the owner; every "
                "comment under it 404s, not just ours. This is collateral "
                "damage, not a moderation strike.\n"
            )
        elif repo_state == "live":
            repo_block = "Repo state: live (only our comment is gone, true strike).\n"
        parts = urlparse(thread_url).path.strip("/").split("/")
        owner = parts[0] if parts else None
        if owner:
            live_n, raw_n = _owner_strike_count(db, owner)
            from github_tools import DYNAMIC_BLOCK_THRESHOLD as THR
            verdict = (
                "AUTO-BLOCKLISTED" if live_n >= THR
                else f"under threshold ({live_n}/{THR})"
            )
            extra = (
                f" ({raw_n - live_n} excluded as repo-gone)"
                if raw_n > live_n else ""
            )
            owner_block = (
                f"Owner: {owner} ({live_n} real strikes in last 90 days{extra}, "
                f"{verdict})\n"
            )

    body = (
        f"Strike on social-autoposter post #{post['id']}\n"
        f"\n"
        f"Platform: {platform}\n"
        f"Status:   {status} (deletion_detect_count={detect_count})\n"
        f"Project:  {project}\n"
        f"Account:  {account}\n"
        f"Style:    {style}\n"
        f"Posted:   {posted_at}\n"
        f"Detected: {checked_at}\n"
        f"{repo_block}"
        f"{owner_block}"
        f"\n"
        f"Thread:  {thread_url}\n"
        f"Title:   {title}\n"
        f"Comment: {our_url}\n"
        f"\n"
        f"--- Our content ---\n"
        f"{content_preview}\n"
        f"\n"
        f"--- Next steps ---\n"
        f"1. Inspect the thread to see if the comment was deleted, hidden,\n"
        f"   or if the whole account was blocked.\n"
        f"2. If the owner should be hard-blocked, add it to\n"
        f"   config.json -> exclusions.github_repos. Owner-level entries\n"
        f"   match all repos under that owner.\n"
        f"3. The auto-blocklist (github_tools._dynamic_owner_blocklist)\n"
        f"   already covers any owner with >=2 strikes in 90 days.\n"
        f"\n"
        f"To re-fire this alert: python3 scripts/strike_alert.py --post-id {post['id']}\n"
    )
    return _scrub_dashes(body)


def _send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = NOTIFICATION_EMAIL
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service = _gmail_service()
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def _select_pending(db, post_id=None, limit=None):
    if post_id is not None:
        cur = db.execute(
            "SELECT id, platform, status, project_name, our_account, posted_at, "
            "  status_checked_at, thread_url, our_url, thread_title, our_content, "
            "  engagement_style, deletion_detect_count, strike_email_sent_at "
            "FROM posts WHERE id=%s",
            [post_id],
        )
        return cur.fetchall()
    # Skip mention-stub rows. These are placeholder rows the Twitter mention
    # scanner (scan_twitter_mentions_browser.py) inserts when someone tags us
    # in a tweet. our_content == '(mention - no original post)' and our_url
    # equals the OTHER user's tweet URL, so when fxtwitter 404s that URL
    # (spammer's tweet/account got taken down), update_stats flips the stub
    # to status='deleted'. That is not a moderation strike against US, just
    # Twitter cleaning up someone else's spam. Filtering them out here so the
    # alert email stays high-signal. See post #25869 (okoroei3 spam mention,
    # 2026-05-15) for the canonical false-positive case.
    sql = (
        "SELECT id, platform, status, project_name, our_account, posted_at, "
        "  status_checked_at, thread_url, our_url, thread_title, our_content, "
        "  engagement_style, deletion_detect_count, strike_email_sent_at "
        "FROM posts "
        "WHERE status IN ('deleted','removed') AND strike_email_sent_at IS NULL "
        "  AND COALESCE(our_content, '') <> '(mention - no original post)' "
        "ORDER BY COALESCE(status_checked_at, posted_at) DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur = db.execute(sql)
    return cur.fetchall()


def _mark_sent(db, post_id):
    db.execute(
        "UPDATE posts SET strike_email_sent_at=NOW() WHERE id=%s", [post_id]
    )
    db.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", action="store_true",
                        help="Scan posts for unalerted strikes (default mode).")
    parser.add_argument("--post-id", type=int,
                        help="Target a single post id; overrides --sweep gating "
                             "and ignores strike_email_sent_at.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max alerts per run (default {DEFAULT_LIMIT}). "
                             f"Sanity gate against a wide moderation event.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be sent without sending or marking.")
    args = parser.parse_args()

    dbmod.load_env()
    db = dbmod.get_conn()

    rows = _select_pending(db, post_id=args.post_id, limit=args.limit)
    if not rows:
        print("[strike_alert] no pending strikes")
        return

    sent = 0
    skipped = 0
    failed = 0
    for r in rows:
        # When --post-id is used, allow re-fire even if already sent.
        if args.post_id is None and r["strike_email_sent_at"] is not None:
            skipped += 1
            continue
        repo_state = None
        if r["platform"] == "github":
            repo_state = _github_repo_state(r["thread_url"])
            print(
                f"[strike_alert] id={r['id']} platform=github "
                f"repo_state={repo_state} thread={r['thread_url']}",
                flush=True,
            )
        subject = _format_subject(r, repo_state=repo_state)
        body = _format_body(db, r, repo_state=repo_state)
        if args.dry_run:
            print(f"[strike_alert] DRY RUN id={r['id']}")
            print(f"  subject: {subject}")
            print("  body:")
            for line in body.split("\n"):
                print(f"    {line}")
            sent += 1
            continue
        try:
            _send_email(subject, body)
            _mark_sent(db, r["id"])
            sent += 1
            print(f"[strike_alert] alerted id={r['id']} ({r['platform']} {r['status']})")
        except Exception as e:
            failed += 1
            print(f"[strike_alert] FAILED id={r['id']}: {e}", file=sys.stderr)

    print(f"[strike_alert] sent={sent} skipped={skipped} failed={failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
