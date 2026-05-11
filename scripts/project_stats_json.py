#!/usr/bin/env python3
"""JSON wrapper around project_stats.py for the dashboard /api/funnel/stats endpoint.

Emits a single JSON object on stdout: { generated_at, days, projects: [ ... ], overall }.
Keeps project_stats.py untouched (it is chflags uchg-locked).
"""

import json
import os
import re
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


def _hogql(api_key, project_id, query, timeout=60, max_attempts=4):
    """Run a HogQL query against /api/projects/{pid}/query/.

    Retries on 429 (throttled) and 5xx. Honors `Retry-After` up to
    `_RETRY_AFTER_CAP_S`; otherwise uses `_RETRY_BACKOFF_S`. Raises
    `HogqlError` on permanent failure so callers can mark rows as
    errored rather than zero.
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
        except urllib.error.URLError as e:
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

    def _top_pages_by_host(event_clause, row_cap=5000):
        q = (
            "SELECT properties.$host AS host, properties.$pathname AS path, count() AS c FROM events "
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

    pv_total = _count_by_host("event = '$pageview'")
    cta_total = _count_by_host("event = 'cta_click'")
    signup_total = _count_by_host(
        _SIGNUP_CLAUSE,
        distinct_key="coalesce(properties.email, distinct_id)",
    )
    sched_total = _count_by_host("event = 'schedule_click'")
    # Get Started = unique users who took the conversion action, not raw clicks.
    # A user iterating on the same project (multiple prompts, multiple
    # download retries, multiple install button presses in a session) is
    # still one conversion. Mirrors the signup_total dedup pattern above.
    get_started_total = _count_by_host(
        f"event IN {_GET_STARTED_EVENTS}",
        distinct_key="distinct_id",
    )
    cross_product_total = _count_by_host("event = 'cross_product_click'")

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
            "SELECT properties.$host AS host, count() AS c FROM events "
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
    if not name:
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
    if not bookings_conn or not name:
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


def _windowed_post_engagement(conn, name, days):
    """Sum engagement only for posts *created within the window*.

    project_stats.get_post_stats aggregates engagement over ALL time for the
    project, which is misleading when the window is a day or a week. Here we
    filter by posted_at so upvotes/comments/views/post_clicks match the same
    24h slice as the 'recent' post count.

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
        "WHERE p.project_name = %s AND p.posted_at >= NOW() - INTERVAL '" + str(days) + " days'",
        (name,),
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


def build_project_entry(conn, proj, days, api_key, ph_pid, bookings_conn, env, ph_results):
    name = proj["name"]
    post_stats = ps.get_post_stats(conn, name, days)
    platforms = ps.get_platform_breakdown(conn, name, days)
    eng_recent = _windowed_post_engagement(conn, name, days)
    seo_pages_recent = _seo_pages_count(conn, name, days)

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
    args = parser.parse_args()

    ps.load_env()
    env = os.environ
    config = ps.load_config()

    api_key = env.get("POSTHOG_PERSONAL_API_KEY")
    project_id = env.get("POSTHOG_PROJECT_ID", "330744")
    bookings_db_url = env.get("BOOKINGS_DATABASE_URL")

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
    ph_results = {}
    if buckets:
        pool_size = max(2, min(8, len(buckets)))
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
                conn, proj, args.days, api_key, project_id, bookings_conn, env, ph_results
            ))
        except Exception as e:
            out_projects.append({"name": name, "error": str(e)})

    cur = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE posted_at >= NOW() - INTERVAL '" + str(args.days) + " days'"
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
        "projects": out_projects,
        "overall": {"total": total_all, "recent": total_recent},
    }))


if __name__ == "__main__":
    main()
