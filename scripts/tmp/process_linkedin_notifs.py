#!/usr/bin/env python3
"""Process LinkedIn notifications JSON, dedup against DB, insert new replies."""
from __future__ import annotations  # PEP 604 unions (str | None) for Python 3.9 launchd

import json
import os
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

# Load env
ENV_PATH = Path.home() / "social-autoposter" / ".env"
for line in ENV_PATH.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DATABASE_URL = os.environ["DATABASE_URL"]

EXCLUDED_AUTHORS = {"louis030195", "louis3195", "Louis Beaumont", "Louis"}
OWN_NAMES = {"Matthew Diakonov", "matthew diakonov", "m13v", "Matthew"}

NOTIF_PATH = Path(__file__).parent / "linkedin_notifications.json"
notifs = json.loads(NOTIF_PATH.read_text())

# Load config for project topic matching
CONFIG_PATH = Path.home() / "social-autoposter" / "config.json"
config = json.loads(CONFIG_PATH.read_text())
projects = config.get("projects", [])

def match_project_for_text(text: str) -> str:
    text_l = text.lower()
    best_name = "general"
    best_score = 0
    for p in projects:
        topics = p.get("search_topics", []) or []
        score = 0
        for t in topics:
            if not t:
                continue
            if t.lower() in text_l:
                score += 1
        if score > best_score:
            best_score = score
            best_name = p.get("name", "general")
    return best_name

# Connect DB
conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True
cur = conn.cursor(cursor_factory=RealDictCursor)

# Existing comment urns
cur.execute("SELECT their_comment_id FROM replies WHERE platform='linkedin'")
existing_urns = {row["their_comment_id"] for row in cur.fetchall() if row["their_comment_id"]}

# Engaged author+post pairs
cur.execute("""
    SELECT DISTINCT r.their_author || '|||' || p.our_url AS k
    FROM replies r JOIN posts p ON r.post_id = p.id
    WHERE r.platform='linkedin' AND r.status IN ('replied','pending','processing')
""")
engaged_pairs = {row["k"] for row in cur.fetchall() if row["k"]}

# Active linkedin posts: id -> our_url
cur.execute("SELECT id, our_url, project_name FROM posts WHERE platform='linkedin' AND status='active'")
posts_rows = cur.fetchall()

def find_post_by_activity(activity_id: str):
    if not activity_id:
        return None
    needle = f"urn:li:activity:{activity_id}"
    needle2 = f"activity:{activity_id}"
    for row in posts_rows:
        url = row.get("our_url") or ""
        if needle in url or needle2 in url:
            return row
    return None

def extract_activity_id_from_comment_urn(curn: str) -> str | None:
    if not curn:
        return None
    m = re.search(r"\(activity:(\d+)", curn)
    if m:
        return m.group(1)
    m = re.search(r"\(ugcPost:(\d+)", curn)
    if m:
        return m.group(1)  # ugcPost id = activity id
    return None

stats = {
    "new": 0,
    "already_tracked": 0,
    "author_already_engaged": 0,
    "excluded": 0,
    "own_account": 0,
    "no_comment_urn": 0,
}

for n in notifs:
    author = (n.get("author") or "").strip()
    href = n.get("href") or ""
    comment_urn = n.get("comment_urn")
    snippet = n.get("snippet") or ""

    if not comment_urn:
        stats["no_comment_urn"] += 1
        continue

    activity_id = extract_activity_id_from_comment_urn(comment_urn)
    if not activity_id:
        stats["no_comment_urn"] += 1
        continue

    if any(ex.lower() == author.lower() for ex in EXCLUDED_AUTHORS):
        stats["excluded"] += 1
        continue
    if "louis" in author.lower() and ("030195" in author.lower() or "3195" in author.lower() or "beaumont" in author.lower()):
        stats["excluded"] += 1
        continue
    if author in OWN_NAMES:
        stats["own_account"] += 1
        continue

    if comment_urn in existing_urns:
        stats["already_tracked"] += 1
        continue

    # Match post
    post = find_post_by_activity(activity_id)
    if post:
        post_id = post["id"]
        our_url = post["our_url"]
    else:
        # Create a stub post
        thread_url = f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
        our_url = thread_url
        proj = match_project_for_text(snippet)
        cur.execute(
            """
            INSERT INTO posts (platform, thread_url, thread_author, thread_author_handle,
                thread_title, thread_content, our_url, our_content, our_account,
                source_summary, project_name, engagement_style, feedback_report_used, status, posted_at)
            VALUES ('linkedin', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, 'active', NOW())
            RETURNING id, our_url
            """,
            (
                thread_url,
                author,
                author,
                "(discovered via notifications)",
                snippet,
                our_url,
                "(notification stub)",
                "linkedin:matthew-diakonov",
                f"discovered_via_notifications:{author}",
                proj,
                "curious_probe",
            ),
        )
        row = cur.fetchone()
        post_id = row["id"]
        our_url = row["our_url"]
        posts_rows.append({"id": post_id, "our_url": our_url, "project_name": proj})

    # Author+post pair check
    pair_key = f"{author}|||{our_url}"
    if pair_key in engaged_pairs:
        stats["author_already_engaged"] += 1
        continue

    # Insert reply
    cur.execute(
        """
        INSERT INTO replies (post_id, platform, their_comment_id, their_author, their_content,
            their_comment_url, depth, status)
        VALUES (%s, 'linkedin', %s, %s, %s, %s, 1, 'pending')
        """,
        (post_id, comment_urn, author, snippet, href),
    )
    existing_urns.add(comment_urn)
    engaged_pairs.add(pair_key)
    stats["new"] += 1

print(json.dumps(stats, indent=2))
cur.close()
conn.close()
