#!/usr/bin/env python3
"""
daily_stats_email.py
Weekly per-recipient social-autoposter activity report.

(The file name is legacy: this used to be a 24h daily email; it is now a 7d
weekly email. The launchd plist label is com.m13v.social-weekly-report. The
schedule is Monday 09:00 local.)

Recipients come from the `dashboard_users` table (report_enabled=true):
- Admins (admin=true, projects empty) get the unscoped master report.
- Non-admins get only their projects' slice.

The headline numbers and engagement totals mimic the dashboard Stats tab:
- Activity events: same union as /api/activity/stats (posted_thread,
  posted_comment, replied, skipped, dm_sent, dm_reply_sent, page_published_*,
  page_improved, resurrected; mention is admin-only). Counts and per-platform
  breakdown match the card grid.
- Engagement totals (views, upvotes, comments): same SUM-of-per-day-delta
  approach as /api/views|upvotes|comments/per-day (post_views_daily LAG +
  aggregate_stats_daily for the admin unscoped view). This is "total views
  earned across ALL our posts in the window", not "views on the few posts
  we made this week".

Quiet-week rule: if a recipient's scope has zero posts in the window, skip
the send (per user instruction 2026-05-14).

Flags:
  --sample        Route every recipient's report to i@m13v.com with a
                  [SAMPLE for <email>] prefix. Use this to preview before
                  going live.
  --dry-run       Print recipient list + subjects, do not send.
  --only <email>  Limit fan-out to a single recipient.
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

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

_RUN_START = time.time()
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Weekly window in hours.
WINDOW_HOURS = 168
WINDOW_LABEL = "last 7 days"


def _emit_run_log() -> None:
    elapsed = max(0, int(time.time() - _RUN_START))
    subprocess.run(
        [
            "python3", str(_REPO_ROOT / "scripts" / "log_run.py"),
            "--script", "weekly_report",
            "--posted", "0", "--skipped", "0", "--failed", "0",
            "--cost", "0", "--elapsed", str(elapsed),
        ],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


atexit.register(_emit_run_log)

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


# Mirrors bin/server.js EVENT_TYPES order so card layout matches the dashboard.
EVENT_TYPES = [
    "posted_thread", "posted_comment", "replied", "skipped",
    "mention",  # admin-only
    "dm_sent", "dm_reply_sent",
    "page_published_serp", "page_published_gsc", "page_published_reddit",
    "page_published_top", "page_published_top_post", "page_published_roundup",
    "page_improved", "page_expired", "resurrected",
]
EVENT_LABELS = {
    "posted_thread": "Thread posted",
    "posted_comment": "Comment posted",
    "replied": "Engage replied",
    "skipped": "Engage skipped",
    "mention": "Mention",
    "dm_sent": "DM sent",
    "dm_reply_sent": "DM reply",
    "page_published_serp": "Page (SERP)",
    "page_published_gsc": "Page (GSC)",
    "page_published_reddit": "Page (Reddit)",
    "page_published_top": "Page (Top)",
    "page_published_top_post": "Page (Top post)",
    "page_published_roundup": "Page (Roundup)",
    "page_improved": "Page improved",
    "page_expired": "Page expired",
    "resurrected": "Resurrected",
}


def get_db():
    return psycopg2.connect(DATABASE_URL)


def query_all(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_recipients(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT email, name, admin, projects
            FROM dashboard_users
            WHERE report_enabled
            ORDER BY admin DESC, email
        """)
        return [dict(r) for r in cur.fetchall()]


def _scope_clauses(scope, columns):
    """
    Build (filter_sql, params) tuples for the projects array. Returns dict
    keyed by column name. Empty/None scope yields ("", ()).
    """
    out = {}
    if not scope:
        for col in columns:
            out[col] = ("", ())
        return out
    for col in columns:
        out[col] = (f" AND {col} = ANY(%s)", (scope,))
    return out


def gather_activity_events(conn, scope, hours):
    """
    Same UNION-ALL shape as bin/server.js /api/activity/stats. Returns a list
    of (type, platform, count) dicts. Mention branch only included for the
    unscoped admin view (octolens_mentions has no project column).
    """
    win = f"INTERVAL '{hours} hours'"
    norm = "CASE WHEN LOWER(pl) = 'x' THEN 'twitter' ELSE LOWER(pl) END"

    pc = _scope_clauses(scope, [
        "project_name", "d.target_project", "product",
    ])
    posts_pc = pc["project_name"]
    replies_pc = pc["project_name"]
    dms_pc = pc["d.target_project"]
    seo_pc = pc["product"]

    parts = []
    params = []

    parts.append(
        "SELECT CASE WHEN thread_url = our_url AND (thread_author IS NULL OR thread_author = our_account) "
        "THEN 'posted_thread' ELSE 'posted_comment' END AS type, platform AS pl "
        f"FROM posts WHERE posted_at >= NOW() - {win}{posts_pc[0]}"
    )
    params.extend(posts_pc[1])

    parts.append(
        f"SELECT 'replied' AS type, platform AS pl FROM replies "
        f"WHERE status='replied' AND replied_at >= NOW() - {win}{replies_pc[0]}"
    )
    params.extend(replies_pc[1])

    parts.append(
        f"SELECT 'skipped' AS type, platform AS pl FROM replies "
        f"WHERE status='skipped' AND COALESCE(processing_at, discovered_at) >= NOW() - {win}{replies_pc[0]}"
    )
    params.extend(replies_pc[1])

    if not scope:
        parts.append(
            f"SELECT 'mention' AS type, platform AS pl FROM octolens_mentions "
            f"WHERE COALESCE(source_timestamp, received_at) >= NOW() - {win}"
        )

    parts.append(
        "SELECT 'dm_sent' AS type, d.platform AS pl FROM dms d "
        "WHERE EXISTS (SELECT 1 FROM dm_messages m WHERE m.dm_id = d.id "
        f"  AND m.direction='outbound' AND m.message_at >= NOW() - {win} "
        "  AND NOT EXISTS (SELECT 1 FROM dm_messages m2 WHERE m2.dm_id = d.id "
        "    AND m2.direction='outbound' AND m2.message_at < m.message_at))"
        f"{dms_pc[0]}"
    )
    params.extend(dms_pc[1])

    parts.append(
        "SELECT 'dm_reply_sent' AS type, d.platform AS pl FROM dm_messages m "
        "JOIN dms d ON d.id = m.dm_id "
        f"WHERE m.direction='outbound' AND m.message_at >= NOW() - {win} "
        "  AND EXISTS (SELECT 1 FROM dm_messages m2 WHERE m2.dm_id = m.dm_id "
        "    AND m2.direction='inbound' AND m2.message_at < m.message_at)"
        f"{dms_pc[0]}"
    )
    params.extend(dms_pc[1])

    # SEO page events: same source-bucketing the dashboard uses. Each branch
    # repeats seo_pc params, so add them once per push below.
    seo_branches = [
        ("page_published_serp",       f"AND page_url IS NOT NULL AND COALESCE(source, '') NOT IN ('reddit', 'top_page', 'top_post', 'roundup')"),
        ("page_published_reddit",     "AND page_url IS NOT NULL AND source='reddit'"),
        ("page_published_top",        "AND page_url IS NOT NULL AND source='top_page'"),
        ("page_published_top_post",   "AND page_url IS NOT NULL AND source='top_post'"),
        ("page_published_roundup",    "AND page_url IS NOT NULL AND source='roundup'"),
    ]
    for ev_type, extra in seo_branches:
        parts.append(
            f"SELECT '{ev_type}' AS type, 'seo' AS pl FROM seo_keywords "
            f"WHERE completed_at >= NOW() - {win} {extra}{seo_pc[0]}"
        )
        params.extend(seo_pc[1])

    parts.append(
        "SELECT 'page_published_gsc' AS type, 'seo' AS pl FROM gsc_queries "
        f"WHERE completed_at >= NOW() - {win} AND page_url IS NOT NULL{seo_pc[0]}"
    )
    params.extend(seo_pc[1])

    parts.append(
        "SELECT 'page_improved' AS type, 'seo' AS pl FROM seo_page_improvements "
        f"WHERE completed_at >= NOW() - {win} AND status='committed'{seo_pc[0]}"
    )
    params.extend(seo_pc[1])

    parts.append(
        "SELECT 'resurrected' AS type, platform AS pl FROM posts "
        f"WHERE resurrected_at >= NOW() - {win}{posts_pc[0]}"
    )
    params.extend(posts_pc[1])

    sql = (
        "SELECT type, " + norm + " AS platform, COUNT(*)::int AS count "
        "FROM (" + " UNION ALL ".join(parts) + ") u "
        "GROUP BY type, platform ORDER BY type, platform"
    )

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [{"type": r[0], "platform": r[1], "count": r[2]} for r in cur.fetchall()]


def gather_engagement_totals(conn, scope, hours):
    """
    Total views/upvotes/comments GAINED in the window across ALL our posts
    (not just posts made in the window). Mirrors the dashboard
    /api/views|upvotes|comments/per-day query approach: post_views_daily LAG
    delta. moltbook + github platforms have no post_views_daily snapshots so
    they're excluded the same way the dashboard does.

    Returns dict with keys: views, upvotes, comments.
    """
    days = max(1, hours // 24)
    project_filter = ""
    params = []
    if scope:
        project_filter = " AND p.project_name = ANY(%s)"
        params.append(scope)

    metrics = {}
    for col in ("views", "upvotes", "comments"):
        sql = (
            f"WITH per_post_daily AS ("
            f"  SELECT pvd.post_id, pvd.day, pvd.{col} AS metric, "
            f"    LAG(pvd.{col}) OVER (PARTITION BY pvd.post_id ORDER BY pvd.day) AS prev_metric "
            f"  FROM post_views_daily pvd "
            f"  JOIN posts p ON p.id = pvd.post_id "
            f"  WHERE LOWER(p.platform) NOT IN ('moltbook', 'github', 'github_issues') "
            f"    AND pvd.{col} IS NOT NULL "
            f"    AND pvd.day >= CURRENT_DATE - INTERVAL '{days} days'{project_filter}"
            f") "
            f"SELECT COALESCE(SUM(GREATEST(metric - prev_metric, 0)), 0)::bigint "
            f"FROM per_post_daily WHERE prev_metric IS NOT NULL"
        )
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            metrics[col] = int(cur.fetchone()[0] or 0)

    # Admin unscoped view also includes the aggregate_stats_daily reconstruction
    # branch (cross-project, cross-platform backfill). Skipped for scoped users
    # because it cannot be filtered by project.
    if not scope:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT "
                f"  COALESCE(SUM(views_gained), 0)::bigint, "
                f"  COALESCE(SUM(upvotes_gained), 0)::bigint, "
                f"  COALESCE(SUM(comments_gained), 0)::bigint "
                f"FROM aggregate_stats_daily "
                f"WHERE day >= CURRENT_DATE - INTERVAL '{days} days'"
            )
            v, u, c = cur.fetchone()
            metrics["views"] += int(v or 0)
            metrics["upvotes"] += int(u or 0)
            metrics["comments"] += int(c or 0)

    return metrics


def gather_stats(conn, projects):
    """
    Build all report sections. If `projects` is None or empty, return the
    unscoped master view. Otherwise filter every section to that scope.
    """
    stats = {}
    scope = projects or None
    hours = WINDOW_HOURS

    stats["events"] = gather_activity_events(conn, scope, hours)
    stats["totals"] = gather_engagement_totals(conn, scope, hours)

    # Per-platform / per-project / per-style "posts made in window" drill-down.
    # No engagement columns here on purpose: aggregating posts.views (cumulative
    # at-snapshot count) for posts made in a 7-day window double-counts views
    # earned before the post was made and double-counts again on Monday.
    pc = _scope_clauses(scope, ["project_name"])
    posts_pc = pc["project_name"]
    replies_pc = pc["project_name"]
    seo_pc = _scope_clauses(scope, ["product"])["product"]
    dms_pc = _scope_clauses(scope, ["target_project"])["target_project"]

    win = f"INTERVAL '{hours} hours'"

    stats["posts_by_platform"] = query_all(conn, (
        f"SELECT platform, COUNT(*) AS posts FROM posts "
        f"WHERE posted_at >= NOW() - {win}{posts_pc[0]} "
        f"GROUP BY platform ORDER BY posts DESC"
    ), posts_pc[1])

    stats["posts_by_project"] = query_all(conn, (
        f"SELECT COALESCE(project_name, '(none)') AS project, COUNT(*) AS posts FROM posts "
        f"WHERE posted_at >= NOW() - {win}{posts_pc[0]} "
        f"GROUP BY project_name ORDER BY posts DESC"
    ), posts_pc[1])

    stats["posts_by_style"] = query_all(conn, (
        f"SELECT COALESCE(engagement_style, '(none)') AS style, COUNT(*) AS posts FROM posts "
        f"WHERE posted_at >= NOW() - {win}{posts_pc[0]} "
        f"GROUP BY engagement_style ORDER BY posts DESC"
    ), posts_pc[1])

    stats["replies"] = query_all(conn, (
        f"SELECT platform, "
        f"  COUNT(*) AS discovered, "
        f"  COUNT(*) FILTER (WHERE replied_at >= NOW() - {win}) AS replied "
        f"FROM replies WHERE discovered_at >= NOW() - {win}{replies_pc[0]} "
        f"GROUP BY platform ORDER BY platform"
    ), replies_pc[1])

    stats["dms"] = query_all(conn, (
        f"SELECT platform, status, COUNT(*) AS cnt FROM dms "
        f"WHERE discovered_at >= NOW() - {win}{dms_pc[0]} "
        f"GROUP BY platform, status ORDER BY platform"
    ), dms_pc[1])

    stats["seo_completed"] = query_all(conn, (
        f"SELECT product, COUNT(*) AS pages_done FROM seo_keywords "
        f"WHERE completed_at >= NOW() - {win}{seo_pc[0]} "
        f"GROUP BY product ORDER BY pages_done DESC"
    ), seo_pc[1])

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


def fmt_int(n):
    return f"{n:,}" if isinstance(n, int) else str(n)


def build_event_card_grid(events, admin):
    """
    Render the activity-event cards in the same shape as the Stats tab grid:
    one card per event type, with total + per-platform breakdown.
    """
    by_type = {t: {"total": 0, "platforms": {}} for t in EVENT_TYPES}
    for r in events:
        t = r["type"]
        if t not in by_type:
            by_type[t] = {"total": 0, "platforms": {}}
        plat = (r["platform"] or "unknown").lower()
        by_type[t]["total"] += int(r["count"])
        by_type[t]["platforms"][plat] = by_type[t]["platforms"].get(plat, 0) + int(r["count"])

    visible = [t for t in EVENT_TYPES if (t != "mention") or admin]
    cards = []
    for t in visible:
        bucket = by_type.get(t, {"total": 0, "platforms": {}})
        total = bucket["total"]
        zero_style = "opacity:0.45;" if total == 0 else ""
        plats_sorted = sorted(bucket["platforms"].items(), key=lambda kv: -kv[1])
        plat_html = ""
        for plat, n in plats_sorted:
            plat_html += (
                f'<span style="display:inline-block;margin-right:8px;color:#555;">'
                f'{plat}: <b>{n}</b></span>'
            )
        if not plat_html:
            plat_html = '<span style="color:#bbb;">—</span>'
        cards.append(
            f'<div style="border:1px solid #e5e5e5;border-radius:8px;padding:10px 12px;'
            f'margin:0;{zero_style}background:#fff;">'
            f'<div style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:0.05em;">'
            f'{EVENT_LABELS.get(t, t)}</div>'
            f'<div style="font-size:24px;font-weight:600;margin:4px 0;">{fmt_int(total)}</div>'
            f'<div style="font-size:12px;">{plat_html}</div>'
            f'</div>'
        )
    return (
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));'
        'gap:10px;margin:8px 0 24px 0;">'
        + "".join(cards) +
        '</div>'
    )


def build_totals_strip(totals):
    """Three big numbers: views, upvotes, comments gained in window."""
    items = [
        ("Total views",    totals.get("views", 0)),
        ("Total upvotes",  totals.get("upvotes", 0)),
        ("Total comments", totals.get("comments", 0)),
    ]
    html = (
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:8px 0 24px 0;">'
    )
    for label, n in items:
        html += (
            '<div style="border:1px solid #e5e5e5;border-radius:8px;padding:14px 16px;background:#fafafa;">'
            f'<div style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:0.05em;">{label}</div>'
            f'<div style="font-size:32px;font-weight:700;margin-top:6px;color:#111;">{fmt_int(n)}</div>'
            '<div style="font-size:11px;color:#999;margin-top:4px;">gained in window, across all your posts</div>'
            '</div>'
        )
    html += '</div>'
    return html


def build_html(stats, recipient):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    name = recipient.get("name") or recipient["email"]
    scope_label = ", ".join(recipient["projects"]) if recipient["projects"] else "all projects"
    greeting_name = name.split()[0] if name and " " in name else name
    admin = bool(recipient.get("admin"))

    sections = []
    sections.append("<h2>Social Autoposter Weekly Report</h2>")
    sections.append(f"<p>Hi {greeting_name},</p>")
    sections.append(f"<p>{WINDOW_LABEL}, {scope_label}. Generated {now_str}.</p>")

    # Headline numbers row.
    sections.append("<h3 style='margin-top:24px;'>Engagement Totals</h3>")
    sections.append(build_totals_strip(stats["totals"]))

    # Activity event cards.
    sections.append("<h3>Activity</h3>")
    sections.append(build_event_card_grid(stats["events"], admin))

    # Drill-down tables. "Posts made" = posts whose posted_at falls in the
    # window. Engagement columns intentionally removed; the headline strip
    # above is the authoritative engagement number.
    sections.append("<h3>Posts Made by Platform</h3>")
    sections.append(html_table(
        stats["posts_by_platform"],
        ["platform", "posts"],
        ["Platform", "Posts"],
    ))

    sections.append("<h3>Posts Made by Project</h3>")
    sections.append(html_table(
        stats["posts_by_project"],
        ["project", "posts"],
        ["Project", "Posts"],
    ))

    sections.append("<h3>Posts Made by Engagement Style</h3>")
    sections.append(html_table(
        stats["posts_by_style"],
        ["style", "posts"],
        ["Style", "Posts"],
    ))

    sections.append("<h3>Replies</h3>")
    sections.append(html_table(
        stats["replies"],
        ["platform", "discovered", "replied"],
        ["Platform", "Discovered", "Replied"],
    ))

    sections.append("<h3>DMs</h3>")
    sections.append(html_table(
        stats["dms"],
        ["platform", "status", "cnt"],
        ["Platform", "Status", "Count"],
    ))

    if stats["seo_completed"]:
        sections.append("<h3>SEO Pages Completed</h3>")
        sections.append(html_table(
            stats["seo_completed"],
            ["product", "pages_done"],
            ["Product", "Pages Done"],
        ))

    footer_scope = f"covers {scope_label}" if recipient["projects"] else "is the unscoped operator view"
    sections.append(
        '<hr style="margin-top:30px;">'
        '<p style="font-size:12px;color:#999;">'
        f'This report {footer_scope}. '
        'Live numbers at <a href="https://app.s4l.ai">app.s4l.ai</a>. '
        'Questions: reply to this email.'
        '</p>'
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

            # Quiet-week rule: skip recipients whose scope had zero posts in
            # the window. The headline engagement total (views/upvotes/comments)
            # could still be non-zero for old posts even when the user posted
            # nothing this week, but the user's gate is on "posts that we made".
            posts_this_window = sum(int(row.get("posts") or 0) for row in stats["posts_by_platform"])
            if posts_this_window == 0:
                print(f"  SKIP -> {r['email']:30s}  (no posts in scope last 7 days)")
                continue

            html_body = build_html(stats, r)

            scope = ", ".join(r["projects"]) if r["projects"] else "all"
            real_subject = f"Social Autoposter Weekly Report ({scope}): {today}"
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
