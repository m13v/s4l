#!/usr/bin/env python3
"""Scan GitHub issues for new replies to our comments.

Finds all issues we've commented on, checks for new comments from other users,
inserts into `replies` table as 'pending' or 'skipped'.

Works by scanning via thread_url + gh API - doesn't require our_url to be set.
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post

MIN_WORDS = 5
# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config
CONFIG_PATH = _canonical_config_path()

# NOTE: posts/replies for GitHub live under platform='github' in the DB; the
# 'github_issues' value used here matches zero rows, so Phase A has long been a
# no-op. Preserved verbatim during the HTTP-only migration to avoid an
# unrequested volume/cost change (switching to 'github' would suddenly scan all
# ~6.8k GitHub posts). If you want to actually scan GitHub replies, flip
# SCAN_PLATFORM to 'github' deliberately.
SCAN_PLATFORM = "github_issues"




def word_count(text):
    return len(text.split()) if text else 0


def main():
    config = load_config()
    from account_resolver import resolve as _resolve_account
    github_user = _resolve_account("github") or ""

    # Get all active GitHub posts we've commented on. The posts GET returns id +
    # thread_url together, so we capture the post_id map here and skip the
    # per-thread lookup the direct-SQL version used to do.
    resp = api_get("/api/v1/posts",
                   query={"platform": SCAN_PLATFORM, "status": "active", "limit": 500})
    rows = ((resp or {}).get("data") or {}).get("posts") or []

    issues = {}
    post_id_by_url = {}
    for row in rows:
        url = row.get("thread_url")
        if not url:
            continue
        # First post per thread_url wins (mirrors the old "use the first one").
        post_id_by_url.setdefault(url, row.get("id"))
        match = re.match(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)", url)
        if match:
            repo = match.group(1)
            issue_num = match.group(2)
            issues[f"{repo}/{issue_num}"] = url

    # Load exclusions
    excluded_authors = {a.lower() for a in config.get("exclusions", {}).get("authors", [])}
    excluded_repos = {r.lower() for r in config.get("exclusions", {}).get("github_repos", [])}

    # Filter out issues from excluded repos
    issues = {k: v for k, v in issues.items()
              if not any(repo_pat in k.lower() for repo_pat in excluded_repos)}

    print(f"Scanning {len(issues)} GitHub issues for replies...")

    discovered = 0
    skipped = 0
    errors = 0

    for issue_key, thread_url in issues.items():
        repo, issue_num = issue_key.rsplit("/", 1)

        # post_id captured alongside thread_url in the posts GET above.
        post_id = post_id_by_url.get(thread_url)
        if not post_id:
            continue

        # Fetch all comments on the issue
        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{repo}/issues/{issue_num}/comments",
                 "--jq", f'[.[] | {{id: .id, user: .user.login, body: .body, url: .html_url, created: .created_at}}]'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                errors += 1
                continue
            comments = json.loads(result.stdout) if result.stdout.strip() else []
        except Exception as e:
            print(f"  ERROR scanning {issue_key}: {e}")
            errors += 1
            continue

        # Find our comments to know their timestamps
        our_comments = [c for c in comments if c.get("user") == github_user]
        other_comments = [c for c in comments if c.get("user") != github_user]

        if not our_comments:
            continue

        # Get the timestamp of our first comment
        our_first_ts = min(c["created"] for c in our_comments)

        # Only look at comments after our first comment
        replies_to_us = [c for c in other_comments if c["created"] > our_first_ts]

        for comment in replies_to_us:
            author = comment.get("user", "")
            body = comment.get("body", "")
            comment_id = str(comment.get("id", ""))
            comment_url = comment.get("url", "")

            # Determine status + skip_reason up front; the (platform,
            # their_comment_id) UNIQUE index on the API handles "already
            # tracked" (returns 409), so the old COUNT pre-check is gone.
            if author.lower() in excluded_authors:
                status, skip_reason = "skipped", "excluded_author"
            elif word_count(body) < MIN_WORDS:
                status, skip_reason = "skipped", f"too_short ({word_count(body)} words)"
            else:
                status, skip_reason = "pending", None

            payload = {
                "post_id": post_id,
                "platform": SCAN_PLATFORM,
                "their_comment_id": comment_id,
                "their_author": author,
                "their_content": body,
                "their_comment_url": comment_url,
                "depth": 1,
                "status": status,
            }
            if skip_reason:
                payload["skip_reason"] = skip_reason

            resp = api_post("/api/v1/replies", payload, ok_on_conflict=True)
            if not (resp or {}).get("ok"):
                # 409 duplicate_reply: already tracked from a prior run. Skip.
                continue
            reply = ((resp or {}).get("data") or {}).get("reply")
            if reply is None:
                # Blocklist / velocity gate dropped this fresh pending row.
                continue
            if status == "skipped":
                skipped += 1
            else:
                discovered += 1
                print(f"  NEW: @{author} on {issue_key}: {body[:80]}...")

        time.sleep(1)  # Light rate limiting

    print(f"\nGitHub scan complete: {discovered} new pending, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
