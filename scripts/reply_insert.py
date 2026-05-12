#!/usr/bin/env python3
"""Shared reply-insertion helper for scan_*_replies.py scripts.

`insert_reply` returns the status string on a NEW insert, or None if the row
already existed. Callers use the return value to update their discovered /
skipped counters.

2026-05-12: dedup now happens exclusively server-side. The /api/v1/replies
POST endpoint has a UNIQUE (platform, their_comment_id) index and uses
ON CONFLICT DO NOTHING; a duplicate returns 409 with the existing row, and
api_post(ok_on_conflict=True) surfaces that as a body with an "error" key.
Previously this module did a `SELECT COUNT(*)` probe before posting; that
was the last direct-SQL hop in the scan-reddit-replies path and has been
removed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def insert_reply(
    db,
    post_id,
    platform,
    comment_id,
    author,
    content,
    comment_url,
    parent_reply_id=None,
    depth=1,
    status="pending",
    skip_reason=None,
    moltbook_post_uuid=None,
    moltbook_parent_comment_uuid=None,
    our_reply_id=None,
    our_reply_content=None,
    our_reply_url=None,
    replied_at=None,
):
    """Insert a reply via /api/v1/replies POST.

    The `db` arg is preserved in the signature for backwards compatibility
    with callers that still pass a psycopg connection — the value is IGNORED.
    All writes go through HTTP now.

    Returns:
        status string when this call performed the INSERT
        None when the (platform, their_comment_id) was already in the table
    """
    comment_id = str(comment_id)

    from http_api import api_post
    body = {
        "platform": platform,
        "their_comment_id": comment_id,
        "status": status,
    }
    if post_id is not None:
        body["post_id"] = post_id
    if author is not None:
        body["their_author"] = author
    if content is not None:
        body["their_content"] = content
    if comment_url is not None:
        body["their_comment_url"] = comment_url
    if parent_reply_id is not None:
        body["parent_reply_id"] = parent_reply_id
    if depth != 1:
        body["depth"] = depth
    if skip_reason is not None:
        body["skip_reason"] = skip_reason
    if moltbook_post_uuid is not None:
        body["moltbook_post_uuid"] = moltbook_post_uuid
    if moltbook_parent_comment_uuid is not None:
        body["moltbook_parent_comment_uuid"] = moltbook_parent_comment_uuid
    if our_reply_id is not None:
        body["our_reply_id"] = our_reply_id
    if our_reply_content is not None:
        body["our_reply_content"] = our_reply_content
    if our_reply_url is not None:
        body["our_reply_url"] = our_reply_url
    if replied_at is not None:
        body["replied_at"] = (
            replied_at.isoformat() if hasattr(replied_at, "isoformat") else str(replied_at)
        )

    resp = api_post("/api/v1/replies", body, ok_on_conflict=True)
    if resp is None:
        return None
    # 409 path returns a body with an "error" key (duplicate_reply); treat as
    # "already in DB" -> None to mirror the previous behavior.
    if resp.get("error"):
        return None
    data = resp.get("data") if isinstance(resp, dict) else None
    if not data or not data.get("reply"):
        return None
    return status
