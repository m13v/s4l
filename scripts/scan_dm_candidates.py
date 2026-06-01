#!/usr/bin/env python3
"""Scan replies table for users worth DMing across all platforms.

Criteria for DM candidates:
- User replied to our post/comment with a substantive comment (status='replied', meaning we already engaged publicly)
- We haven't already DM'd this user for this reply
- User isn't in exclusion list
- Comment has enough substance (>10 words) to continue the conversation
- Not a bot or deleted account
- Post is recent enough (last 7 days)

Supports: Reddit, LinkedIn, Twitter/X

Usage:
    python3 scripts/scan_dm_candidates.py [--dry-run] [--max N] [--platform reddit|linkedin|x|all]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod
from project_topics import topics_for_project

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")
# Min-word floor to promote a public reply into a DM candidate.
# X replies are natively shorter (quote-tweets, @-mentions), so the bar is lower.
# Reddit floor lowered to 4 on 2026-04-21 after data showed 4-9 word Reddit
# replies are often direct questions and strong opinions, not filler; the
# previous 10-word floor was leaving ~66 eligible candidates/30d on the table.
MIN_WORDS_BY_PLATFORM = {"reddit": 4, "linkedin": 10, "x": 4}
MIN_WORDS_DEFAULT = 10
# Wait this long after our public reply before DMing, so the DM doesn't
# feel like a double-tap on the same day. Next scan picks it up.
POST_REPLY_COOLDOWN_HOURS = 5
MAX_AGE_DAYS = 7
DEFAULT_MAX_CANDIDATES = 100
PLATFORMS = ["reddit", "linkedin", "x"]

# Skip reasons that mean "this person can never receive a DM from us, ever".
# These are recipient-side blocks (DMs disabled, suspended, company page,
# inmail credits exhausted) or competitor disqualifications. Stored as ILIKE
# patterns; matched against the dms.skip_reason column to permanently exclude
# an author from future rescans (no 30-day window). Anything not matched here
# (low_value, hostile, thin_conversation, etc.) is treated as transient and
# the user can be re-promoted later.
PERMANENT_SKIP_REASON_PATTERNS = (
    "chat_disabled%",
    "dms_closed%",
    "cannot_send_dms_disabled%",
    "%DMs disabled%",
    "%has DMs closed%",
    "%user has DMs disabled%",
    "%DMs not open%",
    "not_following_no_dm_access%",
    "x_requires_premium_to_dm_non_followers%",
    "%requires verified/premium%",
    "%requires X verification/premium%",
    "%only verified users can send DM%",
    "not_connected_cannot_dm%",
    "not_connected_3rd_degree_cant_message%",
    "not_connected_inmail_credits_exhausted%",
    "requires_inmail_credit%",
    "requires_inmail_no_credits%",
    "no_inmail_credits%",
    "not_1st_connection_no_inmail_credits%",
    "3rd_plus_connection_cannot_dm%",
    "messaging_restricted%",
    "%InMail credits exhausted%",
    "%InMail credits are depleted%",
    "company_page%",
    "company page%",
    "%company page, cannot DM%",
    "account_suspended%",
    "%account is suspended%",
    "cannot_identify_correct_profile%",
    "encrypted_dm_passcode_required%",
    "x_encrypted_dm_passcode_required%",
    "disqualified:%",
    "unable to send a message request%",
    "%unable to send a message request%",
    "send_button_disabled%",
)

# Transient failure patterns: infrastructure errors (browser profile lock
# contention, MCP wrapper death, Playwright launch failures, verify-failed
# sends) that should NOT permanently block a candidate. A dms row in
# status='error' (or 'skipped' tagged with one of these) gets:
#   (a) treated as non-blocking in the discover LEFT JOIN below, so the
#       reply re-appears as a candidate, AND
#   (b) reverted to status='pending' via ON CONFLICT DO UPDATE when
#       re-inserted (see scan_platform).
# Self-heals the 2026-05-12 "7 warm leads burned by twitter_agent_mcp_unavailable"
# regression at the source. Without this, the LEFT JOIN d.id IS NULL filter
# permanently blocked any reply that ever had a transient-error dms row.
TRANSIENT_SKIP_REASON_PATTERNS = (
    "twitter_agent_mcp_unavailable%",
    "reddit_agent_mcp_unavailable%",
    "linkedin_agent_mcp_unavailable%",
    "mcp_unavailable%",
    "%mcp server not connected%",
    "%no mcp tools%",
    "%MCP server not registered%",
    "send_unverified%",
    "%browser launch failed%",
    "%profile locked by another process%",
    "%chromium profile locked%",
    "%target page, context or browser has been closed%",
    "%playwright%timeout%",
    "%SIGTRAP%",
    "%transient_browser_failure%",
)

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def word_count(text):
    return len(text.split()) if text else 0


def build_project_topic_index(config, platform):
    """Return [(project_name, [topic_phrase_lower, ...]), ...] for topic matching.

    Reads from the unified search_topics list (post 2026-04-30 legacy
    cleanup); platform arg kept for callsite compatibility.
    """
    out = []
    for p in config.get("projects", []) or []:
        name = p.get("name") or p.get("id")
        if not name:
            continue
        phrases = []
        for v in topics_for_project(name):
            if isinstance(v, str) and v.strip():
                phrases.append(v.strip().lower())
        if phrases:
            out.append((name, phrases))
    return out


def infer_target_project(text_parts, project_topic_index):
    """Return the project whose topics overlap most with the given text, or None."""
    blob = " ".join(t for t in text_parts if t).lower()
    if not blob:
        return None
    best_name, best_score = None, 0
    for name, phrases in project_topic_index:
        score = 0
        for phrase in phrases:
            if not phrase:
                continue
            if " " in phrase:
                if phrase in blob:
                    score += 2
            else:
                if f" {phrase} " in f" {blob} ":
                    score += 1
        if score > best_score:
            best_score = score
            best_name = name
    return best_name if best_score > 0 else None


def upsert_prospect_row(conn, platform, author):
    """Ensure a prospects row exists for (platform, author); return prospect_id."""
    conn.execute(
        """
        INSERT INTO prospects (platform, author)
        VALUES (%s, %s)
        ON CONFLICT ON CONSTRAINT prospects_platform_author_unique DO NOTHING
        """,
        (platform, author),
    )
    cur = conn.execute(
        "SELECT id FROM prospects WHERE platform=%s AND author=%s",
        (platform, author),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def get_excluded_authors(config, platform):
    """Build excluded authors set for a given platform."""
    excluded = {a.lower() for a in config.get("exclusions", {}).get("authors", [])}
    excluded.add("automoderator")
    excluded.add("[deleted]")

    if platform == "reddit":
        reddit_account = config.get("accounts", {}).get("reddit", {}).get("username", "")
        if reddit_account:
            excluded.add(reddit_account.lower())
    elif platform == "linkedin":
        linkedin_name = config.get("accounts", {}).get("linkedin", {}).get("name", "")
        if linkedin_name:
            excluded.add(linkedin_name.lower())
        for p in config.get("exclusions", {}).get("linkedin_profiles", []):
            excluded.add(p.lower())
    elif platform == "x":
        twitter_handle = config.get("accounts", {}).get("twitter", {}).get("handle", "").lstrip("@")
        if twitter_handle:
            excluded.add(twitter_handle.lower())
        for t in config.get("exclusions", {}).get("twitter_accounts", []):
            excluded.add(t.lower())

    return excluded


def scan_platform(conn, config, platform, max_candidates, dry_run, max_age_days=None):
    """Scan for DM candidates on a single platform."""
    # Canonicalize Twitter as 'x' (the dms/replies/posts tables use 'x'; some
    # callers historically passed 'twitter'). Without this, dedupe leaks across
    # the two names and the same person can be re-queued.
    if platform == "twitter":
        platform = "x"
    excluded = get_excluded_authors(config, platform)
    topic_index = build_project_topic_index(config, platform)
    age_days = max_age_days if max_age_days is not None else MAX_AGE_DAYS

    # Multi-account scoping (Twitter only): when this machine has a Twitter
    # handle configured, only surface candidates from replies on posts THIS
    # account made. Without this, a VM running as @matt_diak would discover
    # DM candidates from @m13v_'s public reply threads and propose outreach
    # about conversations the wrong account had. The other platforms
    # (reddit, linkedin) don't yet have multi-machine fanout, so they fall
    # through unscoped. Treat the filter as additive: NULL handle == legacy
    # unscoped behavior.
    twitter_handle = None
    if platform == "x":
        try:
            from twitter_account import resolve_handle as _resolve_twitter_handle
            twitter_handle = _resolve_twitter_handle()
        except Exception:
            twitter_handle = None

    candidates = conn.execute("""
        SELECT r.id as reply_id, r.post_id, r.platform, r.their_author, r.their_content,
               r.their_comment_url, r.depth,
               r.our_reply_content, r.our_reply_url,
               p.thread_title, p.our_content as our_post_content,
               p.thread_url, p.our_url, p.project_name as post_project,
               r.replied_at
        FROM replies r
        JOIN posts p ON r.post_id = p.id
        LEFT JOIN dms d
               ON d.reply_id = r.id
              AND d.platform = %s
              -- Ignore transient-failure rows when deciding "already has a DM entry".
              -- A reply whose ONLY dms row is e.g. status='error' with
              -- skip_reason='twitter_agent_mcp_unavailable: ...' passes through
              -- this join as d.id IS NULL and gets re-discovered. The ON CONFLICT
              -- DO UPDATE in the INSERT below then flips that row back to pending.
              AND NOT (d.status IN ('error','skipped')
                       AND COALESCE(d.skip_reason,'') ILIKE ANY(%s))
        WHERE r.status = 'replied'
          AND r.platform = %s
          AND r.our_reply_content IS NOT NULL
          AND r.our_reply_content != ''
          AND d.id IS NULL
          AND r.replied_at >= NOW() - INTERVAL '%s days'
          AND r.replied_at <= NOW() - (INTERVAL '1 hour' * %s)
          AND (%s::text IS NULL OR p.our_account = %s)
        ORDER BY r.replied_at DESC
    """, (platform, list(TRANSIENT_SKIP_REASON_PATTERNS), platform, age_days,
          POST_REPLY_COOLDOWN_HOURS, twitter_handle, twitter_handle)).fetchall()

    inserted = 0
    skipped_reasons = {}

    for row in candidates:
        if inserted >= max_candidates:
            break

        author = row["their_author"] or ""
        content = row["their_content"] or ""

        # Skip excluded authors
        if author.lower() in excluded:
            reason = "excluded_author"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue

        # Skip low-substance comments (platform-specific floor)
        min_words = MIN_WORDS_BY_PLATFORM.get(platform, MIN_WORDS_DEFAULT)
        if word_count(content) < min_words:
            reason = "too_short"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue

        # Dedupe: don't re-promote a candidate if either
        #   (a) we sent/queued a REAL private DM (chat_url IS NOT NULL) in the
        #       last 30 days, OR
        #   (b) we permanently can't (or shouldn't) DM them based on a prior
        #       skip/error (chat_disabled, account_suspended, disqualified,
        #       inmail credits exhausted, etc. — see PERMANENT_SKIP_REASON_PATTERNS)
        #
        # NOTE 2026-05-13: the recent_active branch REQUIRES chat_url IS NOT NULL.
        # The dms table is also used as a unified prospect-tracker: dm_conversation.
        # ensure-dm inserts rows with status='sent' + chat_url=NULL after every
        # public reply (engage_reddit hook), which previously self-poisoned the
        # cooldown — every public-comment author looked "already_dmd_recently"
        # even though no real DM was ever sent. Result: real DM outreach collapsed
        # from ~100-225/wk pre-Apr 27 to 0 by mid-May 2026. The chat_url IS NOT
        # NULL filter restores the intended semantics: cool down on actual DM
        # delivery, not on public engagement bookkeeping.
        recent_dm = conn.execute("""
            SELECT
              SUM(CASE WHEN status IN ('sent','pending')
                        AND chat_url IS NOT NULL
                        AND discovered_at >= NOW() - INTERVAL '30 days'
                       THEN 1 ELSE 0 END) AS recent_active,
              SUM(CASE WHEN status IN ('skipped','error')
                        AND COALESCE(skip_reason,'') ILIKE ANY(%s)
                       THEN 1 ELSE 0 END) AS permanent_block
            FROM dms
            WHERE their_author = %s AND platform = %s
        """, (list(PERMANENT_SKIP_REASON_PATTERNS), author, platform)).fetchone()

        if recent_dm and (recent_dm["recent_active"] or 0) > 0:
            reason = "already_dmd_recently"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue
        if recent_dm and (recent_dm["permanent_block"] or 0) > 0:
            reason = "permanently_unreachable_or_disqualified"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue

        # Reject if there's already a pending DM for this (platform, author).
        # The existing ON CONFLICT (platform, their_author, reply_id) only blocks
        # re-inserting the SAME comment. When one author has N matched comments
        # (e.g. Economy_Leopard112 with 7 replies → Terminator on 2026-05-13),
        # the scanner used to queue N separate pending DM rows. If the pipeline
        # ever sent, that person would get N DMs back-to-back. Account-killer.
        existing_pending = conn.execute("""
            SELECT 1 FROM dms
            WHERE platform = %s AND their_author = %s AND status = 'pending'
            LIMIT 1
        """, (platform, author)).fetchone()
        if existing_pending:
            reason = "duplicate_pending_author"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue

        # Build comment context for the DM
        context = f"Thread: {row['thread_title'] or 'N/A'}\n"
        context += f"Their comment: {content}\n"
        context += f"Our reply: {(row['our_reply_content'] or '')}"

        # Pick target_project: inherit from post; fall back to topic match.
        target_project = row["post_project"]
        if not target_project:
            target_project = infer_target_project(
                [row["thread_title"], content, row["our_reply_content"]],
                topic_index,
            )

        if dry_run:
            print(f"  [{platform}] CANDIDATE: {author} (reply #{row['reply_id']}) target={target_project}")
            print(f"    Their comment: {content[:100]}...")
            print(f"    Our reply: {(row['our_reply_content'] or '')[:100]}...")
            print()
            inserted += 1
            continue

        prospect_id = upsert_prospect_row(conn, platform, author)

        # ON CONFLICT DO UPDATE (added 2026-05-13): when a row already exists for
        # this (platform, their_author, reply_id) but its status is a transient
        # error/skipped (twitter_agent_mcp_unavailable, send_unverified,
        # chromium profile locked, etc.), revert it back to status='pending' so
        # the next outreach run picks it up. Non-transient rows (sent, real
        # pending, permanent chat_disabled, disqualified) are left untouched by
        # the WHERE clause. Second prong of the self-heal mechanism, paired
        # with the relaxed LEFT JOIN above.
        conn.execute("""
            INSERT INTO dms (platform, reply_id, post_id, their_author, their_content,
                             comment_context, status, prospect_id, target_project)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
            ON CONFLICT (platform, their_author, reply_id) DO UPDATE
              SET status = 'pending',
                  skip_reason = NULL,
                  claude_session_id = NULL,
                  discovered_at = NOW(),
                  target_project = EXCLUDED.target_project,
                  comment_context = EXCLUDED.comment_context
              WHERE dms.status IN ('error','skipped')
                AND COALESCE(dms.skip_reason,'') ILIKE ANY(%s)
        """, (platform, row["reply_id"], row["post_id"], author, content, context,
              prospect_id, target_project, list(TRANSIENT_SKIP_REASON_PATTERNS)))
        conn.commit()
        inserted += 1
        print(f"  [{platform}] NEW DM candidate: {author} (reply #{row['reply_id']}) "
              f"target={target_project or '-'}: {content[:70]}...")

    if skipped_reasons:
        skip_summary = ", ".join(f"{k}={v}" for k, v in skipped_reasons.items())
        print(f"  [{platform}] Skipped: {skip_summary}")

    return inserted


def _resolve_twitter_handle_for(platform):
    """Same x-only multi-account scoping scan_platform() uses. None elsewhere."""
    if platform != "x":
        return None
    try:
        from twitter_account import resolve_handle as _resolve_twitter_handle
        return _resolve_twitter_handle()
    except Exception:
        return None


def scan_platform_http(config, platform, max_candidates, dry_run, max_age_days=None):
    """DB-free twin of scan_platform().

    The complex discovery JOIN + per-author dedup signals run server-side via
    POST /api/v1/dm-candidates/discover (the transient/permanent ILIKE pattern
    lists, owned here, are sent in the body). The remaining config-driven
    filters (excluded authors, min-word floor, target-project inference) and
    the max-candidates cap stay client-side, identical to the DB path. Inserts
    go through POST /api/v1/prospects + POST /api/v1/dm-candidates.
    """
    from http_api import api_post

    if platform == "twitter":
        platform = "x"
    excluded = get_excluded_authors(config, platform)
    topic_index = build_project_topic_index(config, platform)
    age_days = max_age_days if max_age_days is not None else MAX_AGE_DAYS
    twitter_handle = _resolve_twitter_handle_for(platform)

    resp = api_post(
        "/api/v1/dm-candidates/discover",
        {
            "platform": platform,
            "age_days": age_days,
            "cooldown_hours": POST_REPLY_COOLDOWN_HOURS,
            "twitter_handle": twitter_handle,
            "transient_patterns": list(TRANSIENT_SKIP_REASON_PATTERNS),
            "permanent_patterns": list(PERMANENT_SKIP_REASON_PATTERNS),
            "limit": 2000,
        },
    )
    candidates = (resp.get("data") or {}).get("candidates") or []

    inserted = 0
    skipped_reasons = {}

    for row in candidates:
        if inserted >= max_candidates:
            break

        author = row.get("their_author") or ""
        content = row.get("their_content") or ""

        if author.lower() in excluded:
            reason = "excluded_author"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue

        min_words = MIN_WORDS_BY_PLATFORM.get(platform, MIN_WORDS_DEFAULT)
        if word_count(content) < min_words:
            reason = "too_short"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue

        if (row.get("recent_active") or 0) > 0:
            reason = "already_dmd_recently"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue
        if (row.get("permanent_block") or 0) > 0:
            reason = "permanently_unreachable_or_disqualified"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue
        if (row.get("existing_pending") or 0) > 0:
            reason = "duplicate_pending_author"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue

        context = f"Thread: {row.get('thread_title') or 'N/A'}\n"
        context += f"Their comment: {content}\n"
        context += f"Our reply: {(row.get('our_reply_content') or '')}"

        target_project = row.get("post_project")
        if not target_project:
            target_project = infer_target_project(
                [row.get("thread_title"), content, row.get("our_reply_content")],
                topic_index,
            )

        if dry_run:
            print(f"  [{platform}] CANDIDATE: {author} (reply #{row.get('reply_id')}) target={target_project}")
            print(f"    Their comment: {content[:100]}...")
            print(f"    Our reply: {(row.get('our_reply_content') or '')[:100]}...")
            print()
            inserted += 1
            continue

        prospect = api_post("/api/v1/prospects", {"platform": platform, "author": author})
        prospect_id = ((prospect.get("data") or {}).get("prospect") or {}).get("id")

        api_post(
            "/api/v1/dm-candidates",
            {
                "platform": platform,
                "reply_id": row.get("reply_id"),
                "post_id": row.get("post_id"),
                "their_author": author,
                "their_content": content,
                "comment_context": context,
                "prospect_id": prospect_id,
                "target_project": target_project,
                "transient_patterns": list(TRANSIENT_SKIP_REASON_PATTERNS),
            },
        )
        inserted += 1
        print(f"  [{platform}] NEW DM candidate: {author} (reply #{row.get('reply_id')}) "
              f"target={target_project or '-'}: {content[:70]}...")

    if skipped_reasons:
        skip_summary = ", ".join(f"{k}={v}" for k, v in skipped_reasons.items())
        print(f"  [{platform}] Skipped: {skip_summary}")

    return inserted


def main():
    parser = argparse.ArgumentParser(description="Find users worth DMing based on comment engagement")
    parser.add_argument("--dry-run", action="store_true", help="Print candidates without inserting")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_CANDIDATES, help="Max candidates per platform")
    parser.add_argument("--platform", default="all", choices=PLATFORMS + ["all"],
                        help="Platform to scan (default: all)")
    parser.add_argument("--days", type=int, default=None,
                        help=f"Override MAX_AGE_DAYS (default {MAX_AGE_DAYS}). Use for one-shot backfills after threshold changes.")
    args = parser.parse_args()

    config = load_config()
    dbmod.load_env()

    platforms = PLATFORMS if args.platform == "all" else [args.platform]
    total = 0

    # DB-free lane: no DATABASE_URL -> run the discovery + insert server-side
    # via the s4l.ai HTTP API. DB-equipped machines keep the direct path.
    if not os.environ.get("DATABASE_URL"):
        for platform in platforms:
            print(f"\nScanning {platform} for DM candidates...")
            count = scan_platform_http(config, platform, args.max, args.dry_run, max_age_days=args.days)
            total += count
        action = "found" if args.dry_run else "queued"
        print(f"\nDM scan complete: {total} candidates {action} across {', '.join(platforms)}")
        return total

    conn = dbmod.get_conn()

    for platform in platforms:
        print(f"\nScanning {platform} for DM candidates...")
        count = scan_platform(conn, config, platform, args.max, args.dry_run, max_age_days=args.days)
        total += count

    conn.close()
    action = "found" if args.dry_run else "queued"
    print(f"\nDM scan complete: {total} candidates {action} across {', '.join(platforms)}")
    return total


if __name__ == "__main__":
    count = main()
    sys.exit(0 if count > 0 else 1)
