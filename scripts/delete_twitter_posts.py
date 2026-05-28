#!/usr/bin/env python3
"""Delete our Twitter/X posts (and optionally replies) for a project, in a cycle.

Hits the X API v2 `DELETE /2/tweets/:id` with OAuth 1.0a user context, sleeps to
respect the per-user rate limit (reads `x-rate-limit-*` response headers and
backs off adaptively), and on success flips the source row's status to
`deleted` so the dashboard and stats pipeline stay consistent.

You can only delete tweets posted by the authenticated account, so the script
is account-aware: each row's `our_account` handle is mapped to a keychain
credential entry. Rows whose handle has no creds are reported and skipped (they
are never silently dropped).

Source rows (per project, platform IN ('twitter','x')):
  - posts:   status='active',  tweet url = our_url
  - replies: status='replied', tweet url = our_reply_url   (only with --include-replies)

Usage:
    # Dry run: print every tweet that would be deleted, no API calls, no DB writes
    python3 scripts/delete_twitter_posts.py --project Agora --include-replies --dry-run

    # Real run for one account
    python3 scripts/delete_twitter_posts.py --project Agora --account m13v_ --include-replies

    # Cap the batch and slow it down
    python3 scripts/delete_twitter_posts.py --project Agora --limit 20 --sleep 3
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402

# handle (lowercased) -> keychain service name holding OAuth1 creds for that account.
# Creds are stored as: API_KEY=..|API_SECRET=..|ACCESS_TOKEN=..|ACCESS_SECRET=..
ACCOUNT_KEYCHAIN = {
    "m13v_": "Twitter API - social-autoposter",
}

DELETE_URL = "https://api.twitter.com/2/tweets/{id}"
STATUS_RE = re.compile(r"(?:twitter|x)\.com/[^/]+/status/(\d+)")


def keychain_get(service):
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        return None
    return dict(kv.split("=", 1) for kv in raw.split("|") if "=" in kv)


def load_creds(handle):
    """Return (ck, cs, at, asec) for a handle, or None if no creds on file."""
    service = ACCOUNT_KEYCHAIN.get(handle.lower())
    if not service:
        return None
    d = keychain_get(service)
    if not d:
        return None
    try:
        return d["API_KEY"], d["API_SECRET"], d["ACCESS_TOKEN"], d["ACCESS_SECRET"]
    except KeyError:
        return None


def oauth_header(method, url, creds, params=None):
    ck, cs, at, asec = creds
    params = params or {}
    oauth = {
        "oauth_consumer_key": ck,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": at,
        "oauth_version": "1.0",
    }
    allp = {**params, **oauth}
    param_str = "&".join(
        f"{urllib.parse.quote(k, '')}={urllib.parse.quote(str(allp[k]), '')}"
        for k in sorted(allp)
    )
    base = "&".join([method, urllib.parse.quote(url, ""), urllib.parse.quote(param_str, "")])
    key = f"{urllib.parse.quote(cs, '')}&{urllib.parse.quote(asec, '')}"
    sig = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(
        f'{urllib.parse.quote(k, "")}="{urllib.parse.quote(v, "")}"' for k, v in oauth.items()
    )


def tweet_id(url):
    m = STATUS_RE.search(url or "")
    return m.group(1) if m else None


def collect_rows(conn, project, include_replies):
    """Return list of dicts: {table, row_id, url, tweet_id, handle}."""
    rows = []
    for (rid, url, acct) in conn.execute(
        "SELECT id, our_url, our_account FROM posts "
        "WHERE project_name ILIKE %s AND platform IN ('twitter','x') AND status='active' "
        "ORDER BY id",
        [project],
    ).fetchall():
        tid = tweet_id(url)
        rows.append({"table": "posts", "row_id": rid, "url": url,
                     "tweet_id": tid, "handle": (acct or "").strip()})
    if include_replies:
        for (rid, url, acct) in conn.execute(
            "SELECT id, our_reply_url, our_account FROM replies "
            "WHERE project_name ILIKE %s AND platform IN ('twitter','x') AND status='replied' "
            "ORDER BY id",
            [project],
        ).fetchall():
            tid = tweet_id(url)
            rows.append({"table": "replies", "row_id": rid, "url": url,
                         "tweet_id": tid, "handle": (acct or "").strip()})
    return rows


def flip_deleted(conn, table, row_id):
    conn.execute(f"UPDATE {table} SET status='deleted' WHERE id=%s", [row_id])
    conn.commit()


def delete_one(creds, tid):
    """Return (outcome, http_status, headers, body). outcome in
    {'deleted','already_gone','rate_limited','auth_error','error'}."""
    url = DELETE_URL.format(id=tid)
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"Authorization": oauth_header("DELETE", url, creds)})
    try:
        resp = urllib.request.urlopen(req)
        body = resp.read().decode()
        hdrs = dict(resp.headers)
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        if data.get("data", {}).get("deleted") is True:
            return "deleted", resp.status, hdrs, body
        # 200 but deleted=false is unusual; treat as error so we don't flip wrongly.
        return "error", resp.status, hdrs, body
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        hdrs = dict(e.headers)
        if e.code == 429:
            return "rate_limited", e.code, hdrs, body
        if e.code in (401, 403):
            return "auth_error", e.code, hdrs, body
        if e.code == 404 or "No status found" in body or "Could not find" in body:
            return "already_gone", e.code, hdrs, body
        return "error", e.code, hdrs, body
    except Exception as e:  # network etc.
        return "error", 0, {}, str(e)


def rate_sleep(headers, base_sleep):
    """Sleep base_sleep; if the rate-limit window is almost exhausted, sleep
    until it resets."""
    try:
        remaining = int(headers.get("x-rate-limit-remaining", "1"))
        reset = int(headers.get("x-rate-limit-reset", "0"))
    except (TypeError, ValueError):
        remaining, reset = 1, 0
    if remaining <= 1 and reset:
        wait = max(0, reset - int(time.time())) + 2
        print(f"  [rate] window exhausted, sleeping {wait}s until reset", flush=True)
        time.sleep(wait)
    else:
        time.sleep(base_sleep)


def main():
    ap = argparse.ArgumentParser(description="Delete our Twitter/X posts for a project")
    ap.add_argument("--project", required=True, help="project_name (case-insensitive)")
    ap.add_argument("--account", default=None, help="only this handle (e.g. m13v_); default: all")
    ap.add_argument("--include-replies", action="store_true", help="also delete reply rows")
    ap.add_argument("--dry-run", action="store_true", help="print planned deletions, touch nothing")
    ap.add_argument("--limit", type=int, default=0, help="cap number of deletions (0 = no cap)")
    ap.add_argument("--sleep", type=float, default=2.0, help="base seconds between calls")
    args = ap.parse_args()

    db.load_env()
    conn = db.get_conn()
    rows = collect_rows(conn, args.project, args.include_replies)

    if args.account:
        rows = [r for r in rows if r["handle"].lower() == args.account.lower()]

    # Partition by deletability.
    deletable, no_creds, no_id = [], [], []
    creds_cache = {}
    for r in rows:
        if not r["tweet_id"]:
            no_id.append(r)
            continue
        h = r["handle"].lower()
        if h not in creds_cache:
            creds_cache[h] = load_creds(h)
        if creds_cache[h] is None:
            no_creds.append(r)
        else:
            deletable.append(r)

    print(f"Project={args.project} include_replies={args.include_replies} dry_run={args.dry_run}")
    print(f"  total rows: {len(rows)}")
    print(f"  deletable (creds on file): {len(deletable)}")
    print(f"  skipped (no creds for handle): {len(no_creds)} "
          f"-> {sorted({r['handle'] for r in no_creds})}")
    print(f"  skipped (no tweet id in url): {len(no_id)}")
    if args.limit:
        deletable = deletable[: args.limit]
        print(f"  --limit applied: will process {len(deletable)}")
    print()

    if args.dry_run:
        for r in deletable:
            print(f"  WOULD DELETE [{r['table']}#{r['row_id']}] @{r['handle']} {r['url']}")
        for r in no_creds:
            print(f"  NO-CREDS     [{r['table']}#{r['row_id']}] @{r['handle']} {r['url']}")
        conn.close()
        return

    counts = {"deleted": 0, "already_gone": 0, "auth_error": 0, "error": 0}
    for i, r in enumerate(deletable, 1):
        creds = creds_cache[r["handle"].lower()]
        while True:
            outcome, status, hdrs, body = delete_one(creds, r["tweet_id"])
            if outcome == "rate_limited":
                rate_sleep(hdrs, args.sleep)
                continue
            break
        tag = f"[{i}/{len(deletable)}] {r['table']}#{r['row_id']} @{r['handle']} tweet {r['tweet_id']}"
        if outcome in ("deleted", "already_gone"):
            flip_deleted(conn, r["table"], r["row_id"])
            counts[outcome] += 1
            print(f"  OK {outcome}: {tag}", flush=True)
        else:
            counts[outcome] = counts.get(outcome, 0) + 1
            print(f"  FAIL {outcome} (HTTP {status}): {tag} :: {body[:200]}", flush=True)
        rate_sleep(hdrs, args.sleep)

    conn.close()
    print("\nDone.")
    print(f"  deleted:      {counts['deleted']}")
    print(f"  already_gone: {counts['already_gone']}")
    print(f"  auth_error:   {counts['auth_error']}")
    print(f"  error:        {counts['error']}")
    if no_creds:
        print(f"  NOT touched (no creds): {len(no_creds)} rows on "
              f"{sorted({r['handle'] for r in no_creds})}")


if __name__ == "__main__":
    main()
