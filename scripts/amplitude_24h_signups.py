#!/usr/bin/env python3
"""Compute true rolling-24h attributed signup counts for every project with an
Amplitude block in config.json, using the raw Export API.

Why this exists:
  Amplitude's Segmentation API (used by amplitude_signups.py + project_stats_json.py)
  has two problems for the dashboard's "1d / last 24 hours" view:

    1. Buckets by calendar day in the project's display timezone. A signup that
       happened "20 hours ago" can fall outside the "today" bucket.
    2. Has materialization lag of several hours. Even when the bucket is right,
       segmentation often reports 0 for events that already exist in raw export.

  The Export API (zip of gzipped JSON line files, hourly partitioning) is
  current within ~1 hour and gives us per-event timestamps so we can apply a
  precise rolling window.

What this script does:
  - For every project in config.json that has an `amplitude` block:
      * Pull export for the last 26 hours UTC (1h buffer for clock skew).
      * Iterate every event. Count those where:
          event_type == amplitude.signup_event
          event_properties.utm_source matches amplitude.attribution_filter
          event_time >= now - 24h
      * Surface the latest event time so the dashboard can show data freshness.
  - Write a single small JSON snapshot to:
      ~/social-autoposter/skill/cache/amplitude_24h_signups.json

  project_stats_json.py:_amplitude_signups reads this file when days==1.

Run cadence:
  - Every 30 minutes via com.m13v.social-amplitude-24h.plist (launchd).
  - Manual: /opt/homebrew/bin/python3.11 scripts/amplitude_24h_signups.py

Cost:
  - One Export API call per project with `amplitude` block (currently: 1 = studyly).
  - ~120 MB / call for studyly's volume; ~10-30s per call.
"""

import argparse
import base64
import gzip
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
ENV_PATH = os.path.join(REPO_ROOT, ".env")
CACHE_DIR = os.path.join(REPO_ROOT, "skill", "cache")
CACHE_PATH = os.path.join(CACHE_DIR, "amplitude_24h_signups.json")
EXPORT_API = "https://amplitude.com/api/2/export"

WINDOW_HOURS = 24
PULL_HOURS = 26  # 2h buffer for clock skew + Amplitude ingestion lag
TIMEOUT_SEC = 300


def load_env():
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def fetch_export(api_key, secret_key, start_hour, end_hour):
    """Pull Amplitude export zip for the given UTC hour range (YYYYMMDDTHH)."""
    auth = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    qs = urllib.parse.urlencode({"start": start_hour, "end": end_hour})
    req = urllib.request.Request(
        f"{EXPORT_API}?{qs}",
        headers={"Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return r.read()


def iter_events(zip_bytes):
    """Yield each event dict from a downloaded export zip."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.endswith(".json.gz"):
                continue
            with z.open(name) as f:
                raw = gzip.decompress(f.read())
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue


def parse_event_time(ev):
    """Best-effort parse of an Amplitude export event's UTC timestamp.

    Export rows include `event_time` as 'YYYY-MM-DD HH:MM:SS.ffffff' in UTC.
    Some exports use slightly different formats; fall back to None if we can't
    parse it (the count will simply skip that row).
    """
    ts = ev.get("event_time") or ev.get("client_event_time")
    if not ts:
        return None
    # Try common formats Amplitude uses in exports.
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def utm_source_matches(ev, allowed):
    """Check event_properties.utm_source against an allowed list (case-insensitive)."""
    ep = ev.get("event_properties") or {}
    src = (ep.get("utm_source") or "").strip().lower()
    if not src:
        return False
    return src in {a.lower() for a in allowed}


def project_24h_count(proj, env, now_utc):
    """Compute the rolling-24h signup count for a single project. Returns dict
    with at minimum {name, count_24h, error?}. Non-fatal: any failure surfaces
    as an `error` field and count_24h=None so the dashboard can fall back."""
    name = proj["name"]
    amp = proj.get("amplitude")
    if not amp:
        return None

    api_key = env.get(amp.get("api_key_env", ""))
    secret_key = env.get(amp.get("secret_key_env", ""))
    if not api_key or not secret_key:
        return {"name": name, "count_24h": None, "error": f"missing env: {amp.get('api_key_env')} or {amp.get('secret_key_env')}"}

    signup_event = amp.get("signup_event", "New User Sign Up")
    filt = amp.get("attribution_filter") or {}
    utm_filter = filt.get("utm_source") or []
    if isinstance(utm_filter, str):
        utm_filter = [utm_filter]

    end_hour_dt = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start_hour_dt = end_hour_dt - timedelta(hours=PULL_HOURS)
    start_hour = start_hour_dt.strftime("%Y%m%dT%H")
    end_hour = end_hour_dt.strftime("%Y%m%dT%H")

    cutoff = now_utc - timedelta(hours=WINDOW_HOURS)
    t0 = time.time()
    try:
        blob = fetch_export(api_key, secret_key, start_hour, end_hour)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()[:200]
        except Exception:
            pass
        return {"name": name, "count_24h": None, "error": f"HTTP {exc.code}: {body}"}
    except Exception as exc:
        return {"name": name, "count_24h": None, "error": f"{type(exc).__name__}: {exc}"}

    download_sec = time.time() - t0
    download_mb = len(blob) / 1e6

    count = 0
    total_signup_events = 0
    latest_match_ts = None

    for ev in iter_events(blob):
        if ev.get("event_type") != signup_event:
            continue
        total_signup_events += 1
        if utm_filter and not utm_source_matches(ev, utm_filter):
            continue
        ts = parse_event_time(ev)
        if ts is None or ts < cutoff:
            continue
        count += 1
        if latest_match_ts is None or ts > latest_match_ts:
            latest_match_ts = ts

    elapsed = time.time() - t0
    return {
        "name": name,
        "count_24h": count,
        "signup_event": signup_event,
        "attribution_filter": filt,
        "window_hours": WINDOW_HOURS,
        "pull_window_utc": [start_hour, end_hour],
        "as_of_utc": now_utc.isoformat(),
        "latest_match_utc": latest_match_ts.isoformat() if latest_match_ts else None,
        "total_signup_events_in_pull": total_signup_events,
        "download_mb": round(download_mb, 1),
        "elapsed_sec": round(elapsed, 1),
        "download_sec": round(download_sec, 1),
    }


def atomic_write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="Limit to one project (name from config.json).")
    ap.add_argument("--print", action="store_true", help="Print the resulting JSON to stdout in addition to writing the cache.")
    args = ap.parse_args()

    load_env()
    config = load_config()
    now_utc = datetime.now(timezone.utc)

    projects = []
    for proj in config.get("projects", []):
        if args.project and args.project.lower() != proj.get("name", "").lower():
            continue
        if "amplitude" not in proj:
            continue
        result = project_24h_count(proj, os.environ, now_utc)
        if result is not None:
            projects.append(result)
            tag = f"{result['count_24h']}" if result.get("count_24h") is not None else f"ERROR ({result.get('error')})"
            print(f"  {result['name']}: count_24h={tag} latest={result.get('latest_match_utc')} ({result.get('elapsed_sec','?')}s, {result.get('download_mb','?')} MB)", file=sys.stderr)

    payload = {
        "generated_at_utc": now_utc.isoformat(),
        "window_hours": WINDOW_HOURS,
        "projects": projects,
    }
    atomic_write_json(CACHE_PATH, payload)
    print(f"wrote {CACHE_PATH}", file=sys.stderr)

    if args.print:
        print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
