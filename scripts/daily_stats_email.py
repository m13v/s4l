#!/usr/bin/env python3
"""
daily_stats_email.py
Queries the Neon DB for the last 24h of social-autoposter activity,
formats an HTML email, and sends it via Gmail API to i@m13v.com.
"""

import atexit
import os
import subprocess
import sys
import time
import base64
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import psycopg2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

_RUN_START = time.time()
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _emit_run_log() -> None:
    elapsed = max(0, int(time.time() - _RUN_START))
    subprocess.run(
        [
            "python3", str(_REPO_ROOT / "scripts" / "log_run.py"),
            "--script", "daily_report",
            "--posted", "0", "--skipped", "0", "--failed", "0",
            "--cost", "0", "--elapsed", str(elapsed),
        ],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


atexit.register(_emit_run_log)

# Config
RECIPIENT = "i@m13v.com"
TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
CREDENTIALS_PATH = os.path.expanduser("~/gmail-api/credentials.json")
SCOPES = ["https://mail.google.com/"]

# Load DATABASE_URL from .env
ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set", file=sys.stderr)
    sys.exit(1)


def get_db():
    return psycopg2.connect(DATABASE_URL)


def query_all(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def gather_stats(conn):
    stats = {}

    # Net upvotes: Reddit and Moltbook both auto-apply a +1 OP self-upvote on
    # every post, so a freshly published post shows upvotes=1 with zero real
    # engagement. Strip that +1 per row before summing (clamped at 0 so
    # downvoted posts don't go negative) so the email reflects ORGANIC
    # engagement, not (posts * 1 self-upvote) + organic. X / LinkedIn / GitHub
    # have no equivalent auto-vote so they pass through unchanged.
    # Matches top_performers.SCORE_SQL and bin/server.js upvotes_discounted.
    upvotes_net = (
        "COALESCE(SUM(CASE WHEN LOWER(platform) IN ('reddit', 'moltbook') "
        "THEN GREATEST(0, COALESCE(upvotes, 0) - 1) "
        "ELSE COALESCE(upvotes, 0) END), 0) AS upvotes"
    )

    # 1. Posts by platform
    stats["posts_by_platform"] = query_all(conn, f"""
        SELECT platform, COUNT(*) AS posts,
               {upvotes_net},
               COALESCE(SUM(comments_count), 0) AS comments,
               COALESCE(SUM(views), 0) AS views
        FROM posts WHERE posted_at >= NOW() - INTERVAL '24 hours'
        GROUP BY platform ORDER BY posts DESC
    """)

    # 2. Posts by project
    stats["posts_by_project"] = query_all(conn, f"""
        SELECT COALESCE(project_name, '(none)') AS project, COUNT(*) AS posts,
               {upvotes_net},
               COALESCE(SUM(comments_count), 0) AS comments,
               COALESCE(SUM(views), 0) AS views
        FROM posts WHERE posted_at >= NOW() - INTERVAL '24 hours'
        GROUP BY project_name ORDER BY posts DESC
    """)

    # 3. Posts by style
    stats["posts_by_style"] = query_all(conn, f"""
        SELECT COALESCE(engagement_style, '(none)') AS style, COUNT(*) AS posts,
               {upvotes_net},
               COALESCE(SUM(comments_count), 0) AS comments,
               COALESCE(SUM(views), 0) AS views
        FROM posts WHERE posted_at >= NOW() - INTERVAL '24 hours'
        GROUP BY engagement_style ORDER BY posts DESC
    """)

    # 4. Posts by platform x project x style
    stats["posts_detail"] = query_all(conn, f"""
        SELECT platform, COALESCE(project_name, '(none)') AS project,
               COALESCE(engagement_style, '(none)') AS style,
               COUNT(*) AS posts,
               {upvotes_net},
               COALESCE(SUM(comments_count), 0) AS comments,
               COALESCE(SUM(views), 0) AS views
        FROM posts WHERE posted_at >= NOW() - INTERVAL '24 hours'
        GROUP BY platform, project_name, engagement_style
        ORDER BY platform, posts DESC
    """)

    # 5. Replies
    stats["replies"] = query_all(conn, """
        SELECT platform,
               COUNT(*) AS discovered,
               COUNT(*) FILTER (WHERE replied_at >= NOW() - INTERVAL '24 hours') AS replied,
               COALESCE(engagement_style, '(none)') AS style
        FROM replies WHERE discovered_at >= NOW() - INTERVAL '24 hours'
        GROUP BY platform, engagement_style ORDER BY platform, discovered DESC
    """)

    # 6. DMs
    stats["dms"] = query_all(conn, """
        SELECT platform, status, COUNT(*) AS cnt
        FROM dms WHERE discovered_at >= NOW() - INTERVAL '24 hours'
        GROUP BY platform, status ORDER BY platform
    """)

    # 7. SEO keywords completed
    stats["seo_completed"] = query_all(conn, """
        SELECT product, COUNT(*) AS pages_done
        FROM seo_keywords WHERE completed_at >= NOW() - INTERVAL '24 hours'
        GROUP BY product ORDER BY pages_done DESC
    """)

    # 8. SEO keywords totals by status
    stats["seo_totals"] = query_all(conn, """
        SELECT product, status, COUNT(*) AS cnt
        FROM seo_keywords
        GROUP BY product, status ORDER BY product, cnt DESC
    """)

    # 9. GSC top queries (last 24h updates)
    stats["gsc_top"] = query_all(conn, """
        SELECT product, query, impressions, clicks, ctr, position
        FROM gsc_queries
        WHERE updated_at >= NOW() - INTERVAL '24 hours'
        ORDER BY impressions DESC LIMIT 20
    """)

    return stats


def html_table(rows, columns, col_labels=None):
    if not rows:
        return "<p><em>No data</em></p>"
    labels = col_labels or columns
    html = '<table style="border-collapse:collapse;width:100%;font-size:14px;">'
    html += "<tr>"
    for label in labels:
        html += f'<th style="border:1px solid #ddd;padding:6px 10px;background:#f5f5f5;text-align:left;">{label}</th>'
    html += "</tr>"
    for row in rows:
        html += "<tr>"
        for col in columns:
            val = row.get(col, "")
            if isinstance(val, float):
                val = f"{val:.1f}"
            elif isinstance(val, int) and val >= 1000:
                val = f"{val:,}"
            html += f'<td style="border:1px solid #ddd;padding:6px 10px;">{val}</td>'
        html += "</tr>"
    html += "</table>"
    return html


def build_html(stats):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections = []
    sections.append(f"<h2>Social Autoposter Daily Report</h2><p>Generated: {now_str} (last 24 hours)</p>")

    # Posts by platform
    sections.append("<h3>Posts by Platform</h3>")
    sections.append(html_table(
        stats["posts_by_platform"],
        ["platform", "posts", "upvotes", "comments", "views"],
        ["Platform", "Posts", "Upvotes", "Comments", "Views"]
    ))

    # Posts by project
    sections.append("<h3>Posts by Project</h3>")
    sections.append(html_table(
        stats["posts_by_project"],
        ["project", "posts", "upvotes", "comments", "views"],
        ["Project", "Posts", "Upvotes", "Comments", "Views"]
    ))

    # Posts by style
    sections.append("<h3>Posts by Engagement Style</h3>")
    sections.append(html_table(
        stats["posts_by_style"],
        ["style", "posts", "upvotes", "comments", "views"],
        ["Style", "Posts", "Upvotes", "Comments", "Views"]
    ))

    # Detailed breakdown
    sections.append("<h3>Detailed: Platform x Project x Style</h3>")
    sections.append(html_table(
        stats["posts_detail"],
        ["platform", "project", "style", "posts", "upvotes", "comments", "views"],
        ["Platform", "Project", "Style", "Posts", "Upvotes", "Comments", "Views"]
    ))

    # Replies
    sections.append("<h3>Replies</h3>")
    sections.append(html_table(
        stats["replies"],
        ["platform", "style", "discovered", "replied"],
        ["Platform", "Style", "Discovered", "Replied"]
    ))

    # DMs
    sections.append("<h3>DMs</h3>")
    sections.append(html_table(
        stats["dms"],
        ["platform", "status", "cnt"],
        ["Platform", "Status", "Count"]
    ))

    # SEO pages completed
    sections.append("<h3>SEO Pages Completed (24h)</h3>")
    sections.append(html_table(
        stats["seo_completed"],
        ["product", "pages_done"],
        ["Product", "Pages Done"]
    ))

    # SEO totals
    sections.append("<h3>SEO Pipeline Totals</h3>")
    sections.append(html_table(
        stats["seo_totals"],
        ["product", "status", "cnt"],
        ["Product", "Status", "Count"]
    ))

    # GSC top queries
    sections.append("<h3>GSC Top Queries (updated in 24h)</h3>")
    sections.append(html_table(
        stats["gsc_top"],
        ["product", "query", "impressions", "clicks", "ctr", "position"],
        ["Product", "Query", "Impressions", "Clicks", "CTR", "Position"]
    ))

    body = f"""
    <html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; color: #333;">
    {''.join(sections)}
    <hr style="margin-top:30px;"><p style="font-size:12px;color:#999;">Sent by social-autoposter daily report</p>
    </body></html>
    """
    return body


def send_email(subject, html_body):
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart("alternative")
    msg["to"] = RECIPIENT
    msg["subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Email sent: id={result['id']}")
    return result


def main():
    conn = get_db()
    try:
        stats = gather_stats(conn)
    finally:
        conn.close()

    html_body = build_html(stats)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"Social Autoposter Daily Stats: {today}"
    send_email(subject, html_body)


if __name__ == "__main__":
    main()
