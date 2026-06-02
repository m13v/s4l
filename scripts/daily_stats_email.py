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


def load_recipients():
    data = api_get("/api/v1/dashboard-users", query={"mode": "recipients"}).get("data") or {}
    return data.get("recipients") or []


def gather_stats(projects):
    """
    Build all report sections via one HTTP call to /api/v1/stats/weekly-social.
    If `projects` is None or empty, the endpoint returns the unscoped master
    view (includes the mention branch + aggregate_stats_daily reconstruction).
    Otherwise every section is filtered to that scope.
    """
    scope = projects or []
    data = (api_get("/api/v1/stats/weekly-social", query={
        "hours": int(WINDOW_HOURS),
        "scope": ",".join(scope) if scope else "",
    }).get("data") or {})

    engagement = data.get("engagement") or {}
    return {
        "events": data.get("activity") or [],
        "totals": {
            "views": int(engagement.get("views") or 0),
            "upvotes": int(engagement.get("upvotes") or 0),
            "comments": int(engagement.get("comments") or 0),
        },
        "posts_by_platform": data.get("posts_by_platform") or [],
        "posts_by_project": data.get("posts_by_project") or [],
        "posts_by_style": data.get("posts_by_style") or [],
        "replies": data.get("replies") or [],
        "dms": data.get("dms") or [],
        "seo_completed": data.get("seo_completed") or [],
    }


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

    recipients = load_recipients()
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
        stats = gather_stats(r["projects"])

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


if __name__ == "__main__":
    main()
