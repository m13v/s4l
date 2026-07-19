#!/usr/bin/env python3
"""twitter_prompt_sandbox.py — fetch historical twitter_candidates rows and
format them into the exact pipe-separated shape run-twitter-cycle.sh expects
for $CANDIDATES, so a prompt experiment can be run through the real Phase
2b-prep drafting step against real past threads without running live Phase 1
discovery.

Usage:
  python3 scripts/twitter_prompt_sandbox.py --project fazm --out /tmp/sandbox.txt
  python3 scripts/twitter_prompt_sandbox.py --project fazm --status posted --limit 5 --out /tmp/sandbox.txt
  python3 scripts/twitter_prompt_sandbox.py --urls https://x.com/.../1,https://x.com/.../2 --out /tmp/sandbox.txt

  # Another install's data (matched_project alone is NOT tenant-safe -- see
  # --install-id's help text): pass --install-id, --project becomes optional
  # (any of that install's candidates) or still scopes to one of their
  # projects. Pair with admin_fetch_install_config.py for their real config
  # + persona_corpus.txt too, if testing voice fidelity rather than just
  # replaying their real threads:
  python3 scripts/twitter_prompt_sandbox.py --install-id ba6519ca-edaf-4fee-95b9-446da86bd346 \\
      --project PersonalBrand --out /tmp/sandbox_karol.txt

Then run the cycle against it:
  S4L_SANDBOX_CANDIDATES_FILE=/tmp/sandbox.txt S4L_DRAFT_PROMPT_VARIANT=treatment_v4 \\
      bash skill/run-twitter-cycle.sh

Read-only: GET /api/v1/twitter-candidates only (via http_api.py, the same
lane every other script uses — no direct SQL, no writes).

Assigns synthetic ids from SANDBOX_ID_BASE upward, far outside the real
serial range, so any accidental by-id write-back during Phase 2b-prep
(log_draft.py's set_draft action, or the media pre-fetch's set_media) 404s
harmlessly against a nonexistent row instead of corrupting the real
historical candidate. Mirrors the "rd-" candidate_id prefix
merge_review_queue.py already uses for reddit cards for the identical
reason (see its _reddit_plan_to_candidates docstring).

Draft fields (existing draft text/style/age) are always blanked here: a
sandbox replay drafts fresh against whichever prompt variant is under test,
never inherits the real row's own draft/reuse history.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from http_api import api_get, load_env  # noqa: E402
from twitter_cycle_helper import _sanitize  # noqa: E402

# Comfortably above any real twitter_candidates.id (int4 serial, currently in
# the low hundred-thousands per the schema's own comments) and comfortably
# below int4 max (~2.147e9), so it can never collide with a live row.
SANDBOX_ID_BASE = 900_000_000


def _age_hours(row: dict) -> float:
    ts = row.get("tweet_posted_at")
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return 0.0


def fetch(project, status, limit, since, urls, install_id):
    query = {"limit": limit}
    if project:
        query["matched_project"] = project
    if status and status != "any":
        query["status"] = status
    if since:
        query["since"] = since
    if urls:
        query["tweet_urls"] = ",".join(urls)
    if install_id:
        query["install_id"] = install_id
    resp = api_get("/api/v1/twitter-candidates", query=query)
    return (resp.get("data") or {}).get("candidates") or []


def to_pipe_row(sandbox_id: int, row: dict) -> str:
    cols = [
        str(sandbox_id),
        str(row.get("tweet_url") or ""),
        str(row.get("author_handle") or ""),
        _sanitize(row.get("tweet_text")),
        f"{float(row.get('virality_score') or 0):g}",
        f"{float(row.get('delta_score') or 0):g}",
        str(row.get("matched_project") or ""),
        str(row.get("search_topic") or ""),
        str(row.get("likes") or ""),
        str(row.get("retweets") or ""),
        str(row.get("replies") or ""),
        str(row.get("views") or ""),
        str(row.get("author_followers") or ""),
        f"{_age_hours(row):g}",
        "",     # existing draft text: blanked, sandbox always drafts fresh
        "",     # existing draft style: blanked
        "-1",   # existing draft age: -1 sentinel = none (matches cmd_candidates)
    ]
    return "|".join(cols)


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="matched_project filter")
    ap.add_argument("--status", default="any", help="pending|posted|skipped|expired|any (default any)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--since", help="ISO8601 discovered_at lower bound")
    ap.add_argument("--urls", help="comma-separated tweet_urls, explicit selection instead of a filtered pull")
    ap.add_argument(
        "--install-id",
        help="scope the pull to one install (required for another install's data -- "
        "matched_project alone is NOT tenant-safe, e.g. 'PersonalBrand' spans 7 installs)",
    )
    ap.add_argument(
        "--sort-by",
        default="virality_score",
        choices=["virality_score", "delta_score", "none"],
        help="rank the pulled pool by this field (descending) and keep the top --limit "
        "before writing (default virality_score, matching the real production pipeline's "
        "own selection -- 'status=posted' alone only proves a candidate passed ONCE, "
        "under whatever arm drafted it then; picking the highest-scoring of the pool "
        "raises the odds it also clears the SAME virality bar and the model's own "
        "judgment on replay. 'none' keeps the API's discovered_at-DESC order, i.e. most "
        "recent first).",
    )
    ap.add_argument("--out", required=True, help="output path for the pipe-separated candidates file")
    args = ap.parse_args()

    if not args.project and not args.urls and not args.install_id:
        print("[twitter_prompt_sandbox] pass --project, --urls, or --install-id to scope the pull", file=sys.stderr)
        return 1

    urls = [u.strip() for u in args.urls.split(",") if u.strip()] if args.urls else None
    # Over-fetch when ranking so there's an actual pool to rank within (otherwise
    # sorting the API's own --limit-sized, discovered_at-DESC page is a no-op).
    # 500 is the API's own hard cap (see /api/v1/twitter-candidates GET).
    fetch_limit = min(max(args.limit * 10, 50), 500) if args.sort_by != "none" and not urls else args.limit
    rows = fetch(args.project, args.status, fetch_limit, args.since, urls, args.install_id)
    if not rows:
        print("[twitter_prompt_sandbox] no candidates matched the given filters", file=sys.stderr)
        return 1
    if args.sort_by != "none":
        rows.sort(key=lambda r: float(r.get(args.sort_by) or 0), reverse=True)
    rows = rows[: args.limit]

    lines = [to_pipe_row(SANDBOX_ID_BASE + i, r) for i, r in enumerate(rows)]
    Path(args.out).write_text("\n".join(lines) + "\n")

    print(f"[twitter_prompt_sandbox] wrote {len(lines)} historical candidate(s) to {args.out}:", file=sys.stderr)
    for r in rows:
        print(f"  - {r.get('tweet_url')} (status={r.get('status')}, project={r.get('matched_project')})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
