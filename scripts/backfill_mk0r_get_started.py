#!/usr/bin/env python3
"""
Backfill mk0r `get_started_click` from historical `prompt_submitted` events.

Strategy:
- Pull every `prompt_submitted` event from PostHog project 362951 with its
  distinct_id, timestamp, $host, prompt prop, and original event uuid.
- Re-emit ONLY the first prompt_submitted per distinct_id as a get_started_click
  with the original timestamp so the dashboard funnel counts true conversions
  (matches the "first prompt" gate added to mk0r's sendPrompt code).
- Set `$insert_id` to a deterministic hash so re-runs are idempotent.

PostHog Capture API: https://posthog.com/docs/api/capture
"""
import json
import os
import sys
import subprocess
import urllib.request
import urllib.error
import hashlib
import time

PROJECT_ID = "362951"
PROJECT_TOKEN = "phc_rb44X6JXuZ8Hb3XSwyDi8iGaXaLfNHMzmiNW2QqVBWxn"  # mk0r public client key
CAPTURE_URL = "https://us.i.posthog.com/i/v0/e/"


def get_personal_key():
    out = subprocess.run(
        ["security", "find-generic-password", "-s", "PostHog-Personal-API-Key-m13v", "-w"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def query_first_prompts(personal_key, dry_run_only=False):
    """First prompt_submitted per distinct_id, ordered by timestamp."""
    sql = (
        "SELECT distinct_id, "
        "min(timestamp) as first_ts, "
        "argMin(uuid, timestamp) as first_uuid, "
        "argMin(properties.$host, timestamp) as host, "
        "argMin(properties.prompt, timestamp) as prompt, "
        "argMin(properties.app_count, timestamp) as app_count, "
        "argMin(properties.type, timestamp) as build_type, "
        "argMin(properties.$current_url, timestamp) as current_url, "
        "argMin(properties.$browser, timestamp) as browser, "
        "argMin(properties.$os, timestamp) as os, "
        "argMin(properties.$device_type, timestamp) as device_type "
        "FROM events WHERE event = 'prompt_submitted' "
        "GROUP BY distinct_id ORDER BY first_ts LIMIT 10000"
    )
    req = urllib.request.Request(
        f"https://us.posthog.com/api/projects/{PROJECT_ID}/query/",
        data=json.dumps({"query": {"kind": "HogQLQuery", "query": sql}}).encode(),
        headers={
            "Authorization": f"Bearer {personal_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    cols = data["columns"]
    return [dict(zip(cols, row)) for row in data["results"]]


def already_backfilled(personal_key):
    """How many backfilled get_started_click events already exist (idempotency check)."""
    sql = (
        "SELECT count() FROM events "
        "WHERE event = 'get_started_click' "
        "AND properties.backfill_source = 'prompt_submitted'"
    )
    req = urllib.request.Request(
        f"https://us.posthog.com/api/projects/{PROJECT_ID}/query/",
        data=json.dumps({"query": {"kind": "HogQLQuery", "query": sql}}).encode(),
        headers={
            "Authorization": f"Bearer {personal_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return int(data["results"][0][0])


def emit_get_started(row):
    """Send a single get_started_click event with the original timestamp."""
    # Deterministic insert_id so PostHog dedupes re-runs.
    insert_id = "backfill-gsc-" + hashlib.sha1(
        f"{row['distinct_id']}|{row['first_uuid']}".encode()
    ).hexdigest()[:24]
    payload = {
        "api_key": PROJECT_TOKEN,
        "event": "get_started_click",
        "distinct_id": row["distinct_id"],
        "timestamp": row["first_ts"],
        "properties": {
            "destination": "https://mk0r.com/app",
            "site": "mk0r",
            "section": "hero",
            "text": "submit_prompt",
            "component": "Hero",
            "type": row.get("build_type") or "chat_build",
            "prompt_preview": (row.get("prompt") or "")[:120],
            "backfill_source": "prompt_submitted",
            "backfill_original_event_uuid": row["first_uuid"],
            "backfill_run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "$host": row.get("host"),
            "$current_url": row.get("current_url"),
            "$browser": row.get("browser"),
            "$os": row.get("os"),
            "$device_type": row.get("device_type"),
            "$insert_id": insert_id,
        },
    }
    req = urllib.request.Request(
        CAPTURE_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def main():
    dry_run = "--dry-run" in sys.argv
    personal_key = get_personal_key()

    existing = already_backfilled(personal_key)
    print(f"existing backfilled get_started_click rows: {existing}")
    rows = query_first_prompts(personal_key)
    print(f"unique distinct_ids with prompt_submitted: {len(rows)}")
    if existing >= len(rows) and not dry_run:
        print("already at full coverage, nothing to do")
        return

    if dry_run:
        for r in rows[:5]:
            print("DRY", r["distinct_id"], r["first_ts"], (r.get("prompt") or "")[:60])
        print(f"... ({len(rows)} total)")
        return

    sent = 0
    for i, r in enumerate(rows, 1):
        status, body = emit_get_started(r)
        sent += 1 if status == 200 else 0
        if i % 25 == 0 or status != 200:
            print(f"[{i}/{len(rows)}] status={status} body={body[:80]}")
        # small throttle so we don't burst capture endpoint
        time.sleep(0.05)
    print(f"done. sent={sent}/{len(rows)}")


if __name__ == "__main__":
    main()
