#!/usr/bin/env python3
"""link_edit_helper.py — CLI wrapper used by the three link-edit pipelines
(skill/link-edit-{reddit,github,moltbook}.sh) to replace the inline
`psql "$DATABASE_URL"` one-liners they used to embed. The direct-Postgres lane
was removed 2026-06-01; DATABASE_URL is deliberately ignored, no DB, no
fallback. Every subcommand prints exactly what the corresponding psql call
printed so the surrounding shell capture ($(...)) and string/int compares are
unchanged.

Subcommands:
  eligible --platform P [--age-hours N] [--min-upvotes-exclusive N]
           [--page-gen-rate-pct N] [--order upvotes|posted_at]
      -> GET /api/v1/posts/link-edit-eligible?...
      -> prints the json_agg() array of eligible posts, or the literal `null`
         when none match (matches Postgres json_agg-over-empty-set, so the
         shell `[ "$EDITABLE" = "null" ]` guard still fires).
  mark-edited --post-id N --content "<text>" [--source "<src>"]
      -> PATCH /api/v1/posts/N  { link_edited_now, link_edit_content, link_source? }
         (was: UPDATE posts SET link_edited_at=NOW(), link_edit_content=..,
          link_source=.. WHERE id=..)
  mark-skipped --post-id N --reason "<reason>"
      -> PATCH /api/v1/posts/N  { link_edited_now, link_edit_content:"SKIPPED: <reason>" }
         (was: UPDATE posts SET link_edited_at=NOW(),
          link_edit_content='SKIPPED: ..' WHERE id=..)
  set-project --post-id N --project "<name>"
      -> PATCH /api/v1/posts/N  { project_name }
         (was: UPDATE posts SET project_name=.. WHERE id=..)
  edited-count --platform P
      -> GET /api/v1/posts/count?platform=P&link_edited=true
      -> prints the integer all-time edited count (was: SELECT COUNT(*) FROM
         posts WHERE platform=P AND link_edited_at IS NOT NULL)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_patch  # noqa: E402


def cmd_eligible(args) -> int:
    query = {
        "platform": args.platform,
        "age_hours": args.age_hours,
        "page_gen_rate_pct": args.page_gen_rate_pct,
        "order": args.order,
    }
    if args.min_upvotes_exclusive is not None:
        query["min_upvotes_exclusive"] = args.min_upvotes_exclusive
    resp = api_get("/api/v1/posts/link-edit-eligible", query=query)
    posts = (resp.get("data") or {}).get("posts") or []
    if not posts:
        # Mirror Postgres json_agg() over an empty set, which returns NULL and
        # which psql -t -A prints as the bare string `null`. The shells guard
        # on `[ "$EDITABLE" = "null" ]`, so emit exactly that.
        print("null")
        return 0
    json.dump(posts, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


def _patch_post(post_id: int, body: dict) -> int:
    api_patch(f"/api/v1/posts/{post_id}", body=body)
    return 0


def cmd_mark_edited(args) -> int:
    body: dict = {
        "link_edited_now": True,
        "link_edit_content": args.content,
    }
    if args.source is not None:
        body["link_source"] = args.source
    return _patch_post(args.post_id, body)


def cmd_mark_skipped(args) -> int:
    return _patch_post(
        args.post_id,
        {
            "link_edited_now": True,
            "link_edit_content": f"SKIPPED: {args.reason}",
        },
    )


def cmd_set_project(args) -> int:
    return _patch_post(args.post_id, {"project_name": args.project})


def cmd_edited_count(args) -> int:
    resp = api_get(
        "/api/v1/posts/count",
        query={"platform": args.platform, "link_edited": "true"},
    )
    print(int((resp.get("data") or {}).get("count") or 0))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("eligible")
    e.add_argument("--platform", required=True)
    e.add_argument("--age-hours", type=int, default=6)
    e.add_argument("--min-upvotes-exclusive", type=int, default=None)
    e.add_argument("--page-gen-rate-pct", type=int, default=0)
    e.add_argument("--order", default="upvotes", choices=["upvotes", "posted_at"])

    me = sub.add_parser("mark-edited")
    me.add_argument("--post-id", type=int, required=True)
    me.add_argument("--content", required=True)
    me.add_argument("--source", default=None)

    ms = sub.add_parser("mark-skipped")
    ms.add_argument("--post-id", type=int, required=True)
    ms.add_argument("--reason", required=True)

    sp = sub.add_parser("set-project")
    sp.add_argument("--post-id", type=int, required=True)
    sp.add_argument("--project", required=True)

    ec = sub.add_parser("edited-count")
    ec.add_argument("--platform", required=True)

    args = p.parse_args()
    return {
        "eligible": cmd_eligible,
        "mark-edited": cmd_mark_edited,
        "mark-skipped": cmd_mark_skipped,
        "set-project": cmd_set_project,
        "edited-count": cmd_edited_count,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
