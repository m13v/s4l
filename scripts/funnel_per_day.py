#!/usr/bin/env python3
"""Per-day PostHog funnel metrics for the dashboard stats tab.

Emits JSON on stdout:
  { "days": N,
    "rows": [ {"day": "YYYY-MM-DD",
               "pageviews": int,
               "email_signups": int,
               "schedule_clicks": int,
               "get_started_clicks": int,
               "cross_product_clicks": int,
               "cta_clicks": int}, ... ] }

Aggregates across every project's domains listed in config.json, bucketed
by (POSTHOG_API_KEY, PROJECT_ID) so projects sharing a PostHog bucket
collapse into one HogQL call per metric.

Called by bin/server.js `/api/funnel/per-day`. Mirrors the auth/bucket
pattern of `project_stats_json.py`; cannot import it because that
module runs heavyweight project-stats work at import time.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import project_stats as ps
from project_stats_json import _hogql, _SAFE_DOMAIN_RE, HogqlError, _GET_STARTED_EVENTS


_EVENT_CLAUSES = {
    "pageviews":            "event = '$pageview'",
    # Sessions: count distinct $session_id on $pageview events. PostHog sets
    # $session_id on every autocapture/pageview, so this gives "web sessions"
    # in the standard sense (one session per visitor per ~30min of activity).
    # Used as the denominator for conversion-rate ratios on the Trends tab,
    # since signups/sessions is the meaningful conversion metric (one user
    # hitting 4 pages is one session, not four chances to convert).
    "sessions":             "event = '$pageview'",
    # Email signups: client `newsletter_subscribed` is ad-blocker-lossy
    # (~57% capture). Server-side `newsletter_subscribed_server` (added in
    # @m13v/seo-components v0.38) fires from the API route after the Resend
    # send succeeds, so it's ground truth. Both are counted with DISTINCT
    # email so old client-only sites still show up while we transition;
    # once both fire for the same submission they collapse into one row.
    "email_signups":        "event IN ('newsletter_subscribed', 'newsletter_subscribed_server')",
    "schedule_clicks":      "event = 'schedule_click'",
    "get_started_clicks":   f"event IN {_GET_STARTED_EVENTS}",
    "cross_product_clicks": "event = 'cross_product_click'",
    "cta_clicks":           "event = 'cta_click'",
}

# Metrics that need DISTINCT counting (e.g. dedupe client + server captures
# of the same event by email). Other metrics use plain count().
# `coalesce(properties.email, distinct_id)` because some emitters (studyly's
# /api/signup, custom routes) set only distinct_id=email and leave
# properties.email null; without coalesce those rows fall out of the count.
_DISTINCT_KEY = {
    "email_signups": "coalesce(properties.email, distinct_id)",
    "sessions": "properties.$session_id",
}


def _per_day_for_bucket(api_key, project_id, domains, days):
    """One HogQL query per metric, grouped by day, filtered to this bucket's domains."""
    safe = [d for d in domains if _SAFE_DOMAIN_RE.match(d or "")]
    if not safe or not days:
        return {m: {} for m in _EVENT_CLAUSES}
    in_list = ", ".join(f"'{d}'" for d in safe)
    since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    out = {}
    for metric, clause in _EVENT_CLAUSES.items():
        distinct_key = _DISTINCT_KEY.get(metric)
        count_expr = (
            f"count(DISTINCT {distinct_key}) AS c"
            if distinct_key
            else "count() AS c"
        )
        q = (
            f"SELECT toDate(timestamp) AS day, {count_expr} FROM events "
            f"WHERE {clause} "
            f"AND properties.$host IN ({in_list}) "
            f"AND timestamp >= toDateTime('{since_iso}') "
            "GROUP BY day ORDER BY day"
        )
        try:
            rows = _hogql(api_key, project_id, q)
        except HogqlError as e:
            print(f"  HogQL error ({metric}, pid={project_id}): {e}", file=sys.stderr)
            rows = []
        out[metric] = {str(r[0]): int(r[1]) for r in (rows or []) if r and r[0] is not None}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--project", help="Filter to a single project name")
    args = parser.parse_args()
    days = max(1, min(365, args.days))

    ps.load_env()
    env = os.environ
    config = ps.load_config()

    default_key = env.get("POSTHOG_PERSONAL_API_KEY")
    default_pid = env.get("POSTHOG_PROJECT_ID", "330744")

    if not default_key:
        print(json.dumps({"error": "POSTHOG_PERSONAL_API_KEY not set", "days": days, "rows": []}))
        return

    buckets = {}  # (api_key, project_id) -> set(domains)
    for proj in config.get("projects", []):
        name = proj.get("name") or ""
        if args.project and args.project.lower() != name.lower():
            continue
        domains = ps.get_project_domains(proj) or []
        if not domains:
            continue
        over = proj.get("posthog", {}) or {}
        key = env.get(over.get("api_key_env", ""), default_key)
        pid = over.get("project_id", default_pid)
        bucket = buckets.setdefault((key, pid), set())
        for d in domains:
            bucket.add(d)

    if not buckets:
        print(json.dumps({"days": days, "rows": []}))
        return

    # One thread per bucket; each bucket issues len(_EVENT_CLAUSES) HogQL
    # queries sequentially to stay inside PostHog's rate limit.
    pool_size = max(2, min(8, len(buckets)))
    metric_totals = {m: {} for m in _EVENT_CLAUSES}  # metric -> {day: count}
    error_msg = None
    with ThreadPoolExecutor(max_workers=pool_size) as ex:
        futs = {
            ex.submit(_per_day_for_bucket, k, pid, sorted(ds), days): (k, pid)
            for (k, pid), ds in buckets.items()
        }
        for fut in futs:
            try:
                bucket_metrics = fut.result()
            except Exception as e:
                error_msg = error_msg or f"PostHog batch error: {e}"
                continue
            for metric, day_counts in bucket_metrics.items():
                agg = metric_totals[metric]
                for day, c in day_counts.items():
                    agg[day] = agg.get(day, 0) + c

    # Emit one row per day in the window (even zero-count days), sorted ascending.
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    rows = []
    for i in range(days):
        d = start + timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        row = {"day": key}
        for m in _EVENT_CLAUSES:
            row[m] = int(metric_totals[m].get(key, 0))
        rows.append(row)

    out = {"days": days, "rows": rows}
    if error_msg:
        out["error"] = error_msg
    print(json.dumps(out))


if __name__ == "__main__":
    main()
