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
from stats.py so a Python error in the sweeper cannot break the
stats refresh.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from urllib.parse import urlparse


def _resolve_gh():
    """Locate the `gh` binary. Returns the absolute path or None.

    The launchd plist sets PATH=/opt/homebrew/bin:..., but anyone running
    this script from a shell where /opt/homebrew/bin is not on PATH (or
    from a future cron that drops the path) will silently fall back to
    `state=unknown`, defeating the repo-gone filter. Resolve once at
    import and log loudly on miss."""
    p = shutil.which("gh")
    if p:
        return p
    for c in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh"):
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


_GH_BIN = _resolve_gh()
if _GH_BIN is None:
    print(
        "[strike_alert] WARNING: `gh` binary not found on PATH or in "
        "/opt/homebrew/bin /usr/local/bin. Repo-gone filter will be "
        "disabled and every github strike will email.",
        file=sys.stderr,
    )

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from http_api import api_get, api_patch, load_env  # noqa: E402

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
      - 'issue_deleted'    : repo is live but the specific issue/PR thread is
                             gone (HTTP 410 'This issue was deleted', or 404
                             on `repos/{o}/{r}/issues/{n}`). Every comment
                             under that thread vanishes at once; not a
                             moderation strike against our comment.
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
    the project). Cached per-process; the gh-api fetch is at most two
    round-trips per (repo, issue#) pair per sweep."""
    if not thread_url:
        return "unknown"
    parts = urlparse(thread_url).path.strip("/").split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return "unknown"
    owner, repo = parts[0], parts[1]
    # which sub-feature did this URL live on? issues / discussions / other.
    feature = None
    issue_number = None
    if len(parts) >= 3:
        if parts[2] == "issues":
            feature = "issues"
        elif parts[2] == "discussions":
            feature = "discussions"
        elif parts[2] == "pull":
            feature = "pull"
    if feature in ("issues", "pull") and len(parts) >= 4:
        try:
            issue_number = int(parts[3])
        except (ValueError, IndexError):
            issue_number = None
    key = f"{owner}/{repo}".lower()
    if key in _REPO_STATE_CACHE:
        cached = _REPO_STATE_CACHE[key]
    else:
        if _GH_BIN is None:
            # gh not found at import-time; logged once at module load.
            # Returning 'unknown' here means the in-loop filter will not
            # skip this row, so the email DOES fire. That is intentional
            # graceful degradation: better to send a noisy email than to
            # silently drop a real moderation strike.
            _REPO_STATE_CACHE[key] = {"state": "unknown"}
            return "unknown"
        try:
            proc = subprocess.run(
                [_GH_BIN, "api", f"repos/{owner}/{repo}"],
                capture_output=True, text=True, timeout=20,
            )
        except FileNotFoundError as e:
            print(
                f"[strike_alert] gh subprocess FileNotFoundError "
                f"({_GH_BIN}): {e}", file=sys.stderr,
            )
            _REPO_STATE_CACHE[key] = {"state": "unknown"}
            return "unknown"
        except Exception as e:
            print(
                f"[strike_alert] gh subprocess error for {owner}/{repo}: "
                f"{e}", file=sys.stderr,
            )
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
    # Repo + feature both live; check the individual issue/PR thread. Cached
    # separately so multiple comments on the same thread share one call.
    if feature in ("issues", "pull") and issue_number is not None:
        issue_key = f"{owner}/{repo}#{issue_number}".lower()
        if issue_key in _REPO_STATE_CACHE:
            issue_state = _REPO_STATE_CACHE[issue_key]
        else:
            try:
                proc = subprocess.run(
                    [_GH_BIN, "api", f"repos/{owner}/{repo}/issues/{issue_number}"],
                    capture_output=True, text=True, timeout=20,
                )
            except Exception as e:
                print(
                    f"[strike_alert] gh subprocess error for "
                    f"{owner}/{repo}/issues/{issue_number}: {e}",
                    file=sys.stderr,
                )
                _REPO_STATE_CACHE[issue_key] = {"state": "unknown"}
                return "live"  # graceful: assume live so email fires
            if proc.returncode == 0:
                issue_state = {"state": "live"}
            else:
                err = ((proc.stderr or "") + (proc.stdout or "")).lower()
                if ("not found" in err or "http 404" in err
                        or "http 410" in err
                        or "this issue was deleted" in err):
                    issue_state = {"state": "issue_deleted"}
                else:
                    issue_state = {"state": "unknown"}
            _REPO_STATE_CACHE[issue_key] = issue_state
        if issue_state["state"] == "issue_deleted":
            return "issue_deleted"
    return "live"


def _reddit_live_recheck(our_url, our_account, user_agent):
    """Pre-send Reddit live re-check (added 2026-05-16).

    Before firing a strike email, fetch the comment URL one more time. If
    the comment body is real content (not [deleted]/[removed]), stats.py
    false-flagged it (transient parse error, rate-limit miss, etc.) and the
    strike is bogus. Return one of:

      'alive'   - comment is visible with real content. Caller should flip
                  status back to 'active', reset deletion_detect_count, and
                  skip the email.
      'dead'    - comment is confirmed [deleted]/[removed] or 404. Real
                  strike, send the email.
      'unknown' - couldn't determine (rate limit, network error, malformed
                  response). Fail-open: send the email anyway. Mirrors the
                  github _github_repo_state='unknown' graceful-degradation
                  pattern: better to send a noisy email than silently drop
                  a real moderation strike.

    Self-healing rationale: even with the weekly resurrect job, the alert
    fires at T+0 detection while resurrect runs later. Without this guard
    a brittle 2-detection threshold + a couple of bad scrapes was enough
    to send a false-positive email (see post #23005 / #23223 on 2026-05-07,
    both alive at the time the strike emails went out).
    """
    if not our_url or not our_url.startswith("http"):
        return "unknown"

    json_url = re.sub(r"www\.reddit\.com", "old.reddit.com", our_url).rstrip("/") + ".json"
    req = urllib.request.Request(json_url, headers={"User-Agent": user_agent})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                if not body:
                    return "unknown"
                try:
                    data = json.loads(body)
                except Exception:
                    return "unknown"
                break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "dead"
            if e.code == 429 and attempt == 0:
                time.sleep(10)
                continue
            return "unknown"
        except Exception:
            if attempt == 0:
                time.sleep(5)
                continue
            return "unknown"
    else:
        return "unknown"

    if not isinstance(data, list) or len(data) < 2:
        return "unknown"

    has_comment_id = bool(
        re.search(r"/comment/[a-z0-9]+", our_url) or
        re.search(r"/comments/[a-z0-9]+/[^/]+/[a-z0-9]+", our_url)
    )

    if has_comment_id:
        children = data[1].get("data", {}).get("children", [])
        if not children:
            return "dead"
        cd = children[0].get("data", {})
        cbody = cd.get("body", "")
        cauthor = cd.get("author", "")
        if cbody in ("[deleted]", "[removed]") or cauthor == "[deleted]":
            return "dead"
        if cbody.strip():
            return "alive"
        return "unknown"
    else:
        thread = data[0].get("data", {}).get("children", [{}])[0].get("data", {})
        thread_author = thread.get("author", "")
        if our_account and thread_author.lower() == our_account.lower():
            if thread.get("removed_by_category") or thread.get("selftext") in ("[removed]", "[deleted]"):
                return "dead"
            return "alive"
        children = data[1].get("data", {}).get("children", [])
        for child in children:
            cd = child.get("data", {})
            if our_account and cd.get("author", "").lower() == our_account.lower():
                cbody = cd.get("body", "")
                if cbody in ("[deleted]", "[removed]"):
                    return "dead"
                if cbody.strip():
                    return "alive"
                break
        return "unknown"


def _twitter_live_recheck(our_url):
    """Pre-send Twitter live re-check (added 2026-06-05).

    Mirrors _reddit_live_recheck. stats.py marks a tweet 'deleted' after 2
    fxtwitter 404s, but fxtwitter is an UNAUTHENTICATED guest API: for
    Community-scoped posts and some replies it returns a *tombstone*
    (type="tombstone", reason="unavailable") even though the tweet is alive
    to a logged-in viewer. On 2026-06-05, 5 of 6 twitter strike emails were
    tombstone-unavailable rows that were live in the authenticated harness
    (#35715/#35712 Community posts; #31131/#31130/#29509 normal replies).

    stats.py was patched the same day to stop counting tombstones as
    deletions; this is the second safety net for rows that were flagged
    before that fix shipped, or for any future guest-API blind spot. We re-hit
    fxtwitter and key on the SAME signal:

      'alive'   - fxtwitter returns a real tweet OR a tombstone (guest-API
                  blind spot, not a deletion). Caller flips status back to
                  'active', resets deletion_detect_count, skips the email.
      'dead'    - genuine NOT_FOUND (code 404 with tweet=None, no tombstone).
                  Real deletion, send the email.
      'unknown' - network error / unparseable. Fail-open: send the email.
                  Mirrors the github 'unknown' graceful-degradation pattern.
    """
    if not our_url or not our_url.startswith("http"):
        return "unknown"
    m = re.search(r"(?:twitter|x)\.com/([^/]+)/status/(\d+)", our_url)
    if not m:
        return "unknown"
    username, tweet_id = m.group(1), m.group(2)
    api_url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
    req = urllib.request.Request(
        api_url, headers={"User-Agent": "social-autoposter/1.0"}
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                try:
                    data = json.loads(body) if body else None
                except Exception:
                    return "unknown"
                break
        except urllib.error.HTTPError as e:
            # fxtwitter answers 404 with a JSON body (tombstone OR null tweet).
            # Read it so we can distinguish the two; a bare HTTPError without a
            # parseable body is 'unknown'.
            try:
                data = json.loads(e.read() or b"")
            except Exception:
                if attempt == 0:
                    time.sleep(3)
                    continue
                return "unknown"
            break
        except Exception:
            if attempt == 0:
                time.sleep(3)
                continue
            return "unknown"
    else:
        return "unknown"

    if not isinstance(data, dict):
        return "unknown"
    tweet = data.get("tweet")
    if isinstance(tweet, dict) and tweet.get("type") == "tombstone":
        # Guest-API blind spot (Community post / restricted reply). Alive.
        return "alive"
    if isinstance(tweet, dict):
        # Real tweet object came back -> definitely alive.
        return "alive"
    code = data.get("code", 0)
    if code == 404 or tweet is None:
        return "dead"
    return "unknown"


def _resurrect_post(post_id):
    """Flip a Reddit strike row back to 'active' after a live re-check confirms
    the comment is still visible. Mirrors update_reddit_resurrect's UPDATE."""
    api_patch(f"/api/v1/posts/{int(post_id)}", {
        "status": "active",
        "reset_deletion_detect_count": True,
        "stamp_resurrected_now": True,
        "stamp_status_checked_now": True,
    })


def _owner_strike_count(owner, days=90):
    """How many of our posts under this owner have been moderated in the
    last `days` days, excluding posts whose entire parent repo is now 404
    (repo-gone is not a moderation strike). Mirrors the same filtering used
    by github_tools._dynamic_owner_blocklist so the email body and the
    search-time blocklist stay in sync."""
    if not owner:
        return (0, 0)
    prefix = f"https://github.com/{owner.lower()}/"
    resp = api_get("/api/v1/posts/thread-urls", query={
        "platform": "github", "moderated_within_days": int(days),
    })
    all_urls = (resp.get("data") or {}).get("thread_urls") or []
    raw_count = 0
    live_count = 0
    for url in all_urls:
        if not url or not url.lower().startswith(prefix):
            continue
        raw_count += 1
        state = _github_repo_state(url)
        # repo_gone, issue_deleted, and feature_disabled are all "owner
        # restructured" cases, not moderation. Don't count them against
        # the owner.
        if state not in ("repo_gone", "issue_deleted", "feature_disabled"):
            live_count += 1
    return (live_count, raw_count)


def _subreddit_from_url(url):
    if not url:
        return None
    m = re.search(r"reddit\.com/r/([^/]+)", url)
    return m.group(1).lower() if m else None


def _subreddit_strike_count(sub, days=90):
    """How many of our reddit posts in r/<sub> were moderated (removed/deleted)
    in the last `days` days. The reddit analogue of _owner_strike_count: turns a
    single strike email into 'this is the Nth removal in this community', which
    is the clustering signal that separates a chronic bad-fit venue from a
    one-off removal. Returns 0 on any lookup failure (never blocks the email)."""
    if not sub:
        return 0
    try:
        resp = api_get("/api/v1/posts/thread-urls", query={
            "platform": "reddit", "moderated_within_days": int(days),
        })
    except Exception:
        return 0
    urls = (resp.get("data") or {}).get("thread_urls") or []
    return sum(1 for u in urls if _subreddit_from_url(u) == sub)


def _recorded_ban(sub, config, account):
    """True if r/<sub> is already on the config.json comment_blocked denylist
    with reason 'account_blocked_in_sub' for this account (the recorded output
    of the deterministic ban check). Account-scoped, matching
    reddit_tools._load_comment_blocked_subs semantics."""
    if not sub:
        return False
    acct = (account or "").lower()
    for entry in ((config.get("subreddit_bans") or {}).get("comment_blocked") or []):
        if not isinstance(entry, dict):
            continue
        if (entry.get("sub") or "").strip().lower() != sub:
            continue
        if entry.get("reason") != "account_blocked_in_sub":
            continue
        ent_acct = (entry.get("account") or "").lower()
        if ent_acct and acct and ent_acct != acct:
            continue
        return True
    return False


def _reddit_ban_verdict(subs, config, account):
    """Return {sub: 'banned'|'not_banned'|'unknown'} for reddit strike subs.

    Prefers a single live deterministic check (reddit_ban_check.banned_state,
    one browser attach for the whole sweep, read-only) and falls back to the
    recorded config denylist when no session is reachable or the check is
    disabled via S4L_STRIKE_BAN_CHECK=0. A recorded ban is authoritative even
    when the live check says unknown (session down) so we never downgrade a
    known ban to 'single-post removal'."""
    verdict = {}
    live = {}
    if subs and os.environ.get("S4L_STRIKE_BAN_CHECK", "1") not in ("0", "false", "no"):
        try:
            import reddit_ban_check
            live = reddit_ban_check.banned_state(list(subs)) or {}
        except Exception as e:
            print(f"[strike_alert] reddit_ban_check unavailable (non-fatal): {e}",
                  file=sys.stderr)
    for sub in subs:
        lv = live.get(sub)
        if lv is True:
            verdict[sub] = "banned"
        elif _recorded_ban(sub, config, account):
            verdict[sub] = "banned"
        elif lv is False:
            verdict[sub] = "not_banned"
        else:
            verdict[sub] = "unknown"
    return verdict


def _format_subject(post, repo_state=None, sub_ban=None):
    platform = post["platform"] or "?"
    status = post["status"] or "?"
    tag = "STRIKE"
    if platform == "reddit" and sub_ban == "banned":
        # Account is banned from the whole community, not just this post
        # removed. Loud subject tag so a ban is visually distinct from the
        # far more common single-post removal.
        tag = "STRIKE-BANNED"
    if platform == "github" and repo_state == "repo_gone":
        # Owner nuked the whole repo. Not a moderation strike against us.
        status = "repo-deleted"
        tag = "STRIKE-REPOGONE"
    elif platform == "github" and repo_state == "issue_deleted":
        # Owner deleted the specific issue/PR thread (HTTP 410). Every
        # comment under it vanishes, not just ours.
        status = "issue-deleted"
        tag = "STRIKE-ISSUEGONE"
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


def _ts(v):
    """Render a timestamp field. The HTTP API returns ISO strings; tolerate a
    datetime too (defensive, in case a caller passes one)."""
    if not v:
        return "?"
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _format_body(post, repo_state=None, sub_ban=None, sub_count=None):
    platform = post["platform"] or "?"
    status = post["status"] or "?"
    project = post["project_name"] or "(no project)"
    account = post["our_account"] or "?"
    posted_at = _ts(post["posted_at"])
    checked_at = _ts(post["status_checked_at"])
    thread_url = post["thread_url"] or "?"
    our_url = post["our_url"] or "(no comment URL)"
    title = post["thread_title"] or "(no title)"
    content = (post["our_content"] or "(no content)").strip()
    content_preview = content[:600] + ("..." if len(content) > 600 else "")
    style = post["engagement_style"] or "(none)"
    detect_count = post["deletion_detect_count"] or 0

    owner_block = ""
    repo_block = ""
    reddit_block = ""
    if platform == "reddit":
        sub = _subreddit_from_url(thread_url) or _subreddit_from_url(our_url)
        if sub:
            n = sub_count if sub_count is not None else _subreddit_strike_count(sub)
            count_line = (
                f"Subreddit: r/{sub} ({n} of our post(s) moderated here in the "
                f"last 90 days)\n"
            )
            if sub_ban == "banned":
                ban_line = (
                    f"Ban state: ACCOUNT BANNED from r/{sub} (deterministic "
                    f"user_is_banned check). This is not a single-post removal: "
                    f"every future post here will be removed. The sub is on the "
                    f"comment_blocked denylist so the drafter will stop targeting "
                    f"it, and a platform_banned learning signal feeds the digest.\n"
                )
            elif sub_ban == "not_banned":
                ban_line = (
                    f"Ban state: not banned (user_is_banned=false). This is a "
                    f"single-post removal; the account can still post in r/{sub}.\n"
                )
            else:
                ban_line = (
                    f"Ban state: unknown (no live Reddit session for the check and "
                    f"not on the recorded denylist). Treated as a single-post "
                    f"removal until confirmed.\n"
                )
            reddit_block = count_line + ban_line
    if platform == "github" and thread_url:
        if repo_state == "repo_gone":
            repo_block = (
                "Repo state: GONE (parent repo returns 404). "
                "Owner nuked the whole repo, this is not a moderation strike "
                "against our comment specifically.\n"
            )
        elif repo_state == "issue_deleted":
            repo_block = (
                "Repo state: live but ISSUE/PR THREAD DELETED (HTTP 410 or "
                "404 on the specific issue/PR endpoint). Owner deleted the "
                "entire thread; every comment under it vanishes, not just "
                "ours. Collateral damage, not a moderation strike.\n"
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
            live_n, raw_n = _owner_strike_count(owner)
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


def _select_pending(post_id=None, limit=None):
    if post_id is not None:
        resp = api_get(f"/api/v1/posts/{int(post_id)}", ok_on_404=True)
        post = (resp.get("data") or {}).get("post") if resp else None
        return [post] if post else []
    # Mentions live in the dedicated `mentions` table now (2026-05-23 cutover);
    # no posts-level filter needed. Previously this clause excluded placeholder
    # `posts` rows where our_content = '(mention - no original post)' to avoid
    # alerting on third-party tweets that fxtwitter 404'd (spammer accounts
    # getting cleaned up). Those rows are gone after
    # migrate_mentions_out_of_posts.py --commit-delete; the posts table now
    # only contains content we authored, so every status='deleted' row IS a
    # real moderation strike against us.
    resp = api_get("/api/v1/posts/pending-strikes",
                   query={"limit": int(limit)} if limit else None)
    return (resp.get("data") or {}).get("posts") or []


def _mark_sent(post_id):
    api_patch(f"/api/v1/posts/{int(post_id)}", {"stamp_strike_email_sent_now": True})


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

    load_env()

    # Reddit user-agent for the live re-check. Mirrors stats.py:1924
    # so the pre-send re-check uses the same UA Reddit already saw on the
    # ingest side.
    try:
        from project_config import load_config as _load_cfg  # type: ignore
        _cfg = _load_cfg()
    except Exception:
        try:
            with open(os.path.join(os.path.dirname(SCRIPT_DIR), "config.json")) as _f:
                _cfg = json.load(_f)
        except Exception:
            _cfg = {}
    _reddit_username = (_cfg.get("accounts", {}) or {}).get("reddit", {}).get("username", "")
    _reddit_ua = (
        f"social-autoposter/1.0 (u/{_reddit_username})"
        if _reddit_username else "social-autoposter/1.0"
    )

    rows = _select_pending(post_id=args.post_id, limit=args.limit)
    if not rows:
        print("[strike_alert] no pending strikes")
        return

    sent = 0
    skipped = 0
    filtered = 0
    failed = 0
    for r in rows:
        # When --post-id is used, allow re-fire even if already sent.
        if args.post_id is None and r["strike_email_sent_at"] is not None:
            skipped += 1
            continue

        # Reddit live re-check (added 2026-05-16). stats.py uses a
        # 2-detection threshold which is brittle to transient scrape failures
        # and rate-limit misses. Confirmed false positives on 2026-05-07
        # (post 23005 /r/PAstudent/Active recall with Anki, post 23223 /r/
        # UniversityOfHouston/UH Finals Study App): both comments alive at
        # the time the strike email was sent. This guard fetches the
        # comment URL one more time right before the email goes out; if it's
        # still visible we flip status back to 'active' and skip the alert,
        # eliminating that class of false positive without weakening the
        # detection signal for real strikes.
        if args.post_id is None and r["platform"] == "reddit":
            live_state = _reddit_live_recheck(
                r["our_url"], r["our_account"], _reddit_ua
            )
            print(
                f"[strike_alert] id={r['id']} platform=reddit "
                f"live_recheck={live_state} url={r['our_url']}",
                flush=True,
            )
            if live_state == "alive":
                if not args.dry_run:
                    _resurrect_post(r["id"])
                filtered += 1
                print(
                    f"[strike_alert] filtered id={r['id']} reason=reddit-alive "
                    f"(false positive, status flipped back to active, no email)",
                    flush=True,
                )
                continue

        # Twitter live re-check (added 2026-06-05). stats.py marks a tweet
        # 'deleted' after 2 fxtwitter 404s, but fxtwitter's guest API returns a
        # tombstone for Community posts and some replies that are alive to a
        # logged-in viewer (5 of 6 strikes on 2026-06-05 were this false
        # positive). Re-hit fxtwitter; if it's a tombstone or a real tweet the
        # row is alive, flip it back to 'active' and skip the email.
        if args.post_id is None and r["platform"] == "twitter":
            live_state = _twitter_live_recheck(r["our_url"])
            print(
                f"[strike_alert] id={r['id']} platform=twitter "
                f"live_recheck={live_state} url={r['our_url']}",
                flush=True,
            )
            if live_state == "alive":
                if not args.dry_run:
                    _resurrect_post(r["id"])
                filtered += 1
                print(
                    f"[strike_alert] filtered id={r['id']} reason=twitter-alive "
                    f"(false positive, status flipped back to active, no email)",
                    flush=True,
                )
                continue

        repo_state = None
        if r["platform"] == "github":
            repo_state = _github_repo_state(r["thread_url"])
            print(
                f"[strike_alert] id={r['id']} platform=github "
                f"repo_state={repo_state} thread={r['thread_url']}",
                flush=True,
            )

        # GitHub collateral damage: when the parent repo 404s, or when
        # the issues/discussions feature is disabled on a live repo, the
        # comment vanished as part of a structural change, not a
        # moderation action against us. Don't email; mark sent so the
        # row drops out of the pending queue and stops being evaluated
        # every hour by the cron. The row is retained in the table for
        # archaeology and the dashboard still shows status='deleted'.
        # See the May 15 audit (15 of 27 strikes were REPOGONE) for the
        # canonical false-positive batch.
        if args.post_id is None and repo_state in (
            "repo_gone", "issue_deleted", "feature_disabled"
        ):
            if not args.dry_run:
                _mark_sent(r["id"])
            filtered += 1
            reason_map = {
                "repo_gone": "repo-gone",
                "issue_deleted": "issue-deleted",
                "feature_disabled": "feature-disabled",
            }
            reason = reason_map[repo_state]
            print(
                f"[strike_alert] filtered id={r['id']} reason={reason} "
                f"(marked sent, no email)",
                flush=True,
            )
            continue

        subject = _format_subject(r, repo_state=repo_state)
        body = _format_body(r, repo_state=repo_state)
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
            _mark_sent(r["id"])
            sent += 1
            print(f"[strike_alert] alerted id={r['id']} ({r['platform']} {r['status']})")
        except Exception as e:
            failed += 1
            print(f"[strike_alert] FAILED id={r['id']}: {e}", file=sys.stderr)

    print(
        f"[strike_alert] sent={sent} skipped={skipped} "
        f"filtered={filtered} failed={failed}"
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
