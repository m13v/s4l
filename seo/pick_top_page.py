#!/usr/bin/env python3
"""Pick the top-trafficked page for a project over the last 24h and build
a brief the improvement pipeline can hand to Claude.

The brief bundles:
  - full product config from config.json (so Claude has voice, positioning,
    qualification, proof points, pricing, etc. without re-deriving)
  - the winning page path + absolute URL
  - per-metric counts for three windows: 24h total, 7d average/day, 30d average/day
  - bookings count from cal_bookings keyed on client_slug
  - history of prior improvements on the same page

Emits the brief as a single JSON object to stdout. Exits non-zero with a
short reason on stderr when there is nothing to improve (no pageviews in
the last 24h, or product not configured). The orchestrator uses exit code
2 as "skip, no work".

Usage:
    python3 seo/pick_top_page.py --product PieLine
    python3 seo/pick_top_page.py --product Cyrano --out /tmp/brief.json
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
from http_api import api_get  # noqa: E402


METRIC_EVENTS = {
    # PostHog event name(s) per funnel column. get_started has two legacy names
    # alongside the canonical one, mirroring project_stats_json.py.
    "pageviews":          ["$pageview"],
    "email_signups":      ["newsletter_subscribed"],
    "schedule_clicks":    ["schedule_click"],
    "get_started_clicks": ["get_started_click", "download_click", "cta_get_started_clicked"],
}

WINDOWS = (
    ("24h",  1),
    ("7d",   7),
    ("30d", 30),
)


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
    # Prefer an explicit env var so CI / launchd can override. Fall back to the
    # personal key in keychain for interactive use.
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
# Cap on a single throttle wait. PostHog's throttle window is rolling and the
# hint is usually accurate within a few seconds; if it ever returns something
# pathological we bail rather than block the whole pipeline.
_THROTTLE_MAX_WAIT_SEC = 120


def _hogql(api_key, project_id, query, timeout=60):
    """Run a HogQL query.

    Retries on 429 (PostHog throttle) and 5xx. Honors PostHog's "available in
    N seconds" hint on 429 instead of using a fixed backoff, so we don't burn
    retries inside the throttle window. Total wall-clock cap on retries is
    around 4 minutes, beyond that we propagate the error.
    """
    url = f"https://us.posthog.com/api/projects/{project_id}/query/"
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode("utf-8")
    last_err = None
    # Keep the small backoff for non-429 transient errors; throttle waits are
    # computed dynamically per-attempt below.
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
                # Honor server hint when present, otherwise fall back to
                # exponential-ish wait. Standard Retry-After header is preferred
                # if PostHog ever sends it.
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
                # Add a 2s safety margin so we don't race the bucket boundary.
                time.sleep(wait_sec + 2)
                continue
            if 500 <= e.code < 600:
                continue
            break
        except urllib.error.URLError as e:
            last_err = f"URLError: {e}"
    raise RuntimeError(f"HogQL failed: {last_err}")


def _pick_top_paths(api_key, project_id, domain, limit=10):
    """Return ranked [(path, views), ...] highest-traffic paths over last 24h."""
    q = (
        "SELECT properties.$pathname AS path, count() AS n "
        "FROM events "
        "WHERE event = '$pageview' "
        f"AND properties.$host = '{domain}' "
        "AND timestamp >= now() - interval 24 hour "
        "AND properties.$pathname IS NOT NULL "
        f"GROUP BY path ORDER BY n DESC LIMIT {int(limit)}"
    )
    rows = _hogql(api_key, project_id, q)
    out = []
    for r in rows or []:
        p = r[0] or "/"
        n = int(r[1] or 0)
        out.append((p, n))
    return out


def _recent_improved_paths(product, cooldown_days=3):
    """Paths attempted (any status) for this product within the cooldown window.

    We dedupe based on the same `seo_page_improvements` table that
    `improve_page.py` writes to, so the cooldown updates automatically after
    every run with no extra bookkeeping. We count every attempt (including
    `no_change`) so a page that flat-lined yesterday still yields the floor
    to a different page today.
    """
    try:
        resp = api_get("/api/v1/seo/page-improvements",
                       query={"mode": "recent_paths", "product": product,
                              "cooldown_days": int(cooldown_days)})
        paths = (resp.get("data") or {}).get("paths") or []
        return {p for p in paths if p}
    except Exception as e:
        print(f"  cooldown lookup failed (continuing without rotation): {e}", file=sys.stderr)
        return set()


def _metric_counts_for_path(api_key, project_id, domain, path, days):
    """Return {metric_name: total_count} over the last `days` days."""
    out = {}
    safe_domain = domain.replace("'", "")
    safe_path = path.replace("'", "")
    for metric, events in METRIC_EVENTS.items():
        if len(events) == 1:
            event_clause = f"event = '{events[0]}'"
        else:
            quoted = ",".join(f"'{e}'" for e in events)
            event_clause = f"event IN ({quoted})"
        q = (
            "SELECT count() FROM events "
            f"WHERE {event_clause} "
            f"AND properties.$host = '{safe_domain}' "
            f"AND properties.$pathname = '{safe_path}' "
            f"AND timestamp >= now() - interval {days} day"
        )
        rows = _hogql(api_key, project_id, q)
        out[metric] = int(rows[0][0]) if rows and rows[0] else 0
    return out


def _bookings_counts(client_slug, days):
    """Count real (non-test) bookings in the window. Return None on any failure
    so the brief records 'unknown' rather than a misleading 0."""
    if not client_slug:
        return None
    try:
        resp = api_get("/api/v1/seo/bookings",
                       query={"mode": "counts", "client": client_slug, "days": int(days)})
        return int((resp.get("data") or {}).get("count") or 0)
    except Exception as e:
        print(f"  bookings query failed: {e}", file=sys.stderr)
        return None


def _history_for_page(product, page_path, limit=5):
    try:
        resp = api_get("/api/v1/seo/page-improvements",
                       query={"mode": "history", "product": product,
                              "page_path": page_path, "limit": int(limit)})
        return (resp.get("data") or {}).get("rows") or []
    except Exception as e:
        print(f"  history lookup failed: {e}", file=sys.stderr)
        return []


def _build_metrics(api_key, project_id, domain, path, client_slug):
    # 24h = totals, 7d/30d are expressed as per-day averages so the magnitudes
    # on the dashboard are comparable across windows.
    metrics = {}
    for label, days in WINDOWS:
        raw = _metric_counts_for_path(api_key, project_id, domain, path, days)
        bookings = _bookings_counts(client_slug, days)
        if label == "24h":
            metrics["24h"] = {**raw, "bookings": bookings, "window_days": 1}
        else:
            per_day = {k: round(v / days, 3) for k, v in raw.items()}
            per_day["bookings"] = round(bookings / days, 3) if bookings is not None else None
            metrics[f"{label}_avg_per_day"] = {
                **per_day,
                "window_days": days,
                "totals": {**raw, "bookings": bookings},
            }
    return metrics


def build_brief(product, cooldown_days=3, candidate_limit=10):
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
        raise SystemExit("ERROR: PostHog API key not available (set POSTHOG_PERSONAL_API_KEY or keychain)")

    candidates = _pick_top_paths(api_key, project_id, domain, limit=candidate_limit)
    if not candidates:
        print(f"SKIP: no pageviews in last 24h for {domain}", file=sys.stderr)
        sys.exit(2)

    # Homepage protection: when landing_pages.homepage_protected is true on a
    # project, the client owns the brand site's homepage visually and we must
    # never edit it. Cyrano 2026-05-11 was the first case: cyrano.systems is
    # Soorya's brand domain and the homepage is a hand-tuned static design we
    # ported into the Next.js shell only to wire integrations, not to mutate.
    # Strip the homepage row out of the candidate list BEFORE cooldown / pick.
    homepage_protected = bool(lp.get("homepage_protected"))
    if homepage_protected:
        before = len(candidates)
        candidates = [
            (p, n) for (p, n) in candidates
            if p not in ("/", "", "/index", "/index.html")
        ]
        if not candidates:
            print(
                f"SKIP: homepage_protected=true on {product}; "
                f"all {before} top candidates were the homepage",
                file=sys.stderr,
            )
            sys.exit(2)

    # Rotation: skip pages we've already touched in the cooldown window.
    # Fall through to the next candidate. If everything is on cooldown,
    # take the top page anyway so the pipeline never goes dark.
    on_cooldown = _recent_improved_paths(product, cooldown_days=cooldown_days)
    skipped = []
    chosen = None
    for p, n in candidates:
        if p in on_cooldown:
            skipped.append({"path": p, "views_24h": n, "reason": f"improved within last {cooldown_days}d"})
            continue
        chosen = (p, n)
        break

    if chosen is None:
        chosen = candidates[0]
        selection_reason = (
            f"all top {len(candidates)} pages on {cooldown_days}d cooldown; "
            f"falling back to highest-traffic page"
        )
        if homepage_protected:
            selection_reason += " (homepage filtered: homepage_protected=true)"
    elif skipped:
        selection_reason = (
            f"rotation: skipped {len(skipped)} page(s) on {cooldown_days}d cooldown, "
            f"picked next-highest-traffic page"
        )
        if homepage_protected:
            selection_reason += " (homepage filtered: homepage_protected=true)"
    else:
        selection_reason = "highest-traffic page in last 24h"
        if homepage_protected:
            selection_reason += " (homepage filtered: homepage_protected=true)"

    path, views_24h = chosen
    if views_24h <= 0:
        print(f"SKIP: chosen page {path} has 0 views in last 24h for {domain}", file=sys.stderr)
        sys.exit(2)

    client_slug = _client_slug(product)
    metrics = _build_metrics(api_key, project_id, domain, path, client_slug)

    base_url = (lp.get("base_url") or website).rstrip("/")
    page_url = base_url + (path if path.startswith("/") else "/" + path)

    history = _history_for_page(product, path)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "product": proj.get("name"),
        "domain": domain,
        "repo_path": repo_abs,
        "page_path": path,
        "page_url": page_url,
        "posthog_project_id": project_id,
        "client_slug": client_slug,
        "metrics": metrics,
        "history": history,
        "project_config": proj,
        "selection": {
            "reason": selection_reason,
            "cooldown_days": cooldown_days,
            "candidate_count": len(candidates),
            "skipped_on_cooldown": skipped,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", required=True)
    ap.add_argument("--out", help="Write brief to this path instead of stdout")
    ap.add_argument("--cooldown-days", type=int, default=3,
                    help="Skip pages improved within this many days (default 3)")
    ap.add_argument("--candidate-limit", type=int, default=10,
                    help="Pull this many top-traffic candidates before applying cooldown (default 10)")
    args = ap.parse_args()

    brief = build_brief(
        args.product,
        cooldown_days=args.cooldown_days,
        candidate_limit=args.candidate_limit,
    )
    blob = json.dumps(brief, indent=2, ensure_ascii=False, default=str)
    if args.out:
        Path(args.out).write_text(blob)
        print(args.out)
    else:
        sys.stdout.write(blob)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
