#!/usr/bin/env python3
"""Pull signup counts from a client Amplitude project, filtered by our UTM source.

Reads `projects[].amplitude` blocks from config.json. For each project that has one,
queries Amplitude's Dashboard REST API:
  - daily series of `signup_event` filtered by event property `utm_source = <our value>`
  - daily series of the same event with no filter (denominator)

Usage:
  amplitude_signups.py                          # all projects with amplitude block, last 30d, JSON
  amplitude_signups.py --project studyly        # one project
  amplitude_signups.py --days 7                 # custom window
  amplitude_signups.py --pretty                 # human-readable table

Env vars per project (resolved from `api_key_env` / `secret_key_env` on the block):
  AMPLITUDE_STUDYLY_API_KEY, AMPLITUDE_STUDYLY_SECRET_KEY, ...

Auth: HTTP Basic (API_KEY:SECRET_KEY) against amplitude.com/api/2/events/segmentation.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
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
API_BASE = "https://amplitude.com/api/2/events/segmentation"


def load_env():
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())




def fetch_signup_series(api_key, secret_key, signup_event, attribution_filter, start, end):
    """Return (filtered_series, total_series, x_values) for signup_event over [start, end]."""
    auth = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}

    def call(filters):
        e = json.dumps({"event_type": signup_event, "filters": filters})
        qs = urllib.parse.urlencode({
            "e": e,
            "start": start,
            "end": end,
            "i": "1",
            "m": "totals",
        })
        req = urllib.request.Request(f"{API_BASE}?{qs}", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    filters = [
        {
            "subprop_type": "event",
            "subprop_key": k,
            "subprop_op": "is",
            "subprop_value": v if isinstance(v, list) else [v],
        }
        for k, v in (attribution_filter or {}).items()
    ]
    filtered = call(filters)
    total = call([])

    x = filtered.get("data", {}).get("xValues", [])
    f_series = (filtered.get("data", {}).get("series") or [[0] * len(x)])[0]
    t_series = (total.get("data", {}).get("series") or [[0] * len(x)])[0]
    return f_series, t_series, x


def project_amplitude_stats(project, days):
    """Pull signup stats for a single project. Returns dict or None if no amplitude block."""
    amp = project.get("amplitude")
    if not amp:
        return None

    api_key = os.environ.get(amp.get("api_key_env", ""))
    secret_key = os.environ.get(amp.get("secret_key_env", ""))
    if not api_key or not secret_key:
        return {
            "project": project["name"],
            "error": f"missing env: {amp.get('api_key_env')} or {amp.get('secret_key_env')}",
        }

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days - 1)
    start = start_dt.strftime("%Y%m%d")
    end = end_dt.strftime("%Y%m%d")

    try:
        f_series, t_series, x = fetch_signup_series(
            api_key, secret_key,
            amp.get("signup_event", "New User Sign Up"),
            amp.get("attribution_filter") or {},
            start, end,
        )
    except urllib.error.HTTPError as e:
        return {"project": project["name"], "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"project": project["name"], "error": f"{type(e).__name__}: {e}"}

    return {
        "project": project["name"],
        "amplitude_project_id": amp.get("project_id"),
        "signup_event": amp.get("signup_event", "New User Sign Up"),
        "attribution_filter": amp.get("attribution_filter") or {},
        "days": days,
        "start": start,
        "end": end,
        "x_values": x,
        "attributed_series": f_series,
        "total_series": t_series,
        "attributed_total": sum(f_series),
        "total_total": sum(t_series),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help="Filter to specific project name")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default 30)")
    parser.add_argument("--pretty", action="store_true", help="Human-readable output")
    args = parser.parse_args()

    load_env()
    config = load_config()

    rows = []
    for proj in config.get("projects", []):
        if args.project and args.project.lower() != proj["name"].lower():
            continue
        if "amplitude" not in proj:
            continue
        stats = project_amplitude_stats(proj, args.days)
        if stats:
            rows.append(stats)

    if args.pretty:
        for r in rows:
            print(f"\n{r['project']}  (Amplitude project {r.get('amplitude_project_id', '?')})")
            if "error" in r:
                print(f"  ERROR: {r['error']}")
                continue
            filt = r["attribution_filter"]
            filt_str = ", ".join(f"{k}={v}" for k, v in filt.items()) or "(none)"
            print(f"  event: {r['signup_event']}  filter: {filt_str}  window: {r['start']}-{r['end']}")
            print(f"  attributed signups: {r['attributed_total']} / {r['total_total']} total")
            for d, a, t in zip(r["x_values"], r["attributed_series"], r["total_series"]):
                print(f"    {d}  attributed={a:>4}  total={t:>6}")
    else:
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "days": args.days,
            "projects": rows,
        }, indent=2))


if __name__ == "__main__":
    main()
