#!/usr/bin/env python3
"""Compute true rolling-24h signup counts for every project with an Amplitude
block in config.json.

Why this exists:
  Amplitude's Segmentation API (used by amplitude_signups.py + project_stats_json.py)
  has two problems for the dashboard's "1d / last 24 hours" view:

    1. Buckets by calendar day in the project's display timezone. A signup
       that happened "20 hours ago" can fall outside the "today" bucket.
    2. Has materialization lag of several hours; the Export API also lags
       1-2 hours behind real time.

  The truly real-time source is our own server-side PostHog capture of the
  `newsletter_subscribed` event in /api/signup. It fires synchronously when
  the partner-signin call to Jungle succeeds, carrying:
     - $host:           which client site fired it (e.g. studyly.io)
     - partner_outcome: 'partner_created' | 'partner_reused' | 'fallback'

  For "how many users did we actually create in the Jungle backend in the
  last 24h?", `newsletter_subscribed` with partner_outcome IN
  ('partner_created', 'partner_reused') is the authoritative real-time count.

  We *also* pull Amplitude Export (last ~26h) as an eventually-consistent
  cross-check so the dashboard can show "X signups (Y attributed in
  Amplitude)" once attribution catches up. Export is still expensive
  (~120 MB / call for studyly), so this script runs on a slow cadence.

What this script writes:
  ~/social-autoposter/skill/cache/amplitude_24h_signups.json
    {
      "generated_at_utc": ...,
      "window_hours": 24,
      "projects": [
        {
          "name": "studyly",
          "count_24h": <int>,                # primary, from PostHog (real-time)
          "count_24h_source": "posthog_newsletter_subscribed",
          "amplitude_count_24h": <int|null>, # secondary, from Amplitude export
          "amplitude_count_source": "export_api",
          "amplitude_lag_min": <int|null>,   # how stale the amplitude side is
          "latest_posthog_match_utc": ...,
          "latest_amplitude_match_utc": ...,
          "partner_outcome_breakdown": {"partner_created": N, "partner_reused": N, "fallback": N},
          ...
        }
      ]
    }

  project_stats_json.py:_amplitude_signups reads `count_24h` for the days==1
  case so the dashboard's "1d" funnel reflects real-time Jungle signups.

Run cadence:
  - PostHog half: cheap (~1s, two HTTP calls). Fine to run every 5 min.
  - Amplitude export half: ~30s + 120 MB. Set to skip when last successful
    pull is < 30 min old (or always run with --no-export).
  - launchd: com.m13v.social-amplitude-24h.plist (StartInterval 300).

Usage:
  amplitude_24h_signups.py                       # all amplitude projects, both sources
  amplitude_24h_signups.py --project studyly     # one
  amplitude_24h_signups.py --no-export           # PostHog only (fast)
  amplitude_24h_signups.py --print               # echo result JSON to stdout
"""

import argparse
import base64
import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config
CONFIG_PATH = _canonical_config_path()
ENV_PATH = os.path.join(REPO_ROOT, ".env")
CACHE_DIR = os.path.join(REPO_ROOT, "skill", "cache")
CACHE_PATH = os.path.join(CACHE_DIR, "amplitude_24h_signups.json")

EXPORT_API = "https://amplitude.com/api/2/export"
POSTHOG_HOST = "https://us.posthog.com"
POSTHOG_PROJECT_ID = 330744  # m13v org / s4l project; same key all client sites share

WINDOW_HOURS = 24
EXPORT_PULL_HOURS = 26      # 2h buffer for clock skew + ingestion lag
EXPORT_REFRESH_MIN = 25     # skip export pull if cache is fresher than this
TIMEOUT_SEC = 300


# ---------- env / config ----------


def load_env():
    """Best-effort load .env (so launchd jobs see API keys)."""
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())




def keychain_get(name):
    """Read a generic-password keychain entry, stripping trailing newlines."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", name, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return None
        return (out.stdout or "").strip()
    except Exception:
        return None


# ---------- PostHog primary count ----------


def posthog_24h_count(proj, posthog_key, now_utc):
    """Return the real-time 24h signup count for `proj` from PostHog.

    Counts `newsletter_subscribed` events whose `$host` equals the project's
    primary domain and whose `partner_outcome` is 'partner_created' or
    'partner_reused' (i.e. we actually created or reused a Jungle user).

    Returns dict { count, partner_outcome_breakdown, latest_match_utc, error? }.
    """
    website = (proj.get("website") or "").lower()
    if "://" in website:
        website = website.split("://", 1)[1]
    website = website.rstrip("/")

    if not website:
        return {"count": None, "error": "no website in config"}
    if not posthog_key:
        return {"count": None, "error": "no PostHog key"}

    # HogQL: count unique signups per partner_outcome in last 24h for $host = website.
    # DISTINCT on (email, distinct_id) so client + server captures of the
    # same submission collapse to one (consistent with project_stats_json.py),
    # and a user that retries the form 3 times still counts as 1 signup.
    query = (
        "SELECT properties.partner_outcome AS outcome, "
        "count(DISTINCT coalesce(properties.email, distinct_id)) AS n, "
        "max(timestamp) AS latest "
        "FROM events "
        "WHERE event = 'newsletter_subscribed' "
        f"AND properties.$host = '{website}' "
        f"AND timestamp > now() - interval {WINDOW_HOURS} hour "
        "GROUP BY outcome"
    )
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode()
    req = urllib.request.Request(
        f"{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT_ID}/query/",
        headers={
            "Authorization": f"Bearer {posthog_key}",
            "Content-Type": "application/json",
        },
        data=body,
        method="POST",
    )
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as exc:
        return {"count": None, "error": f"posthog: {type(exc).__name__}: {exc}"}

    breakdown = {}
    latest = None
    real_count = 0
    for row in data.get("results") or []:
        outcome = (row[0] or "unknown")
        n = int(row[1] or 0)
        ts = row[2]
        breakdown[outcome] = n
        if outcome in ("partner_created", "partner_reused"):
            real_count += n
        if ts and (latest is None or ts > latest):
            latest = ts
    return {
        "count": real_count,
        "partner_outcome_breakdown": breakdown,
        "latest_match_utc": latest,
    }


# ---------- Amplitude eventually-consistent confirmation ----------


def fetch_amplitude_export(api_key, secret_key, start_hour, end_hour):
    auth = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    qs = urllib.parse.urlencode({"start": start_hour, "end": end_hour})
    req = urllib.request.Request(
        f"{EXPORT_API}?{qs}",
        headers={"Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return r.read()


def iter_amplitude_events(zip_bytes):
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


def parse_amplitude_event_time(ev):
    ts = ev.get("event_time") or ev.get("client_event_time")
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def amplitude_24h_count(proj, env, now_utc):
    """Return eventually-consistent 24h count from Amplitude Export.

    Lag: typically 1-2 hours behind real time. Used as a cross-check, not
    the primary number. Returns dict with count, lag, latest_match_utc.
    """
    amp = proj.get("amplitude")
    if not amp:
        return None
    api_key = env.get(amp.get("api_key_env", ""))
    secret_key = env.get(amp.get("secret_key_env", ""))
    if not api_key or not secret_key:
        return {"count": None, "error": f"missing env: {amp.get('api_key_env')} / {amp.get('secret_key_env')}"}

    signup_event = amp.get("signup_event", "New User Sign Up")
    filt = amp.get("attribution_filter") or {}
    utm_filter = filt.get("utm_source") or []
    if isinstance(utm_filter, str):
        utm_filter = [utm_filter]
    utm_set = {a.lower() for a in utm_filter}

    end_hour_dt = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start_hour_dt = end_hour_dt - timedelta(hours=EXPORT_PULL_HOURS)
    start_hour = start_hour_dt.strftime("%Y%m%dT%H")
    end_hour = end_hour_dt.strftime("%Y%m%dT%H")
    cutoff = now_utc - timedelta(hours=WINDOW_HOURS)

    t0 = time.time()
    try:
        blob = fetch_amplitude_export(api_key, secret_key, start_hour, end_hour)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()[:200]
        except Exception:
            pass
        return {"count": None, "error": f"HTTP {exc.code}: {body}"}
    except Exception as exc:
        return {"count": None, "error": f"{type(exc).__name__}: {exc}"}

    download_sec = time.time() - t0
    download_mb = len(blob) / 1e6

    count = 0
    latest_match = None
    latest_any = None  # latest signup_event of any UTM (lets us measure ingestion lag)
    for ev in iter_amplitude_events(blob):
        if ev.get("event_type") != signup_event:
            continue
        ts = parse_amplitude_event_time(ev)
        if ts and (latest_any is None or ts > latest_any):
            latest_any = ts
        if not utm_set or not ((ev.get("event_properties") or {}).get("utm_source", "").lower() in utm_set):
            continue
        if ts is None or ts < cutoff:
            continue
        count += 1
        if latest_match is None or ts > latest_match:
            latest_match = ts

    lag_min = None
    if latest_any:
        lag_min = int((now_utc - latest_any).total_seconds() / 60)

    return {
        "count": count,
        "latest_match_utc": latest_match.isoformat() if latest_match else None,
        "latest_any_signup_utc": latest_any.isoformat() if latest_any else None,
        "lag_min": lag_min,
        "pull_window_utc": [start_hour, end_hour],
        "download_mb": round(download_mb, 1),
        "elapsed_sec": round(time.time() - t0, 1),
        "download_sec": round(download_sec, 1),
    }


# ---------- combine + write ----------


def existing_export_age_min(now_utc):
    """How old (minutes) is the cached Amplitude export half?"""
    if not os.path.exists(CACHE_PATH):
        return 999_999
    try:
        with open(CACHE_PATH) as f:
            cur = json.load(f)
        # Use latest amplitude pull's recorded timestamp.
        for p in cur.get("projects") or []:
            ts = p.get("amplitude_pulled_at_utc")
            if ts:
                pulled = datetime.fromisoformat(ts)
                return int((now_utc - pulled).total_seconds() / 60)
    except Exception:
        pass
    return 999_999


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
    ap.add_argument("--no-export", action="store_true", help="Skip the Amplitude export pull (PostHog primary only).")
    ap.add_argument("--force-export", action="store_true", help="Always pull export, ignoring the refresh interval.")
    ap.add_argument("--print", action="store_true", help="Print the resulting JSON to stdout.")
    args = ap.parse_args()

    load_env()
    config = load_config()
    now_utc = datetime.now(timezone.utc)

    posthog_key = (
        os.environ.get("POSTHOG_PERSONAL_API_KEY")
        or keychain_get("PostHog-Personal-API-Key-m13v")
    )

    # Decide whether to refresh the export half this run.
    do_export = (not args.no_export) and (
        args.force_export or existing_export_age_min(now_utc) >= EXPORT_REFRESH_MIN
    )
    # Preserve previous export results on this run if we're skipping.
    prev_amplitude = {}
    if not do_export and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                cur = json.load(f)
            for p in cur.get("projects") or []:
                prev_amplitude[p["name"]] = {
                    "amplitude_count_24h": p.get("amplitude_count_24h"),
                    "amplitude_lag_min": p.get("amplitude_lag_min"),
                    "amplitude_latest_match_utc": p.get("amplitude_latest_match_utc"),
                    "amplitude_pulled_at_utc": p.get("amplitude_pulled_at_utc"),
                    "amplitude_error": p.get("amplitude_error"),
                }
        except Exception:
            prev_amplitude = {}

    out_projects = []
    for proj in config.get("projects", []):
        if args.project and args.project.lower() != proj.get("name", "").lower():
            continue
        if "amplitude" not in proj:
            continue

        name = proj["name"]
        ph = posthog_24h_count(proj, posthog_key, now_utc)
        ph_count = ph.get("count")
        ph_breakdown = ph.get("partner_outcome_breakdown") or {}
        ph_latest = ph.get("latest_match_utc")
        ph_error = ph.get("error")

        amp_count = None
        amp_latest = None
        amp_lag = None
        amp_pulled_at = None
        amp_error = None
        if do_export:
            res = amplitude_24h_count(proj, os.environ, now_utc)
            if res:
                amp_count = res.get("count")
                amp_latest = res.get("latest_match_utc")
                amp_lag = res.get("lag_min")
                amp_pulled_at = now_utc.isoformat()
                amp_error = res.get("error")
        else:
            prev = prev_amplitude.get(name) or {}
            amp_count = prev.get("amplitude_count_24h")
            amp_latest = prev.get("amplitude_latest_match_utc")
            amp_lag = prev.get("amplitude_lag_min")
            amp_pulled_at = prev.get("amplitude_pulled_at_utc")
            amp_error = prev.get("amplitude_error")

        out_projects.append({
            "name": name,
            "count_24h": ph_count,
            "count_24h_source": "posthog_newsletter_subscribed",
            "partner_outcome_breakdown": ph_breakdown,
            "latest_posthog_match_utc": ph_latest,
            "posthog_error": ph_error,
            "amplitude_count_24h": amp_count,
            "amplitude_count_source": "export_api",
            "amplitude_lag_min": amp_lag,
            "amplitude_latest_match_utc": amp_latest,
            "amplitude_pulled_at_utc": amp_pulled_at,
            "amplitude_error": amp_error,
            "attribution_filter": (proj.get("amplitude") or {}).get("attribution_filter"),
        })
        print(
            f"  {name}: posthog_count={ph_count} ({ph_breakdown}) "
            f"amplitude_count={amp_count} (lag={amp_lag} min, pulled={amp_pulled_at})",
            file=sys.stderr,
        )

    payload = {
        "generated_at_utc": now_utc.isoformat(),
        "window_hours": WINDOW_HOURS,
        "amplitude_export_refreshed": do_export,
        "projects": out_projects,
    }
    atomic_write_json(CACHE_PATH, payload)
    print(f"wrote {CACHE_PATH}", file=sys.stderr)

    if args.print:
        print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
