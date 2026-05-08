#!/usr/bin/env python3
"""One-time cleanup of duplicate matthew-autoposter comments under
moltbook post ce609188 / parent comment e7563b39, caused by 2026-05-07
s4l rate-limit storm + engage_reddit.py subprocess.run fall-through bug.

Keep landing comment a5e1f58d-... (recorded as our_reply_url for reply 16060).
Delete all other matthew-autoposter direct children of e7563b39.

Tolerates 500/timeout (Moltbook backend is fragile under nested-tree load).
Pacing 2s between deletes; re-fetches every batch.
"""
import os, sys, json, time, urllib.request, urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ""))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from moltbook_tools import fetch_moltbook_json

REPO_DIR = os.path.expanduser("~/social-autoposter")
ENV_PATH = os.path.join(REPO_DIR, ".env")

def get_key():
    with open(ENV_PATH) as f:
        for line in f:
            if line.startswith("MOLTBOOK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("MOLTBOOK_API_KEY not in .env")

KEY = get_key()
BASE = "https://www.moltbook.com/api/v1"
POST = "ce609188-af69-42cc-be20-9da640cb1a79"
PARENT = "e7563b39-6a47-40a9-8d25-6e1fe2474b3c"
KEEP = "a5e1f58d-f7eb-45da-8f34-4103e34c8743"

PACE_SECONDS = 2.0
FETCH_RETRY = 5
DELETE_RETRY = 2  # per individual comment
ROUND_LIMIT = 30  # max passes before giving up

def walk(comments, out, seen):
    for c in comments:
        cid = c.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(c)
        for r in c.get("replies", []) or []:
            walk([r], out, seen)


def get_alive_dupes():
    for attempt in range(FETCH_RETRY):
        try:
            data = fetch_moltbook_json(f"{BASE}/posts/{POST}/comments?limit=500", api_key=KEY)
            if not data:
                time.sleep(10)
                continue
            collected, seen = [], set()
            walk(data.get("comments", []), collected, seen)
            return [c for c in collected
                    if (c.get("author") or {}).get("name") == "matthew-autoposter"
                    and c.get("parent_id") == PARENT
                    and not c.get("is_deleted")
                    and c.get("id") != KEEP]
        except Exception as e:
            print(f"  fetch error attempt {attempt+1}: {e}", flush=True)
            time.sleep(15)
    return None


def delete_comment(cid):
    """Returns (status_code, body_str). status==200 or 404/410 means gone."""
    last = (0, "")
    for attempt in range(DELETE_RETRY + 1):
        req = urllib.request.Request(
            f"{BASE}/comments/{cid}",
            method="DELETE",
            headers={"Authorization": f"Bearer {KEY}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            body = (e.read().decode("utf-8", errors="replace")[:200]
                    if hasattr(e, "read") else "")
            last = (e.code, body)
            if e.code in (404, 410):
                return e.code, body  # already gone
            if e.code == 429:
                time.sleep(60)
                continue
            if e.code in (500, 502, 503, 504) and attempt < DELETE_RETRY:
                time.sleep(8 + attempt * 4)
                continue
            return e.code, body
        except Exception as e:
            last = (0, str(e)[:200])
            if attempt < DELETE_RETRY:
                time.sleep(5)
                continue
            return 0, str(e)[:200]
    return last


total_deleted = 0
total_500 = 0
total_other = 0
round_n = 0
while True:
    round_n += 1
    if round_n > ROUND_LIMIT:
        print(f"Hit ROUND_LIMIT={ROUND_LIMIT}, stopping", flush=True)
        break
    dupes = get_alive_dupes()
    if dupes is None:
        print("Could not fetch dupes after retries; aborting", flush=True)
        break
    print(f"\n=== Round {round_n}: {len(dupes)} alive dupes ===", flush=True)
    if not dupes:
        print("All dupes cleared.", flush=True)
        break

    # Sort newest first so visible-noise drops fastest
    dupes.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    progressed = 0
    for i, c in enumerate(dupes):
        cid = c["id"]
        code, body = delete_comment(cid)
        if code in (200, 404, 410):
            total_deleted += 1
            progressed += 1
        elif code in (500, 502, 503, 504):
            total_500 += 1
        else:
            total_other += 1
            print(f"  [{i}] {cid[:8]} unexpected {code}: {body[:120]}", flush=True)
        if (i + 1) % 25 == 0:
            print(f"  progress {i+1}/{len(dupes)}: deleted={total_deleted} 500={total_500} other={total_other}", flush=True)
        time.sleep(PACE_SECONDS)

    print(f"Round {round_n} done: progressed={progressed}, total deleted so far={total_deleted}", flush=True)
    if progressed == 0:
        # No deletes succeeded this round; backend stuck. Wait longer.
        print("  No progress this round; sleeping 60s before retry", flush=True)
        time.sleep(60)

print(f"\nFINAL: deleted={total_deleted}, 500={total_500}, other={total_other}", flush=True)
