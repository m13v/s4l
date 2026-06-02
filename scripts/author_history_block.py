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
from datetime import datetime

REPO_DIR = os.path.expanduser("~/social-autoposter")
sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))
from http_api import api_get  # noqa: E402


PLATFORM_ALIAS = {
    "x": "twitter",
    "twitter": "twitter",
    "reddit": "reddit",
    "linkedin": "linkedin",
    "github": "github",
    "github_issues": "github",
    "moltbook": "moltbook",
}


# Per-process cache for active campaign suffixes; populated lazily on first
# format_block() call. None = not loaded yet; [] = loaded but empty.
_ACTIVE_CAMPAIGN_SUFFIXES_CACHE = None


def _load_active_campaign_suffixes():
    """Best-effort: return a list of currently-active campaign suffix literals.

    Mirrors the helper of the same name in scripts/top_performers.py. We
    duplicate (rather than import) to keep this module's failure mode
    independent of top_performers' larger dependency surface.

    Used to strip the suffix from `our_content` before injecting prior
    interactions into the draft prompt, so the LLM never learns to echo
    the suffix in its drafts (which would then double-fire when the
    tool-layer injection at twitter_browser.reply_to_tweet / reddit_browser
    appends a second copy). See feedback_suffix_injection_gating.md for the
    history; this closes the 4th leak path that the 2026-05-19 sweep missed.

    On any failure returns []: missing strip is preferable to crashing the
    prompt assembly path.
    """
    global _ACTIVE_CAMPAIGN_SUFFIXES_CACHE
    if _ACTIVE_CAMPAIGN_SUFFIXES_CACHE is not None:
        return _ACTIVE_CAMPAIGN_SUFFIXES_CACHE
    suffixes = []
    try:
        from http_api import api_get  # noqa: E402
        resp = api_get(
            "/api/v1/campaigns",
            query={"status": "active", "has_suffix": "true", "limit": 500},
        )
        rows = ((resp or {}).get("data") or {}).get("campaigns") or []
        for r in rows:
            s = (r.get("suffix") or "").strip()
            if s and s not in suffixes:
                suffixes.append(s)
    except Exception as e:
        print(
            f"[author_history_block] _load_active_campaign_suffixes failed: {e!r}",
            file=sys.stderr,
        )
    _ACTIVE_CAMPAIGN_SUFFIXES_CACHE = suffixes
    return suffixes


def _strip_active_campaign_suffixes(text, suffixes):
    """Trailing-only, idempotent strip of any active-campaign suffix.

    Identical contract to top_performers._strip_active_campaign_suffixes.
    Idempotent loop also collapses an already-doubled historical suffix
    (e.g. "... written with s4lai written with s4lai") to clean text.
    Trailing-only so we never touch the body of the comment.
    """
    if not text or not suffixes:
        return text
    cleaned = text.rstrip()
    changed = True
    while changed:
        changed = False
        for sfx in suffixes:
            if sfx and cleaned.endswith(sfx):
                cleaned = cleaned[: -len(sfx)].rstrip()
                changed = True
    return cleaned


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


# Column order returned by GET /api/v1/posts/author-history (which is the
# single source of truth for the SQL; the route comment notes "keep the column
# list + filters in sync"). format_block below indexes the tuple positionally,
# so this order is load-bearing.
def _parse_dt(s):
    """Parse the API's ISO posted_at into a datetime; None on any failure.

    format_block / fetch's summary call `.date()` on this, so a real datetime
    is required where present. Falls back to the leading YYYY-MM-DD so older
    Pythons (pre-3.11 fromisoformat can't take 'Z' or variable fractional
    digits) still yield a usable date instead of crashing the prompt path.
    """
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.fromisoformat(str(s)[:10])
        except Exception:
            return None


def _row_to_tuple(r):
    return (
        r.get("id"),
        _parse_dt(r.get("posted_at")),
        r.get("project_name"),
        r.get("our_content"),
        r.get("thread_title"),
        r.get("upvotes") or 0,
        r.get("replies_count") or 0,
        r.get("views") or 0,
        r.get("their_first_reply"),
    )


def fetch(platform, author, days=30, limit=5):
    """Return list of tuples matching SQL columns. Empty on bad input/no rows.

    Always emits one stderr line per call so pipeline logs show injection
    activity. Status token (INJECTED / EMPTY / SKIPPED / ERROR) is the
    leading word after the tag for fast grep.

    Grep recipes (see latest log via `ls -t skill/logs/<platform>*.log | head -1`):
      grep '\\[author_history_block\\] INJECTED' <log>    # confirmed wins
      grep '\\[author_history_block\\] EMPTY'    <log>    # author has no prior history
      grep '\\[author_history_block\\] SKIPPED'  <log>    # blank/unknown author field
      grep '\\[author_history_block\\] ERROR'    <log>    # DB or query failure
    """
    plat = PLATFORM_ALIAS.get(str(platform).lower(), str(platform).lower())
    norm = _normalize(author)
    if not norm:
        print(
            f"[author_history_block] SKIPPED platform={plat} "
            f"author_input={author!r} reason=empty_or_unknown_handle",
            file=sys.stderr,
        )
        return []
    try:
        resp = api_get(
            "/api/v1/posts/author-history",
            query={
                "platform": plat,
                "author": norm,
                "days": int(days),
                "limit": int(limit),
            },
        )
        json_rows = ((resp or {}).get("data") or {}).get("rows") or []
        rows = [_row_to_tuple(r) for r in json_rows]
        if not rows:
            print(
                f"[author_history_block] EMPTY platform={plat} "
                f"author={norm} days={days} limit={limit}",
                file=sys.stderr,
            )
            return rows
        # Compute compact summary: latest + oldest date + project, total likes
        # received on prior comments, count of prior threads that got a reply.
        # These give a one-line "what got injected" preview without dumping
        # the full block to the log.
        latest = rows[0]
        oldest = rows[-1]
        latest_date = latest[1].date().isoformat() if latest[1] else "?"
        oldest_date = oldest[1].date().isoformat() if oldest[1] else "?"
        latest_proj = latest[2] or "?"
        total_likes = sum((r[5] or 0) for r in rows)
        n_with_their_reply = sum(1 for r in rows if r[8])
        print(
            f"[author_history_block] INJECTED platform={plat} "
            f"author={norm} rows={len(rows)} days={days} "
            f"latest={latest_date}({latest_proj}) oldest={oldest_date} "
            f"likes_total={total_likes} they_replied={n_with_their_reply}",
            file=sys.stderr,
        )
        return rows
    except Exception as e:
        print(
            f"[author_history_block] ERROR platform={plat} author={norm} "
            f"error={e!r}",
            file=sys.stderr,
        )
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
    # Load active campaign suffixes ONCE per format_block call so we strip
    # them off `our_content` BEFORE truncation. Short Twitter replies
    # (≤140 chars total) would otherwise show the suffix verbatim in the
    # exemplar, training the LLM to echo it; the tool layer then appends
    # a second copy. See feedback_suffix_injection_gating.md.
    suffix_strip_list = _load_active_campaign_suffixes()
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
        our_content_clean = _strip_active_campaign_suffixes(
            our_content, suffix_strip_list
        )
        ours = _truncate(our_content_clean, 140)
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

    block = render(args.platform, args.author, days=args.days, limit=args.limit)
    if block:
        print(block)


if __name__ == "__main__":
    main()
