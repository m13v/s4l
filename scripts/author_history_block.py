#!/usr/bin/env python3
"""scripts/author_history_block.py — cross-platform prior-interaction context.

Given a target author + platform, prints a soft-context block summarizing
our recent comments on that author's threads (last N days, capped at K
most-recent). Empty output when no history. Designed to be injected into
the per-candidate section of draft prompts so the model can vary angle and
not repeat itself.

Wired into (one callsite each):
  - skill/run-twitter-cycle.sh   (Phase 2b-prep CANDIDATE_BLOCK loop)
  - scripts/engage_reddit.py     (reply draft prompt builder)
  - scripts/post_reddit.py       (build_draft_prompt)
  - scripts/post_github.py       (build_prompt)
  - skill/run-linkedin.sh        (Phase B prompt template)

CLI:
  python3 scripts/author_history_block.py --platform twitter --author tom_doerr
  python3 scripts/author_history_block.py --platform reddit  --author lazycodewiz \\
      --days 60 --limit 8

Stdout is prose, ready to paste into a prompt. Empty stdout when no rows.
Stderr only on argparse errors and DB failures; never raises mid-cycle.

LinkedIn caveat: thread_author_handle is the display name (not a unique
vanity slug), so two distinct LinkedIn users with the same display name
will collide. We document this in the block header rather than guard,
because the cost of a collision is "show one more harmless prior comment"
not anything dangerous.
"""

import argparse
import os
import sys

REPO_DIR = os.path.expanduser("~/social-autoposter")
sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))
import db as dbmod  # noqa: E402


PLATFORM_ALIAS = {
    "x": "twitter",
    "twitter": "twitter",
    "reddit": "reddit",
    "linkedin": "linkedin",
    "github": "github",
    "github_issues": "github",
    "moltbook": "moltbook",
}


def _normalize(handle):
    """Lowercase + strip @, u/, / prefixes. Empty/'unknown' → empty string."""
    if not handle:
        return ""
    h = str(handle).strip().lower()
    while h and h[0] in "@/":
        h = h[1:]
    if h.startswith("u/"):
        h = h[2:]
    h = h.strip()
    if h in ("", "unknown", "[deleted]", "deleted"):
        return ""
    return h


SQL = """
SELECT
  p.id,
  p.posted_at,
  p.project_name,
  p.our_content,
  p.thread_title,
  COALESCE(p.upvotes, 0)        AS upvotes,
  COALESCE(p.comments_count, 0) AS replies_count,
  COALESCE(p.views, 0)          AS views,
  (
    SELECT r.their_content
    FROM replies r
    WHERE r.post_id = p.id
      AND r.their_content IS NOT NULL
      AND COALESCE(r.their_content, '') <> ''
    ORDER BY r.discovered_at ASC
    LIMIT 1
  ) AS their_first_reply
FROM posts p
WHERE p.platform = %s
  AND LOWER(REGEXP_REPLACE(COALESCE(p.thread_author_handle, ''), '^(@|u/)', ''))
        = %s
  AND p.status NOT IN ('deleted', 'removed', 'migrated')
  AND p.posted_at > NOW() - (%s || ' days')::interval
ORDER BY p.posted_at DESC
LIMIT %s
"""


def fetch(platform, author, days=30, limit=5):
    """Return list of tuples matching SQL columns. Empty on bad input/no rows."""
    norm = _normalize(author)
    if not norm:
        return []
    plat = PLATFORM_ALIAS.get(str(platform).lower(), str(platform).lower())
    try:
        conn = dbmod.get_conn()
        cur = conn.execute(SQL, (plat, norm, int(days), int(limit)))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"[author_history_block] DB error: {e}", file=sys.stderr)
        return []


def _truncate(s, n):
    s = (s or "").replace("\n", " ").replace("\r", " ").strip()
    if len(s) <= n:
        return s
    return s[: n - 3].rstrip() + "..."


def format_block(rows, author, platform, days):
    """Render the per-candidate prompt block. Returns '' when rows is empty."""
    if not rows:
        return ""
    plat = PLATFORM_ALIAS.get(str(platform).lower(), str(platform).lower())
    norm = _normalize(author)
    # Display: keep author's natural form on platforms where the handle IS the
    # name (linkedin/moltbook). Use the canonical @/u/ prefix on platforms
    # where users recognize it that way.
    if plat == "twitter":
        handle_disp = "@" + norm
    elif plat == "reddit":
        handle_disp = "u/" + norm
    elif plat == "linkedin":
        handle_disp = str(author).strip()
    else:
        handle_disp = norm

    header = (
        f"PRIOR INTERACTIONS WITH {handle_disp} "
        f"(our last {len(rows)} comments to this author, "
        f"window={days}d, latest first):"
    )
    lines = [header]
    for row in rows:
        (
            _id,
            posted_at,
            project,
            our_content,
            _thread_title,
            upvotes,
            replies_count,
            views,
            their_first_reply,
        ) = row
        date = posted_at.date().isoformat() if posted_at else "?"
        proj = project or "?"
        ours = _truncate(our_content, 140)
        eng_bits = []
        if upvotes:
            eng_bits.append(f"likes={upvotes}")
        if replies_count:
            eng_bits.append(f"replies={replies_count}")
        if views:
            eng_bits.append(f"views={views}")
        eng_str = (" [" + ", ".join(eng_bits) + "]") if eng_bits else " [no engagement]"
        lines.append(f"- {date} ({proj}): \"{ours}\"{eng_str}")
        if their_first_reply:
            tr = _truncate(their_first_reply, 110)
            lines.append(f"   -> they replied: \"{tr}\"")
    lines.append(
        "Use as SOFT CONTEXT only: vary angle, avoid repeating phrasing or "
        "anecdotes. Do NOT over-reference (never write 'as I said before'). "
        "If our prior take got pushback, soften; if it landed well, keep the voice."
    )
    if plat == "linkedin":
        lines.append(
            "(LinkedIn caveat: matched on display name. If the candidate's "
            "account looks unrelated to the prior posts, ignore this block.)"
        )
    return "\n".join(lines)


def render(platform, author, days=30, limit=5):
    """Convenience for Python callers: returns the block string (possibly empty)."""
    rows = fetch(platform, author, days=days, limit=limit)
    return format_block(rows, author, platform, days)


def main():
    p = argparse.ArgumentParser(
        description="Print prior-interaction context for a target author."
    )
    p.add_argument(
        "--platform",
        required=True,
        help="twitter | reddit | linkedin | github | moltbook (aliases: x, github_issues)",
    )
    p.add_argument(
        "--author",
        required=True,
        help="Target author's handle (any case, leading @ or u/ tolerated)",
    )
    p.add_argument("--days", type=int, default=30, help="Look-back window (default 30)")
    p.add_argument(
        "--limit", type=int, default=5, help="Max interactions to include (default 5)"
    )
    args = p.parse_args()

    dbmod.load_env()
    block = render(args.platform, args.author, days=args.days, limit=args.limit)
    if block:
        print(block)


if __name__ == "__main__":
    main()
