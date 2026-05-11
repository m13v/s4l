#!/usr/bin/env python3
"""
Post to Instagram via Graph API (Instagram-with-Instagram-Login flow).

Two-step publish flow per Meta docs:
  1. POST https://graph.instagram.com/v23.0/<ig-user-id>/media
       params: image_url|video_url, caption, media_type, [is_carousel_item, ...]
       returns a creation_id (a.k.a. container id)
  2. POST https://graph.instagram.com/v23.0/<ig-user-id>/media_publish
       params: creation_id
       returns the published media id

Reels:
  media_type=REELS, video_url=<public https url>, caption=...
  9:16 aspect, 3-90s, mp4 H.264 + AAC.

Single image:
  image_url=<public https url>, caption=...

Carousel (2-10 items):
  Step 1a: create child containers with is_carousel_item=true
  Step 1b: create parent container with media_type=CAROUSEL, children=<csv-of-child-ids>
  Step 2: media_publish

Usage:
  ./post_via_graph.py image  --url https://... --caption "..."
  ./post_via_graph.py reel   --url https://... --caption "..." [--cover https://...]
  ./post_via_graph.py status --container <id>     # poll publishing status
"""

from __future__ import annotations  # PEP 604 unions (str | None) for Python 3.9 launchd

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_PATH = Path.home() / ".config" / "fazm" / "ig_graph_token.json"
GRAPH_BASE = "https://graph.instagram.com/v23.0"


def load_token() -> dict:
    return json.loads(TOKEN_PATH.read_text())


def http_post(url: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read())}


def http_get(url: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(f"{url}?{qs}") as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read())}


def create_image_container(ig_user_id: str, token: str, image_url: str, caption: str) -> dict:
    return http_post(
        f"{GRAPH_BASE}/{ig_user_id}/media",
        {"image_url": image_url, "caption": caption, "access_token": token},
    )


def create_reel_container(
    ig_user_id: str, token: str, video_url: str, caption: str, cover_url: str | None = None
) -> dict:
    p = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": token,
    }
    if cover_url:
        p["cover_url"] = cover_url
    return http_post(f"{GRAPH_BASE}/{ig_user_id}/media", p)


def container_status(container_id: str, token: str) -> dict:
    return http_get(
        f"{GRAPH_BASE}/{container_id}",
        {"fields": "status_code,status,id", "access_token": token},
    )


def media_publish(ig_user_id: str, token: str, creation_id: str) -> dict:
    return http_post(
        f"{GRAPH_BASE}/{ig_user_id}/media_publish",
        {"creation_id": creation_id, "access_token": token},
    )


def wait_for_finished(container_id: str, token: str, timeout: int = 300) -> dict:
    """Reels async-process. Poll until status_code=FINISHED."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = container_status(container_id, token)
        code = (last or {}).get("status_code")
        print(f"  [{int(time.time())}] status_code={code}")
        if code in ("FINISHED", "PUBLISHED"):
            return last
        if code == "ERROR":
            return last
        time.sleep(5)
    return last


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_img = sub.add_parser("image"); p_img.add_argument("--url", required=True); p_img.add_argument("--caption", default="")
    p_reel = sub.add_parser("reel"); p_reel.add_argument("--url", required=True); p_reel.add_argument("--caption", default=""); p_reel.add_argument("--cover", default=None)
    p_st = sub.add_parser("status"); p_st.add_argument("--container", required=True)
    args = ap.parse_args()

    tok = load_token()
    token = tok["access_token"]
    ig_user_id = tok["ig_user_id"]

    if args.cmd == "image":
        print("Creating image container ...")
        c = create_image_container(ig_user_id, token, args.url, args.caption)
        print(json.dumps(c, indent=2))
        if "error" in c: sys.exit(1)
        cid = c["id"]
        print(f"Publishing creation_id={cid} ...")
        out = media_publish(ig_user_id, token, cid)
        print(json.dumps(out, indent=2))
        return
    if args.cmd == "reel":
        print("Creating reel container ...")
        c = create_reel_container(ig_user_id, token, args.url, args.caption, args.cover)
        print(json.dumps(c, indent=2))
        if "error" in c: sys.exit(1)
        cid = c["id"]
        print("Polling status until FINISHED ...")
        st = wait_for_finished(cid, token)
        print(json.dumps(st, indent=2))
        if (st or {}).get("status_code") not in ("FINISHED", "PUBLISHED"):
            sys.exit(1)
        print(f"Publishing creation_id={cid} ...")
        out = media_publish(ig_user_id, token, cid)
        print(json.dumps(out, indent=2))
        return
    if args.cmd == "status":
        print(json.dumps(container_status(args.container, token), indent=2))
        return


if __name__ == "__main__":
    main()
