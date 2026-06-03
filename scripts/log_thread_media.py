#!/usr/bin/env python3
"""Persist captured thread media on a twitter_candidates row.

Deterministic, model-free companion to the main posting cycle (2026-06-03
thread-media feature). The cycle pre-fetches the media of every candidate it is
about to draft against (twitter_browser.py thread-media-batch), then calls this
script once per candidate to persist the media into
twitter_candidates.thread_media so the reply-writer prompt can "see" the
image / video / GIF / link-card it is replying to, and the record survives
independent of the model.

Media shape: a JSON array of {url, alt, type} objects, type in
image|video|gif|card. An empty array [] is valid and meaningful ("captured,
none found", distinct from NULL = "never captured").

Usage:
    # Pass media JSON inline:
    python3 scripts/log_thread_media.py --candidate-id 12345 \\
        --media '[{"url":"https://pbs.twimg.com/...","alt":"Image","type":"image"}]'

    # Or read the media JSON array from a file (handy for batch wiring):
    python3 scripts/log_thread_media.py --candidate-id 12345 --media-file /tmp/m.json

Output (JSON):
    {"logged": true, "candidate_id": 12345, "media_count": 1}
    {"error": "CANDIDATE_NOT_FOUND", ...}
    {"error": "BAD_MEDIA_JSON", ...}
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_patch


def _load_media(args):
    """Return a parsed media list (or raise ValueError) from --media/--media-file."""
    raw = None
    if args.media_file:
        with open(args.media_file) as f:
            raw = f.read()
    elif args.media is not None:
        raw = args.media
    else:
        raise ValueError("one of --media or --media-file is required")
    raw = (raw or "").strip()
    if raw == "":
        # Treat an empty arg as "captured, none found" -> [].
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("media must be a JSON array")
    return parsed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candidate-id", type=int, required=True)
    p.add_argument(
        "--media", default=None,
        help='JSON array of {url,alt,type}. Empty/"" means captured-none ([]).',
    )
    p.add_argument(
        "--media-file", default=None,
        help="Path to a file containing the media JSON array (alternative to --media).",
    )
    args = p.parse_args()

    try:
        media = _load_media(args)
    except Exception as e:
        print(json.dumps({"error": "BAD_MEDIA_JSON", "detail": str(e)}))
        sys.exit(1)

    payload = {
        "id": args.candidate_id,
        "action": "set_media",
        "thread_media": media,
    }

    resp = api_patch(
        "/api/v1/twitter-candidates/by-id", payload,
        ok_on_conflict=True, ok_on_404=True,
    )

    if (resp or {}).get("_not_found"):
        print(json.dumps({"error": "CANDIDATE_NOT_FOUND", "candidate_id": args.candidate_id}))
        sys.exit(1)
    if not (resp or {}).get("ok"):
        print(json.dumps({
            "error": "SET_MEDIA_FAILED",
            "candidate_id": args.candidate_id,
            "detail": (resp or {}).get("error"),
        }))
        sys.exit(1)

    print(json.dumps({
        "logged": True,
        "candidate_id": args.candidate_id,
        "media_count": len(media),
    }))


if __name__ == "__main__":
    main()
