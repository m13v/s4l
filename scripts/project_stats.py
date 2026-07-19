#!/usr/bin/env python3
"""Unified funnel stats per project: social posts -> pageviews -> CTA clicks -> bookings.

Reads config.json for project definitions, queries:
  - Posts + bookings stats via s4l.ai HTTP /api/v1/stats/* (no direct DB)
  - PostHog API (POSTHOG_PERSONAL_API_KEY): pageviews + CTA clicks by domain

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
from http_api import api_get  # noqa: E402
from project_slugs import get_client_slug, get_booking_table  # noqa: E402

ENV_PATH = os.path.expanduser("~/social-autoposter/.env")
# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config
CONFIG_PATH = _canonical_config_path()


def load_env():
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())




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

    if not api_key:
        print("ERROR: POSTHOG_PERSONAL_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

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

        client_slug = get_client_slug(name)
        booking_table = get_booking_table(name)
        detail = (api_get("/api/v1/stats/project-detail", query={
            "project": name, "days": int(args.days), "platform": "",
            "client_slug": client_slug or "",
            "booking_table": booking_table or "cal_bookings",
            "require_utm": "0",
        }).get("data") or {})
        post_stats = detail.get("post_stats") or {}
        platforms = detail.get("platforms") or {}
        bookings = detail.get("bookings") if client_slug else None

        domains = get_project_domains(proj)
        ph_override = proj.get("posthog", {})
        ph_key = os.environ.get(ph_override.get("api_key_env", ""), api_key)
        ph_pid = ph_override.get("project_id", project_id)
        posthog = get_posthog_stats(ph_key, ph_pid, domains, args.days) if domains else None

        print_project_report(name, post_stats, platforms, posthog, bookings, args.quiet)

    # Overall summary
    overall = (api_get("/api/v1/stats/posts-overall", query={
        "days": int(args.days), "platform": "",
    }).get("data") or {})
    total_all = int(overall.get("total") or 0)
    total_recent = int(overall.get("recent") or 0)
    print(f"\n{'='*60}")
    print(f"  Overall: {total_all} total posts, {total_recent} in last {args.days} days")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
