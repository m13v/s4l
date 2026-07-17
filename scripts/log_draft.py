#!/usr/bin/env python3
"""Persist a Phase 2b draft on a twitter_candidates row.

Called by Claude inside Phase 2b BEFORE the twitter_browser.py post attempt,
so a CDP / browser / monthly-cap failure doesn't waste the LLM redraft on the
next cycle. The next cycle's Phase 2b sees draft_reply_text on the salvaged
row and posts it as-is when fresh (DRAFT_TTL).

Usage:
    python3 scripts/log_draft.py \\
        --candidate-id 12345 \\
        --text "your reply text here" \\
        --style curious_probe \\
        [--assigned-style curious_probe --assigned-mode use] \\
        [--new-style '{"description":"...","example":"...","why_existing_didnt_fit":"..."}'] \\
        [--platform twitter]

Engagement-style fields (2026-05-22 cutover, closes the Twitter enforcement gap):
    --assigned-style / --assigned-mode
        Picker output from s4l_pick_style. Persisted to
        twitter_candidates.assigned_style + .assigned_mode so
        twitter_post_plan.py can call validate_or_register with the
        original assignment and coerce USE-mode drift back, or accept the
        INVENT-mode invention and POST it to /api/v1/engagement-styles/registry.
        Both flags are optional for backward compatibility; legacy callers
        that don't pass them get NULL columns and the post path falls back
        to legacy (uncoerced) behaviour.
    --new-style
        JSON object literal with at minimum {description, example,
        why_existing_didnt_fit}, optionally {note}. Persisted to
        twitter_candidates.draft_new_style JSONB. Only meaningful in
        INVENT mode; when present, twitter_post_plan.py bundles it into
        the validate_or_register decision so the registry endpoint upserts
        the new style row exactly like Reddit/GitHub/Moltbook do.

Output (JSON):
    {"logged": true, "candidate_id": 12345, "drafted_at": "..."}
    {"error": "CANDIDATE_NOT_FOUND", ...}
    {"error": "ALREADY_POSTED", ...}    # candidate has status != 'pending'
    {"error": "BAD_NEW_STYLE_JSON", ...}  # --new-style was not parseable
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_patch


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candidate-id", type=int, default=None,
                   help="twitter_candidates row id (required for --platform twitter).")
    p.add_argument("--thread-url", default=None,
                   help="reddit_candidates thread_url (required for --platform reddit).")
    p.add_argument("--text", required=True)
    p.add_argument("--style", default=None)
    p.add_argument(
        "--assigned-style", default=None,
        help="Picker's USE-mode pinned style name (NULL in INVENT mode).",
    )
    p.add_argument(
        "--assigned-mode", default=None, choices=[None, "use", "invent"],
        help="Picker mode for this batch: 'use' or 'invent'.",
    )
    p.add_argument(
        "--new-style", default=None,
        help="JSON object literal {description, example, why_existing_didnt_fit, note?} "
             "when model invents a new style.",
    )
    p.add_argument(
        "--platform",
        default="twitter",
        choices=["twitter", "reddit"],
        help="twitter targets twitter_candidates by id; reddit targets "
             "reddit_candidates by thread_url (save_draft action).",
    )
    args = p.parse_args()

    text = args.text.strip()
    if not text:
        print(json.dumps({"error": "EMPTY_TEXT"}))
        sys.exit(1)

    # Reddit lane (2026-07-16): the queue worker persists Draft A per thread
    # (keep-alive + durability, mirroring the twitter-prep flow). Routes
    # through the same save_draft action post_reddit._db_save_draft uses, so
    # a later salvage reuses the draft without a second LLM spend.
    if args.platform == "reddit":
        if not args.thread_url:
            print(json.dumps({"error": "THREAD_URL_REQUIRED"}))
            sys.exit(1)
        resp = api_patch(
            "/api/v1/reddit-candidates/by-thread-url",
            {
                "thread_url": args.thread_url,
                "action": "save_draft",
                "draft_text": text,
                "draft_engagement_style": args.style,
            },
            ok_on_404=True,
        )
        if (resp or {}).get("_not_found"):
            print(json.dumps({"error": "CANDIDATE_NOT_FOUND", "thread_url": args.thread_url}))
            sys.exit(1)
        if not (resp or {}).get("ok"):
            print(json.dumps({"error": "SAVE_DRAFT_FAILED", "thread_url": args.thread_url}))
            sys.exit(1)
        print(json.dumps({"logged": True, "thread_url": args.thread_url, "platform": "reddit"}))
        return

    if args.candidate_id is None:
        print(json.dumps({"error": "CANDIDATE_ID_REQUIRED"}))
        sys.exit(1)

    # Parse --new-style early so a malformed JSON arg fails before we touch
    # the DB. We do NOT validate required fields here (description, example,
    # why_existing_didnt_fit); validate_or_register/register_style does that
    # at post time so this stays a pure persistence layer.
    new_style_json = None
    if args.new_style:
        try:
            parsed = json.loads(args.new_style)
        except Exception as e:
            print(json.dumps({"error": "BAD_NEW_STYLE_JSON", "detail": str(e)}))
            sys.exit(1)
        if not isinstance(parsed, dict):
            print(json.dumps({"error": "BAD_NEW_STYLE_JSON",
                              "detail": "must be a JSON object"}))
            sys.exit(1)
        new_style_json = json.dumps(parsed)

    # platform is twitter-only today; the by-id endpoint targets twitter_candidates.
    payload = {
        "id": args.candidate_id,
        "action": "set_draft",
        "text": text,
        "style": args.style,
        "assigned_style": args.assigned_style,
        "assigned_mode": args.assigned_mode,
    }
    if new_style_json is not None:
        # Pass the already-validated JSON string; the endpoint re-parses it.
        payload["new_style"] = new_style_json

    resp = api_patch(
        "/api/v1/twitter-candidates/by-id", payload,
        ok_on_conflict=True, ok_on_404=True,
    )

    if (resp or {}).get("_not_found"):
        print(json.dumps({"error": "CANDIDATE_NOT_FOUND", "candidate_id": args.candidate_id}))
        sys.exit(1)
    if not (resp or {}).get("ok"):
        # 409 already_posted (status carried under error.details.status).
        details = ((resp or {}).get("error") or {}).get("details") or {}
        print(json.dumps({
            "error": "ALREADY_POSTED",
            "candidate_id": args.candidate_id,
            "status": details.get("status"),
        }))
        sys.exit(1)

    candidate = ((resp or {}).get("data") or {}).get("candidate") or {}
    print(json.dumps({
        "logged": True,
        "candidate_id": args.candidate_id,
        "drafted_at": candidate.get("drafted_at"),
        "assigned_style": args.assigned_style,
        "assigned_mode": args.assigned_mode,
        "new_style_persisted": bool(new_style_json),
    }))


if __name__ == "__main__":
    main()
