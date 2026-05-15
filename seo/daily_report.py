#!/usr/bin/env python3
"""
Weekly SEO pipeline report. Queries Postgres for the last 7d of activity and
sends a per-recipient summary email via Gmail API.

(The file name is legacy: this used to be a 24h daily email; it is now a 7d
weekly email. The launchd plist label is com.m13v.seo-weekly-report.)

Recipients come from `dashboard_users` (report_enabled=true). Admins get the
unscoped master view. Non-admins get their projects' slice; if none of their
projects participate in the SEO pipeline, the report is skipped for them.

Usage:
    python3 daily_report.py                 # live send to all recipients
    python3 daily_report.py --sample        # send to i@m13v.com with [SAMPLE for <email>] prefix
    python3 daily_report.py --dry-run       # print plan, no email
    python3 daily_report.py --only <email>  # limit to one recipient (combine with --sample)
"""

import argparse
import atexit
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

GMAIL_DIR = Path.home() / "gmail-api"
sys.path.insert(0, str(GMAIL_DIR))

SCRIPT_DIR = Path(__file__).parent
ENV_PATH = SCRIPT_DIR.parent / ".env"

_RUN_START = time.time()


def _emit_run_log() -> None:
    elapsed = max(0, int(time.time() - _RUN_START))
    subprocess.run(
        [
            "python3", str(SCRIPT_DIR.parent / "scripts" / "log_run.py"),
            "--script", "seo_weekly_report",
            "--posted", "0", "--skipped", "0", "--failed", "0",
            "--cost", "0", "--elapsed", str(elapsed),
        ],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


atexit.register(_emit_run_log)

OPERATOR_EMAIL = "i@m13v.com"
TOKEN_PATH = GMAIL_DIR / "token_i_at_m13v.com.json"
CREDENTIALS_PATH = GMAIL_DIR / "credentials.json"

# Products actively in the SEO pipeline. Used to short-circuit recipients
# whose projects don't intersect (e.g. Kent gets nothing here).
SEO_PRODUCTS = {"Assrt", "Cyrano", "Fazm", "PieLine", "NightOwl"}


def get_db_url() -> str:
    with open(ENV_PATH) as f:
        for line in f:
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("DATABASE_URL not found in .env")


def load_recipients(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT email, name, admin, projects
        FROM dashboard_users
        WHERE report_enabled
        ORDER BY admin DESC, email
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    return rows


def query_report(conn, projects) -> dict:
    """If projects is None/empty, return unscoped master view."""
    cur = conn.cursor()
    now_utc = datetime.now(timezone.utc)
    since = now_utc - timedelta(days=7)
    scope = projects or None

    pages_filter = ""
    pool_filter = ""
    stuck_filter = ""
    summary_filter = ""
    pages_params = [since]
    summary_params = [since]
    pool_params = []
    stuck_params = []
    if scope:
        pages_filter = " AND product = ANY(%s)"
        pool_filter = " WHERE product = ANY(%s)"
        stuck_filter = " AND product = ANY(%s)"
        summary_filter = " AND product = ANY(%s)"
        pages_params.append(scope)
        summary_params.append(scope)
        pool_params.append(scope)
        stuck_params.append(scope)

    cur.execute(f"""
        SELECT product, keyword, slug, page_url, completed_at
        FROM seo_keywords
        WHERE status = 'done' AND page_url IS NOT NULL
          AND completed_at >= %s{pages_filter}
        ORDER BY completed_at DESC
    """, pages_params)
    pages_created = cur.fetchall()

    cur.execute(f"""
        SELECT product, status, COUNT(*)
        FROM seo_keywords{pool_filter}
        GROUP BY product, status
        ORDER BY product, status
    """, pool_params)
    pool_status = cur.fetchall()

    cur.execute(f"""
        SELECT product, keyword, status
        FROM seo_keywords
        WHERE status IN ('scoring', 'in_progress'){stuck_filter}
        ORDER BY product
    """, stuck_params)
    stuck = cur.fetchall()

    cur.close()
    return {
        "pages_created": pages_created,
        "pool_status": pool_status,
        "stuck": stuck,
        "since": since,
        "now": now_utc,
        "scope": scope,
    }


def format_report(data: dict, recipient: dict) -> tuple[str, str]:
    pages = data["pages_created"]
    since = data["since"]
    now = data["now"]
    stuck = data["stuck"]
    scope = data["scope"]

    name = recipient.get("name") or recipient["email"]
    greeting_name = name.split()[0] if name and " " in name else name
    scope_label = ", ".join(scope) if scope else "all products"

    date_str = now.strftime("%Y-%m-%d")
    subject = f"SEO Pipeline Weekly ({scope_label}): {len(pages)} pages created ({date_str})"

    lines = []
    lines.append("<h2>SEO Pipeline Weekly Report</h2>")
    lines.append(f"<p>Hi {greeting_name},</p>")
    lines.append(
        f"<p>Last 7 days, scope: {scope_label}. "
        f"Period: {since.strftime('%Y-%m-%d %H:%M')} to "
        f"{now.strftime('%Y-%m-%d %H:%M')} UTC.</p>"
    )

    lines.append(f"<h3>{len(pages)} Pages Created</h3>")
    if pages:
        lines.append("<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:14px'>")
        lines.append("<tr><th>Product</th><th>Keyword</th><th>URL</th><th>Time</th></tr>")
        for product, keyword, slug, url, completed_at in pages:
            time_str = completed_at.strftime("%H:%M") if completed_at else "?"
            lines.append(
                f"<tr><td>{product}</td><td>{keyword}</td>"
                f"<td><a href='{url}'>{slug}</a></td><td>{time_str}</td></tr>"
            )
        lines.append("</table>")
        from collections import Counter
        product_counts = Counter(p[0] for p in pages)
        lines.append(
            "<p><strong>By product:</strong> "
            + ", ".join(f"{p}: {c}" for p, c in sorted(product_counts.items()))
            + "</p>"
        )
    else:
        lines.append("<p>No pages created in this period.</p>")

    lines.append("<h3>Keyword Pool</h3>")
    lines.append("<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:14px'>")
    lines.append(
        "<tr><th>Product</th><th>Done</th><th>Skip</th><th>Unscored</th>"
        "<th>Scoring</th><th>Pending</th><th>In Progress</th></tr>"
    )
    pool = {}
    for product, status, count in data["pool_status"]:
        pool.setdefault(product, {})[status] = count
    for product in sorted(pool.keys()):
        s = pool[product]
        unscored = s.get("unscored", 0)
        warning = " (low)" if unscored < 20 else ""
        lines.append(
            f"<tr><td>{product}</td>"
            f"<td>{s.get('done', 0)}</td>"
            f"<td>{s.get('skip', 0)}</td>"
            f"<td>{unscored}{warning}</td>"
            f"<td>{s.get('scoring', 0)}</td>"
            f"<td>{s.get('pending', 0)}</td>"
            f"<td>{s.get('in_progress', 0)}</td></tr>"
        )
    lines.append("</table>")

    if stuck:
        lines.append(f"<h3>Stuck Keywords ({len(stuck)})</h3>")
        lines.append("<p style='color:#b00'>These keywords are stuck in scoring/in_progress and may need manual intervention:</p>")
        lines.append("<ul>")
        for product, keyword, status in stuck:
            lines.append(f"<li>[{product}] {keyword} ({status})</li>")
        lines.append("</ul>")

    footer_scope = f"covers {scope_label}" if scope else "is the unscoped operator view"
    lines.append(
        f"<hr><p style='color:gray;font-size:12px'>"
        f"This SEO report {footer_scope}. "
        f"Sign in at <a href='https://app.s4l.ai'>app.s4l.ai</a> for the live dashboard. "
        f"Questions: reply to this email."
        f"</p>"
    )

    return subject, "\n".join(lines)


def send_email(to_addr: str, subject: str, html_body: str) -> None:
    from gmail_client import GmailClient
    client = GmailClient(
        credentials_path=str(CREDENTIALS_PATH),
        token_path=str(TOKEN_PATH),
    )
    client.authenticate()
    client.send_message(to=to_addr, subject=subject, body=html_body, html=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true",
                        help="Route every recipient's report to i@m13v.com with a [SAMPLE for <email>] prefix.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan, do not send.")
    parser.add_argument("--only", type=str, default=None,
                        help="Only process the recipient with this email (case-insensitive).")
    args = parser.parse_args()

    import psycopg2
    conn = psycopg2.connect(get_db_url())
    try:
        recipients = load_recipients(conn)
        if args.only:
            wanted = args.only.lower()
            recipients = [r for r in recipients if r["email"].lower() == wanted]
            if not recipients:
                print(f"No recipient matches --only={args.only}", file=sys.stderr)
                sys.exit(2)

        mode = " (SAMPLE mode)" if args.sample else ""
        print(f"Processing {len(recipients)} recipient(s){mode}")
        for r in recipients:
            projects = r["projects"] or []
            # Quiet-week rule (per user instruction 2026-05-14): skip recipients
            # whose scope had zero pages created in the last 7 days. Applies in
            # both live and sample modes so the preview matches reality.
            # Recipients whose projects don't intersect SEO_PRODUCTS at all
            # naturally have pages_created=[], so they fall through the same
            # gate without needing a separate check.
            data = query_report(conn, projects)
            if not data["pages_created"]:
                print(f"  SKIP -> {r['email']:30s}  (no pages created in scope last 7 days)")
                continue

            subject, html_body = format_report(data, r)

            to_addr = OPERATOR_EMAIL if args.sample else r["email"]
            full_subject = (
                f"[SAMPLE for {r['email']}] {subject}"
                if args.sample else subject
            )

            if args.dry_run:
                print(f"  DRY  -> {to_addr:30s}  subj='{full_subject}'")
                continue

            send_email(to_addr, full_subject, html_body)
            print(f"  SENT -> {to_addr:30s}  subj='{full_subject}'")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
