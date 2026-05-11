#!/usr/bin/env python3
"""Unified funnel stats per project: social posts -> pageviews -> CTA clicks -> bookings.

Reads config.json for project definitions, queries:
  - Posts DB (DATABASE_URL): post counts, engagement by project
  - PostHog API (POSTHOG_PERSONAL_API_KEY): pageviews + CTA clicks by domain
  - Bookings DB (BOOKINGS_DATABASE_URL): cal_bookings by client_slug

Usage:
    python3 scripts/project_stats.py [--project NAME] [--days 30] [--quiet]
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod
from project_slugs import get_client_slug, get_booking_table  # noqa: E402

ENV_PATH = os.path.expanduser("~/social-autoposter/.env")
CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")


def load_env():
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_post_stats(conn, project_name, days):
    """Get social post stats for a project from the posts DB.

    upvotes is reported NET of the OP self-upvote on Reddit and Moltbook
    (those platforms automatically +1 every post; we strip that per row,
    clamped at 0, so the dashboard total reflects organic engagement
    rather than (posts * 1) + organic). Other platforms pass through.
    Mirrors top_performers.SCORE_SQL and bin/server.js upvotes_discounted.
    """
    cur = conn.execute(
        "SELECT COUNT(*), "
        "COUNT(*) FILTER (WHERE posted_at >= NOW() - INTERVAL '" + str(days) + " days'), "
        "COUNT(*) FILTER (WHERE status = 'active'), "
        "COUNT(*) FILTER (WHERE status IN ('removed', 'deleted')), "
        "COALESCE(SUM(CASE WHEN LOWER(platform) IN ('reddit', 'moltbook') "
        "  THEN GREATEST(0, COALESCE(upvotes, 0) - 1) "
        "  ELSE COALESCE(upvotes, 0) END), 0), "
        "COALESCE(SUM(comments_count), 0), "
        "COALESCE(SUM(views), 0) "
        "FROM posts WHERE project_name = %s",
        (project_name,),
    )
    row = cur.fetchone()
    if not row:
        return {}
    cols = ["total", "recent", "active", "removed", "total_upvotes", "total_comments", "total_views"]
    return dict(zip(cols, row))


def get_platform_breakdown(conn, project_name, days):
    """Get per-platform post counts."""
    cur = conn.execute(
        "SELECT platform, COUNT(*) as cnt FROM posts "
        "WHERE project_name = %s AND posted_at >= NOW() - INTERVAL '" + str(days) + " days' "
        "GROUP BY platform ORDER BY cnt DESC",
        (project_name,),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def posthog_query(api_key, project_id, event, host_filter, after_date):
    """Query PostHog events API for events matching a host."""
    url = f"https://us.posthog.com/api/projects/{project_id}/events/"
    params = {
        "event": event,
        "limit": 1000,
        "after": after_date,
    }
    if host_filter:
        params["properties"] = json.dumps([
            {"key": "$host", "value": host_filter, "type": "event"}
        ])

    query = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
    full_url = f"{url}?{query}"

    req = urllib.request.Request(full_url, headers={
        "Authorization": f"Bearer {api_key}",
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("results", [])
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"  PostHog API error for {event} on {host_filter}: {e}", file=sys.stderr)
        return []


def get_posthog_stats(api_key, project_id, domains, days):
    """Get pageviews and CTA clicks from PostHog for given domains."""
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    stats = {"pageviews": 0, "cta_clicks": 0, "pageview_details": {}, "cta_details": []}

    for domain in domains:
        pvs = posthog_query(api_key, project_id, "$pageview", domain, after)
        stats["pageviews"] += len(pvs)
        paths = {}
        for ev in pvs:
            path = ev.get("properties", {}).get("$pathname", "/")
            paths[path] = paths.get(path, 0) + 1
        stats["pageview_details"][domain] = {
            "total": len(pvs),
            "top_pages": dict(sorted(paths.items(), key=lambda x: -x[1])[:10]),
        }

        ctas = posthog_query(api_key, project_id, "cta_click", domain, after)
        if not ctas:
            ctas = posthog_query(api_key, project_id, "$autocapture", domain, after)
            ctas = [e for e in ctas if "book" in (e.get("properties", {}).get("$el_text", "") or "").lower()]
        stats["cta_clicks"] += len(ctas)
        for c in ctas:
            props = c.get("properties", {})
            stats["cta_details"].append({
                "text": props.get("$el_text") or props.get("text", "?"),
                "section": props.get("section", "?"),
                "time": c.get("timestamp", "?")[:16],
            })

    return stats


def get_booking_stats(bookings_db_url, client_slug, days, table="cal_bookings"):
    """Get booking stats from the separate bookings DB.
    `table` is `cal_bookings` (Cal.com) or `calendly_bookings` (Calendly).
    Both tables share the columns used here."""
    if not bookings_db_url:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(bookings_db_url)
        cur = conn.cursor()
        if table not in {"cal_bookings", "calendly_bookings"}:
            raise ValueError(f"unsupported booking table: {table}")
        cur.execute(
            "SELECT COUNT(*), "
            "COUNT(*) FILTER (WHERE status = 'created'), "
            "COUNT(*) FILTER (WHERE status = 'cancelled'), "
            "COUNT(*) FILTER (WHERE status = 'rescheduled'), "
            "COUNT(*) FILTER (WHERE attendee_email NOT LIKE '%%test%%' "
            "AND attendee_email NOT LIKE '%%example%%' "
            "AND attendee_name NOT LIKE '%%TEST%%' "
            "AND attendee_name NOT LIKE '%%John Doe%%') "
            "FROM " + table + " WHERE client_slug = %s "
            "AND created_at >= NOW() - INTERVAL '" + str(days) + " days'",
            (client_slug,),
        )
        row = cur.fetchone()
        cols = ["total", "booked", "cancelled", "rescheduled", "real_bookings"]
        result = dict(zip(cols, row)) if row else {}

        cur.execute(
            "SELECT attendee_name, attendee_email, status, start_time, created_at "
            "FROM " + table + " WHERE client_slug = %s "
            "AND created_at >= NOW() - INTERVAL '" + str(days) + " days' "
            "ORDER BY created_at DESC LIMIT 5",
            (client_slug,),
        )
        result["recent"] = [
            {"name": r[0], "email": r[1], "status": r[2],
             "start": str(r[3])[:16] if r[3] else "?",
             "created": str(r[4])[:16] if r[4] else "?"}
            for r in cur.fetchall()
        ]
        conn.close()
        return result
    except Exception as e:
        print(f"  Bookings DB error for {client_slug}: {e}", file=sys.stderr)
        return None


def get_project_domains(project):
    """Extract all domains associated with a project."""
    domains = []
    website = project.get("website", "")
    if website:
        domain = website.replace("https://", "").replace("http://", "").rstrip("/")
        domains.append(domain)

    lp = project.get("landing_pages")
    if isinstance(lp, dict):
        base = lp.get("base_url", "")
        if base:
            domain = base.replace("https://", "").replace("http://", "").rstrip("/")
            if domain not in domains:
                domains.append(domain)
    elif isinstance(lp, str) and lp.startswith("http"):
        domain = lp.replace("https://", "").replace("http://", "").rstrip("/")
        if domain not in domains:
            domains.append(domain)

    return domains


def print_project_report(name, post_stats, platforms, posthog, bookings, quiet=False):
    """Print formatted report for one project."""
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    print(f"\n  Social Posts:")
    print(f"    Total: {post_stats.get('total', 0)}  |  Recent: {post_stats.get('recent', 0)}  |  Active: {post_stats.get('active', 0)}  |  Removed: {post_stats.get('removed', 0)}")
    print(f"    Engagement: {post_stats.get('total_upvotes', 0)} upvotes, {post_stats.get('total_comments', 0)} comments, {post_stats.get('total_views', 0)} views")
    if platforms:
        parts = [f"{p}: {c}" for p, c in platforms.items()]
        print(f"    Platforms: {', '.join(parts)}")

    if posthog and (posthog["pageviews"] > 0 or posthog["cta_clicks"] > 0):
        print(f"\n  Website Analytics (PostHog):")
        print(f"    Pageviews: {posthog['pageviews']}  |  CTA Clicks: {posthog['cta_clicks']}")
        if not quiet:
            for domain, info in posthog.get("pageview_details", {}).items():
                print(f"    {domain}: {info['total']} pageviews")
                for path, count in list(info.get("top_pages", {}).items())[:5]:
                    print(f"      {path}: {count}")
            if posthog["cta_details"]:
                print(f"    CTA clicks:")
                for cta in posthog["cta_details"][:5]:
                    print(f"      [{cta['time']}] \"{cta['text']}\" ({cta['section']})")

    if bookings:
        print(f"\n  Cal.com Bookings:")
        print(f"    Total: {bookings.get('total', 0)}  |  Booked: {bookings.get('booked', 0)}  |  Cancelled: {bookings.get('cancelled', 0)}  |  Real: {bookings.get('real_bookings', 0)}")
        if not quiet and bookings.get("recent"):
            for b in bookings["recent"][:3]:
                flag = " [TEST]" if "test" in (b["name"] or "").lower() or "example" in (b["email"] or "").lower() else ""
                print(f"      {b['created']} - {b['name']} ({b['email']}) - {b['status']}{flag}")

    if posthog and bookings:
        pvs = posthog["pageviews"]
        ctas = posthog["cta_clicks"]
        real = bookings.get("real_bookings", 0)
        print(f"\n  Funnel:")
        if pvs:
            print(f"    Pageviews -> CTA Clicks: {pvs} -> {ctas} ({(ctas/pvs*100):.1f}% CTR)")
        else:
            print(f"    Pageviews -> CTA Clicks: 0 -> {ctas}")
        if ctas:
            print(f"    CTA Clicks -> Bookings: {ctas} -> {real} ({(real/ctas*100):.1f}% conversion)")
        else:
            print(f"    CTA Clicks -> Bookings: 0 -> {real}")


def main():
    parser = argparse.ArgumentParser(description="Unified project funnel stats")
    parser.add_argument("--project", help="Filter to specific project name")
    parser.add_argument("--days", type=int, default=30, help="Lookback period in days (default: 30)")
    parser.add_argument("--quiet", action="store_true", help="Compact output")
    args = parser.parse_args()

    load_env()
    config = load_config()

    api_key = os.environ.get("POSTHOG_PERSONAL_API_KEY")
    project_id = os.environ.get("POSTHOG_PROJECT_ID", "330744")
    bookings_db_url = os.environ.get("BOOKINGS_DATABASE_URL")

    if not api_key:
        print("ERROR: POSTHOG_PERSONAL_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    conn = dbmod.get_conn()

    projects_with_stats = [
        "fazm", "Cyrano", "PieLine", "Terminator", "S4L",
        "macOS MCP", "Vipassana", "WhatsApp MCP", "AI Browser Profile", "macOS Session Replay",
    ]

    print(f"Project Funnel Stats (last {args.days} days)")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    for proj in config.get("projects", []):
        name = proj["name"]
        if args.project and args.project.lower() != name.lower():
            continue
        if name not in projects_with_stats and not args.project:
            continue

        post_stats = get_post_stats(conn, name, args.days)
        platforms = get_platform_breakdown(conn, name, args.days)

        domains = get_project_domains(proj)
        ph_override = proj.get("posthog", {})
        ph_key = os.environ.get(ph_override.get("api_key_env", ""), api_key)
        ph_pid = ph_override.get("project_id", project_id)
        posthog = get_posthog_stats(ph_key, ph_pid, domains, args.days) if domains else None

        client_slug = get_client_slug(name)
        booking_table = get_booking_table(name)
        bookings = get_booking_stats(bookings_db_url, client_slug, args.days, booking_table) if client_slug else None

        print_project_report(name, post_stats, platforms, posthog, bookings, args.quiet)

    # Overall summary
    cur = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE posted_at >= NOW() - INTERVAL '" + str(args.days) + " days'"
    )
    total_recent = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM posts")
    total_all = cur.fetchone()[0]
    print(f"\n{'='*60}")
    print(f"  Overall: {total_all} total posts, {total_recent} in last {args.days} days")
    print(f"{'='*60}")

    conn.close()


if __name__ == "__main__":
    main()
