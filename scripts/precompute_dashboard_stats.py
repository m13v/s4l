#!/usr/bin/env python3
"""Precompute dashboard stat snapshots to disk so the dashboard never cold-starts.

Writes atomic JSON snapshots under ~/social-autoposter/skill/cache/:
  - funnel_stats_<N>d.json  for N in {1, 7, 14, 30, 90}   (Top -> Pages + funnel)
  - activity_stats_<H>h.json for H in {24, 168, 336, 720} (Activity tab counts)
  - style_stats_<H>h.json    for H in {24, 168, 336, 720} (Style tab, all/all)

Run on a launchd timer (see com.m13v.social-precompute-stats.plist). The
/api/funnel/stats, /api/activity/stats, and /api/style/stats endpoints in
bin/server.js read these files when fresh; live queries only run on miss.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http_api import api_get, api_post, load_env

REPO_DIR = os.path.expanduser("~/social-autoposter")
CACHE_DIR = os.path.join(REPO_DIR, "skill", "cache")
SCRIPTS_DIR = os.path.join(REPO_DIR, "scripts")


def upsert_cache(key, payload):
    """Mirror a snapshot to dashboard_cache over HTTP so Cloud Run (which has no
    access to the operator's filesystem) can serve it. Tolerant: a mirror
    failure logs and continues, since local disk is still the primary path."""
    try:
        api_post(
            "/api/v1/dashboard/cache-upsert",
            {"cache_key": key, "payload": payload},
        )
    except SystemExit as e:
        print(f"  [api] cache-upsert {key} failed: {e}", file=sys.stderr)
    except Exception as e:
        print(f"  [api] cache-upsert {key} failed: {e}", file=sys.stderr)


def atomic_write_json(path, payload):
    """Write JSON to `path` atomically (temp file + rename). Also mirrors
    to Postgres dashboard_cache under the filename stem so hosted deploys can
    read the same snapshot."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise
    key = os.path.splitext(os.path.basename(path))[0]
    upsert_cache(key, payload)


def precompute_funnel(days):
    """Shell out to project_stats_json.py (it already knows how to build the
    payload and hits PostHog + bookings DB). Returns parsed JSON or None."""
    script = os.path.join(SCRIPTS_DIR, "project_stats_json.py")
    t0 = time.time()
    try:
        out = subprocess.check_output(
            ["python3", script, "--days", str(days)],
            cwd=REPO_DIR,
            env=os.environ.copy(),
            timeout=180,
        )
    except subprocess.CalledProcessError as e:
        print(f"  funnel days={days} FAILED exit={e.returncode}: {e.stderr or e.output!r}", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(f"  funnel days={days} TIMEOUT after 180s", file=sys.stderr)
        return None
    try:
        data = json.loads(out)
    except Exception as e:
        print(f"  funnel days={days} JSON decode failed: {e}", file=sys.stderr)
        return None
    # Match the wire shape /api/funnel/stats returns: { days, ...data, cachedAt }
    payload = {"days": days, **data, "cachedAt": int(time.time() * 1000)}
    path = os.path.join(CACHE_DIR, f"funnel_stats_{days}d.json")
    atomic_write_json(path, payload)
    elapsed = time.time() - t0
    print(f"  funnel days={days} ok ({elapsed:.1f}s) -> {path}")
    return payload


def precompute_activity(hours=24):
    """Mirror the 15-way activity UNION (now served by
    GET /api/v1/dashboard/activity-stats)."""
    t0 = time.time()
    resp = api_get("/api/v1/dashboard/activity-stats", query={"hours": int(hours)})
    value = (resp.get("data") or {}).get("rows") or []
    payload = {
        "windowHours": int(hours),
        "rows": value,
        "cachedAt": int(time.time() * 1000),
    }
    path = os.path.join(CACHE_DIR, f"activity_stats_{int(hours)}h.json")
    atomic_write_json(path, payload)
    elapsed = time.time() - t0
    print(f"  activity hours={hours} ok ({elapsed:.1f}s) -> {path}")
    return payload


def precompute_style(hours=24):
    """Mirror the engagement-style aggregate (now served by
    GET /api/v1/dashboard/style-stats) for the default all/all filter the
    dashboard asks for on load."""
    t0 = time.time()
    resp = api_get("/api/v1/dashboard/style-stats", query={"hours": int(hours)})
    data = resp.get("data") or {}
    payload = {
        "windowHours": int(hours),
        "platform": "all",
        "project": "all",
        "rows": data.get("rows") or [],
        "platforms": data.get("platforms") or [],
        "projects": data.get("projects") or [],
        "cachedAt": int(time.time() * 1000),
    }
    path = os.path.join(CACHE_DIR, f"style_stats_{int(hours)}h.json")
    atomic_write_json(path, payload)
    elapsed = time.time() - t0
    print(f"  style hours={hours} ok ({elapsed:.1f}s) -> {path}")
    return payload


def main():
    load_env()
    os.makedirs(CACHE_DIR, exist_ok=True)

    started = datetime.now(timezone.utc).isoformat()
    print(f"=== precompute_dashboard_stats: {started} ===")
    overall_t0 = time.time()

    # Activity + style snapshots, one per Stats-tab window pill
    # (24h / 7d / 14d / 30d = 24 / 168 / 336 / 720 hours). The dashboard's
    # readSnapshotCached gate rejects anything older than 15 min, so every
    # window must refresh every cycle or it falls through to the live query
    # (the 15-way activity UNION costs ~15s under load and blocks Node's
    # single event loop, freezing the whole dashboard). Pre-2026-05-30 only
    # 24h was precomputed, so 7d/14d/30d hit the live path on every switch.
    STATS_WINDOW_HOURS = (24, 168, 336, 720)
    for h in STATS_WINDOW_HOURS:
        try:
            precompute_activity(h)
        except Exception as e:
            print(f"  activity hours={h} FAILED: {e}", file=sys.stderr)
        try:
            precompute_style(h)
        except Exception as e:
            print(f"  style hours={h} FAILED: {e}", file=sys.stderr)

    # Funnel snapshots: one per window the dashboard pills can show.
    #
    # The job fires every 5 min, but each funnel window re-queries every
    # PostHog bucket (~10 HogQL queries each). Recomputing all 5 windows
    # every cycle = ~5x the query burst, which trips PostHog's short-window
    # rate limiter (429 "throttled") and leaves whole buckets errored ('err'
    # on the dashboard). The longer windows barely move between 5-min cycles,
    # so only 1d + 7d refresh every cycle; 14/30/90d refresh at most every
    # ~25 min (skipped while their snapshot is still fresh). This cuts the
    # steady-state PostHog query volume by ~3/5 with no meaningful staleness.
    HEAVY_WINDOW_MIN_AGE_S = 25 * 60
    # Small gap between window runs so two adjacent window subprocesses don't
    # stack their bursts back-to-back into the rate limiter.
    INTER_WINDOW_SLEEP_S = 3
    windows = (1, 7, 14, 30, 90)
    for d in windows:
        if d >= 14:
            snap_path = os.path.join(CACHE_DIR, f"funnel_stats_{d}d.json")
            try:
                age = time.time() - os.path.getmtime(snap_path)
            except OSError:
                age = None  # missing -> always compute
            if age is not None and age < HEAVY_WINDOW_MIN_AGE_S:
                print(f"  funnel days={d} skipped (snapshot {age/60:.0f}m old < 25m)")
                continue
        try:
            precompute_funnel(d)
        except Exception as e:
            print(f"  funnel days={d} FAILED: {e}", file=sys.stderr)
        time.sleep(INTER_WINDOW_SLEEP_S)

    # Stamp a marker so ops can see when the last full cycle finished.
    atomic_write_json(
        os.path.join(CACHE_DIR, "_last_run.json"),
        {"finished_at": datetime.now(timezone.utc).isoformat(),
         "elapsed_sec": round(time.time() - overall_t0, 2)},
    )
    print(f"=== done in {time.time() - overall_t0:.1f}s ===")


if __name__ == "__main__":
    main()
