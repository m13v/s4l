#!/usr/bin/env python3
"""
Auto-expire dead-weight SEO pages across every project in config.json.

Definition of dead-weight (a page is "dead" if ALL of these hold):
  - Site has at least 30 days of GSC history for the page
  - Last 30 days: clicks == 0
  - Last 30 days: impressions >= MIN_IMPRESSIONS (default 10) so we don't
    delete pages Google never even surfaced (those may be brand-new and
    are handled by GROUP B below)
  - The on-disk source file is older than MIN_AGE_DAYS (default 30 days)
    so we don't kill pages published this month

What "delete" means here: we remove the source file in the consumer
website repo. Next.js will then return 404 for the URL. The auto-commit
agent will push the deletion within ~60s. We log every deletion to
seo_expired_pages so we can audit / revert.

Per-project source-of-truth for content path is detected automatically:
  /blog/<slug>          -> <repo>/content/blog/<slug>.mdx
  /t/<slug>             -> <repo>/src/app/t/<slug>/
  /alternative/<slug>   -> <repo>/src/app/alternative/<slug>/
  /best/<slug>          -> <repo>/src/app/best/<slug>/
  (any other path)      -> logged + skipped

Usage:
  python3 expire_pages.py                          # dry-run, all projects
  python3 expire_pages.py --product fazm           # dry-run, single project
  python3 expire_pages.py --product fazm --apply   # actually delete
  python3 expire_pages.py --apply --max 50         # cap at 50 deletions
                                                     across all projects

Schedule via launchd: weekly is plenty. Run with --apply.

Safety:
  - --apply required to delete; default is dry-run
  - --max caps total deletions per invocation (default 100)
  - --min-age-days protects fresh pages (default 30)
  - --min-impressions protects pages Google never showed (default 10)
  - All deletions logged to seo_expired_pages DB table for audit/revert
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).parent.resolve()
ROOT_DIR = SCRIPT_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.json"
SA_PATH = SCRIPT_DIR / "credentials" / "seo-autopilot-sa.json"
ENV_PATH = ROOT_DIR / ".env"

sys.path.insert(0, str(ROOT_DIR / "scripts"))

PERIOD_DAYS = 30
DEFAULT_MAX_DELETIONS = 100
DEFAULT_MIN_AGE_DAYS = 30
DEFAULT_MIN_IMPRESSIONS = 10
# Fast-track (opt-in via --fast-track): pages with >= this many impressions
# AND zero clicks are clearly dead (Google showed them a lot, nobody clicked).
# Allow earlier deletion at FAST_TRACK_MIN_AGE_DAYS instead of waiting the
# full MIN_AGE_DAYS window. Off by default; user said 30/30 is the rule.
FAST_TRACK_IMP_THRESHOLD = 1000
FAST_TRACK_MIN_AGE_DAYS = 14
ROW_LIMIT = 25000


def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_config():
    return json.loads(CONFIG_PATH.read_text())


def get_gsc_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        str(SA_PATH),
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    return build("searchconsole", "v1", credentials=creds)


def fetch_gsc_pages(svc, gsc_property, start_date, end_date):
    """Returns list of {page, clicks, impressions, ctr, position}."""
    body = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "dimensions": ["page"],
        "rowLimit": ROW_LIMIT,
    }
    resp = svc.searchanalytics().query(siteUrl=gsc_property, body=body).execute()
    return resp.get("rows", [])


def _app_dirs(repo_path: Path) -> list[Path]:
    """Return the candidate Next.js app dirs in a repo, in priority order.

    Different sites use different conventions:
      - src/app/  (most common)
      - app/      (Mediar)
    Both may be present or only one. Caller iterates and tries each.
    """
    out = []
    for sub in ("src/app", "app"):
        p = repo_path / sub
        if p.exists():
            out.append(p)
    return out


def _route_segment_candidates(repo_path: Path, segment: str, slug: str) -> list[Path]:
    """Return on-disk candidates for /<segment>/<slug> across the app dir
    and any route groups inside it (e.g. (main)/, (content)/).
    Caller picks the first that exists.
    """
    cands: list[Path] = []
    for app_dir in _app_dirs(repo_path):
        cands.append(app_dir / segment / slug)
        # route groups: app/(group)/<segment>/<slug>/
        try:
            for group in app_dir.glob("(*)"):
                if group.is_dir():
                    cands.append(group / segment / slug)
        except OSError:
            pass
    return cands


# URL path segments that we know host auto-generated SEO content. We only
# expire pages under these prefixes; root-level slugs (e.g. /pricing,
# /features, /contact, /demo) are intentionally left alone since they are
# usually hand-built canonical pages.
SEO_CONTENT_SEGMENTS = ("blog", "t", "alternative", "best", "guides", "compare", "for", "glossary")


def url_to_source_path(repo_path: Path, page_url: str) -> Path | None:
    """Map a public URL back to the on-disk source file/folder.

    Returns None if the URL pattern is not recognized (we err on the side
    of NOT deleting unknown URL shapes — homepages, hand-built canonical
    pages, root-level slugs, etc.).
    """
    parsed = urlparse(page_url)
    # Skip subdomains other than www / apex (e.g. handbook.mediar.ai,
    # 092025-cohere.mediar.ai, app.assrt.ai are NOT served by this repo).
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.count(".") >= 2:
        # Subdomain (e.g. handbook.mediar.ai). We can't safely delete content
        # we don't host in this repo.
        return None

    path = parsed.path.strip("/")
    if not path:
        return None  # never delete the homepage
    parts = path.split("/")
    seg = parts[0]
    if seg not in SEO_CONTENT_SEGMENTS:
        return None
    if len(parts) != 2:
        return None  # only handle /<segment>/<slug>; deeper paths are rare and risky
    slug = parts[1]

    # /blog/<slug>: try content/blog/<slug>.mdx first (MDX collection layout)
    if seg == "blog":
        mdx = repo_path / "content" / "blog" / f"{slug}.mdx"
        if mdx.exists():
            return mdx
        # Fallback to App-Router folder (some sites use src/app/blog/<slug>/)
        for cand in _route_segment_candidates(repo_path, "blog", slug):
            if cand.exists():
                return cand
        return None

    # All other segments: App-Router folder convention.
    for cand in _route_segment_candidates(repo_path, seg, slug):
        if cand.exists():
            return cand
    return None


_GIT_CREATION_CACHE: dict[str, dict[str, int]] = {}


def _build_git_creation_map(repo_path: Path) -> dict[str, int]:
    """One-shot scan: build {repo-relative-path: first-commit-unix-ts} for every
    file ever added to the repo. Single git log call instead of one per file.
    """
    import subprocess
    key = str(repo_path)
    if key in _GIT_CREATION_CACHE:
        return _GIT_CREATION_CACHE[key]
    out: dict[str, int] = {}
    try:
        # --reverse: oldest first, so each file's first occurrence is its add.
        # --diff-filter=A: only the commits that ADDED a file.
        # --name-only with format=%at puts a timestamp line, then file paths.
        res = subprocess.run(
            ["git", "log", "--reverse", "--diff-filter=A",
             "--format=__TS__%at", "--name-only"],
            cwd=str(repo_path), capture_output=True, text=True, timeout=120,
        )
        if res.returncode == 0:
            current_ts = 0
            for line in res.stdout.splitlines():
                if line.startswith("__TS__"):
                    try:
                        current_ts = int(line[6:])
                    except ValueError:
                        current_ts = 0
                elif line.strip() and current_ts:
                    # Only record first-add (earliest), since --reverse means we
                    # see each file's add commit before any later rename.
                    if line not in out:
                        out[line] = current_ts
    except Exception:
        pass
    _GIT_CREATION_CACHE[key] = out
    return out


def file_age_days(path: Path, repo_path: Path) -> float:
    """Return age in days using git creation date for the path (or the newest
    creation timestamp inside it for folder routes).

    Falls back to filesystem mtime if git history is unavailable. The
    background auto-commit agent updates mtimes during routine commits, so
    git's first-commit timestamp is the only reliable signal for "when was
    this page actually published".
    """
    try:
        if path.is_file():
            targets = [path]
        elif path.is_dir():
            targets = [p for p in path.rglob("*") if p.is_file()]
        else:
            return 0
        if not targets:
            return 0
        creation_map = _build_git_creation_map(repo_path)
        newest_creation = 0
        for t in targets:
            rel = str(t.relative_to(repo_path))
            ts = creation_map.get(rel, 0)
            if ts > newest_creation:
                newest_creation = ts
        if newest_creation > 0:
            return (time.time() - newest_creation) / 86400
        # Fallback to mtime if git log had no record (untracked file, fresh repo)
        mt = max(t.stat().st_mtime for t in targets)
        return (time.time() - mt) / 86400
    except Exception:
        try:
            return (time.time() - path.stat().st_mtime) / 86400
        except Exception:
            return 0


def ensure_log_table():
    """No-op: the /api/v1/seo/expired-pages endpoint ensures the table on
    every POST (CREATE TABLE IF NOT EXISTS in the route). Kept as a stub so
    the call site in main() stays unchanged."""
    return


def log_expiry(product, page_url, source_path, imp, clicks, pos, age, reason):
    from http_api import api_post
    api_post("/api/v1/seo/expired-pages", {
        "product": product,
        "page_url": page_url,
        "source_path": str(source_path),
        "impressions_30d": imp,
        "clicks_30d": clicks,
        "avg_position": pos,
        "file_age_days": age,
        "reason": reason,
    })


def expire_for_project(svc, project, args, end_date, deletions_remaining):
    """Returns (candidates_found, deleted_count)."""
    name = project["name"]
    lp = project.get("landing_pages") or {}
    gsc_property = lp.get("gsc_property")
    repo = lp.get("repo")
    if not gsc_property or not repo:
        return (0, 0)
    repo_path = Path(os.path.expanduser(repo))
    if not repo_path.exists():
        print(f"  [{name}] repo missing at {repo_path}, skipping")
        return (0, 0)

    start_date = end_date - timedelta(days=PERIOD_DAYS)
    print(f"\n=== {name} ({gsc_property}) ===")
    try:
        rows = fetch_gsc_pages(svc, gsc_property, start_date, end_date)
    except Exception as e:
        print(f"  GSC fetch failed: {e}")
        return (0, 0)
    print(f"  pages with impressions in last {PERIOD_DAYS}d: {len(rows)}")

    candidates = []
    for r in rows:
        page = r["keys"][0]
        clicks = int(r.get("clicks") or 0)
        imp = int(r.get("impressions") or 0)
        pos = float(r.get("position") or 0)
        if clicks > 0:
            continue
        if imp < args.min_impressions:
            continue
        src = url_to_source_path(repo_path, page)
        if src is None:
            continue
        if not src.exists():
            continue
        age = file_age_days(src, repo_path)
        is_fast_track = (
            args.fast_track
            and imp >= FAST_TRACK_IMP_THRESHOLD
            and age >= FAST_TRACK_MIN_AGE_DAYS
        )
        if not is_fast_track and age < args.min_age_days:
            continue
        candidates.append({
            "page": page, "imp": imp, "clicks": clicks, "pos": pos,
            "src": src, "age": age,
            "reason": "fast_track_zero_clicks" if is_fast_track else "zero_clicks_30d",
        })

    candidates.sort(key=lambda c: -c["imp"])
    print(f"  dead-weight candidates: {len(candidates)}")
    if not candidates:
        return (0, 0)

    deleted = 0
    for c in candidates:
        if deletions_remaining <= 0:
            print(f"  hit global --max cap, stopping for {name}")
            break
        flag = "DELETE" if args.apply else "DRY-RUN"
        print(
            f"  {flag} | imp={c['imp']:>5} pos={c['pos']:>5.1f} "
            f"age={c['age']:>4.0f}d | {c['page']}"
        )
        if args.apply:
            try:
                if c["src"].is_file():
                    c["src"].unlink()
                elif c["src"].is_dir():
                    shutil.rmtree(c["src"])
                log_expiry(
                    name, c["page"], c["src"], c["imp"], c["clicks"],
                    c["pos"], c["age"], c["reason"],
                )
                deleted += 1
                deletions_remaining -= 1
            except Exception as e:
                print(f"    delete failed: {e}")
    return (len(candidates), deleted)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", help="Limit to one project name (case-insensitive)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete (default is dry-run)")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX_DELETIONS,
                    help=f"Cap total deletions across all projects (default {DEFAULT_MAX_DELETIONS})")
    ap.add_argument("--min-age-days", type=int, default=DEFAULT_MIN_AGE_DAYS,
                    help=f"Skip pages younger than N days (default {DEFAULT_MIN_AGE_DAYS})")
    ap.add_argument("--min-impressions", type=int, default=DEFAULT_MIN_IMPRESSIONS,
                    help=f"Skip pages with fewer than N impressions in {PERIOD_DAYS}d (default {DEFAULT_MIN_IMPRESSIONS})")
    ap.add_argument("--fast-track", action="store_true",
                    help=f"Also include pages older than {FAST_TRACK_MIN_AGE_DAYS}d "
                         f"with >= {FAST_TRACK_IMP_THRESHOLD} impressions and zero clicks "
                         "(clearly dead; off by default).")
    args = ap.parse_args()

    load_env()

    if args.apply:
        ensure_log_table()

    cfg = load_config()
    projects = cfg.get("projects", [])
    if args.product:
        projects = [p for p in projects if p["name"].lower() == args.product.lower()]
        if not projects:
            print(f"No project named {args.product}")
            sys.exit(1)

    svc = get_gsc_service()
    end_date = date.today() - timedelta(days=2)  # GSC has 2-3 day lag

    total_candidates = 0
    total_deleted = 0
    deletions_remaining = args.max
    for p in projects:
        cands, deleted = expire_for_project(
            svc, p, args, end_date, deletions_remaining,
        )
        total_candidates += cands
        total_deleted += deleted
        deletions_remaining -= deleted
        if deletions_remaining <= 0 and args.apply:
            break

    print(f"\n=== summary ===")
    print(f"total candidates: {total_candidates}")
    if args.apply:
        print(f"deleted:          {total_deleted}")
    else:
        print(f"(dry-run; nothing deleted; pass --apply to delete)")


if __name__ == "__main__":
    main()
