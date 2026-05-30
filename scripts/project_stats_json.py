#!/usr/bin/env python3
"""JSON wrapper around project_stats.py for the dashboard /api/funnel/stats endpoint.

Emits a single JSON object on stdout: { generated_at, days, projects: [ ... ], overall }.
Keeps project_stats.py untouched (it is chflags uchg-locked).
"""

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import project_stats as ps
from project_slugs import bookings_require_utm as _bookings_require_utm


_PAGE_FILENAMES = ("page.tsx", "page.ts", "page.jsx", "page.js", "page.mdx", "page.md")


def _normalize_platform(p):
    """Lowercase + alias 'x' -> 'twitter'. Empty / 'all' / None -> '' (no filter).

    Matches the same normalization used by /api/style/stats so the
    project final stats table speaks the same vocabulary as the
    engagement-style table when the dashboard's platform pill is set.
    """
    if not p:
        return ""
    v = str(p).strip().lower()
    if v in ("", "all"):
        return ""
    return "twitter" if v == "x" else v


def _platform_sql_clause(platform, table_alias=""):
    """Return an SQL fragment (string, no placeholders) that:

    1. Filters to the given platform when one is provided (empty = no filter).

    Mentions live in the dedicated `mentions` table now (2026-05-23 cutover);
    no posts-level filter needed. Previously this clause excluded placeholder
    `posts` rows where our_content = '(mention - no original post)', which is
    no longer present after migrate_mentions_out_of_posts.py --commit-delete.

    Folds the 'x' -> 'twitter' alias inside the SQL so reddit/linkedin/twitter
    all just work. Caller is responsible for placement inside the WHERE.
    """
    if not platform:
        return ""
    prefix = (table_alias + ".") if table_alias else ""
    # Safe: platform has already passed the [a-z0-9_]{1,32} regex in the caller.
    return (
        " AND LOWER(CASE WHEN LOWER(" + prefix + "platform)='x' "
        "THEN 'twitter' ELSE " + prefix + "platform END) = '" + platform + "'"
    )


# Synthetic project name for rows in `posts` where project_name IS NULL.
# Keeps off-config / un-tagged posts visible on the dashboard without
# polluting real project rows. Chosen to be unambiguous and distinct from
# any historical 'General' rows the table once had.
SYNTHETIC_NO_PROJECT_NAME = "(no project)"


def _project_filter_sql(proj_name, table_alias="p"):
    """Return (clause, params) for a per-project WHERE filter.

    Real projects -> "<alias>.project_name = %s" with (proj_name,).
    Synthetic "(no project)" bucket -> "<alias>.project_name IS NULL" with ().
    Centralizes the NULL-vs-equality choice so every per-project SQL site
    handles the synthetic bucket the same way.
    """
    prefix = (table_alias + ".") if table_alias else ""
    if proj_name == SYNTHETIC_NO_PROJECT_NAME:
        return (prefix + "project_name IS NULL", ())
    return (prefix + "project_name = %s", (proj_name,))


def _bridge_per_project_posthog_keys_from_keychain(config, env):
    import subprocess
    seen = set()
    for proj in config.get("projects", []) or []:
        name_env = ((proj.get("posthog") or {}).get("api_key_env") or "").strip()
        if not name_env or name_env in seen or name_env == "POSTHOG_PERSONAL_API_KEY":
            continue
        seen.add(name_env)
        if env.get(name_env):
            continue
        try:
            v = subprocess.check_output(
                ["security", "find-generic-password", "-s", name_env, "-w"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except subprocess.CalledProcessError:
            continue
        if v:
            env[name_env] = v


def _scan_repo_pages(repo_path):
    """Walk a Next.js app-router repo and return URL paths we ship as static files.

    Skips dynamic segments ([slug], [...rest]), route groups ((group)), private
    folders (_foo), and parallel-route slots (@slot) per Next.js conventions.
    Route groups collapse to nothing; dynamic segments exclude the whole branch.
    """
    out = set()
    if not repo_path:
        return out
    repo = os.path.expanduser(repo_path)
    app_roots = [
        os.path.join(repo, "src", "app"),
        os.path.join(repo, "app"),
    ]
    for root in app_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            segs = [] if rel == "." else rel.split(os.sep)
            if any(s.startswith(("[", "_", "@")) for s in segs):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(("[", "_", "@", "."))
                           and d not in ("node_modules",)]
            has_page = any(f in _PAGE_FILENAMES for f in filenames)
            if has_page:
                url_segs = [s for s in segs if not (s.startswith("(") and s.endswith(")"))]
                path = "/" + "/".join(url_segs) if url_segs else "/"
                out.add(path)
    return out


def _db_created_pages(conn, product_name, days=None):
    """Return {domain: set(paths)} for pages this project published via the SEO
    pipelines (seo_keywords) or GSC-driven page generation (gsc_queries).

    When `days` is set, restrict to pages whose `completed_at` falls inside the
    window. The seo_keywords / gsc_queries rows get `completed_at` stamped when
    the page is actually generated, so this matches "pages created in the last
    N days" as used by the dashboard's period selector.
    """
    out = {}
    window_sql = ""
    if days is not None:
        window_sql = f" AND completed_at >= NOW() - INTERVAL '{int(days)} days'"
    for sql in (
        "SELECT page_url FROM seo_keywords WHERE product = %s AND page_url IS NOT NULL" + window_sql,
        "SELECT page_url FROM gsc_queries WHERE product = %s AND page_url IS NOT NULL" + window_sql,
    ):
        try:
            cur = conn.execute(sql, (product_name,))
            for row in cur.fetchall():
                url = row[0]
                if not url:
                    continue
                try:
                    parsed = urllib.parse.urlparse(url)
                except Exception:
                    continue
                host = (parsed.netloc or "").lower()
                path = parsed.path or "/"
                while len(path) > 1 and path.endswith("/"):
                    path = path[:-1]
                if not host:
                    continue
                out.setdefault(host, set()).add(path)
        except Exception as e:
            print(f"  _db_created_pages query error: {e}", file=sys.stderr)
    return out


def _created_paths_for_project(conn, proj, days=None):
    """Return {domain: set(paths)} of pages we created for this project.

    Source-of-truth union: filesystem scan of the project's landing-pages repo
    (applies to every domain the project owns) plus any URLs logged in
    seo_keywords / gsc_queries (keyed by their own host).

    When `days` is set, the filesystem scan is skipped entirely — static page
    files on disk carry no creation timestamp we can trust, so a window-scoped
    "pages created in the last N days" answer has to come from the DB alone.
    """
    by_domain = {}
    domains = ps.get_project_domains(proj) or []
    if days is None:
        lp = proj.get("landing_pages") or {}
        repo_path = lp.get("repo") if isinstance(lp, dict) else None
        fs_paths = _scan_repo_pages(repo_path) if repo_path else set()
        for d in domains:
            by_domain.setdefault(d.lower(), set()).update(fs_paths)
    for host, paths in _db_created_pages(conn, proj.get("name") or "", days=days).items():
        by_domain.setdefault(host, set()).update(paths)
    return by_domain


def _norm_path(p):
    """Match the frontend `normPath` in bin/server.js so PostHog pathnames
    (`properties.$pathname`) and DB-derived created paths compare cleanly.
    """
    s = str(p or "/")
    if not s.startswith("/"):
        s = "/" + s
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    return s


# HogQL-based PostHog query layer.
#
# project_stats.py uses the events LIST endpoint with limit=1000 and no
# pagination, so any (domain, event) that exceeds 1000 occurrences in the
# window silently caps at 1000 and misreports the funnel. We swap that out
# for HogQL aggregate queries (COUNT/GROUP BY), which return the true
# totals in a single call per query.
_SAFE_DOMAIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class HogqlError(Exception):
    """Raised when a HogQL query fails after all retries.

    Caller is expected to surface this as an error on the affected rows
    instead of silently rendering zeros.
    """


_RETRY_BACKOFF_S = (2.0, 5.0, 12.0)
_RETRY_AFTER_CAP_S = 30.0


def _hogql(api_key, project_id, query, timeout=120, max_attempts=4):
    """Run a HogQL query against /api/projects/{pid}/query/.

    Retries on 429 (throttled), 5xx, and socket read timeouts. Honors
    `Retry-After` up to `_RETRY_AFTER_CAP_S`; otherwise uses
    `_RETRY_BACKOFF_S`. Raises `HogqlError` on permanent failure so
    callers can mark rows as errored rather than zero.

    NOTE: the batched-by-$host queries cover many domains in one scan, so a
    single query for a large shared PostHog bucket (e.g. pid 330744 with
    ~18 projects) can run >60s on a cold cache. A socket read timeout
    surfaces as `socket.timeout`/`TimeoutError`, which is a sibling of
    `urllib.error.URLError` (both subclass OSError), so it must be caught
    explicitly; otherwise it escapes this retry loop and the caller marks
    the entire bucket as errored on the very first slow query, rendering
    'err' for every project sharing that bucket.
    """
    url = f"https://us.posthog.com/api/projects/{project_id}/query/"
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode("utf-8")
    last_err = None
    for attempt in range(max_attempts):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                return data.get("results", []) or []
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = ""
            last_err = f"HTTP {e.code}: {detail}"
            retryable = (e.code == 429) or (500 <= e.code < 600)
            if not retryable or attempt == max_attempts - 1:
                print(f"  HogQL HTTPError {e.code}: {detail} | query={query[:120]}", file=sys.stderr)
                break
            wait = _RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)]
            try:
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra is not None:
                    wait = min(_RETRY_AFTER_CAP_S, max(wait, float(ra)))
            except Exception:
                pass
            print(f"  HogQL {e.code} retry {attempt + 1}/{max_attempts - 1} in {wait:.1f}s | query={query[:80]}", file=sys.stderr)
            time.sleep(wait)
            continue
        except (socket.timeout, TimeoutError) as e:
            # Read timeout on a heavy batched query. Retryable: a retry
            # often hits a warm cache and returns in time. Caught before
            # URLError because TimeoutError is NOT a URLError subclass.
            last_err = f"read timeout after {timeout}s: {e}"
            if attempt == max_attempts - 1:
                print(f"  HogQL timeout (>{timeout}s): {e} | query={query[:120]}", file=sys.stderr)
                break
            wait = _RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)]
            print(f"  HogQL timeout retry {attempt + 1}/{max_attempts - 1} in {wait:.1f}s | query={query[:80]}", file=sys.stderr)
            time.sleep(wait)
            continue
        except urllib.error.URLError as e:
            # A URLError can also wrap a socket.timeout (e.reason). Treat
            # those as the retryable timeout case above.
            last_err = f"URLError: {e}"
            if attempt == max_attempts - 1:
                print(f"  HogQL URLError: {e} | query={query[:120]}", file=sys.stderr)
                break
            wait = _RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)]
            print(f"  HogQL URLError retry {attempt + 1}/{max_attempts - 1} in {wait:.1f}s: {e}", file=sys.stderr)
            time.sleep(wait)
            continue
    raise HogqlError(last_err or "unknown HogQL failure")


def _empty_domain_stats(domain, error=None):
    """Zero'd per-domain stats. If `error` is set, treat the zeros as
    UNKNOWN (not truly 0) so the dashboard can render an error cell
    instead of silently misreporting."""
    out = {
        "pageviews": 0,
        "cta_clicks": 0,
        "email_signups": 0,
        "schedule_clicks": 0,
        "get_started_clicks": 0,
        "cross_product_clicks": 0,
        "pageview_details": {domain: {
            "total": 0,
            "top_pages": {},
            "top_pages_signups": {},
            "top_pages_schedule": {},
            "top_pages_get_started": {},
        }},
        "cta_details": [],
    }
    if error:
        out["error"] = error
    return out


# Legacy + canonical event names for the "get started" click.  Fazm fires
# `download_click`, Assrt fires `cta_get_started_clicked`, new sites fire
# `get_started_click`.  Collapsed back to a single name once both old sites
# migrate to trackGetStartedClick.
_GET_STARTED_EVENTS = "('get_started_click', 'download_click', 'cta_get_started_clicked')"


def _ph_batch_counts(api_key, project_id, domains, after_iso):
    """Fetch per-domain PostHog aggregates for every `domain` in one batched
    pass against a single (api_key, project_id) bucket.

    The previous implementation fired ~10 HogQL queries per domain, which
    fanned out to 100+ concurrent requests and tripped PostHog's rate
    limiter; throttled calls silently returned 0, misreporting every
    project except the one with its own dedicated API key.

    This version groups each aggregate by `properties.$host`, so one query
    covers every domain in the bucket. Returns `{domain: stats_dict}` in
    the same shape the old per-domain function produced. On permanent
    HogQL failure, raises `HogqlError` so the caller can mark rows as
    errored rather than rendering a misleading zero.
    """
    result = {d: _empty_domain_stats(d) for d in domains}
    safe_domains = []
    for d in domains:
        if _SAFE_DOMAIN_RE.match(d or ""):
            safe_domains.append(d)
        else:
            print(f"  skip unsafe domain: {d!r}", file=sys.stderr)
            result[d]["error"] = "unsafe domain"
    if not safe_domains:
        return result

    after_str = (after_iso or "").replace("T", " ")
    if not after_str:
        return result

    in_list = ", ".join(f"'{d}'" for d in safe_domains)

    def _count_by_host(event_clause, distinct_key=None):
        # Pass `distinct_key` (e.g. "properties.email") to dedupe across
        # double-fired events for the same conversion. Used for email
        # signups where both `newsletter_subscribed` (client) and
        # `newsletter_subscribed_server` (server) fire for one submission.
        count_expr = (
            f"count(DISTINCT {distinct_key}) AS c"
            if distinct_key
            else "count() AS c"
        )
        q = (
            f"SELECT properties.$host AS host, {count_expr} FROM events "
            f"WHERE {event_clause} "
            f"AND properties.$host IN ({in_list}) "
            f"AND timestamp >= toDateTime('{after_str}') "
            "GROUP BY host"
        )
        rows = _hogql(api_key, project_id, q)
        return {r[0]: int(r[1]) for r in (rows or []) if r and r[0]}

    def _top_pages_by_host(event_clause, row_cap=5000, distinct_key="distinct_id"):
        # All per-page breakdowns count unique users (distinct_id) rather than
        # raw events. A visitor that views the same /pricing twice or rage-
        # clicks the same CTA still counts as 1. Pass `distinct_key=None` to
        # opt back into raw count() for legacy callers.
        count_expr = (
            f"count(DISTINCT {distinct_key}) AS c"
            if distinct_key
            else "count() AS c"
        )
        q = (
            f"SELECT properties.$host AS host, properties.$pathname AS path, {count_expr} FROM events "
            f"WHERE {event_clause} "
            f"AND properties.$host IN ({in_list}) "
            f"AND timestamp >= toDateTime('{after_str}') "
            f"GROUP BY host, path ORDER BY c DESC LIMIT {int(row_cap)}"
        )
        rows = _hogql(api_key, project_id, q)
        out = {d: {} for d in safe_domains}
        for r in (rows or []):
            host = r[0] if len(r) > 0 else None
            path = r[1] if len(r) > 1 and r[1] else "/"
            cnt = int(r[2]) if len(r) > 2 else 0
            if host in out:
                out[host][path] = cnt
        return out

    # Email signups: client `newsletter_subscribed` is ad-blocker-lossy
    # (~57% capture). Server-side `newsletter_subscribed_server` (added in
    # @m13v/seo-components v0.38) is ground truth. Count both with DISTINCT
    # email so a client + server pair for the same submission collapses to one.
    _SIGNUP_CLAUSE = (
        "event IN ('newsletter_subscribed', 'newsletter_subscribed_server')"
    )

    # Visitors, not raw pageviews. Globally consistent with every other
    # column in this batch (cta_clicks, schedule_clicks, get_started_clicks,
    # cross_product_clicks, email_signups all count unique users). A visitor
    # bouncing between /pricing and /docs still counts as 1.
    pv_total = _count_by_host("event = '$pageview'", distinct_key="distinct_id")
    cta_total = _count_by_host("event = 'cta_click'", distinct_key="distinct_id")
    signup_total = _count_by_host(
        _SIGNUP_CLAUSE,
        distinct_key="coalesce(properties.email, distinct_id)",
    )
    sched_total = _count_by_host("event = 'schedule_click'", distinct_key="distinct_id")
    # Get Started = unique users who took the conversion action, not raw clicks.
    # A user iterating on the same project (multiple prompts, multiple
    # download retries, multiple install button presses in a session) is
    # still one conversion. Mirrors the signup_total dedup pattern above.
    get_started_total = _count_by_host(
        f"event IN {_GET_STARTED_EVENTS}",
        distinct_key="distinct_id",
    )
    cross_product_total = _count_by_host("event = 'cross_product_click'", distinct_key="distinct_id")

    top_pv = _top_pages_by_host("event = '$pageview'", row_cap=5000)
    top_signup = _top_pages_by_host(_SIGNUP_CLAUSE, row_cap=500)
    top_sched = _top_pages_by_host("event = 'schedule_click'", row_cap=500)
    top_get_started = _top_pages_by_host(f"event IN {_GET_STARTED_EVENTS}", row_cap=500)

    cta_details_by_host = {d: [] for d in safe_domains}
    if any(v > 0 for v in cta_total.values()):
        cta_detail_q = (
            "SELECT properties.$host AS host, properties.$el_text, properties.text, properties.section, timestamp "
            "FROM events "
            "WHERE event = 'cta_click' "
            f"AND properties.$host IN ({in_list}) "
            f"AND timestamp >= toDateTime('{after_str}') "
            "ORDER BY timestamp DESC LIMIT 200"
        )
        rows = _hogql(api_key, project_id, cta_detail_q)
        for r in (rows or []):
            host = r[0] if len(r) > 0 else None
            el_text = r[1] if len(r) > 1 else None
            text = r[2] if len(r) > 2 else None
            section = r[3] if len(r) > 3 else None
            ts = r[4] if len(r) > 4 else None
            bucket = cta_details_by_host.get(host)
            if bucket is None or len(bucket) >= 10:
                continue
            bucket.append({
                "text": el_text or text or "?",
                "section": section or "?",
                "time": (str(ts)[:16] if ts else "?"),
            })

    # Autocapture fallback: only domains with zero `cta_click` get the
    # "$autocapture clicks whose text contains 'book'" treatment. Batched
    # like everything else so we don't fan out.
    fallback_hosts = [d for d in safe_domains if cta_total.get(d, 0) == 0]
    if fallback_hosts:
        fb_in = ", ".join(f"'{d}'" for d in fallback_hosts)
        ac_total_q = (
            "SELECT properties.$host AS host, count(DISTINCT distinct_id) AS c FROM events "
            "WHERE event = '$autocapture' "
            f"AND properties.$host IN ({fb_in}) "
            f"AND timestamp >= toDateTime('{after_str}') "
            "AND lower(properties.$el_text) LIKE '%book%' "
            "GROUP BY host"
        )
        ac_rows = _hogql(api_key, project_id, ac_total_q)
        ac_total = {r[0]: int(r[1]) for r in (ac_rows or []) if r and r[0]}
        hosts_with_ac = [d for d in fallback_hosts if ac_total.get(d, 0) > 0]
        if hosts_with_ac:
            ac_in = ", ".join(f"'{d}'" for d in hosts_with_ac)
            ac_detail_q = (
                "SELECT properties.$host AS host, properties.$el_text, properties.text, properties.section, timestamp "
                "FROM events "
                "WHERE event = '$autocapture' "
                f"AND properties.$host IN ({ac_in}) "
                f"AND timestamp >= toDateTime('{after_str}') "
                "AND lower(properties.$el_text) LIKE '%book%' "
                "ORDER BY timestamp DESC LIMIT 200"
            )
            rows = _hogql(api_key, project_id, ac_detail_q)
            for r in (rows or []):
                host = r[0] if len(r) > 0 else None
                el_text = r[1] if len(r) > 1 else None
                text = r[2] if len(r) > 2 else None
                section = r[3] if len(r) > 3 else None
                ts = r[4] if len(r) > 4 else None
                bucket = cta_details_by_host.get(host)
                if bucket is None or len(bucket) >= 10:
                    continue
                bucket.append({
                    "text": el_text or text or "?",
                    "section": section or "?",
                    "time": (str(ts)[:16] if ts else "?"),
                })
        # Roll autocapture counts into cta_total so the funnel "cta_clicks"
        # column matches the detail list for fallback domains.
        for h, c in ac_total.items():
            cta_total[h] = max(cta_total.get(h, 0), c)

    for d in safe_domains:
        pv = pv_total.get(d, 0)
        result[d] = {
            "pageviews": pv,
            "cta_clicks": cta_total.get(d, 0),
            "email_signups": signup_total.get(d, 0),
            "schedule_clicks": sched_total.get(d, 0),
            "get_started_clicks": get_started_total.get(d, 0),
            "cross_product_clicks": cross_product_total.get(d, 0),
            "pageview_details": {d: {
                "total": pv,
                "top_pages": top_pv.get(d, {}),
                "top_pages_signups": top_signup.get(d, {}),
                "top_pages_schedule": top_sched.get(d, {}),
                "top_pages_get_started": top_get_started.get(d, {}),
            }},
            "cta_details": cta_details_by_host.get(d, []),
        }
    return result


def _ph_combine(per_domain):
    out = {
        "pageviews": 0,
        "cta_clicks": 0,
        "email_signups": 0,
        "schedule_clicks": 0,
        "get_started_clicks": 0,
        "cross_product_clicks": 0,
        "pageview_details": {},
        "cta_details": [],
    }
    for s in per_domain:
        out["pageviews"] += s.get("pageviews", 0)
        out["cta_clicks"] += s.get("cta_clicks", 0)
        out["email_signups"] += s.get("email_signups", 0)
        out["schedule_clicks"] += s.get("schedule_clicks", 0)
        out["get_started_clicks"] += s.get("get_started_clicks", 0)
        out["cross_product_clicks"] += s.get("cross_product_clicks", 0)
        out["pageview_details"].update(s.get("pageview_details", {}))
        out["cta_details"].extend(s.get("cta_details", []))
    return out


def _bookings_shared(bookings_conn, client_slug, days, table="cal_bookings", require_utm=False):
    """Same output shape as ps.get_booking_stats, but reuses a shared psycopg2
    connection instead of opening a fresh one per project.
    `table` is `cal_bookings` (Cal.com) or `calendly_bookings` (Calendly).
    `require_utm` gates `real_bookings` on `utm_source IS NOT NULL` for
    projects whose booking destination is shared with non-marketing inbound
    (set in config.json via `bookings_require_utm`)."""
    if not bookings_conn or not client_slug:
        return None
    try:
        if table not in {"cal_bookings", "calendly_bookings"}:
            raise ValueError(f"unsupported booking table: {table}")
        utm_clause = " AND utm_source IS NOT NULL" if require_utm else ""
        cur = bookings_conn.cursor()
        cur.execute(
            "SELECT COUNT(*), "
            "COUNT(*) FILTER (WHERE status = 'created'), "
            "COUNT(*) FILTER (WHERE status = 'cancelled'), "
            "COUNT(*) FILTER (WHERE status = 'rescheduled'), "
            "COUNT(*) FILTER (WHERE attendee_email NOT ILIKE '%%test%%' "
            "AND attendee_email NOT ILIKE '%%example%%' "
            "AND attendee_email NOT ILIKE '%%+%%verify%%' "
            "AND attendee_name NOT ILIKE '%%test%%' "
            "AND attendee_name NOT ILIKE '%%verification%%' "
            "AND attendee_name NOT ILIKE '%%delete-me%%' "
            "AND attendee_name NOT ILIKE '%%john doe%%'"
            + utm_clause + ") "
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
        cur.close()
        return result
    except Exception as e:
        print(f"  Bookings DB error for {client_slug}: {e}", file=sys.stderr)
        return None


def _dm_short_link_stats(conn, name, days):
    """Per-project DM short-link click attribution.

    `dm_clicks`: SUM(dm_links.clicks) JOIN dms d for DMs that reference this
    project (target_project OR membership in target_projects[]) and were last
    touched in the window. Captures every DM click — booking, github, website,
    or kind=other — bumped at the resolver. Multi-link, multi-turn safe.
    """
    if not name or name == SYNTHETIC_NO_PROJECT_NAME:
        return 0
    try:
        cur = conn.execute(
            "SELECT COALESCE(SUM(l.clicks), 0)::int "
            "FROM dm_links l "
            "JOIN dms d ON d.id = l.dm_id "
            "WHERE (COALESCE(d.target_project, d.project_name) = %s "
            "       OR %s = ANY(d.target_projects)) "
            "AND COALESCE(d.last_message_at, d.discovered_at) >= NOW() - INTERVAL '" + str(int(days)) + " days'",
            (name, name),
        )
        return int((cur.fetchone() or (0,))[0])
    except Exception as e:
        print(f"  dm_short_link_stats error for {name}: {e}", file=sys.stderr)
        return 0


def _dm_booking_count(conn, bookings_conn, name, days):
    """Count cal_bookings within the window whose metadata.utm_content
    (`dm_<id>`) maps to a DM targeting this project.

    The webhook stores the entire Cal.com payload under cal_bookings.metadata,
    and the original UTM lives at metadata.payload.metadata.utm_content. We
    parse the dm_id out of `dm_<n>`, then join against dms.target_project /
    project_name in the main DB to scope by project.
    """
    if not bookings_conn or not name or name == SYNTHETIC_NO_PROJECT_NAME:
        return 0
    try:
        cur = bookings_conn.cursor()
        cur.execute(
            "SELECT metadata#>>'{payload,metadata,utm_content}' AS utm_content "
            "FROM cal_bookings "
            "WHERE metadata#>>'{payload,metadata,utm_content}' LIKE 'dm_%%' "
            "AND created_at >= NOW() - INTERVAL '" + str(int(days)) + " days' "
            "AND COALESCE(attendee_email, '') NOT ILIKE '%%test%%'"
        )
        dm_ids = []
        for (utm,) in cur.fetchall():
            if utm and utm.startswith('dm_'):
                try:
                    dm_ids.append(int(utm.split('_', 1)[1]))
                except (ValueError, IndexError):
                    pass
        cur.close()
        if not dm_ids:
            return 0
        cur2 = conn.execute(
            "SELECT COUNT(*)::int FROM dms WHERE id = ANY(%s) "
            "AND COALESCE(target_project, project_name) = %s",
            (dm_ids, name),
        )
        return int((cur2.fetchone() or (0,))[0])
    except Exception as e:
        print(f"  dm_booking_count error for {name}: {e}", file=sys.stderr)
        return 0


def _period_total_engagement(conn, name, days, platform=None):
    """Total engagement *gained during the window* across ALL posts, regardless
    of when each post was created.

    Used to populate the "(total)" bracketed value on the project panel.
    Logic per post:
      gain = latest_snapshot_in_window - latest_snapshot_before_window
    with the "before" leg treated as 0 when the post did not exist before
    the window (new posts contribute their full current value, which is
    why this differs from the Trends-tab LAG() approach: that one excludes
    every post's first snapshot and therefore undercounts fresh activity).

    Same platform filter as the Trends tab: excludes moltbook / github /
    github_issues. Same project filter via posts.project_name.

    For post_clicks: COUNT of post_link_clicks rows with is_bot=FALSE in the
    window, joined post_links -> posts so we can apply the project filter.
    Pre-2026-05-07 click rows do not exist (is_bot logging started then), so
    the count returns 0 for older days rather than mixing inflated counters.
    """
    # Period total = engagement gained during the last N days, summed from
    # two complementary branches that always together produce a value
    # >= the panel's scoped column:
    #
    #   (1) new_posts_branch — posts CREATED in the window. Their full
    #       live posts.* counters are credited as in-window gain (all of
    #       it was earned during the window since the post didn't exist
    #       before). No reddit/moltbook -1 OP self-vote discount here
    #       (the scoped column applies that discount, so the un-discounted
    #       sum here is guaranteed >= scoped).
    #
    #   (2) old_posts_branch — posts created BEFORE the window. Uses the
    #       Trends-tab LAG approach over post_views_daily, summing daily
    #       gains across snapshots inside the window. NULL values
    #       (Reddit posts don't write upvotes/comments to post_views_daily
    #       at all) are excluded by the IS NOT NULL FILTER, so old
    #       Reddit posts contribute 0 here — that's a known limitation
    #       of the snapshot pipeline and matches the Trends chart.
    # Per-metric platform filter matches the SCOPED column's filter so the
    # bracket is always >= scoped for the same metric:
    #   upvotes:  no platform filter (scoped sums all platforms with reddit/
    #             moltbook -1 OP self-vote discount; bracket uses raw values).
    #   comments: no platform filter (scoped sums all platforms).
    #   views:    excludes moltbook/github/github_issues (matches scoped's
    #             FILTER clause in _windowed_post_engagement).
    days_sql = "INTERVAL '" + str(int(days)) + " days'"
    views_excl = "LOWER(p.platform) NOT IN ('moltbook', 'github', 'github_issues')"
    # When platform is set, also apply the mention-row exclusion so this
    # function lines up with the /api/style/stats view that the dashboard
    # shows above the Project Final Stats table.
    plat_clause = _platform_sql_clause(platform, "p")
    proj_clause, proj_params = _project_filter_sql(name, "p")
    cur = conn.execute(
        # Branch 1: posts CREATED in the window. Full live posts.* values
        # are credited as in-window gain. No -1 OP discount on upvotes, so
        # bracket >= scoped on reddit/moltbook by exactly #posts_in_window.
        "WITH new_posts AS ("
          "SELECT "
            "COALESCE(SUM(p.upvotes),        0)::bigint AS upvotes, "
            "COALESCE(SUM(p.comments_count), 0)::bigint AS comments, "
            "COALESCE(SUM(p.views) FILTER (WHERE " + views_excl + "), 0)::bigint AS views "
          "FROM posts p "
          "WHERE " + proj_clause + " "
            "AND p.posted_at >= NOW() - " + days_sql + plat_clause +
        "), "
        # Branch 2: posts created BEFORE the window. LAG over snapshots
        # inside the window. Reddit/moltbook post_views_daily rows carry
        # NULL upvotes/comments by design (those stats pipelines only
        # write views), so the IS NOT NULL FILTER drops them: old Reddit
        # upvotes/comments gain is structurally invisible here, matching
        # the Trends chart.
        "old_post_daily AS ("
          "SELECT pvd.post_id, p.platform, "
            "pvd.upvotes,  LAG(pvd.upvotes)  OVER w AS prev_upvotes, "
            "pvd.comments, LAG(pvd.comments) OVER w AS prev_comments, "
            "pvd.views,    LAG(pvd.views)    OVER w AS prev_views "
          "FROM post_views_daily pvd "
          "JOIN posts p ON p.id = pvd.post_id "
          "WHERE pvd.day >= CURRENT_DATE - " + days_sql + " "
            "AND " + proj_clause + " "
            "AND p.posted_at < NOW() - " + days_sql + plat_clause + " "
          "WINDOW w AS (PARTITION BY pvd.post_id ORDER BY pvd.day)"
        "), "
        "old_posts AS ("
          "SELECT "
            "COALESCE(SUM(GREATEST(upvotes  - prev_upvotes,  0)) "
              "FILTER (WHERE prev_upvotes  IS NOT NULL AND upvotes  IS NOT NULL), 0)::bigint AS upvotes, "
            "COALESCE(SUM(GREATEST(comments - prev_comments, 0)) "
              "FILTER (WHERE prev_comments IS NOT NULL AND comments IS NOT NULL), 0)::bigint AS comments, "
            "COALESCE(SUM(GREATEST(views    - prev_views,    0)) "
              "FILTER (WHERE prev_views    IS NOT NULL AND views    IS NOT NULL "
                "AND LOWER(platform) NOT IN ('moltbook', 'github', 'github_issues')), 0)::bigint AS views "
          "FROM old_post_daily"
        ") "
        "SELECT "
          "n.upvotes  + o.upvotes, "
          "n.comments + o.comments, "
          "n.views    + o.views "
        "FROM new_posts n CROSS JOIN old_posts o",
        proj_params + proj_params,
    )
    row = cur.fetchone() or (0, 0, 0)
    upvotes_total = int(row[0] or 0)
    comments_total = int(row[1] or 0)
    views_total = int(row[2] or 0)

    # post_clicks bracket = scoped (post_links.clicks SUM for new posts in
    # window) + COUNT of post_link_clicks events on OLD posts during the
    # window. The "new posts" leg matches the scoped column exactly so
    # bracket >= scoped is guaranteed; the "old posts" leg captures
    # click traffic that hit pre-existing posts during the period.
    cur2 = conn.execute(
        "WITH new_clicks AS ("
          "SELECT COALESCE(SUM(pl.total_clicks), 0)::bigint AS clicks "
          "FROM posts p "
          "LEFT JOIN ("
            "SELECT post_id, SUM(clicks)::int AS total_clicks "
            "FROM post_links WHERE post_id IS NOT NULL GROUP BY post_id"
          ") pl ON pl.post_id = p.id "
          "WHERE " + proj_clause + " "
            "AND p.posted_at >= NOW() - " + days_sql + plat_clause +
        "), "
        "old_event_clicks AS ("
          "SELECT COALESCE(COUNT(*), 0)::bigint AS clicks "
          "FROM post_link_clicks plc "
          "JOIN post_links pl ON pl.code = plc.code "
          "JOIN posts p ON p.id = pl.post_id "
          "WHERE plc.ts >= NOW() - " + days_sql + " "
            "AND plc.is_bot = FALSE "
            "AND " + proj_clause + " "
            "AND p.posted_at < NOW() - " + days_sql + plat_clause +
        ") "
        "SELECT n.clicks + o.clicks "
        "FROM new_clicks n CROSS JOIN old_event_clicks o",
        proj_params + proj_params,
    )
    row2 = cur2.fetchone() or (0,)
    post_clicks_total = int(row2[0] or 0)

    return {
        "upvotes": upvotes_total,
        "comments": comments_total,
        "views": views_total,
        "post_clicks": post_clicks_total,
    }


def _windowed_post_engagement(conn, name, days, platform=None):
    """Sum engagement only for posts *created within the window*.

    project_stats.get_post_stats aggregates engagement over ALL time for the
    project, which is misleading when the window is a day or a week. Here we
    filter by posted_at so upvotes/comments/views/post_clicks match the same
    24h slice as the 'recent' post count.

    When `platform` is set, also folds in the same platform/mention filter
    that /api/style/stats uses so the Project Final Stats and Posts by
    Engagement Style tables agree on the same denominator.

    post_clicks: SUM of post_links.clicks attributable to short links minted
    for posts in this project's window (post_id-keyed; reply-keyed clicks
    excluded so we don't double-count engagement on replies hanging off
    someone else's thread).
    """
    # upvotes is NET of the Reddit/Moltbook OP self-upvote (both platforms auto-
    # apply a +1 to every post). Discounting per row before the SUM means the
    # funnel reflects organic engagement, not (posts * 1) + organic. X /
    # LinkedIn / GitHub have no equivalent auto-vote so they pass through.
    # Matches top_performers.SCORE_SQL and bin/server.js upvotes_discounted.
    plat_clause = _platform_sql_clause(platform, "p")
    proj_clause, proj_params = _project_filter_sql(name, "p")
    cur = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN LOWER(p.platform) IN ('reddit', 'moltbook') "
        "  THEN GREATEST(0, COALESCE(p.upvotes, 0) - 1) "
        "  ELSE COALESCE(p.upvotes, 0) END), 0), "
        "COALESCE(SUM(p.comments_count), 0), "
        "COALESCE(SUM(p.views) FILTER (WHERE LOWER(p.platform) NOT IN ('moltbook', 'github', 'github_issues')), 0), "
        "COUNT(*) FILTER (WHERE LOWER(p.platform) NOT IN ('moltbook', 'github', 'github_issues')), "
        "COALESCE(SUM(pl.total_clicks), 0) "
        "FROM posts p "
        "LEFT JOIN ("
        "  SELECT post_id, SUM(clicks)::int AS total_clicks "
        "  FROM post_links WHERE post_id IS NOT NULL GROUP BY post_id"
        ") pl ON pl.post_id = p.id "
        "WHERE " + proj_clause + " AND p.posted_at >= NOW() - INTERVAL '" + str(days) + " days'"
        + plat_clause,
        proj_params,
    )
    row = cur.fetchone() or (0, 0, 0, 0, 0)
    return {
        "upvotes": int(row[0] or 0),
        "comments": int(row[1] or 0),
        "views": int(row[2] or 0),
        "views_posts": int(row[3] or 0),
        "post_clicks": int(row[4] or 0),
    }


def _seo_pages_count(conn, name, days):
    """Count SEO pages published in window. seo_keywords.product matches project_name."""
    cur = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM seo_keywords WHERE product = %s "
        "   AND completed_at >= NOW() - INTERVAL '" + str(days) + " days' "
        "   AND page_url IS NOT NULL) + "
        "(SELECT COUNT(*) FROM gsc_queries WHERE product = %s "
        "   AND completed_at >= NOW() - INTERVAL '" + str(days) + " days' "
        "   AND page_url IS NOT NULL)",
        (name, name),
    )
    row = cur.fetchone()
    return int((row and row[0]) or 0)


def _amplitude_signups_24h_from_cache(proj):
    """For days==1, read the precomputed rolling-24h count from the cache
    written by scripts/amplitude_24h_signups.py.

    That script uses our own server-side PostHog `newsletter_subscribed`
    event (real-time, partner_outcome IN ('partner_created','partner_reused'))
    as the primary source, because Amplitude segmentation/export both lag
    several hours behind real time and bucket by calendar day in the
    project's display timezone.

    Returns int (count) or None when:
      - cache file missing / unreadable
      - project not present in cache
      - cache is older than 30 minutes (stale, fall back to live segmentation)
    """
    cache_path = os.path.expanduser(
        "~/social-autoposter/skill/cache/amplitude_24h_signups.json"
    )
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path) as f:
            cur = json.load(f)
        gen = cur.get("generated_at_utc")
        if gen:
            age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(gen)).total_seconds() / 60
            if age_min > 30:
                return None
        for p in cur.get("projects") or []:
            if p.get("name") == proj.get("name"):
                v = p.get("count_24h")
                return int(v) if v is not None else None
    except Exception:
        return None
    return None


def _amplitude_signups(proj, days, env):
    """Pull attributed end-product signup count from the client's Amplitude.

    For projects with an `amplitude` config block (project_id, api_key_env,
    secret_key_env, signup_event, attribution_filter). Returns total signups
    matching the filter over the last `days`, or None if not configured /
    creds missing / API errors. Errors are non-fatal — they collapse to None
    so the dashboard falls back to the click-based metric.

    Special case: days == 1 reads from the rolling-24h cache populated by
    scripts/amplitude_24h_signups.py, which uses real-time PostHog data
    instead of Amplitude segmentation (which lags hours and buckets by
    calendar day in the project's display timezone). Falls through to the
    segmentation path if the cache is missing or stale.
    """
    amp = proj.get("amplitude")
    if not amp:
        return None
    if days == 1:
        cached = _amplitude_signups_24h_from_cache(proj)
        if cached is not None:
            return cached
    api_key = env.get(amp.get("api_key_env", ""))
    secret_key = env.get(amp.get("secret_key_env", ""))
    if not api_key or not secret_key:
        return None
    import base64
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=max(1, days) - 1)
    e = json.dumps({
        "event_type": amp.get("signup_event", "New User Sign Up"),
        "filters": [
            {
                "subprop_type": "event",
                "subprop_key": k,
                "subprop_op": "is",
                "subprop_value": v if isinstance(v, list) else [v],
            }
            for k, v in (amp.get("attribution_filter") or {}).items()
        ],
    })
    qs = urllib.parse.urlencode({
        "e": e,
        "start": start_dt.strftime("%Y%m%d"),
        "end": end_dt.strftime("%Y%m%d"),
        "i": "1",
        "m": "totals",
    })
    auth_b64 = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    req = urllib.request.Request(
        f"https://amplitude.com/api/2/events/segmentation?{qs}",
        headers={"Authorization": f"Basic {auth_b64}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"  amplitude signups fetch error ({proj.get('name')}): {exc}", file=sys.stderr)
        return None
    series = (data.get("data", {}).get("series") or [[]])[0]
    return int(sum(int(x or 0) for x in series))


def _post_stats_synthetic_null(conn, days):
    """NULL-project sibling of ps.get_post_stats. Same shape, same upvote
    discount logic; filters posts.project_name IS NULL instead of = name.

    ps.get_post_stats lives in the chflags-locked project_stats.py, so the
    synthetic '(no project)' bucket reuses this in build_project_entry
    rather than passing a magic string into a function that would return
    all-zeros for it.
    """
    cur = conn.execute(
        "SELECT COUNT(*), "
        "COUNT(*) FILTER (WHERE posted_at >= NOW() - INTERVAL '" + str(int(days)) + " days'), "
        "COUNT(*) FILTER (WHERE status = 'active'), "
        "COUNT(*) FILTER (WHERE status IN ('removed', 'deleted')), "
        "COALESCE(SUM(CASE WHEN LOWER(platform) IN ('reddit', 'moltbook') "
        "  THEN GREATEST(0, COALESCE(upvotes, 0) - 1) "
        "  ELSE COALESCE(upvotes, 0) END), 0), "
        "COALESCE(SUM(comments_count), 0), "
        "COALESCE(SUM(views), 0) "
        "FROM posts WHERE project_name IS NULL"
    )
    row = cur.fetchone()
    if not row:
        return {}
    cols = ["total", "recent", "active", "removed", "total_upvotes", "total_comments", "total_views"]
    return dict(zip(cols, row))


def _platform_breakdown_synthetic_null(conn, days):
    """NULL-project sibling of ps.get_platform_breakdown."""
    cur = conn.execute(
        "SELECT platform, COUNT(*) as cnt FROM posts "
        "WHERE project_name IS NULL AND posted_at >= NOW() - INTERVAL '" + str(int(days)) + " days' "
        "GROUP BY platform ORDER BY cnt DESC"
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def build_project_entry(conn, proj, days, api_key, ph_pid, bookings_conn, env, ph_results, platform=None):
    name = proj["name"]
    if name == SYNTHETIC_NO_PROJECT_NAME:
        # Bypass ps.* (locked) for the synthetic bucket; both helpers above
        # query posts WHERE project_name IS NULL with the same shape.
        post_stats = _post_stats_synthetic_null(conn, days)
        platforms = _platform_breakdown_synthetic_null(conn, days)
    else:
        post_stats = ps.get_post_stats(conn, name, days)
        platforms = ps.get_platform_breakdown(conn, name, days)
    eng_recent = _windowed_post_engagement(conn, name, days, platform=platform)
    eng_period_total = _period_total_engagement(conn, name, days, platform=platform)
    seo_pages_recent = _seo_pages_count(conn, name, days)

    # When a platform filter is active, override the "recent" post count
    # in post_stats (which project_stats.get_post_stats computes across all
    # platforms and is chflags-locked so we can't add a param there) so the
    # "Posts" column in the dashboard's Project Final Stats table speaks the
    # same vocabulary as Upvotes/Comments/Views.
    if platform:
        plat_clause = _platform_sql_clause(platform, "")
        proj_clause, proj_params = _project_filter_sql(name, "")
        cur_pf = conn.execute(
            "SELECT COUNT(*) FROM posts "
            "WHERE " + proj_clause + " "
            "AND posted_at >= NOW() - INTERVAL '" + str(int(days)) + " days'"
            + plat_clause,
            proj_params,
        )
        post_stats["recent"] = int((cur_pf.fetchone() or (0,))[0])

    domains = ps.get_project_domains(proj)
    ph_override = proj.get("posthog", {}) or {}
    ph_key = env.get(ph_override.get("api_key_env", ""), api_key)
    ph_pid_proj = ph_override.get("project_id", ph_pid)
    analytics_error = None
    if domains:
        per_domain = []
        for d in domains:
            stats = ph_results.get((ph_key, ph_pid_proj, d))
            if stats is None:
                stats = _empty_domain_stats(d)
            if stats.get("error") and not analytics_error:
                analytics_error = stats["error"]
            per_domain.append(stats)
        posthog = _ph_combine(per_domain)
        if analytics_error:
            posthog["error"] = analytics_error
    else:
        posthog = None

    # Window-scoped: `created_paths` is now restricted to pages whose
    # seo_keywords/gsc_queries `completed_at` falls inside `days`. Top tab →
    # Pages sub-tab already filters rows on this set, so it becomes "pages
    # created in the selected period" automatically.
    created_by_domain = _created_paths_for_project(conn, proj, days=days)
    if posthog is not None:
        for d, detail in (posthog.get("pageview_details") or {}).items():
            paths = created_by_domain.get((d or "").lower(), set())
            detail["created_paths"] = sorted(paths)

    # Preserve the pre-rewrite, domain-wide totals for the analytics-broken
    # canary below — it's meant to answer "is window.posthog wired up on this
    # site at all?", which requires domain-level signal, not per-new-page.
    domain_wide_pv = int(posthog["pageviews"]) if posthog else 0
    domain_wide_signups = int(posthog["email_signups"]) if posthog else 0
    domain_wide_sched = int(posthog["schedule_clicks"]) if posthog else 0
    domain_wide_get_started = int(posthog["get_started_clicks"]) if posthog else 0

    # Recompute funnel totals against the window-scoped created set so the
    # Status tab → project funnel columns reflect "pageviews / signups /
    # schedule clicks / download clicks ONLY on pages we generated in this
    # window" instead of domain-wide traffic. cta_clicks and real_bookings
    # are not tracked per-page so they stay domain/project-wide.
    #
    # Skip entirely when PostHog is errored: the top_pages maps are empty
    # for errored domains, so scoping would silently collapse everything to
    # zero. Keep the funnel values as None below so the dashboard renders
    # 'err' instead of a misleading 0.
    # Only pageviews get window-scoped to "traffic on pages we generated in
    # this window". Conversion events (newsletter_subscribed, schedule_click,
    # get_started_click) fire on dedicated landing pages (/, /use-case, /ig,
    # etc.), almost never on the freshly-generated /blog/* and /t/* SEO pages
    # we ship each cycle. Scoping those collapsed every project to 0 and made
    # the dashboard's Email Signups / Schedule Clicks / Get Started columns
    # useless. Domain-wide is the honest metric for those.
    if posthog is not None and not analytics_error:
        scoped_pv = 0
        for d, detail in (posthog.get("pageview_details") or {}).items():
            created = {_norm_path(p) for p in created_by_domain.get((d or "").lower(), set())}
            if not created:
                continue
            for path, cnt in (detail.get("top_pages") or {}).items():
                if _norm_path(path) in created:
                    scoped_pv += int(cnt or 0)
        posthog["pageviews"] = scoped_pv

    client_slug = ps.get_client_slug(name)
    booking_table = ps.get_booking_table(name)
    require_utm = _bookings_require_utm(name)
    bookings = _bookings_shared(bookings_conn, client_slug, days, booking_table, require_utm) if client_slug else None

    # When the PostHog batch failed, the aggregate numbers on `posthog` are
    # all 0 but that doesn't mean there are no events, it means we couldn't
    # read them. Surface null + an error string on the funnel so the
    # dashboard renders 'err' instead of silently claiming "zero pageviews".
    if analytics_error:
        pvs = None
        ctas = None
        email_signups = None
        schedule_clicks = None
        get_started_clicks = None
        cross_product_clicks = None
        ctr = None
        conv = None
        dw_pv_out = None
        dw_signups_out = None
        dw_sched_out = None
        dw_get_started_out = None
        analytics_suspected_broken = False
    else:
        pvs = posthog["pageviews"] if posthog else 0
        ctas = posthog["cta_clicks"] if posthog else 0
        email_signups = (posthog["email_signups"] if posthog else 0)
        schedule_clicks = (posthog["schedule_clicks"] if posthog else 0)
        get_started_clicks = (posthog["get_started_clicks"] if posthog else 0)
        # Cross-product stays domain-wide on purpose: it's a lightweight
        # signal ("how many clicks went to a sibling product from this site")
        # with no per-page top-pages breakdown, so there's nothing to scope.
        cross_product_clicks = (posthog.get("cross_product_clicks", 0) if posthog else 0)
        # Domain-wide counterparts for the "scoped (domain-wide)" dashboard
        # rendering. domain_wide_* were captured before the window-scoping
        # overwrote posthog["pageviews"] etc.
        dw_pv_out = domain_wide_pv if posthog else 0
        dw_signups_out = domain_wide_signups if posthog else 0
        dw_sched_out = domain_wide_sched if posthog else 0
        dw_get_started_out = domain_wide_get_started if posthog else 0
        ctr = (ctas / pvs * 100) if pvs else None
        conv = None  # computed below once `real` is in scope
        # Canary: real traffic but zero tracked conversion events almost
        # always means window.posthog was never wired up on the site (e.g.
        # Fazm newsletter bug where signups worked but nothing fired to
        # PostHog). Use domain-wide totals so the signal isn't diluted by
        # the window-scoped funnel numbers above.
        analytics_suspected_broken = (domain_wide_pv >= 500) and ((domain_wide_signups + domain_wide_sched + domain_wide_get_started) == 0)

    real = bookings.get("real_bookings", 0) if bookings else 0
    dm_clicks = _dm_short_link_stats(conn, name, days)
    dm_bookings = _dm_booking_count(conn, bookings_conn, name, days)
    amplitude_signups = _amplitude_signups(proj, days, env)
    if not analytics_error:
        conv = (real / ctas * 100) if ctas else None

    return {
        "name": name,
        "posts": {
            "total": post_stats.get("total", 0),
            "recent": post_stats.get("recent", 0),
            "active": post_stats.get("active", 0),
            "removed": post_stats.get("removed", 0),
            # Lifetime engagement across ALL posts for this project (kept for context).
            "upvotes": post_stats.get("total_upvotes", 0),
            "comments": post_stats.get("total_comments", 0),
            "views": post_stats.get("total_views", 0),
            # Window-scoped engagement: only posts created in the last `days`.
            "upvotes_recent": eng_recent["upvotes"],
            "comments_recent": eng_recent["comments"],
            "views_recent": eng_recent["views"] if eng_recent["views_posts"] > 0 else None,
            # post_clicks_recent: SUM of post_links.clicks for short links
            # minted for posts in this project's window. Pre-2026-05-07 rows
            # may include bot prefetches; post-2026-05-07 rows are humans-only
            # (Twitter card / LinkedIn unfurl / Slack preview filtered at the
            # resolver via post_link_clicks.is_bot). See server.js /api/top.
            "post_clicks_recent": eng_recent["post_clicks"],
            # Period totals: engagement GAINED during the window across ALL
            # posts (regardless of posted_at), mirroring the Trends-tab
            # /api/{views,upvotes,comments,clicks}/per-day SUM. The dashboard
            # renders each as "<scoped> (<period_total>)" in gray brackets.
            # post_clicks_period_total counts post_link_clicks (is_bot=FALSE)
            # in the window joined to this project's posts.
            "upvotes_period_total": eng_period_total["upvotes"],
            "comments_period_total": eng_period_total["comments"],
            "views_period_total": eng_period_total["views"],
            "post_clicks_period_total": eng_period_total["post_clicks"],
        },
        "seo": {"pages_recent": seo_pages_recent},
        "platforms": platforms,
        "posthog": posthog,
        "bookings": bookings,
        "funnel": {
            "pageviews": pvs,
            "cta_clicks": ctas,
            "email_signups": email_signups,
            "schedule_clicks": schedule_clicks,
            "get_started_clicks": get_started_clicks,
            "cross_product_clicks": cross_product_clicks,
            "real_bookings": real,
            "dm_clicks": dm_clicks,
            "dm_bookings": dm_bookings,
            # Attributed signups on the client's product (Amplitude), filtered
            # by the UTM source we forward (config.json projects[].amplitude).
            # null when the project has no `amplitude` block or the fetch
            # fails — dashboard falls back to get_started_clicks.
            "amplitude_signups": amplitude_signups,
            # Filter shape (e.g. {"utm_source": "studyly.io"}) for tooltip;
            # null when the project has no `amplitude` block.
            "amplitude_filter": (proj.get("amplitude") or {}).get("attribution_filter") if proj.get("amplitude") else None,
            "ctr_pct": ctr,
            "conv_pct": conv,
            # Domain-wide siblings: the dashboard shows each as "<scoped>
            # (<domain>)" so "0 pv for mk0r" doesn't hide 62 real visits
            # that happened to land on older pages.
            "domain_pageviews": dw_pv_out,
            "domain_email_signups": dw_signups_out,
            "domain_schedule_clicks": dw_sched_out,
            "domain_get_started_clicks": dw_get_started_out,
        },
        "analytics_error": analytics_error,
        "analytics_suspected_broken": analytics_suspected_broken,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--project", help="Filter to a single project name")
    parser.add_argument(
        "--platform",
        default="",
        help=(
            "Filter to a single platform (twitter|reddit|linkedin|github|moltbook). "
            "'x' is folded into 'twitter'. Empty / 'all' = no filter. "
            "Matches the same normalization used by /api/style/stats."
        ),
    )
    parser.add_argument(
        "--posts-only",
        action="store_true",
        help=(
            "Emit ONLY the per-project posts.* engagement counters; skip the "
            "PostHog batch (pageviews/CTAs), the bookings DB, Amplitude, and "
            "SEO page counts. Drops the python runtime from ~30s+ to ~1s. "
            "Used by /api/funnel/stats as a fast overlay path when the "
            "dashboard's platform pill changes — those slow sources are "
            "platform-independent so the all-platform snapshot's values for "
            "them stay correct, and only the engagement columns need to "
            "react to the filter."
        ),
    )
    args = parser.parse_args()

    # Normalize platform early; pass empty string when no filter so build_project_entry
    # can splat it unconditionally without spreading the alias logic everywhere.
    platform = _normalize_platform(args.platform)
    # Safety: enforce the same regex /api/funnel/stats accepts so a bad CLI
    # value can't smuggle SQL through _platform_sql_clause.
    if platform and not re.match(r"^[a-z0-9_]{1,32}$", platform):
        print(json.dumps({"error": f"invalid platform: {args.platform!r}"}), file=sys.stdout)
        sys.exit(1)

    ps.load_env()
    env = os.environ
    config = ps.load_config()

    api_key = env.get("POSTHOG_PERSONAL_API_KEY")
    project_id = env.get("POSTHOG_PROJECT_ID", "330744")
    bookings_db_url = env.get("BOOKINGS_DATABASE_URL")

    _bridge_per_project_posthog_keys_from_keychain(config, env)

    # Fast path: --posts-only skips the slow PostHog/Amplitude/bookings work
    # and emits ONLY the per-project posts.* counters. Used as a low-latency
    # overlay on top of the cached all-platform snapshot when the dashboard's
    # platform pill changes (see /api/funnel/stats in bin/server.js). Runs
    # in ~1s instead of ~30s because there are no external HTTP calls AND
    # the per-project SQL is collapsed into 3 batched GROUP BY queries
    # (the naive per-project loop pays N x ~180ms Postgres round-trip).
    if args.posts_only:
        conn = ps.dbmod.get_conn()
        days_sql = "INTERVAL '" + str(int(args.days)) + " days'"
        plat_clause_p = _platform_sql_clause(platform, "p")
        plat_clause_bare = _platform_sql_clause(platform, "")

        # Query 1: lifetime stats per project (matches project_stats.get_post_stats
        # shape). Lifetime aggregates are platform-independent in scope (they
        # describe what's in the table) — but for the posts-only overlay we
        # echo the snapshot's lifetime numbers anyway by NOT applying the
        # platform filter here, so the "Total" tooltip etc. stays stable.
        cur = conn.execute(
            "SELECT project_name, "
              "COUNT(*)::bigint AS total, "
              "COUNT(*) FILTER (WHERE posted_at >= NOW() - " + days_sql + plat_clause_bare + ")::bigint AS recent, "
              "COUNT(*) FILTER (WHERE status = 'active')::bigint AS active, "
              "COUNT(*) FILTER (WHERE status IN ('removed', 'deleted'))::bigint AS removed, "
              "COALESCE(SUM(CASE WHEN LOWER(platform) IN ('reddit', 'moltbook') "
                "THEN GREATEST(0, COALESCE(upvotes, 0) - 1) "
                "ELSE COALESCE(upvotes, 0) END), 0)::bigint AS total_upvotes, "
              "COALESCE(SUM(comments_count), 0)::bigint AS total_comments, "
              "COALESCE(SUM(views), 0)::bigint AS total_views "
            "FROM posts WHERE project_name IS NOT NULL "
            "GROUP BY project_name"
        )
        lifetime = {r[0]: {
            "total": int(r[1]), "recent": int(r[2]), "active": int(r[3]),
            "removed": int(r[4]), "total_upvotes": int(r[5]),
            "total_comments": int(r[6]), "total_views": int(r[7]),
        } for r in cur.fetchall()}

        # Query 2: windowed engagement per project (_windowed_post_engagement).
        cur = conn.execute(
            "SELECT p.project_name, "
              "COALESCE(SUM(CASE WHEN LOWER(p.platform) IN ('reddit', 'moltbook') "
                "THEN GREATEST(0, COALESCE(p.upvotes, 0) - 1) "
                "ELSE COALESCE(p.upvotes, 0) END), 0)::bigint AS upvotes_recent, "
              "COALESCE(SUM(p.comments_count), 0)::bigint AS comments_recent, "
              "COALESCE(SUM(p.views) FILTER (WHERE LOWER(p.platform) NOT IN ('moltbook', 'github', 'github_issues')), 0)::bigint AS views_recent, "
              "COUNT(*) FILTER (WHERE LOWER(p.platform) NOT IN ('moltbook', 'github', 'github_issues'))::bigint AS views_posts, "
              "COALESCE(SUM(pl.total_clicks), 0)::bigint AS post_clicks_recent "
            "FROM posts p "
            "LEFT JOIN ("
              "SELECT post_id, SUM(clicks)::int AS total_clicks "
              "FROM post_links WHERE post_id IS NOT NULL GROUP BY post_id"
            ") pl ON pl.post_id = p.id "
            "WHERE p.project_name IS NOT NULL "
              "AND p.posted_at >= NOW() - " + days_sql + plat_clause_p + " "
            "GROUP BY p.project_name"
        )
        windowed = {r[0]: {
            "upvotes": int(r[1]), "comments": int(r[2]),
            "views": int(r[3]), "views_posts": int(r[4]),
            "post_clicks": int(r[5]),
        } for r in cur.fetchall()}

        # Query 3: period-total engagement per project (new_posts + old_posts
        # branches, GROUP BY project_name). Mirrors _period_total_engagement.
        views_excl = "LOWER(p.platform) NOT IN ('moltbook', 'github', 'github_issues')"
        cur = conn.execute(
            "WITH new_posts AS ("
              "SELECT p.project_name, "
                "COALESCE(SUM(p.upvotes), 0)::bigint AS upvotes, "
                "COALESCE(SUM(p.comments_count), 0)::bigint AS comments, "
                "COALESCE(SUM(p.views) FILTER (WHERE " + views_excl + "), 0)::bigint AS views "
              "FROM posts p "
              "WHERE p.project_name IS NOT NULL "
                "AND p.posted_at >= NOW() - " + days_sql + plat_clause_p + " "
              "GROUP BY p.project_name"
            "), "
            "old_post_daily AS ("
              "SELECT p.project_name, pvd.post_id, p.platform, "
                "pvd.upvotes,  LAG(pvd.upvotes)  OVER w AS prev_upvotes, "
                "pvd.comments, LAG(pvd.comments) OVER w AS prev_comments, "
                "pvd.views,    LAG(pvd.views)    OVER w AS prev_views "
              "FROM post_views_daily pvd "
              "JOIN posts p ON p.id = pvd.post_id "
              "WHERE pvd.day >= CURRENT_DATE - " + days_sql + " "
                "AND p.project_name IS NOT NULL "
                "AND p.posted_at < NOW() - " + days_sql + plat_clause_p + " "
              "WINDOW w AS (PARTITION BY pvd.post_id ORDER BY pvd.day)"
            "), "
            "old_posts AS ("
              "SELECT project_name, "
                "COALESCE(SUM(GREATEST(upvotes - prev_upvotes, 0)) "
                  "FILTER (WHERE prev_upvotes IS NOT NULL AND upvotes IS NOT NULL), 0)::bigint AS upvotes, "
                "COALESCE(SUM(GREATEST(comments - prev_comments, 0)) "
                  "FILTER (WHERE prev_comments IS NOT NULL AND comments IS NOT NULL), 0)::bigint AS comments, "
                "COALESCE(SUM(GREATEST(views - prev_views, 0)) "
                  "FILTER (WHERE prev_views IS NOT NULL AND views IS NOT NULL "
                    "AND LOWER(platform) NOT IN ('moltbook', 'github', 'github_issues')), 0)::bigint AS views "
              "FROM old_post_daily GROUP BY project_name"
            ") "
            "SELECT COALESCE(n.project_name, o.project_name) AS project_name, "
              "COALESCE(n.upvotes, 0) + COALESCE(o.upvotes, 0), "
              "COALESCE(n.comments, 0) + COALESCE(o.comments, 0), "
              "COALESCE(n.views, 0) + COALESCE(o.views, 0) "
            "FROM new_posts n FULL OUTER JOIN old_posts o ON n.project_name = o.project_name"
        )
        period = {r[0]: {
            "upvotes": int(r[1]), "comments": int(r[2]), "views": int(r[3]),
        } for r in cur.fetchall()}

        # Query 4: period-total post_clicks per project (new clicks + old
        # plc events). Same shape as _period_total_engagement's clicks leg.
        cur = conn.execute(
            "WITH new_clicks AS ("
              "SELECT p.project_name, "
                "COALESCE(SUM(pl.total_clicks), 0)::bigint AS clicks "
              "FROM posts p "
              "LEFT JOIN ("
                "SELECT post_id, SUM(clicks)::int AS total_clicks "
                "FROM post_links WHERE post_id IS NOT NULL GROUP BY post_id"
              ") pl ON pl.post_id = p.id "
              "WHERE p.project_name IS NOT NULL "
                "AND p.posted_at >= NOW() - " + days_sql + plat_clause_p + " "
              "GROUP BY p.project_name"
            "), "
            "old_event_clicks AS ("
              "SELECT p.project_name, COUNT(*)::bigint AS clicks "
              "FROM post_link_clicks plc "
              "JOIN post_links pl ON pl.code = plc.code "
              "JOIN posts p ON p.id = pl.post_id "
              "WHERE plc.ts >= NOW() - " + days_sql + " "
                "AND plc.is_bot = FALSE "
                "AND p.project_name IS NOT NULL "
                "AND p.posted_at < NOW() - " + days_sql + plat_clause_p + " "
              "GROUP BY p.project_name"
            ") "
            "SELECT COALESCE(n.project_name, o.project_name), "
              "COALESCE(n.clicks, 0) + COALESCE(o.clicks, 0) "
            "FROM new_clicks n FULL OUTER JOIN old_event_clicks o ON n.project_name = o.project_name"
        )
        period_clicks = {r[0]: int(r[1]) for r in cur.fetchall()}

        # Synthetic '(no project)' bucket: same shape as Queries 1-4 above,
        # filtered to posts.project_name IS NULL instead of GROUP BY name.
        # Keeps the funnel total aligned with /api/style/stats (which has no
        # project filter) by surfacing un-tagged rows as their own row.
        cur = conn.execute(
            "SELECT "
              "COUNT(*)::bigint, "
              "COUNT(*) FILTER (WHERE posted_at >= NOW() - " + days_sql + plat_clause_bare + ")::bigint, "
              "COUNT(*) FILTER (WHERE status = 'active')::bigint, "
              "COUNT(*) FILTER (WHERE status IN ('removed', 'deleted'))::bigint, "
              "COALESCE(SUM(CASE WHEN LOWER(platform) IN ('reddit', 'moltbook') "
                "THEN GREATEST(0, COALESCE(upvotes, 0) - 1) "
                "ELSE COALESCE(upvotes, 0) END), 0)::bigint, "
              "COALESCE(SUM(comments_count), 0)::bigint, "
              "COALESCE(SUM(views), 0)::bigint "
            "FROM posts WHERE project_name IS NULL"
        )
        r = cur.fetchone() or (0, 0, 0, 0, 0, 0, 0)
        lifetime[SYNTHETIC_NO_PROJECT_NAME] = {
            "total": int(r[0]), "recent": int(r[1]), "active": int(r[2]),
            "removed": int(r[3]), "total_upvotes": int(r[4]),
            "total_comments": int(r[5]), "total_views": int(r[6]),
        }
        cur = conn.execute(
            "SELECT "
              "COALESCE(SUM(CASE WHEN LOWER(p.platform) IN ('reddit', 'moltbook') "
                "THEN GREATEST(0, COALESCE(p.upvotes, 0) - 1) "
                "ELSE COALESCE(p.upvotes, 0) END), 0)::bigint, "
              "COALESCE(SUM(p.comments_count), 0)::bigint, "
              "COALESCE(SUM(p.views) FILTER (WHERE LOWER(p.platform) NOT IN ('moltbook', 'github', 'github_issues')), 0)::bigint, "
              "COUNT(*) FILTER (WHERE LOWER(p.platform) NOT IN ('moltbook', 'github', 'github_issues'))::bigint, "
              "COALESCE(SUM(pl.total_clicks), 0)::bigint "
            "FROM posts p "
            "LEFT JOIN ("
              "SELECT post_id, SUM(clicks)::int AS total_clicks "
              "FROM post_links WHERE post_id IS NOT NULL GROUP BY post_id"
            ") pl ON pl.post_id = p.id "
            "WHERE p.project_name IS NULL "
              "AND p.posted_at >= NOW() - " + days_sql + plat_clause_p
        )
        r = cur.fetchone() or (0, 0, 0, 0, 0)
        windowed[SYNTHETIC_NO_PROJECT_NAME] = {
            "upvotes": int(r[0]), "comments": int(r[1]),
            "views": int(r[2]), "views_posts": int(r[3]),
            "post_clicks": int(r[4]),
        }
        # Period total (new_posts + old_posts) for the NULL bucket. Same
        # branch logic as Query 3 above without the GROUP BY.
        cur = conn.execute(
            "WITH new_posts AS ("
              "SELECT "
                "COALESCE(SUM(p.upvotes), 0)::bigint AS upvotes, "
                "COALESCE(SUM(p.comments_count), 0)::bigint AS comments, "
                "COALESCE(SUM(p.views) FILTER (WHERE " + views_excl + "), 0)::bigint AS views "
              "FROM posts p "
              "WHERE p.project_name IS NULL "
                "AND p.posted_at >= NOW() - " + days_sql + plat_clause_p +
            "), "
            "old_post_daily AS ("
              "SELECT pvd.post_id, p.platform, "
                "pvd.upvotes,  LAG(pvd.upvotes)  OVER w AS prev_upvotes, "
                "pvd.comments, LAG(pvd.comments) OVER w AS prev_comments, "
                "pvd.views,    LAG(pvd.views)    OVER w AS prev_views "
              "FROM post_views_daily pvd "
              "JOIN posts p ON p.id = pvd.post_id "
              "WHERE pvd.day >= CURRENT_DATE - " + days_sql + " "
                "AND p.project_name IS NULL "
                "AND p.posted_at < NOW() - " + days_sql + plat_clause_p + " "
              "WINDOW w AS (PARTITION BY pvd.post_id ORDER BY pvd.day)"
            "), "
            "old_posts AS ("
              "SELECT "
                "COALESCE(SUM(GREATEST(upvotes - prev_upvotes, 0)) "
                  "FILTER (WHERE prev_upvotes IS NOT NULL AND upvotes IS NOT NULL), 0)::bigint AS upvotes, "
                "COALESCE(SUM(GREATEST(comments - prev_comments, 0)) "
                  "FILTER (WHERE prev_comments IS NOT NULL AND comments IS NOT NULL), 0)::bigint AS comments, "
                "COALESCE(SUM(GREATEST(views - prev_views, 0)) "
                  "FILTER (WHERE prev_views IS NOT NULL AND views IS NOT NULL "
                    "AND LOWER(platform) NOT IN ('moltbook', 'github', 'github_issues')), 0)::bigint AS views "
              "FROM old_post_daily"
            ") "
            "SELECT n.upvotes + o.upvotes, n.comments + o.comments, n.views + o.views "
            "FROM new_posts n CROSS JOIN old_posts o"
        )
        r = cur.fetchone() or (0, 0, 0)
        period[SYNTHETIC_NO_PROJECT_NAME] = {
            "upvotes": int(r[0]), "comments": int(r[1]), "views": int(r[2]),
        }
        # Period-total post_clicks for the NULL bucket.
        cur = conn.execute(
            "WITH new_clicks AS ("
              "SELECT COALESCE(SUM(pl.total_clicks), 0)::bigint AS clicks "
              "FROM posts p "
              "LEFT JOIN ("
                "SELECT post_id, SUM(clicks)::int AS total_clicks "
                "FROM post_links WHERE post_id IS NOT NULL GROUP BY post_id"
              ") pl ON pl.post_id = p.id "
              "WHERE p.project_name IS NULL "
                "AND p.posted_at >= NOW() - " + days_sql + plat_clause_p +
            "), "
            "old_event_clicks AS ("
              "SELECT COUNT(*)::bigint AS clicks "
              "FROM post_link_clicks plc "
              "JOIN post_links pl ON pl.code = plc.code "
              "JOIN posts p ON p.id = pl.post_id "
              "WHERE plc.ts >= NOW() - " + days_sql + " "
                "AND plc.is_bot = FALSE "
                "AND p.project_name IS NULL "
                "AND p.posted_at < NOW() - " + days_sql + plat_clause_p +
            ") "
            "SELECT n.clicks + o.clicks FROM new_clicks n CROSS JOIN old_event_clicks o"
        )
        r = cur.fetchone() or (0,)
        period_clicks[SYNTHETIC_NO_PROJECT_NAME] = int(r[0] or 0)

        # Project list: real projects from config + the synthetic NULL bucket.
        proj_list = list(config.get("projects", [])) + [{"name": SYNTHETIC_NO_PROJECT_NAME}]
        out_projects = []
        for proj in proj_list:
            name = proj["name"]
            if args.project and args.project.lower() != name.lower():
                continue
            life = lifetime.get(name) or {}
            w = windowed.get(name) or {"upvotes": 0, "comments": 0, "views": 0, "views_posts": 0, "post_clicks": 0}
            pe = period.get(name) or {"upvotes": 0, "comments": 0, "views": 0}
            out_projects.append({
                "name": name,
                "posts": {
                    "total": int(life.get("total", 0)),
                    "recent": int(life.get("recent", 0)),
                    "active": int(life.get("active", 0)),
                    "removed": int(life.get("removed", 0)),
                    "upvotes": int(life.get("total_upvotes", 0)),
                    "comments": int(life.get("total_comments", 0)),
                    "views": int(life.get("total_views", 0)),
                    "upvotes_recent": w["upvotes"],
                    "comments_recent": w["comments"],
                    "views_recent": w["views"] if w["views_posts"] > 0 else None,
                    "post_clicks_recent": w["post_clicks"],
                    "upvotes_period_total": pe["upvotes"],
                    "comments_period_total": pe["comments"],
                    "views_period_total": pe["views"],
                    "post_clicks_period_total": int(period_clicks.get(name, 0)),
                },
            })
        conn.close()
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "days": args.days,
            "platform": platform or "all",
            "posts_only": True,
            "projects": out_projects,
        }))
        return

    if not api_key:
        print(json.dumps({"error": "POSTHOG_PERSONAL_API_KEY not set"}), file=sys.stdout)
        sys.exit(1)

    conn = ps.dbmod.get_conn()

    bookings_conn = None
    if bookings_db_url:
        try:
            import psycopg2
            bookings_conn = psycopg2.connect(bookings_db_url)
        except Exception as e:
            print(f"  Bookings DB connect error: {e}", file=sys.stderr)
            bookings_conn = None

    selected_projects = []
    for proj in config.get("projects", []):
        name = proj["name"]
        if args.project and args.project.lower() != name.lower():
            continue
        selected_projects.append(proj)

    # Synthetic '(no project)' bucket: surfaces posts.project_name IS NULL rows
    # (e.g. IG drafts that landed without a project tag) so the funnel total
    # lines up with /api/style/stats. No website/landing_pages/posthog block,
    # so get_project_domains() returns [] -> PostHog/SEO/booking lookups all
    # become no-ops; per-project SQL helpers route through _project_filter_sql
    # to use `IS NULL` instead of `= name`.
    if not args.project or args.project.lower() == SYNTHETIC_NO_PROJECT_NAME.lower():
        selected_projects.append({"name": SYNTHETIC_NO_PROJECT_NAME})

    # Group domains by (api_key, project_id) so we issue one batched set of
    # HogQL calls per PostHog bucket instead of one-per-domain. Projects that
    # share a bucket collapse into a single batched fetch; projects with
    # dedicated credentials run in their own bucket concurrently.
    after = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S")
    buckets = {}
    for proj in selected_projects:
        domains = ps.get_project_domains(proj)
        if not domains:
            continue
        ph_over = proj.get("posthog", {}) or {}
        ph_key = env.get(ph_over.get("api_key_env", ""), api_key)
        ph_pid_proj = ph_over.get("project_id", project_id)
        bucket_domains = buckets.setdefault((ph_key, ph_pid_proj), set())
        for d in domains:
            bucket_domains.add(d)

    # One batched fetch per bucket. When a batch fails after retries, mark
    # every domain in that bucket as errored rather than rendering zeros.
    #
    # Concurrency is capped low (2) on purpose: PostHog's query endpoint
    # enforces a short-window burst limit (429 "throttled", recovery 1-12s),
    # and the personal API key is shared across most buckets. Firing 8
    # buckets at once (each ~10 sequential HogQL queries) created a
    # thundering herd that all hit the limiter together, all backed off
    # together, and re-collided on retry until the 4 attempts were
    # exhausted, marking whole buckets errored ('err' on the dashboard for
    # every project sharing them). Two-at-a-time keeps us under the burst
    # ceiling while the Retry-After-honoring backoff in _hogql absorbs the
    # occasional 429.
    ph_results = {}
    if buckets:
        pool_size = max(1, min(2, len(buckets)))
        with ThreadPoolExecutor(max_workers=pool_size) as ex:
            futs = {
                ex.submit(_ph_batch_counts, k, pid, sorted(ds), after): (k, pid, ds)
                for (k, pid), ds in buckets.items()
            }
            for fut, (k, pid, ds) in futs.items():
                try:
                    per_domain = fut.result()
                    for d, stats in per_domain.items():
                        ph_results[(k, pid, d)] = stats
                except HogqlError as e:
                    msg = f"PostHog unavailable: {e}"
                    print(f"  PostHog batch error (pid={pid}): {e}", file=sys.stderr)
                    for d in ds:
                        ph_results[(k, pid, d)] = _empty_domain_stats(d, error=msg)
                except Exception as e:
                    msg = f"PostHog batch error: {e}"
                    print(f"  PostHog batch unexpected error (pid={pid}): {e}", file=sys.stderr)
                    for d in ds:
                        ph_results[(k, pid, d)] = _empty_domain_stats(d, error=msg)

    out_projects = []
    for proj in selected_projects:
        name = proj["name"]
        try:
            out_projects.append(build_project_entry(
                conn, proj, args.days, api_key, project_id, bookings_conn, env, ph_results,
                platform=platform,
            ))
        except Exception as e:
            out_projects.append({"name": name, "error": str(e)})

    # `overall.recent` also respects the platform filter so the dashboard's
    # "N project(s)" / total header stays self-consistent with the per-row data.
    plat_clause_overall = _platform_sql_clause(platform, "")
    cur = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE posted_at >= NOW() - INTERVAL '" + str(args.days) + " days'"
        + plat_clause_overall
    )
    total_recent = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM posts")
    total_all = cur.fetchone()[0]
    conn.close()
    if bookings_conn:
        try: bookings_conn.close()
        except Exception: pass

    print(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "platform": platform or "all",
        "projects": out_projects,
        "overall": {"total": total_all, "recent": total_recent},
    }))


if __name__ == "__main__":
    main()
