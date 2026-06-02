#!/usr/bin/env python3
"""Fetch active campaigns for a given platform with budget remaining.

A campaign is "active" when:
  - status = 'active'
  - its platforms list includes the requested platform
  - max_posts_total is set AND posts_made < max_posts_total

Campaigns without max_posts_total are ignored by this script on purpose.
Every campaign must declare a lifetime cap to be considered.

Usage:
    python3 active_campaigns.py --platform reddit          # prompt block (stdout)
    python3 active_campaigns.py --platform reddit --json   # machine-readable
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _get_active_via_api(platform):
    from http_api import api_get
    resp = api_get(
        "/api/v1/campaigns",
        query={
            "status": "active",
            "platform": platform,
            "with_budget_remaining": "true",
            "limit": 500,
        },
    )
    rows = ((resp or {}).get("data") or {}).get("campaigns") or []
    out = []
    for r in rows:
        max_total = r.get("max_posts_total")
        posts_made = r.get("posts_made") or 0
        if max_total is None or posts_made >= max_total:
            continue
        out.append({
            "id": int(r["id"]),
            "name": r.get("name"),
            "prompt": r.get("prompt"),
            "max_posts_total": int(max_total),
            "posts_made": int(posts_made),
            "remaining": int(max_total) - int(posts_made),
        })
    return out


def get_active_campaigns(platform):
    """Active campaigns for `platform` with budget remaining.

    Routes through /api/v1/campaigns (HTTP-only).
    """
    return _get_active_via_api(platform)


def format_prompt_block(campaigns, repo_dir):
    if not campaigns:
        return ""

    ids_csv = ",".join(str(c["id"]) for c in campaigns)
    lines = []
    lines.append("## ACTIVE CAMPAIGNS (mandatory for every post this run)")
    lines.append("")
    lines.append("The following campaign instructions override your defaults. Follow them exactly.")
    lines.append("")

    for c in campaigns:
        lines.append(f"### CAMPAIGN id={c['id']} name={c['name']}")
        lines.append(f"Lifetime budget: {c['remaining']} of {c['max_posts_total']} posts remaining.")
        lines.append("Instruction:")
        lines.append(c["prompt"])
        lines.append("")

    lines.append("## REQUIRED campaign attribution (do this for EVERY post you create)")
    lines.append("")
    lines.append("1. When inserting the post row, use `INSERT INTO posts (...) VALUES (...) RETURNING id;` to capture NEW_POST_ID.")
    lines.append("2. Immediately after, run this shell command to attach the post to the active campaigns:")
    lines.append("")
    lines.append(f"   python3 {repo_dir}/scripts/campaign_bump.py --post-id NEW_POST_ID --campaign-ids {ids_csv}")
    lines.append("")
    lines.append("This is mandatory. If you skip it, the campaign counter does not advance and the campaign will over-post.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo-dir", default=os.path.expanduser("~/social-autoposter"))
    args = ap.parse_args()

    campaigns = get_active_campaigns(args.platform)

    if args.json:
        print(json.dumps({
            "platform": args.platform,
            "active_count": len(campaigns),
            "campaign_ids": ",".join(str(c["id"]) for c in campaigns),
            "campaigns": campaigns,
        }))
    else:
        block = format_prompt_block(campaigns, args.repo_dir)
        if block:
            print(block)


if __name__ == "__main__":
    main()
