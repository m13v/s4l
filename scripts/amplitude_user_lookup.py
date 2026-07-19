#!/usr/bin/env python3
"""Look up a user in a client Amplitude project by email or user_id.

Amplitude's `/api/2/usersearch` endpoint matches against `amplitude_id`,
`device_id`, and `user_id`, but NOT against the `email` user property. Many
products (e.g. jungleai) don't set `user_id` on the `New User Sign Up` event
itself, so usersearch by email returns nomatch even when the user exists.

This script downloads raw events via `/api/2/export` for a given window and
filters locally by `user_properties.email` (case-insensitive) or `user_id`.

Usage:
  amplitude_user_lookup.py --project studyly --email someone@gmail.com --days 1
  amplitude_user_lookup.py --project studyly --user-id abc123 --start 20260501T13 --end 20260501T15
  amplitude_user_lookup.py --project studyly --email someone@gmail.com --json

Auth: HTTP Basic (API_KEY:SECRET_KEY) against amplitude.com/api/2/export.
"""

import argparse
import base64
import gzip
import io
import json
import os
import sys
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
EXPORT_API = "https://amplitude.com/api/2/export"


def load_env():
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())




def resolve_creds(project_block):
    amp = project_block.get("amplitude") or {}
    api_env = amp.get("api_key_env")
    sec_env = amp.get("secret_key_env")
    api_key = os.environ.get(api_env) if api_env else None
    secret_key = os.environ.get(sec_env) if sec_env else None
    if not api_key or not secret_key:
        sys.exit(
            f"missing Amplitude creds for project '{project_block.get('name')}': "
            f"set {api_env} and {sec_env} in env or .env"
        )
    return api_key, secret_key


def parse_window(args):
    """Return (start_str, end_str) in YYYYMMDDTHH format that Amplitude expects."""
    fmt = "%Y%m%dT%H"
    if args.start and args.end:
        return args.start, args.end
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    return start.strftime(fmt), end.strftime(fmt)


def fetch_export(api_key, secret_key, start, end):
    auth = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    qs = urllib.parse.urlencode({"start": start, "end": end})
    req = urllib.request.Request(
        f"{EXPORT_API}?{qs}",
        headers={"Authorization": f"Basic {auth}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        sys.exit(f"export api {e.code}: {body[:400]}")


def iter_events(zip_bytes):
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


def matches(event, email, user_id):
    if user_id and event.get("user_id") == user_id:
        return True
    if email:
        ev_uid = (event.get("user_id") or "").lower()
        if ev_uid == email:
            return True
        up = event.get("user_properties") or {}
        for key in ("email", "$email", "Email"):
            v = up.get(key)
            if isinstance(v, str) and v.lower() == email:
                return True
    return False


def summarize(event):
    up = event.get("user_properties") or {}
    ep = event.get("event_properties") or {}
    return {
        "time": event.get("event_time"),
        "event_type": event.get("event_type"),
        "user_id": event.get("user_id"),
        "amplitude_id": event.get("amplitude_id"),
        "device_id": event.get("device_id"),
        "email": up.get("email") or up.get("$email"),
        "utm_source": ep.get("utm_source"),
        "utm_medium": ep.get("utm_medium"),
        "utm_campaign": ep.get("utm_campaign"),
        "utm_content": ep.get("utm_content"),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True, help="config.json projects[].name")
    p.add_argument("--email", help="user-property email to search for (case-insensitive)")
    p.add_argument("--user-id", dest="user_id", help="exact user_id to search for")
    p.add_argument("--days", type=int, default=1, help="window size in days back from now (default 1)")
    p.add_argument("--start", help="explicit YYYYMMDDTHH start (UTC), overrides --days")
    p.add_argument("--end", help="explicit YYYYMMDDTHH end (UTC), overrides --days")
    p.add_argument("--json", action="store_true", help="emit JSON instead of human-readable table")
    args = p.parse_args()

    if not args.email and not args.user_id:
        sys.exit("--email or --user-id required")

    email = args.email.lower() if args.email else None

    load_env()
    cfg = load_config()
    project = next(
        (proj for proj in cfg.get("projects", []) if proj.get("name") == args.project),
        None,
    )
    if not project:
        sys.exit(f"project '{args.project}' not found in config.json")
    if not project.get("amplitude"):
        sys.exit(f"project '{args.project}' has no `amplitude` block")

    api_key, secret_key = resolve_creds(project)
    start, end = parse_window(args)

    print(f"# project={args.project}  window={start}..{end}", file=sys.stderr)
    zip_bytes = fetch_export(api_key, secret_key, start, end)

    raw = list(iter_events(zip_bytes))

    direct = [ev for ev in raw if matches(ev, email, args.user_id)]
    amp_ids = {ev.get("amplitude_id") for ev in direct if ev.get("amplitude_id") is not None}

    seen_keys = set()
    hits = []
    for ev in raw:
        if matches(ev, email, args.user_id) or (ev.get("amplitude_id") in amp_ids):
            key = (ev.get("event_time"), ev.get("event_type"), ev.get("amplitude_id"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            hits.append(summarize(ev))

    hits.sort(key=lambda x: x.get("time") or "")

    if args.json:
        print(json.dumps(hits, indent=2))
        return

    if not hits:
        print(f"no events found for {'email='+email if email else 'user_id='+args.user_id} in window")
        return

    seen_ids = sorted(
        {(h.get("user_id"), h.get("amplitude_id")) for h in hits},
        key=lambda t: (t[0] or "", t[1] or 0),
    )
    print(f"found {len(hits)} events across {len(seen_ids)} identity (user_id, amplitude_id) pair(s)")
    for uid, amp in seen_ids:
        print(f"  user_id={uid}  amplitude_id={amp}")
    print()
    print(f"{'time':<27} {'event_type':<46} user_id  utm")
    for h in hits:
        utm = []
        for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
            if h.get(k):
                utm.append(f"{k.split('_',1)[1]}={h[k]}")
        utm_str = ",".join(utm) if utm else ""
        uid_str = (h.get("user_id") or "")[:24]
        print(f"{(h.get('time') or '')[:26]:<27} {(h.get('event_type') or '')[:45]:<46} {uid_str:<24} {utm_str}")


if __name__ == "__main__":
    main()
