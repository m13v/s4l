#!/usr/bin/env python3
"""
daily_stats_email.py
Queries the Neon DB for the last 24h of social-autoposter activity and sends
a per-recipient HTML email summary via Gmail API.

Recipients come from the `dashboard_users` table (report_enabled=true):
- Admins (admin=true, projects empty) get the unscoped master report.
- Non-admins get only their projects' slice. SEO/GSC sections are dropped if
  none of their projects participate in the SEO pipeline.

Flags:
  --sample        Send every report to i@m13v.com instead of the real
                  recipient, prefixed with "[SAMPLE for <recipient>]".
                  Use this to preview the per-user output before going live.
  --dry-run       Print recipient list + subjects, do not send.
  --only <email>  Limit fan-out to a single recipient. Combine with --sample.
"""

import argparse
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
from psycopg2.extras import RealDictCursor
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

# Operator inbox. All --sample sends are routed here. The Gmail OAuth token
# also belongs to this address, so it's the From: as well.
OPERATOR_EMAIL = "i@m13v.com"
TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
CREDENTIALS_PATH = os.path.expanduser("~/gmail-api/credentials.json")
SCOPES = ["https://mail.google.com/"]

ENV_FILE = _REPO_ROOT / ".env"
if ENV_FILE.exists():
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


def load_recipients(conn):
    """Return list of dicts with email, name, admin, projects."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT email, name, admin, projects
            FROM dashboard_users
            WHERE report_enabled
            ORDER BY admin DESC, email
        """)
        return [dict(r) for r in cur.fetchall()]


def gather_stats(conn, projects):
    """
    Build the 9 sections of the report. If `projects` is None or empty,
    return the unscoped master view (admin). Otherwise filter every section
    to rows whose project (or product, for SEO/GSC) is in the list.
    """
    stats = {}
    scope = projects or None

    # Reddit/Moltbook auto +1 OP self-upvote stripped for organic accuracy.
    upvotes_net = (
        "COALESCE(SUM(CASE WHEN LOWER(platform) IN ('reddit', 'moltbook') "
        "THEN GREATEST(0, COALESCE(upvotes, 0) - 1) "
        "ELSE COALESCE(upvotes, 0) END), 0) AS upvotes"
    )

    # SQL fragments swap in or out based on scope. For posts and replies
    # we filter on project_name; for dms on target_project; for SEO/GSC on
    # product. We never JOIN config; the project name itself is the key.
    posts_filter = ""
    replies_filter = ""
    dms_filter = ""
    seo_filter = ""
    gsc_filter = ""
    params_posts = ()
    params_replies = ()
    params_dms = ()
    params_seo = ()
    params_gsc = ()
    if scope:
        posts_filter = " AND project_name = ANY(%s)"
        replies_filter = " AND project_name = ANY(%s)"
        dms_filter = " AND target_project = ANY(%s)"
        seo_filter = " AND product = ANY(%s)"
        gsc_filter = " AND product = ANY(%s)"
        params_posts = (scope,)
        params_replies = (scope,)
        params_dms = (scope,)
        params_seo = (scope,)
        params_gsc = (scope,)

    stats["posts_by_platform"] = query_all(conn, f"""
        SELECT platform, COUNT(*) AS posts,
               {upvotes_net},
               COALESCE(SUM(comments_count), 0) AS comments,
               COALESCE(SUM(views), 0) AS views
        FROM posts WHERE posted_at >= NOW() - INTERVAL '24 hours'{posts_filter}
        GROUP BY platform ORDER BY posts DESC
    """, params_posts)

    stats["posts_by_project"] = query_all(conn, f"""
        SELECT COALESCE(project_name, '(none)') AS project, COUNT(*) AS posts,
               {upvotes_net},
               COALESCE(SUM(comments_count), 0) AS comments,
               COALESCE(SUM(views), 0) AS views
        FROM posts WHERE posted_at >= NOW() - INTERVAL '24 hours'{posts_filter}
        GROUP BY project_name ORDER BY posts DESC
    """, params_posts)

    stats["posts_by_style"] = query_all(conn, f"""
        SELECT COALESCE(engagement_style, '(none)') AS style, COUNT(*) AS posts,
               {upvotes_net},
               COALESCE(SUM(comments_count), 0) AS comments,
               COALESCE(SUM(views), 0) AS views
        FROM posts WHERE posted_at >= NOW() - INTERVAL '24 hours'{posts_filter}
        GROUP BY engagement_style ORDER BY posts DESC
    """, params_posts)

    stats["posts_detail"] = query_all(conn, f"""
        SELECT platform, COALESCE(project_name, '(none)') AS project,
               COALESCE(engagement_style, '(none)') AS style,
               COUNT(*) AS posts,
               {upvotes_net},
               COALESCE(SUM(comments_count), 0) AS comments,
               COALESCE(SUM(views), 0) AS views
        FROM posts WHERE posted_at >= NOW() - INTERVAL '24 hours'{posts_filter}
        GROUP BY platform, project_name, engagement_style
        ORDER BY platform, posts DESC
    """, params_posts)

    stats["replies"] = query_all(conn, f"""
        SELECT platform,
               COUNT(*) AS discovered,
               COUNT(*) FILTER (WHERE replied_at >= NOW() - INTERVAL '24 hours') AS replied,
               COALESCE(engagement_style, '(none)') AS style
        FROM replies WHERE discovered_at >= NOW() - INTERVAL '24 hours'{replies_filter}
        GROUP BY platform, engagement_style ORDER BY platform, discovered DESC
    """, params_replies)

    stats["dms"] = query_all(conn, f"""
        SELECT platform, status, COUNT(*) AS cnt
        FROM dms WHERE discovered_at >= NOW() - INTERVAL '24 hours'{dms_filter}
        GROUP BY platform, status ORDER BY platform
    """, params_dms)

    stats["seo_completed"] = query_all(conn, f"""
        SELECT product, COUNT(*) AS pages_done
        FROM seo_keywords WHERE completed_at >= NOW() - INTERVAL '24 hours'{seo_filter}
        GROUP BY product ORDER BY pages_done DESC
    """, params_seo)

    stats["seo_totals"] = query_all(conn, f"""
        SELECT product, status, COUNT(*) AS cnt
        FROM seo_keywords
        WHERE 1=1{seo_filter}
        GROUP BY product, status ORDER BY product, cnt DESC
    """, params_seo)

    stats["gsc_top"] = query_all(conn, f"""
        SELECT product, query, impressions, clicks, ctr, position
        FROM gsc_queries
        WHERE updated_at >= NOW() - INTERVAL '24 hours'{gsc_filter}
        ORDER BY impressions DESC LIMIT 20
    """, params_gsc)

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


def build_html(stats, recipient):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    name = recipient.get("name") or recipient["email"]
    scope_label = ", ".join(recipient["projects"]) if recipient["projects"] else "all projects"
    greeting_name = name.split()[0] if name and " " in name else name

    sections = []
    sections.append(f"<h2>Social Autoposter Daily Report</h2>")
    sections.append(f"<p>Hi {greeting_name},</p>")
    sections.append(f"<p>Last 24 hours, {scope_label}. Generated {now_str}.</p>")

    sections.append("<h3>Posts by Platform</h3>")
    sections.append(html_table(
        stats["posts_by_platform"],
        ["platform", "posts", "upvotes", "comments", "views"],
        ["Platform", "Posts", "Upvotes", "Comments", "Views"]
    ))

    sections.append("<h3>Posts by Project</h3>")
    sections.append(html_table(
        stats["posts_by_project"],
        ["project", "posts", "upvotes", "comments", "views"],
        ["Project", "Posts", "Upvotes", "Comments", "Views"]
    ))

    sections.append("<h3>Posts by Engagement Style</h3>")
    sections.append(html_table(
        stats["posts_by_style"],
        ["style", "posts", "upvotes", "comments", "views"],
        ["Style", "Posts", "Upvotes", "Comments", "Views"]
    ))

    sections.append("<h3>Detailed: Platform x Project x Style</h3>")
    sections.append(html_table(
        stats["posts_detail"],
        ["platform", "project", "style", "posts", "upvotes", "comments", "views"],
        ["Platform", "Project", "Style", "Posts", "Upvotes", "Comments", "Views"]
    ))

    sections.append("<h3>Replies</h3>")
    sections.append(html_table(
        stats["replies"],
        ["platform", "style", "discovered", "replied"],
        ["Platform", "Style", "Discovered", "Replied"]
    ))

    sections.append("<h3>DMs</h3>")
    sections.append(html_table(
        stats["dms"],
        ["platform", "status", "cnt"],
        ["Platform", "Status", "Count"]
    ))

    # SEO + GSC: only meaningful if any of the recipient's projects actually
    # appear in seo_keywords. Skip the empty sections for non-SEO clients
    # (Kent, NightOwl) to keep their report short and on-topic.
    has_seo_data = bool(stats["seo_completed"] or stats["seo_totals"])
    if has_seo_data:
        sections.append("<h3>SEO Pages Completed (24h)</h3>")
        sections.append(html_table(
            stats["seo_completed"],
            ["product", "pages_done"],
            ["Product", "Pages Done"]
        ))

        sections.append("<h3>SEO Pipeline Totals</h3>")
        sections.append(html_table(
            stats["seo_totals"],
            ["product", "status", "cnt"],
            ["Product", "Status", "Count"]
        ))

    if stats["gsc_top"]:
        sections.append("<h3>GSC Top Queries (updated in 24h)</h3>")
        sections.append(html_table(
            stats["gsc_top"],
            ["product", "query", "impressions", "clicks", "ctr", "position"],
            ["Product", "Query", "Impressions", "Clicks", "CTR", "Position"]
        ))

    footer_scope = f"covers {scope_label}" if recipient["projects"] else "is the unscoped operator view"
    sections.append(
        f'<hr style="margin-top:30px;">'
        f'<p style="font-size:12px;color:#999;">'
        f'This report {footer_scope}. '
        f'Sign in at <a href="https://app.s4l.ai">app.s4l.ai</a> for the live dashboard. '
        f'Questions: reply to this email.'
        f'</p>'
    )

    body = (
        '<html><body style="font-family: -apple-system, BlinkMacSystemFont, '
        '\'Segoe UI\', Roboto, sans-serif; max-width: 900px; margin: 0 auto; color: #333;">'
        + "".join(sections) +
        '</body></html>'
    )
    return body


def gmail_service():
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def send_email(service, to_addr, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["to"] = to_addr
    msg["subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return result["id"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true",
                        help="Route every recipient's report to i@m13v.com with a [SAMPLE for <email>] prefix.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan, do not send.")
    parser.add_argument("--only", type=str, default=None,
                        help="Only process the recipient with this email (case-insensitive).")
    args = parser.parse_args()

    conn = get_db()
    try:
        recipients = load_recipients(conn)
        if args.only:
            wanted = args.only.lower()
            recipients = [r for r in recipients if r["email"].lower() == wanted]
            if not recipients:
                print(f"No recipient matches --only={args.only}", file=sys.stderr)
                sys.exit(2)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        service = None if args.dry_run else gmail_service()

        mode = " (SAMPLE mode)" if args.sample else ""
        print(f"Processing {len(recipients)} recipient(s){mode}")
        for r in recipients:
            stats = gather_stats(conn, r["projects"])
            html_body = build_html(stats, r)

            scope = ", ".join(r["projects"]) if r["projects"] else "all"
            real_subject = f"Social Autoposter Daily Stats ({scope}): {today}"
            to_addr = OPERATOR_EMAIL if args.sample else r["email"]
            subject = (
                f"[SAMPLE for {r['email']}] {real_subject}"
                if args.sample else real_subject
            )

            if args.dry_run:
                print(f"  DRY  -> {to_addr:30s}  subj='{subject}'")
                continue

            mid = send_email(service, to_addr, subject, html_body)
            print(f"  SENT -> {to_addr:30s}  id={mid}  subj='{subject}'")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
