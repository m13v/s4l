#!/usr/bin/env python3
"""Pick the top-scoring page for a project using the composite formula:

    score = pageviews*1
          + email_signups*100
          + schedule_clicks*500
          + get_started_clicks*300
          + bookings*1000

Bookings are attributed to a path via cal_bookings.utm_campaign (the page
path captured by withBookingAttribution on the CTA click).

Writes a brief identical in shape to pick_top_page.py so the caller (or
follow-up Claude step) can read it the same way. The difference is that
this picker ranks by the full weighted formula, not pageviews alone, and
exposes the top-N list alongside the single winner.

Exits:
  0 - brief written / printed
  2 - no signal in the window (skip, same convention as pick_top_page.py)

Usage:
    python3 seo/pick_top_pages.py --product Fazm
    python3 seo/pick_top_pages.py --product Fazm --out /tmp/brief.json
    python3 seo/pick_top_pages.py --list-enabled
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.json"

ENV_PATH = ROOT_DIR / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(ROOT_DIR / "scripts"))
from project_slugs import get_client_slug as _client_slug  # noqa: E402
from http_api import api_get, api_post  # noqa: E402


WEIGHTS = {
    "pageviews":          1,
    "email_signups":      100,
    "schedule_clicks":    500,
    "get_started_clicks": 300,
    "bookings":           1000,
}

GET_STARTED_EVENTS = ("get_started_click", "download_click", "cta_get_started_clicked")


def _load_config():
    return json.loads(CONFIG_PATH.read_text())


def _find_project(cfg, product):
    for p in cfg.get("projects", []):
        if (p.get("name") or "").lower() == (product or "").lower():
            return p
    return None


def _domain_from_url(url):
    if not url:
        return ""
    return url.replace("https://", "").replace("http://", "").rstrip("/")


def _posthog_api_key():
    v = os.environ.get("POSTHOG_PERSONAL_API_KEY")
    if v:
        return v.strip()
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", "PostHog-Personal-API-Key-m13v", "-w"],
            stderr=subprocess.DEVNULL, timeout=10,
        )
        return out.decode().strip()
    except Exception:
        return ""


_THROTTLE_HINT_RE = re.compile(r"available in (\d+) seconds?", re.IGNORECASE)
_THROTTLE_MAX_WAIT_SEC = 120


def _hogql(api_key, project_id, query, timeout=60):
    """Run a HogQL query.

    Retries on 429 (PostHog throttle) and 5xx. Honors PostHog's "available in
    N seconds" hint on 429 instead of using a fixed backoff.
    """
    url = f"https://us.posthog.com/api/projects/{project_id}/query/"
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode("utf-8")
    last_err = None
    backoffs = [0.0, 2.0, 5.0, 12.0]
    for attempt, fallback_wait in enumerate(backoffs):
        if fallback_wait:
            time.sleep(fallback_wait)
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                return data.get("results") or []
        except urllib.error.HTTPError as e:
            try:
                body_txt = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                body_txt = ""
            last_err = f"HTTP {e.code}: {body_txt}"
            if e.code == 429:
                wait_sec = None
                ra = (e.headers.get("Retry-After") or "").strip() if e.headers else ""
                if ra.isdigit():
                    wait_sec = int(ra)
                if wait_sec is None:
                    m = _THROTTLE_HINT_RE.search(body_txt)
                    if m:
                        wait_sec = int(m.group(1))
                if wait_sec is None:
                    wait_sec = max(15, int(backoffs[min(attempt + 1, len(backoffs) - 1)]))
                wait_sec = min(wait_sec, _THROTTLE_MAX_WAIT_SEC)
                time.sleep(wait_sec + 2)
                continue
            if 500 <= e.code < 600:
                continue
            break
        except urllib.error.URLError as e:
            last_err = f"URLError: {e}"
    raise RuntimeError(f"HogQL failed: {last_err}")


def _event_counts_by_path(api_key, project_id, domain, event_clause, days, row_cap=2000):
    q = (
        "SELECT properties.$pathname AS path, count() AS n "
        "FROM events "
        f"WHERE {event_clause} "
        f"AND properties.$host = '{domain}' "
        f"AND timestamp >= now() - interval {int(days)} day "
        "AND properties.$pathname IS NOT NULL "
        "GROUP BY path "
        f"ORDER BY n DESC LIMIT {int(row_cap)}"
    )
    rows = _hogql(api_key, project_id, q)
    out = {}
    for r in rows:
        p = r[0] or ""
        if not p:
            continue
        out[p] = int(r[1] or 0)
    return out


def _bookings_by_path(client_slug, days):
    """Return {path: bookings} using cal_bookings.utm_campaign.

    utm_campaign is populated by withBookingAttribution with the page path
    (e.g. '/t/accessibility-api-ai-agents-vs-screenshots'). Test bookings
    are filtered with the same heuristics as pick_top_page.py.
    """
    if not client_slug:
        return {}
    try:
        resp = api_get("/api/v1/seo/bookings",
                       query={"mode": "by_path", "client": client_slug, "days": int(days)})
        return (resp.get("data") or {}).get("by_path") or {}
    except Exception as e:
        print(f"  bookings query failed: {e}", file=sys.stderr)
        return {}


def collect_metrics(api_key, project_id, domain, client_slug, days=1):
    get_started_clause = "event IN (" + ",".join(f"'{e}'" for e in GET_STARTED_EVENTS) + ")"
    pv       = _event_counts_by_path(api_key, project_id, domain, "event = '$pageview'",            days, row_cap=2000)
    signups  = _event_counts_by_path(api_key, project_id, domain, "event = 'newsletter_subscribed'", days, row_cap=500)
    sched    = _event_counts_by_path(api_key, project_id, domain, "event = 'schedule_click'",        days, row_cap=500)
    gs       = _event_counts_by_path(api_key, project_id, domain, get_started_clause,                days, row_cap=500)
    bookings = _bookings_by_path(client_slug, days)
    paths = set().union(pv, signups, sched, gs, bookings)
    out = []
    for p in paths:
        metrics = {
            "pageviews":          pv.get(p, 0),
            "email_signups":      signups.get(p, 0),
            "schedule_clicks":    sched.get(p, 0),
            "get_started_clicks": gs.get(p, 0),
            "bookings":           bookings.get(p, 0),
        }
        score = sum(metrics[k] * WEIGHTS[k] for k in metrics)
        out.append({"path": p, "score": score, "metrics": metrics})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def _created_paths_for_project(proj, days=None):
    """Reuse the dashboard's `_created_paths_for_project` helper from
    scripts/project_stats_json.py so this picker stays identical to the
    Top -> Pages subtab's "created" set.

    With `days=None`, includes the filesystem scan of the landing repo (which
    leaks the homepage and every other ambient marketing page — every Next.js
    app ships `src/app/page.tsx`). The dashboard always passes a window, which
    skips the FS scan and restricts to seo_keywords UNION gsc_queries rows
    whose `completed_at` falls inside the window. Pass the same window here
    to match."""
    try:
        if "SCRIPTS_DIR" not in _created_paths_for_project.__dict__:
            _created_paths_for_project.SCRIPTS_DIR = str(ROOT_DIR / "scripts")
            sys.path.insert(0, _created_paths_for_project.SCRIPTS_DIR)
        import project_stats_json as psj  # noqa: E402

        by_domain = {}
        # Filesystem scan (local, no DB): only when no window is requested —
        # static files on disk carry no trustworthy creation timestamp.
        if days is None:
            lp = proj.get("landing_pages") or {}
            repo_path = lp.get("repo") if isinstance(lp, dict) else None
            fs_paths = psj._scan_repo_pages(repo_path) if repo_path else set()
            for p in fs_paths:
                by_domain.setdefault("_fs", set()).add(p)
        # DB half (seo_keywords UNION gsc_queries page_urls by host) via HTTP.
        q = {"product": proj.get("name") or ""}
        if days is not None:
            q["days"] = int(days)
        resp = api_get("/api/v1/seo/created-pages", query=q)
        db_by_domain = (resp.get("data") or {}).get("by_domain") or {}
        for host, paths in db_by_domain.items():
            by_domain.setdefault(host, set()).update(paths)
        # Flatten into a single set of paths; the picker compares paths
        # only (we already scope the PostHog query to the project's
        # primary domain).
        all_paths = set()
        for paths in by_domain.values():
            all_paths.update(paths)
        return all_paths
    except Exception as e:
        print(f"  _created_paths_for_project failed: {e}", file=sys.stderr)
        return set()


def _history_for_path(product, page_path, limit=5):
    try:
        slug = page_path.lstrip("/").split("/")[-1]
        resp = api_get("/api/v1/seo/keywords",
                       query={"mode": "created_paths", "product": product,
                              "slug": slug, "page_url_like": f"%{page_path}%",
                              "limit": int(limit)})
        rows = (resp.get("data") or {}).get("rows") or []
        return [
            {"at": r.get("created_at"), "status": r.get("status"),
             "slug": r.get("slug"), "keyword": r.get("keyword"),
             "page_url": r.get("page_url")}
            for r in rows
        ]
    except Exception as e:
        print(f"  history lookup failed: {e}", file=sys.stderr)
        return []


def build_brief(product, days=1, top_n=10):
    cfg = _load_config()
    proj = _find_project(cfg, product)
    if not proj:
        raise SystemExit(f"ERROR: product '{product}' not found in config.json")

    website = proj.get("website") or ""
    domain = _domain_from_url(website)
    if not domain:
        raise SystemExit(f"ERROR: product '{product}' has no website domain")

    lp = proj.get("landing_pages") or {}
    repo_raw = lp.get("repo") or ""
    repo_abs = os.path.expanduser(repo_raw)
    if not repo_abs or not os.path.isdir(repo_abs):
        raise SystemExit(f"ERROR: repo path missing for '{product}': {repo_raw!r}")

    ph = (proj.get("posthog") or {})
    project_id = ph.get("project_id")
    if not project_id:
        raise SystemExit(f"ERROR: posthog.project_id not set for '{product}'")

    api_key = _posthog_api_key()
    if not api_key:
        raise SystemExit("ERROR: PostHog API key not available")

    client_slug = _client_slug(product)
    ranking = collect_metrics(api_key, project_id, domain, client_slug, days=days)
    if not ranking or ranking[0]["score"] <= 0:
        print(f"SKIP: no ranked activity in last {days}d for {domain}", file=sys.stderr)
        sys.exit(2)

    winner = ranking[0]
    base_url = (lp.get("base_url") or website).rstrip("/")
    page_url = base_url + (winner["path"] if winner["path"].startswith("/") else "/" + winner["path"])
    history = _history_for_path(product, winner["path"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "product": proj.get("name"),
        "domain": domain,
        "repo_path": repo_abs,
        "window_days": days,
        "weights": WEIGHTS,
        "winner": {
            "path": winner["path"],
            "page_url": page_url,
            "score": winner["score"],
            "metrics": winner["metrics"],
        },
        "ranking": ranking[:top_n],
        "history": history,
        "project_config": proj,
    }


def _enabled_products(cfg):
    out = []
    for p in cfg.get("projects", []):
        lp = p.get("landing_pages") or {}
        # Also require weight > 0 so paused projects (Clone, tenxats,
        # macOS Session Replay as of 2026-05-08) don't get top-pages SEO
        # work. Posting + Moltbook + SERP/GSC already filter weight > 0;
        # this brings top-pages into line. Same gate added to
        # pick_top_posts.py and run_improve_pipeline.sh.
        if lp.get("top_pages_enabled") and (p.get("weight") or 0) > 0:
            out.append(p.get("name"))
    return out


def _recent_winner_keys(cooldown_days=7):
    """Return set of (product, path) pairs that won within the cooldown
    window. Used to rotate seeds so the same page doesn't reseed every day.
    Failures return empty set (fail-open: better a repeat than a dark day)."""
    try:
        resp = api_get("/api/v1/seo/top-page-winners",
                       query={"mode": "recent", "cooldown_days": int(cooldown_days)})
        keys = (resp.get("data") or {}).get("keys") or []
        return {(k.get("product"), k.get("path")) for k in keys}
    except Exception as e:
        print(f"  recent winners query failed (fail-open): {e}", file=sys.stderr)
        return set()


def _record_winner(winner, cooldown_days=7):
    """Insert the picked winner into top_page_winners so future runs can
    enforce the cooldown. Best-effort: if the insert fails, log but don't
    fail the pipeline (the brief is already written)."""
    try:
        api_post("/api/v1/seo/top-page-winners", {
            "product": winner["product"],
            "path": winner["path"],
            "page_url": winner["page_url"],
            "score": winner["score"],
            "metrics": winner["metrics"],
        })
        print(f"  recorded winner: {winner['product']} {winner['path']}", file=sys.stderr)
    except Exception as e:
        print(f"  record winner failed: {e}", file=sys.stderr)


def build_global_brief(days=1, top_n=10, cooldown_days=7):
    """Cross-project mode: rank all paths across every top_pages_enabled
    project by the same weighted score, pick ONE global winner, and list
    every enabled project as a replication target. The caller then asks
    Claude (per target) to propose an adjacent keyword/slug that adapts
    the winner's concept to that project's audience.

    Rotation: any (product, path) that won within `cooldown_days` is
    skipped when picking the winner; if every ranked row is in cooldown,
    the oldest-cooldown row wins anyway (pipeline never goes dark)."""
    cfg = _load_config()
    # Same gate as _enabled_products: top_pages_enabled AND weight > 0.
    enabled = [
        p for p in cfg.get("projects", [])
        if (p.get("landing_pages") or {}).get("top_pages_enabled")
        and (p.get("weight") or 0) > 0
    ]
    if not enabled:
        raise SystemExit("ERROR: no projects have landing_pages.top_pages_enabled=true AND weight > 0")

    api_key = _posthog_api_key()
    if not api_key:
        raise SystemExit("ERROR: PostHog API key not available")

    # Filter to the same "created" set the dashboard uses for its
    # Top -> Pages subtab: filesystem scan of each landing repo UNION
    # seo_keywords UNION gsc_queries. This keeps the top-pages pipeline
    # in sync with whatever the dashboard treats as a real SEO page, and
    # includes pages committed by any pipeline (including the auto-commit
    # agent) even if they aren't in seo_keywords yet.

    all_rows = []
    targets = []
    dropped_counts = {}
    for proj in enabled:
        name = proj.get("name")
        website = proj.get("website") or ""
        domain = _domain_from_url(website)
        lp = proj.get("landing_pages") or {}
        repo_raw = lp.get("repo") or ""
        repo_abs = os.path.expanduser(repo_raw)
        base_url = (lp.get("base_url") or website).rstrip("/")
        ph = (proj.get("posthog") or {})
        project_id = ph.get("project_id")

        if not domain or not project_id or not repo_abs or not os.path.isdir(repo_abs):
            print(f"  skip {name}: missing domain/project_id/repo", file=sys.stderr)
            continue

        # Homepage protection: when landing_pages.homepage_protected is true,
        # the client owns the homepage visually and we must NEVER touch it.
        # Surface the flag on the target so the downstream replication step
        # can refuse to write to `/` even on an adjacent-slug adaptation.
        homepage_protected = bool(lp.get("homepage_protected"))

        targets.append({
            "product": name,
            "domain": domain,
            "website": website,
            "base_url": base_url,
            "repo_path": repo_abs,
            "homepage_protected": homepage_protected,
            "project_config": proj,
        })

        try:
            ranking = collect_metrics(api_key, project_id, domain, _client_slug(name), days=days)
        except Exception as e:
            print(f"  {name} metrics failed: {e}", file=sys.stderr)
            continue
        allowed = _created_paths_for_project(proj, days=days)
        kept = 0; dropped = 0; dropped_homepage = 0
        for r in ranking:
            path = r["path"]
            if path not in allowed:
                dropped += 1
                continue
            # Strip the homepage from the global winner candidate pool when
            # the source project is homepage_protected: we don't want the
            # cross-project replicator to study and clone a hand-tuned brand
            # homepage as a SEO template.
            if homepage_protected and path in ("/", "", "/index", "/index.html"):
                dropped_homepage += 1
                continue
            kept += 1
            r["product"] = name
            r["domain"] = domain
            r["page_url"] = base_url + (path if path.startswith("/") else "/" + path)
            all_rows.append(r)
        if dropped_homepage:
            print(
                f"  {name}: stripped {dropped_homepage} homepage row(s) "
                f"(homepage_protected=true)",
                file=sys.stderr,
            )
        dropped_counts[name] = (kept, dropped)

    print(
        f"  seo-filtered counts: "
        + ", ".join(f"{k}={v[0]}kept/{v[1]}dropped" for k, v in dropped_counts.items()),
        file=sys.stderr,
    )
    all_rows.sort(key=lambda r: r["score"], reverse=True)
    if not all_rows or all_rows[0]["score"] <= 0:
        print(f"SKIP: no ranked activity in last {days}d across enabled projects", file=sys.stderr)
        sys.exit(2)

    # Rotation: skip any (product, path) that won within the cooldown window.
    # Fall back to the top-scoring row if every ranked row is on cooldown so
    # the pipeline never goes dark.
    recent = _recent_winner_keys(cooldown_days=cooldown_days)
    winner = None
    skipped_on_cooldown = []
    for r in all_rows:
        key = (r["product"], r["path"])
        r["on_cooldown"] = key in recent
        if r["on_cooldown"]:
            skipped_on_cooldown.append(key)
            continue
        if winner is None:
            winner = r
    if winner is None:
        print(
            f"  all {len(all_rows)} candidates on {cooldown_days}d cooldown; "
            f"falling back to top-scoring row",
            file=sys.stderr,
        )
        winner = all_rows[0]
    if skipped_on_cooldown:
        print(
            f"  rotation skipped {len(skipped_on_cooldown)} candidate(s) on cooldown: "
            + ", ".join(f"{p}{pa}" for p, pa in skipped_on_cooldown[:5])
            + ("..." if len(skipped_on_cooldown) > 5 else ""),
            file=sys.stderr,
        )

    _record_winner(winner, cooldown_days=cooldown_days)
    history = _history_for_path(winner["product"], winner["path"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "global",
        "window_days": days,
        "cooldown_days": cooldown_days,
        "weights": WEIGHTS,
        "winner": {
            "product": winner["product"],
            "domain": winner["domain"],
            "path": winner["path"],
            "page_url": winner["page_url"],
            "score": winner["score"],
            "metrics": winner["metrics"],
            "on_cooldown": bool(winner.get("on_cooldown")),
        },
        "ranking": all_rows[:top_n],
        "skipped_on_cooldown": [
            {"product": p, "path": pa} for p, pa in skipped_on_cooldown
        ],
        "history": history,
        "targets": targets,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--out")
    ap.add_argument("--list-enabled", action="store_true")
    ap.add_argument("--global-mode", action="store_true", help="Cross-project: rank all enabled projects together, one global winner, every enabled project is a replication target.")
    ap.add_argument("--cooldown-days", type=int, default=7, help="Global mode: skip any (product, path) that won within this many days. Falls back to top-scoring row if every candidate is on cooldown.")
    args = ap.parse_args()

    if args.list_enabled:
        cfg = _load_config()
        for name in _enabled_products(cfg):
            print(name)
        return 0

    if args.global_mode:
        brief = build_global_brief(days=args.days, top_n=args.top_n, cooldown_days=args.cooldown_days)
    else:
        if not args.product:
            print("--product is required (or use --list-enabled / --global-mode)", file=sys.stderr)
            return 1
        brief = build_brief(args.product, days=args.days, top_n=args.top_n)

    blob = json.dumps(brief, indent=2, ensure_ascii=False, default=str)
    if args.out:
        Path(args.out).write_text(blob)
        print(f"wrote brief -> {args.out}")
    else:
        print(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
