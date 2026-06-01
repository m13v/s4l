#!/usr/bin/env python3
"""DM conversation tracker - log messages, query history, update state.

This is the central module for all DM conversation tracking. Every DM
interaction (outbound or inbound) should go through here.

Usage:
    # Log an outbound message we sent
    python3 scripts/dm_conversation.py log-outbound --dm-id 5 --content "hey, what stack..."

    # Log an inbound message we received
    python3 scripts/dm_conversation.py log-inbound --dm-id 5 --author tolley --content "I use React..."

    # Show full conversation history for a DM
    python3 scripts/dm_conversation.py history --dm-id 5

    # Show all conversations with pending inbound (needs reply)
    python3 scripts/dm_conversation.py pending

    # Set chat URL for a conversation
    python3 scripts/dm_conversation.py set-url --dm-id 5 --url "https://www.reddit.com/chat/room/..."

    # Update conversation tier
    python3 scripts/dm_conversation.py set-tier --dm-id 5 --tier 2

    # Mark conversation status
    python3 scripts/dm_conversation.py set-status --dm-id 5 --status converted

    # Find DM by author name (fuzzy)
    python3 scripts/dm_conversation.py find --author tolley

    # Summary of all active conversations
    python3 scripts/dm_conversation.py summary
"""

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")
GMAIL_TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
GMAIL_SCOPES = ["https://mail.google.com/"]


def _valid_chat_url(platform, url):
    """Return a cleaned chat_url or None.

    The dashboard only treats it as an "open chat" link when it looks like a
    real DM thread URL. Post URLs / profile URLs silently leak in when the
    prompt passes the wrong variable, so we reject anything that is not the
    per-platform DM-thread shape.
    """
    if not url:
        return None
    u = url.strip()
    if not u:
        return None
    p = (platform or "").lower()
    if p == "reddit":
        if "/chat/room/" in u or "/message/messages/" in u:
            return u
        if "/room/!" in u and "/chat/room/!" not in u:
            return u.replace("/room/!", "/chat/room/!", 1)
        return None
    if p in ("twitter", "x"):
        if "/i/chat/" in u or "/messages/" in u:
            return u
        return None
    if p == "linkedin":
        if "/messaging/thread/" in u:
            return u
        return None
    return u


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def get_our_account(config, platform):
    accounts = config.get("accounts", {})
    if platform == "reddit":
        return accounts.get("reddit", {}).get("username", "Deep_Ad1959")
    elif platform == "linkedin":
        return accounts.get("linkedin", {}).get("name", "Matthew Diakonov")
    elif platform == "x":
        return accounts.get("twitter", {}).get("handle", "@m13v_").lstrip("@")
    return "unknown"


def log_outbound(conn, dm_id, content, author=None, verified=False):
    """Log a message we sent. Includes dedup guard to prevent double-sending.

    --verified is REQUIRED. Without it the function refuses to insert.
    This is the gate against the LLM-driven "fabricated send" bug
    (see April 2026 incident: Haiku-fired LinkedIn DM cycle inserted 5
    phantom outbound rows into dm_messages while dms.status stayed
    pending because dm_send_log.py blocked the dms UPDATE). The browser
    send_dm/compose_dm tool must return verified=true; only then may
    the caller pass --verified.

    Additional DB-enforced gate: for the FIRST outbound (no prior
    outbound rows for this dm_id), dms.status must already be 'sent'.
    Only dm_send_log.py --verified flips that, so this forces the
    canonical path on first messages even if the LLM tries to skip it.
    """
    row = conn.execute(
        "SELECT platform, their_author, message_count, qualification_status, icp_matches, status "
        "FROM dms WHERE id = %s",
        (dm_id,),
    ).fetchone()
    if not row:
        print(f"ERROR: DM #{dm_id} not found")
        return False

    if not verified:
        print(
            f"  VERIFY BLOCKED: refusing to log outbound to DM #{dm_id} without --verified.\n"
            "  The browser send_dm/compose_dm tool must return verified=true first.\n"
            "  Pass --verified only after a real send. If verification failed, "
            "log nothing and let the next cycle retry."
        )
        return False

    prior_outbound = conn.execute(
        "SELECT 1 FROM dm_messages WHERE dm_id = %s AND direction = 'outbound' LIMIT 1",
        (dm_id,),
    ).fetchone()
    if not prior_outbound and (row.get("status") or "pending") != "sent":
        print(
            f"  STATUS BLOCKED: DM #{dm_id} status is '{row.get('status')}', not 'sent', "
            "and there is no prior outbound row.\n"
            "  Run scripts/dm_send_log.py --verified first; that script flips "
            "dms.status to 'sent' and forwards to log-outbound."
        )
        return False

    # Bare-URL guard: real replies contain prose. A content string that is
    # nothing but a URL is almost always a scraper misattribution being
    # reconciled by the agent (see DM #1486 / session d986d23e where the
    # twitter browser layer surfaced a profile card URL as "is_from_us=true").
    stripped = (content or "").strip()
    if re.match(r'^https?://\S+$', stripped):
        print(f"  BAREURL BLOCKED: DM #{dm_id} content is a lone URL with no prose ({stripped[:100]}).")
        print(f"  This is usually a scraper misattribution. If the URL really was sent, add a sentence of context before logging.")
        return False

    # Link-wrap guard (LinkedIn safety net): every URL we send through the DM
    # pipeline must be a wrapped /r/<code> short link OR a third-party URL the
    # classifier maps to kind='other'. reddit_browser/twitter_browser enforce
    # this in-tool before typing; LinkedIn types via MCP browser without a
    # Python pre-pass, so this is the final gate. If an unwrapped project URL
    # slips through, refuse to log — the next cycle will resurface the thread
    # and the model retypes a wrapped version. Catches both schemed
    # (https://github.com/...) and bare-domain (github.com/...) references.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from dm_short_links import _classify_url, _load_projects, _URL_RE, _TRAILING_PUNCT
        _wrap_check_projects = _load_projects()
        for m in _URL_RE.finditer(content or ""):
            raw_url = m.group(0).rstrip(_TRAILING_PUNCT)
            # Allow already-wrapped /r/<code> short links on any of our domains
            if re.search(r'/r/[a-z0-9]{4,32}(?:[/?#]|$)', raw_url, re.IGNORECASE):
                continue
            kind, matched = _classify_url(raw_url, _wrap_check_projects)
            if kind != 'other':
                # An unwrapped URL pointing at one of our projects.
                print(f"  LINK BLOCKED: DM #{dm_id} content contains unwrapped {kind} URL "
                      f"({raw_url[:80]}) for project {matched!r}. Re-send via the wrap-text "
                      f"helper (python3 scripts/dm_short_links.py wrap-text --dm-id {dm_id} "
                      f"--text '...').")
                return False
    except Exception as _wrap_err:
        # Don't block on classifier import errors; surface and continue.
        print(f"  WARNING: link-wrap guard skipped due to error: {_wrap_err}", file=sys.stderr)

    # Timeline gate: the Phase D prompt requires that by the 4th message in
    # a thread, qualification_status is not 'pending' (either 'asked',
    # 'answered', 'qualified', or 'disqualified'), and Step 2.4 has rescored
    # ICP so icp_matches is non-empty. If we are about to send message 4+
    # with neither done, the agent skipped both gates. Refuse and force a
    # rerun instead of letting the thread drift further into rapport-only.
    cur_count = (row.get("message_count") or 0)
    qual_status = (row.get("qualification_status") or "pending")
    icp_list = row.get("icp_matches") or []
    if cur_count >= 3 and qual_status == "pending" and not icp_list:
        print(f"  TIMELINE BLOCKED: DM #{dm_id} is at msg {cur_count} with qualification_status=pending and empty icp_matches.")
        print(f"  Run Step 2.4 (set-icp-precheck for every project in $PROJECTS) before logging this outbound.")
        print(f"  If nothing in $PROJECTS plausibly fits this prospect, call set-qualification --status disqualified --notes 'reason' and retry.")
        return False

    # Dedup guard: check if last message is already outbound (no inbound since our last reply)
    last_msg = conn.execute("""
        SELECT direction, content, message_at FROM dm_messages
        WHERE dm_id = %s ORDER BY message_at DESC LIMIT 1
    """, (dm_id,)).fetchone()

    if last_msg and last_msg["direction"] == "outbound":
        hours_ago = (datetime.now() - last_msg["message_at"]).total_seconds() / 3600
        print(f"  DEDUP BLOCKED: Last message to {row['their_author']} (DM #{dm_id}) was already outbound ({hours_ago:.1f}h ago). Skipping.")
        return False

    config = load_config()
    if not author:
        author = get_our_account(config, row["platform"])

    claude_session_id = os.environ.get("CLAUDE_SESSION_ID") or None

    inserted = conn.execute("""
        INSERT INTO dm_messages (dm_id, direction, author, content, message_at, logged_at, claude_session_id)
        VALUES (%s, 'outbound', %s, %s, NOW(), NOW(), %s)
        RETURNING id
    """, (dm_id, author, content, claude_session_id)).fetchone()
    new_msg_id = inserted["id"] if inserted else None

    # Backfill dm_links.message_id for codes the calling browser tool minted in
    # this turn. Set via WRAP_MINTED_CODES env var (comma-separated list) by
    # _log_dm_outbound / _log_twitter_dm_outbound. Unknown/stale codes are
    # silently ignored — the join in the dashboard tolerates NULL message_id.
    if new_msg_id:
        wrap_codes = (os.environ.get("WRAP_MINTED_CODES") or "").strip()
        if wrap_codes:
            codes = [c.strip() for c in wrap_codes.split(",") if c.strip()]
            if codes:
                try:
                    conn.execute(
                        "UPDATE dm_links SET message_id = %s "
                        "WHERE dm_id = %s AND code = ANY(%s) AND message_id IS NULL",
                        (new_msg_id, dm_id, codes),
                    )
                except Exception as _link_err:
                    print(f"  WARNING: dm_links.message_id backfill failed: {_link_err}",
                          file=sys.stderr)

    conn.execute("""
        UPDATE dms SET last_message_at = NOW(), message_count = message_count + 1,
                       conversation_status = 'active',
                       claude_session_id = COALESCE(%s, claude_session_id)
        WHERE id = %s
    """, (claude_session_id, dm_id))
    conn.commit()

    # Auto-attribute to any active Reddit campaign whose literal suffix matches
    # the tail of `content`. The LLM never passes campaign IDs; we detect from
    # the actual stored text. This works because reddit_browser.py send_dm
    # appends the suffix at the tool layer before the message is typed, so
    # whatever is stored here mirrors what was actually delivered.
    if new_msg_id and (row.get("platform") == "reddit"):
        try:
            cur = conn.execute(
                """SELECT id, suffix FROM campaigns
                   WHERE status='active'
                     AND (',' || platforms || ',') LIKE '%,reddit,%'
                     AND max_posts_total IS NOT NULL
                     AND posts_made < max_posts_total
                     AND suffix IS NOT NULL AND suffix <> ''"""
            )
            for camp_row in cur.fetchall():
                cid = camp_row["id"]
                cs = camp_row["suffix"]
                if content.endswith(cs):
                    conn.execute(
                        "UPDATE dm_messages SET campaign_id = %s WHERE id = %s AND campaign_id IS NULL",
                        (cid, new_msg_id),
                    )
                    conn.execute(
                        "UPDATE campaigns SET posts_made = posts_made + 1, updated_at = NOW() "
                        "WHERE id = %s",
                        (cid,),
                    )
            conn.commit()
        except Exception as e:
            print(f"  WARNING: campaign suffix auto-attribution failed: {e}")

    print(f"  Logged outbound to {row['their_author']} (DM #{dm_id})")
    return True


def ensure_dm(conn, platform, author, chat_url=None, lookback_hours=720):
    """Return the dm_id for (platform, author), creating the row if missing.

    On insert, backfill reply_id + post_id from the most recent matching
    replies row so the dashboard (and any other join-based tooling) has
    the public-engagement chain linked. Without this, inbound-first DMs
    get stored with both FKs NULL and the operator has to fall back to
    the dashboard's fuzzy-match renderer. The lookback window prevents
    linking to an unrelated reply from months earlier, which would be
    misleading for a cold DM that happens to share an author name with
    some ancient thread engagement.
    """
    clean_chat_url = _valid_chat_url(platform, chat_url)
    if chat_url and not clean_chat_url:
        print(f"  WARN: ignoring non-DM chat_url for {platform}/@{author}: {chat_url[:120]}",
              file=sys.stderr)
    existing = conn.execute(
        "SELECT id FROM dms WHERE platform = %s AND their_author = %s ORDER BY id DESC LIMIT 1",
        (platform, author),
    ).fetchone()
    if existing:
        if clean_chat_url:
            conn.execute(
                "UPDATE dms SET chat_url = COALESCE(chat_url, %s) WHERE id = %s",
                (clean_chat_url, existing["id"]),
            )
            conn.commit()
        return existing["id"], False, None

    match = conn.execute(
        """
        SELECT id, post_id, their_content, their_comment_url, our_reply_content
        FROM replies
        WHERE platform = %s AND their_author = %s
          AND discovered_at >= NOW() - (%s || ' hours')::interval
        ORDER BY discovered_at DESC
        LIMIT 1
        """,
        (platform, author, str(lookback_hours)),
    ).fetchone()

    reply_id = match["id"] if match else None
    post_id = match["post_id"] if match else None
    comment_ctx = None
    if match and match.get("their_content"):
        comment_ctx = match["their_content"]

    # No real DM channel + we already replied publicly = this row is a
    # public-comment artifact, not an actionable DM. Mark it 'public_only' so
    # the engage-dm-replies queue ignores it from day one. The row still exists
    # for cross-thread history (engage_reddit.py soft context block).
    has_public_reply = bool(
        match and (match.get("our_reply_content") or "").strip()
    )
    initial_status = (
        "public_only"
        if (clean_chat_url is None and has_public_reply)
        else "active"
    )

    row = conn.execute(
        """
        INSERT INTO dms (platform, their_author, reply_id, post_id,
                         comment_context, chat_url, status, conversation_status,
                         tier, discovered_at, sent_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'sent', %s, 1, NOW(), NOW())
        RETURNING id
        """,
        (platform, author, reply_id, post_id, comment_ctx, clean_chat_url, initial_status),
    ).fetchone()
    conn.commit()
    return row["id"], True, reply_id


def log_inbound(conn, dm_id, author, content, message_at=None, event_id=None):
    """Log a message we received.

    event_id, when provided, is a platform-native globally-unique message id
    (Matrix `$...` event_ids from Reddit Chat). If supplied, dedup is against
    event_id (perfect key, UNIQUE index backs it). Otherwise we fall back to
    the content-match guard that catches cron-re-ingestion of the same text.
    """
    row = conn.execute("SELECT platform, their_author FROM dms WHERE id = %s", (dm_id,)).fetchone()
    if not row:
        print(f"ERROR: DM #{dm_id} not found")
        return False

    # Idempotency guard: the DM-replies cron re-reads each chat every run, so
    # without a check the same inbound gets inserted on every firing. Prefer
    # the platform event_id when we have one (perfect key); fall back to
    # content match otherwise.
    if event_id:
        dup = conn.execute(
            "SELECT id, message_at FROM dm_messages WHERE event_id = %s LIMIT 1",
            (event_id,),
        ).fetchone()
    else:
        dup = conn.execute("""
            SELECT id, message_at FROM dm_messages
            WHERE dm_id = %s AND direction = 'inbound' AND author = %s AND content = %s
            ORDER BY message_at ASC LIMIT 1
        """, (dm_id, author, content)).fetchone()
    if dup:
        key = f"event_id={event_id}" if event_id else f"content match"
        print(f"  DEDUP BLOCKED: Inbound from {author} (DM #{dm_id}) already logged as msg #{dup['id']} at {dup['message_at']} ({key}). Skipping.")
        return False

    if message_at:
        conn.execute("""
            INSERT INTO dm_messages (dm_id, direction, author, content, message_at, logged_at, event_id)
            VALUES (%s, 'inbound', %s, %s, %s, NOW(), %s)
        """, (dm_id, author, content, message_at, event_id))
    else:
        conn.execute("""
            INSERT INTO dm_messages (dm_id, direction, author, content, message_at, logged_at, event_id)
            VALUES (%s, 'inbound', %s, %s, NOW(), NOW(), %s)
        """, (dm_id, author, content, event_id))

    conn.execute("""
        UPDATE dms SET last_message_at = NOW(), message_count = message_count + 1,
                       conversation_status = CASE
                           WHEN conversation_status IN ('needs_human','converted','closed','public_only') THEN conversation_status
                           ELSE 'needs_reply'
                       END,
                       snoozed_until = NULL
        WHERE id = %s
    """, (dm_id,))
    conn.commit()
    print(f"  Logged inbound from {author} (DM #{dm_id})")
    return True


def show_history(conn, dm_id):
    """Print full conversation history."""
    dm = conn.execute("""
        SELECT d.id, d.platform, d.their_author, d.chat_url, d.conversation_status,
               d.tier, d.message_count, d.comment_context
        FROM dms d WHERE d.id = %s
    """, (dm_id,)).fetchone()

    if not dm:
        print(f"DM #{dm_id} not found")
        return

    print(f"=== DM #{dm['id']} with {dm['their_author']} [{dm['platform']}] ===")
    print(f"Status: {dm['conversation_status']}  Tier: {dm['tier']}  Messages: {dm['message_count']}")
    if dm['chat_url']:
        print(f"Chat URL: {dm['chat_url']}")
    if dm['comment_context']:
        print(f"Original context: {dm['comment_context'][:200]}...")
    print()

    messages = conn.execute("""
        SELECT direction, author, content, message_at
        FROM dm_messages WHERE dm_id = %s ORDER BY message_at ASC
    """, (dm_id,)).fetchall()

    for msg in messages:
        arrow = ">>" if msg["direction"] == "outbound" else "<<"
        ts = msg["message_at"].strftime("%Y-%m-%d %H:%M") if msg["message_at"] else "?"
        print(f"  {arrow} [{ts}] {msg['author']}: {msg['content']}")
    print()


def show_pending(conn):
    """Show conversations that have inbound messages we haven't replied to."""
    rows = conn.execute("""
        SELECT d.id, d.platform, d.their_author, d.chat_url, d.tier,
               d.message_count, d.last_message_at,
               (SELECT content FROM dm_messages
                WHERE dm_id = d.id ORDER BY message_at DESC LIMIT 1) as last_msg,
               (SELECT direction FROM dm_messages
                WHERE dm_id = d.id ORDER BY message_at DESC LIMIT 1) as last_dir
        FROM dms d
        WHERE d.conversation_status = 'needs_reply'
          OR (d.status = 'sent' AND d.conversation_status = 'active'
              AND EXISTS (
                SELECT 1 FROM dm_messages m
                WHERE m.dm_id = d.id AND m.direction = 'inbound'
                AND m.message_at > COALESCE(
                    (SELECT MAX(m2.message_at) FROM dm_messages m2
                     WHERE m2.dm_id = d.id AND m2.direction = 'outbound'), '1970-01-01')
              ))
        ORDER BY d.last_message_at DESC
    """).fetchall()

    if not rows:
        print("No conversations needing reply.")
        return

    print(f"=== {len(rows)} conversations need reply ===\n")
    for r in rows:
        tier_label = f"T{r['tier']}" if r['tier'] else "T1"
        ts = r['last_message_at'].strftime("%m/%d %H:%M") if r['last_message_at'] else "?"
        last = (r['last_msg'] or "")[:100]
        print(f"  DM #{r['id']} [{r['platform']}] {r['their_author']} ({tier_label}, {r['message_count']} msgs, last: {ts})")
        print(f"    Last: {last}")
        if r['chat_url']:
            print(f"    URL: {r['chat_url']}")
        print()


def find_by_author(conn, author_query):
    """Find DM conversations by author name (case-insensitive partial match)."""
    rows = conn.execute("""
        SELECT d.id, d.platform, d.their_author, d.status, d.conversation_status,
               d.tier, d.message_count, d.chat_url, d.last_message_at
        FROM dms d
        WHERE LOWER(d.their_author) LIKE LOWER(%s)
        ORDER BY d.last_message_at DESC NULLS LAST
    """, (f"%{author_query}%",)).fetchall()

    if not rows:
        print(f"No DMs found matching '{author_query}'")
        return

    for r in rows:
        ts = r['last_message_at'].strftime("%m/%d %H:%M") if r['last_message_at'] else "never"
        print(f"  DM #{r['id']} [{r['platform']}] {r['their_author']} - {r['status']}/{r['conversation_status']} T{r['tier'] or 1} ({r['message_count']} msgs, last: {ts})")
        if r['chat_url']:
            print(f"    URL: {r['chat_url']}")


def show_summary(conn):
    """Summary of all DM conversations."""
    stats = conn.execute("""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status = 'sent') as sent,
            COUNT(*) FILTER (WHERE status = 'skipped') as skipped,
            COUNT(*) FILTER (WHERE conversation_status = 'needs_reply') as needs_reply,
            COUNT(*) FILTER (WHERE conversation_status = 'active') as active,
            COUNT(*) FILTER (WHERE conversation_status = 'converted') as converted,
            COUNT(*) FILTER (WHERE conversation_status = 'stale') as stale,
            COUNT(*) FILTER (WHERE tier = 2) as tier2,
            COUNT(*) FILTER (WHERE tier = 3) as tier3,
            COUNT(DISTINCT their_author) as unique_authors
        FROM dms
    """).fetchone()

    msg_stats = conn.execute("""
        SELECT
            COUNT(*) as total_messages,
            COUNT(*) FILTER (WHERE direction = 'outbound') as outbound,
            COUNT(*) FILTER (WHERE direction = 'inbound') as inbound
        FROM dm_messages
    """).fetchone()

    # Conversations with replies
    with_replies = conn.execute("""
        SELECT COUNT(DISTINCT dm_id) FROM dm_messages WHERE direction = 'inbound'
    """).fetchone()

    print("=== DM Pipeline Summary ===")
    print(f"  Conversations: {stats['total']} total ({stats['sent']} sent, {stats['skipped']} skipped)")
    print(f"  Unique authors: {stats['unique_authors']}")
    print(f"  Status: {stats['needs_reply']} needs_reply, {stats['active']} active, {stats['converted']} converted, {stats['stale']} stale")
    print(f"  Tiers: {stats['tier2']} at T2, {stats['tier3']} at T3")
    print(f"  Messages: {msg_stats['total_messages']} total ({msg_stats['outbound']} outbound, {msg_stats['inbound']} inbound)")
    print(f"  Reply rate: {with_replies[0]}/{stats['sent']} conversations have inbound replies")
    print()

    # Top active conversations (most messages)
    top = conn.execute("""
        SELECT d.id, d.their_author, d.platform, d.message_count, d.tier,
               d.conversation_status, d.last_message_at
        FROM dms d
        WHERE d.message_count > 1
        ORDER BY d.message_count DESC, d.last_message_at DESC
        LIMIT 10
    """).fetchall()

    if top:
        print("  Top conversations by message count:")
        for r in top:
            ts = r['last_message_at'].strftime("%m/%d %H:%M") if r['last_message_at'] else "?"
            print(f"    DM #{r['id']} {r['their_author']} [{r['platform']}] - {r['message_count']} msgs, T{r['tier'] or 1}, {r['conversation_status']} (last: {ts})")


def _gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _scrub_dashes(s):
    """Replace em/en dashes with commas. Em dashes in email subjects cause
    UTF-8 garbling in some clients, and the user has a no-dashes preference."""
    if not s:
        return s
    return s.replace("\u2014", ",").replace("\u2013", ",")


def _fetch_upstream_context(conn, dm_id):
    """Pull the public-thread chain that preceded this DM.

    Mirrors the COALESCE strategy used by bin/server.js /api/top/dms so the
    email matches what the dashboard expansion shows: thread post, our
    top-level comment, the prospect's comment that triggered outreach, and
    our public reply to that comment. Each leg is rendered only when data
    exists, so cold DMs with no public-engagement trail print a short
    "(no upstream public exchange logged)" stub.
    """
    return conn.execute(
        """
        SELECT
            COALESCE(p_direct.thread_title,   p_via_reply.thread_title,   p_via_fb.thread_title)   AS thread_title,
            COALESCE(p_direct.thread_url,     p_via_reply.thread_url,     p_via_fb.thread_url)     AS thread_url,
            COALESCE(p_direct.thread_content, p_via_reply.thread_content, p_via_fb.thread_content) AS thread_content,
            COALESCE(p_direct.thread_author,  p_via_reply.thread_author,  p_via_fb.thread_author)  AS thread_author,
            COALESCE(p_direct.our_content,    p_via_reply.our_content,    p_via_fb.our_content)    AS our_top_content,
            COALESCE(p_direct.our_url,        p_via_reply.our_url,        p_via_fb.our_url)        AS our_top_url,
            COALESCE(p_direct.our_account,    p_via_reply.our_account,    p_via_fb.our_account)    AS our_top_account,
            COALESCE(p_direct.posted_at,      p_via_reply.posted_at,      p_via_fb.posted_at)      AS our_top_posted_at,
            COALESCE(r_link.their_content,     r_fallback.their_content)     AS their_comment_content,
            COALESCE(r_link.their_comment_url, r_fallback.their_comment_url) AS their_comment_url,
            COALESCE(r_link.their_author,      r_fallback.their_author)      AS their_comment_author,
            COALESCE(r_link.our_reply_content, r_fallback.our_reply_content) AS our_reply_content,
            COALESCE(r_link.our_reply_url,     r_fallback.our_reply_url)     AS our_reply_url,
            COALESCE(r_link.replied_at,        r_fallback.replied_at)        AS our_reply_at,
            CASE WHEN r_fallback.id IS NOT NULL AND d.reply_id IS NULL AND d.post_id IS NULL THEN TRUE ELSE FALSE END AS is_fallback
        FROM dms d
        LEFT JOIN posts   p_direct    ON p_direct.id    = d.post_id
        LEFT JOIN replies r_link      ON r_link.id      = d.reply_id
        LEFT JOIN posts   p_via_reply ON p_via_reply.id = r_link.post_id
        LEFT JOIN LATERAL (
            SELECT r2.* FROM replies r2
            WHERE d.reply_id IS NULL AND d.post_id IS NULL
              AND r2.platform = d.platform AND r2.their_author = d.their_author
            ORDER BY r2.discovered_at DESC LIMIT 1
        ) r_fallback ON TRUE
        LEFT JOIN posts   p_via_fb    ON p_via_fb.id    = r_fallback.post_id
        WHERE d.id = %s
        """,
        (dm_id,),
    ).fetchone()


def _render_upstream_card(label, lines):
    """Render one card as a labeled plain-text block.

    Cards are separated by a divider so the human can scan the public chain
    distinctly from the DM tracker. Empty cards return ''.
    """
    body = "\n".join(line for line in lines if line)
    if not body.strip():
        return ""
    return f"--- {label} ---\n{body}\n"


def _ts(dt_value):
    if not dt_value:
        return ""
    try:
        return dt_value.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt_value)


def _send_escalation_email(conn, dm_id, platform, their_author, reason):
    """Send an escalation email with conversation history.

    Sends from i@m13v.com to NOTIFICATION_EMAIL (defaults to i@m13v.com).
    Subject embeds [DM #N] so ingest can match the reply back to this thread.

    The body renders the full chain as distinct cards: the public thread,
    our top-level post/comment, their public comment that triggered
    outreach, our public reply, the DM-promotion exchange, and the actual
    DM thread (if a private chat_url exists). This mirrors the dashboard
    expansion view so the human sees the same context in either surface.
    """
    to_email = os.environ.get("NOTIFICATION_EMAIL", "i@m13v.com")

    dm = conn.execute("""
        SELECT d.id, d.tier, d.chat_url, d.project_name, d.target_project,
               d.human_reason, d.flagged_at, d.comment_context, d.our_dm_content
        FROM dms d WHERE d.id = %s
    """, (dm_id,)).fetchone()

    messages = conn.execute("""
        SELECT direction, author, content, message_at
        FROM dm_messages WHERE dm_id = %s ORDER BY message_at ASC
    """, (dm_id,)).fetchall()

    upstream = _fetch_upstream_context(conn, dm_id) or {}

    # --- Card: public thread (the post) ------------------------------------
    thread_lines = []
    if upstream.get("thread_title"):
        thread_lines.append(f"Title: {upstream['thread_title']}")
    if upstream.get("thread_author"):
        thread_lines.append(f"OP: @{upstream['thread_author']}")
    if upstream.get("thread_url"):
        thread_lines.append(f"URL: {upstream['thread_url']}")
    if upstream.get("thread_content"):
        thread_lines.append(f"Body: {upstream['thread_content']}")
    thread_card = _render_upstream_card(
        "PUBLIC THREAD" + (" (inferred via author match)" if upstream.get("is_fallback") else ""),
        thread_lines,
    )

    # --- Card: our top-level public reply / comment ------------------------
    our_top_lines = []
    if upstream.get("our_top_account"):
        our_top_lines.append(f"From: @{upstream['our_top_account']}")
    if upstream.get("our_top_posted_at"):
        our_top_lines.append(f"Posted: {_ts(upstream['our_top_posted_at'])}")
    if upstream.get("our_top_url"):
        our_top_lines.append(f"URL: {upstream['our_top_url']}")
    if upstream.get("our_top_content"):
        our_top_lines.append(f"Content: {upstream['our_top_content']}")
    our_top_card = _render_upstream_card("OUR TOP-LEVEL PUBLIC COMMENT", our_top_lines)

    # --- Card: their public comment that triggered outreach ----------------
    their_comment_lines = []
    if upstream.get("their_comment_author"):
        their_comment_lines.append(f"From: @{upstream['their_comment_author']}")
    if upstream.get("their_comment_url"):
        their_comment_lines.append(f"URL: {upstream['their_comment_url']}")
    if upstream.get("their_comment_content"):
        their_comment_lines.append(f"Content: {upstream['their_comment_content']}")
    their_comment_card = _render_upstream_card(
        f"THEIR PUBLIC COMMENT (@{their_author})",
        their_comment_lines,
    )

    # --- Card: our public reply to their comment ---------------------------
    our_reply_lines = []
    if upstream.get("our_reply_at"):
        our_reply_lines.append(f"Replied: {_ts(upstream['our_reply_at'])}")
    if upstream.get("our_reply_url"):
        our_reply_lines.append(f"URL: {upstream['our_reply_url']}")
    if upstream.get("our_reply_content"):
        our_reply_lines.append(f"Content: {upstream['our_reply_content']}")
    our_reply_card = _render_upstream_card("OUR PUBLIC REPLY TO THEIR COMMENT", our_reply_lines)

    # --- Card: the DM tracker thread ---------------------------------------
    # If chat_url is set this is an actual private DM. Otherwise dm_messages
    # is tracking a continued *public* reply chain that we treated as a DM
    # for funnel purposes. Label the card accordingly so the human knows
    # whether they need to initiate a private chat or just keep replying.
    has_chat_url = bool(dm and dm.get("chat_url"))
    dm_label = (
        "PRIVATE DM THREAD"
        if has_chat_url
        else f"DM-PROMOTION PUBLIC THREAD (no private chat opened yet, chat_url=NULL)"
    )
    if messages:
        dm_lines = []
        for m in messages:
            arrow = ">>" if m["direction"] == "outbound" else "<<"
            ts = _ts(m["message_at"]) or "?"
            dm_lines.append(f"  {arrow} [{ts}] {m['author']}: {m['content']}")
        dm_card_body = "\n".join(dm_lines)
    else:
        # Fall back to the seed `our_dm_content` if no dm_messages rows exist
        seed_lines = []
        if dm and dm.get("our_dm_content"):
            seed_lines.append(f"  >> (seed) us: {dm['our_dm_content']}")
        if dm and dm.get("comment_context") and not seed_lines:
            seed_lines.append(f"  context: {dm['comment_context']}")
        dm_card_body = "\n".join(seed_lines) if seed_lines else "(no messages logged)"

    # --- Assemble body -----------------------------------------------------
    project = (dm.get("target_project") or dm.get("project_name") or "unset") if dm else "unset"
    tier = (dm.get("tier") if dm else None) or 1
    chat_url_line = f"Chat URL: {dm['chat_url']}\n" if has_chat_url else "Chat URL: (none, private chat not yet initiated)\n"

    upstream_block = "".join(c for c in (thread_card, our_top_card, their_comment_card, our_reply_card) if c)
    if not upstream_block:
        upstream_block = "--- PUBLIC THREAD ---\n(no upstream public exchange logged for this DM)\n"

    body = (
        f"DM #{dm_id} [{platform}] with {their_author} needs your attention.\n\n"
        f"Reason: {reason}\n"
        f"Tier: {tier}   Project: {project}\n"
        f"{chat_url_line}\n"
        f"=== Public exchange that preceded this DM ===\n"
        f"(Each block is a distinct card matching the dashboard expansion view.)\n\n"
        f"{upstream_block}\n"
        f"=== {dm_label} ===\n{dm_card_body}\n\n"
        f"---\n"
        f"Reply to this email to respond. Your reply will be sent as a DM on {platform}.\n"
        f"Keep the [DM #{dm_id}] token in the subject line so the pipeline can route it.\n"
    )

    subject = _scrub_dashes(f"[DM #{dm_id}] {their_author} [{platform}]: {(reason or '')}")
    body = _scrub_dashes(body)

    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = to_email
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    try:
        service = _gmail_service()
        result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        gmail_id = result.get("id", "")
        print(f"  Escalation email sent for DM #{dm_id} to {to_email} (gmail id: {gmail_id})")
        return gmail_id
    except Exception as e:
        print(f"  WARNING: Failed to send escalation email for DM #{dm_id}: {e}")
        return None


def flag_human(conn, dm_id, reason):
    """Flag a conversation as needing human attention and send escalation email.

    Guard: if the most recent message in the thread is outbound, we have
    already replied (probably via Phase 0 of engage-dm-replies.sh after the
    human gave instructions). Re-flagging in that state pins threads at
    needs_human even though the ball is in the prospect's court. Skip
    re-flagging until a fresh inbound arrives.
    """
    row = conn.execute("SELECT platform, their_author, conversation_status FROM dms WHERE id = %s", (dm_id,)).fetchone()
    if not row:
        print(f"ERROR: DM #{dm_id} not found")
        return False

    last_msg = conn.execute("""
        SELECT direction FROM dm_messages
        WHERE dm_id = %s ORDER BY message_at DESC LIMIT 1
    """, (dm_id,)).fetchone()
    if last_msg and last_msg["direction"] == "outbound":
        print(f"  SKIP flag-human: DM #{dm_id} ({row['their_author']} [{row['platform']}]) last message is OUTBOUND. We already replied; ball is in their court. Reason was: {reason}")
        return False

    conn.execute("""
        UPDATE dms SET conversation_status = 'needs_human', human_reason = %s, flagged_at = NOW()
        WHERE id = %s
    """, (reason, dm_id))
    conn.commit()
    print(f"  FLAGGED DM #{dm_id} ({row['their_author']} [{row['platform']}]) for human attention: {reason}")

    _send_escalation_email(conn, dm_id, row['platform'], row['their_author'], reason)
    return True


def show_flagged(conn):
    """Show all conversations flagged for human attention."""
    rows = conn.execute("""
        SELECT d.id, d.platform, d.their_author, d.tier, d.human_reason, d.flagged_at,
               d.chat_url, d.message_count,
               (SELECT content FROM dm_messages WHERE dm_id = d.id ORDER BY message_at DESC LIMIT 1) as last_msg,
               (SELECT direction FROM dm_messages WHERE dm_id = d.id ORDER BY message_at DESC LIMIT 1) as last_dir
        FROM dms d
        WHERE d.conversation_status = 'needs_human'
        ORDER BY d.flagged_at DESC
    """).fetchall()

    if not rows:
        print("No conversations flagged for human attention.")
        return

    print(f"=== {len(rows)} conversations need HUMAN attention ===\n")
    for r in rows:
        ts = r['flagged_at'].strftime("%m/%d %H:%M") if r['flagged_at'] else "?"
        last = (r['last_msg'] or "")[:150]
        print(f"  DM #{r['id']} [{r['platform']}] {r['their_author']} (T{r['tier'] or 1}, {r['message_count']} msgs)")
        print(f"    REASON: {r['human_reason']}")
        print(f"    Flagged: {ts}")
        print(f"    Last msg ({r['last_dir']}): {last}")
        if r['chat_url']:
            print(f"    URL: {r['chat_url']}")
        print()


def set_chat_url(conn, dm_id, url):
    row = conn.execute("SELECT platform FROM dms WHERE id = %s", (dm_id,)).fetchone()
    if not row:
        print(f"  ERROR: DM #{dm_id} not found")
        return
    clean = _valid_chat_url(row["platform"], url)
    if url and not clean:
        print(f"  ERROR: '{url[:120]}' is not a valid {row['platform']} DM-thread URL; refusing to save.")
        print(f"         Expected shapes: reddit=/chat/room/!..., x=/i/chat/..., linkedin=/messaging/thread/...")
        sys.exit(2)
    conn.execute("UPDATE dms SET chat_url = %s WHERE id = %s", (clean, dm_id))
    conn.commit()
    print(f"  Set chat_url for DM #{dm_id}")


def backfill_urls(conn, platform, records):
    """Stamp chat_url onto existing orphan dms rows from a bulk scan.

    records: iterable of dicts with at least {"author": str, "chat_url"|"thread_url": str}.
    Only rows where chat_url is currently NULL are updated. Invalid URLs
    (post permalinks, profile URLs) are skipped by the validator.
    """
    stats = {"updated": 0, "skipped_invalid": 0, "skipped_already_set": 0, "no_match": 0, "ambiguous": 0}
    for rec in records:
        author = (rec.get("author") or rec.get("handle") or "").strip()
        raw = rec.get("chat_url") or rec.get("thread_url") or ""
        if not author:
            continue
        clean = _valid_chat_url(platform, raw)
        if not clean:
            stats["skipped_invalid"] += 1
            continue
        matches = conn.execute(
            "SELECT id, chat_url FROM dms WHERE platform = %s AND LOWER(their_author) = LOWER(%s)",
            (platform, author),
        ).fetchall()
        if not matches:
            stats["no_match"] += 1
            continue
        target = None
        for m in matches:
            if not m["chat_url"]:
                if target is not None:
                    target = "ambiguous"
                    break
                target = m["id"]
        if target == "ambiguous":
            stats["ambiguous"] += 1
            continue
        if target is None:
            stats["skipped_already_set"] += 1
            continue
        conn.execute("UPDATE dms SET chat_url = %s WHERE id = %s AND chat_url IS NULL",
                     (clean, target))
        conn.commit()
        stats["updated"] += 1
    print(f"  backfill-urls [{platform}]: updated={stats['updated']} "
          f"already_set={stats['skipped_already_set']} no_match={stats['no_match']} "
          f"invalid={stats['skipped_invalid']} ambiguous={stats['ambiguous']}")


def _parse_sidebar_time_seconds(t):
    """Parse X DM sidebar relative time string to seconds.

    Examples: "5m" -> 300, "2h" -> 7200, "1d" -> 86400, "3w" -> 1814400,
    "Just now" -> 30. Returns None when the string is unrecognized.
    """
    if not t:
        return None
    s = t.strip().lower()
    if s in ("just now", "active now", "now", "0m", "0s"):
        return 30
    m = re.match(r"^(\d+)\s*([smhdw])$", s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def filter_inbox(conn, platform, records):
    """Filter a sidebar scan down to threads that need inspection.

    Combines two signals to drop threads we have no business opening:

      1. Sidebar visual cues from the scanner (`is_from_us`, `has_unread`).
      2. DB-backed cross-check: if our last outbound message_at is inside
         the sidebar's reported activity window, our reply IS the latest
         message in the thread, so there's nothing new to read.

    Skip rules:
      - is_from_us=true                          (definitive: we sent last)
      - has_unread=false AND outbound_age <= sidebar_seconds + buffer
        AND last_inbound <= last_outbound        (DB confirms we replied last)
      - conversation_status in (needs_human, closed)
      - chat_url is unparseable

    Always inspect:
      - has_unread=true                          (X marks the row unread)
      - no DM row exists                         (brand-new contact)
      - sidebar timestamp unparseable + no clear DB signal (be conservative)

    Output: emits a JSON array on stdout containing only records that
    survived the filter. Each survivor is enriched with `_filter_reason`
    and `_dm_id`. Counts are logged to stderr.
    """
    norm_platform = "x" if platform in ("twitter", "x") else platform

    keep = []
    counters = {
        "kept_unread": 0,
        "kept_no_db_row": 0,
        "kept_ambiguous": 0,
        "skip_is_from_us": 0,
        "skip_we_replied_after": 0,
        "skip_recently_inspected": 0,
        "skip_needs_human": 0,
        "skip_closed": 0,
        "skip_invalid_url": 0,
    }

    for rec in records:
        chat_url_raw = rec.get("chat_url") or rec.get("thread_url") or ""
        author = (rec.get("handle") or rec.get("author") or "").strip()
        chat_url = _valid_chat_url(norm_platform, chat_url_raw)

        if not chat_url:
            counters["skip_invalid_url"] += 1
            continue

        # Sidebar signals (some scanners may not emit `has_unread` yet)
        is_from_us = bool(rec.get("is_from_us"))
        has_unread = rec.get("has_unread", None)
        sidebar_time = rec.get("time")
        sidebar_seconds = _parse_sidebar_time_seconds(sidebar_time)

        # Hard skip: sidebar shows we sent last
        if is_from_us:
            counters["skip_is_from_us"] += 1
            continue

        # Look up the DM row (chat_url first, fall back to author).
        # Pull last_inspected_at too: if we've already opened this thread
        # recently and confirmed nothing new, we don't want to re-open it.
        row = None
        if chat_url:
            row = conn.execute(
                "SELECT id, conversation_status, last_inspected_at FROM dms "
                "WHERE platform = %s AND chat_url = %s "
                "ORDER BY last_message_at DESC NULLS LAST LIMIT 1",
                (norm_platform, chat_url),
            ).fetchone()
        if row is None and author:
            row = conn.execute(
                "SELECT id, conversation_status, last_inspected_at FROM dms "
                "WHERE platform = %s AND LOWER(their_author) = LOWER(%s) "
                "ORDER BY last_message_at DESC NULLS LAST LIMIT 1",
                (norm_platform, author),
            ).fetchone()

        if row is None:
            # Brand new contact: definitely inspect.
            keep.append({**rec, "_filter_reason": "no_db_row", "_dm_id": None})
            counters["kept_no_db_row"] += 1
            continue

        dm_id = row["id"]
        status = row.get("conversation_status") or "active"
        last_inspected_at = row.get("last_inspected_at")

        # Skip already-escalated or explicitly closed convos
        if status == "needs_human":
            counters["skip_needs_human"] += 1
            continue
        if status == "closed":
            counters["skip_closed"] += 1
            continue

        # If sidebar visually marks the thread unread, trust it: inspect.
        if has_unread is True:
            keep.append({**rec, "_filter_reason": "sidebar_unread", "_dm_id": dm_id})
            counters["kept_unread"] += 1
            continue

        # DB-backed short-circuit: pull last outbound and last inbound.
        # If our outbound timestamp is inside the sidebar's activity window
        # AND we have no inbound newer than that outbound, our reply IS
        # the sidebar's latest message; nothing new for us to read.
        msg_row = conn.execute(
            """
            SELECT
                MAX(message_at) FILTER (WHERE direction = 'outbound') AS last_outbound_at,
                MAX(message_at) FILTER (WHERE direction = 'inbound')  AS last_inbound_at
            FROM dm_messages
            WHERE dm_id = %s
            """,
            (dm_id,),
        ).fetchone()

        last_outbound_at = msg_row["last_outbound_at"] if msg_row else None
        last_inbound_at = msg_row["last_inbound_at"] if msg_row else None

        if last_outbound_at and sidebar_seconds is not None:
            # Buffer accounts for X's bucketed times (e.g. "13m" can mean
            # anywhere in [13m, 14m)) and DB clock skew.
            buffer_seconds = max(120, int(sidebar_seconds * 0.25))
            window_seconds = sidebar_seconds + buffer_seconds

            cmp = conn.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - %s::timestamptz))::bigint "
                "AS outbound_age_seconds",
                (last_outbound_at,),
            ).fetchone()
            outbound_age = int(cmp["outbound_age_seconds"]) if cmp else None

            inbound_is_newer = (
                last_inbound_at is not None and last_inbound_at > last_outbound_at
            )

            if (
                outbound_age is not None
                and outbound_age <= window_seconds
                and not inbound_is_newer
            ):
                counters["skip_we_replied_after"] += 1
                continue

        # Recently-inspected short-circuit. Some threads stay near the top
        # of the sidebar with stale activity (cold conversations, reactions,
        # X bumps for "you both follow N people now" cards) but have no new
        # inbound text. If we already opened this thread after the most
        # recent message we logged, AND that visit was recent (default 24h),
        # don't open it again until something new actually happens.
        if last_inspected_at is not None:
            inspect_after_messages = (
                last_inbound_at is None or last_inspected_at >= last_inbound_at
            ) and (
                last_outbound_at is None or last_inspected_at >= last_outbound_at
            )
            if inspect_after_messages:
                age = conn.execute(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - %s::timestamptz))::bigint AS age_s",
                    (last_inspected_at,),
                ).fetchone()
                inspected_age = int(age["age_s"]) if age else None
                # Re-inspect at most once per 24h. Sidebar timestamps roll
                # past this naturally, so this caps the "we keep checking
                # the same stale thread" cost at one open per day per thread.
                if inspected_age is not None and inspected_age <= 86400:
                    counters["skip_recently_inspected"] += 1
                    continue

        # Sidebar timestamp unparseable, or our outbound is older than the
        # sidebar window: there's likely a fresh inbound. Inspect.
        keep.append({
            **rec,
            "_filter_reason": "outbound_older_than_window",
            "_dm_id": dm_id,
        })
        counters["kept_ambiguous"] += 1

    total_in = len(records)
    total_keep = len(keep)
    print(
        f"  filter-inbox [{norm_platform}]: in={total_in} kept={total_keep} "
        f"(unread={counters['kept_unread']}, "
        f"no_db_row={counters['kept_no_db_row']}, "
        f"ambiguous={counters['kept_ambiguous']}) "
        f"skipped={total_in - total_keep} "
        f"(is_from_us={counters['skip_is_from_us']}, "
        f"we_replied_after={counters['skip_we_replied_after']}, "
        f"recently_inspected={counters['skip_recently_inspected']}, "
        f"needs_human={counters['skip_needs_human']}, "
        f"closed={counters['skip_closed']}, "
        f"invalid_url={counters['skip_invalid_url']})",
        file=sys.stderr,
    )
    print(json.dumps(keep, default=str))


def mark_inspected(conn, dm_id):
    """Stamp NOW() onto dms.last_inspected_at.

    Called by the engagement prompt after every successful read-conversation
    that doesn't produce a new outbound or new inbound row, so the next
    cycle's filter-inbox can short-circuit threads we've already verified
    have nothing new in them.
    """
    conn.execute(
        "UPDATE dms SET last_inspected_at = NOW() WHERE id = %s",
        (dm_id,),
    )
    conn.commit()
    print(f"  Marked DM #{dm_id} inspected at NOW()")


def set_tier(conn, dm_id, tier):
    conn.execute(
        """
        UPDATE dms
        SET tier = %s,
            first_product_mention_at = CASE
                WHEN %s >= 2 AND first_product_mention_at IS NULL THEN NOW()
                ELSE first_product_mention_at
            END
        WHERE id = %s
        """,
        (tier, tier, dm_id),
    )
    conn.commit()
    print(f"  Set tier={tier} for DM #{dm_id}")


def set_status(conn, dm_id, status):
    conn.execute("UPDATE dms SET conversation_status = %s WHERE id = %s", (status, dm_id))
    conn.commit()
    print(f"  Set conversation_status={status} for DM #{dm_id}")


def set_interest(conn, dm_id, interest):
    conn.execute("UPDATE dms SET interest_level = %s WHERE id = %s", (interest, dm_id))
    conn.commit()
    print(f"  Set interest_level={interest} for DM #{dm_id}")


def set_mode(conn, dm_id, mode):
    """Set the per-turn conversational posture (rapport vs pitch).

    Reversible: a thread can flip back to 'rapport' after a 'pitch' turn if
    the next message drops product talk and goes back to casual. The tier
    ratchet and first_product_mention_at stamp handle the historical
    'we ever pitched' signal independently.
    """
    conn.execute("UPDATE dms SET mode = %s WHERE id = %s", (mode, dm_id))
    conn.commit()
    print(f"  Set mode={mode} for DM #{dm_id}")


def set_project(conn, dm_id, project, append=False):
    """Set the canonical project_name for the DM. With --append, also adds to
    target_projects[] (the union of every project this thread has pursued)."""
    conn.execute("UPDATE dms SET project_name = %s WHERE id = %s", (project, dm_id))
    if append:
        # Idempotent append: PostgreSQL array_append returns NULL on a NULL
        # input but target_projects has DEFAULT '{}', so a simple
        # array_append + dedup pattern is safe.
        conn.execute(
            "UPDATE dms SET target_projects = ("
            "  SELECT ARRAY(SELECT DISTINCT unnest(target_projects || ARRAY[%s::text]))"
            "  FROM dms WHERE id = %s"
            ") WHERE id = %s",
            (project, dm_id, dm_id),
        )
    conn.commit()
    extra = " (appended to target_projects)" if append else ""
    print(f"  Set project_name={project} for DM #{dm_id}{extra}")


def set_target_project(conn, dm_id, project, append=False):
    """Set the canonical (primary) target_project AND extend target_projects[]
    with the new project (idempotent dedup). The wrap-tool checks against
    target_projects[] when deciding whether a URL's project is allowed.

    `append` is accepted for explicit caller intent but is a semantic no-op:
    the union always grows, the primary is always the latest set. We never
    remove projects from the list — once a thread has pursued a project, links
    for that project remain wrappable even after pivoting the primary.
    """
    conn.execute("UPDATE dms SET target_project = %s WHERE id = %s", (project, dm_id))
    conn.execute(
        "UPDATE dms SET target_projects = ("
        "  SELECT ARRAY(SELECT DISTINCT unnest(target_projects || ARRAY[%s::text]))"
        "  FROM dms WHERE id = %s"
        ") WHERE id = %s",
        (project, dm_id, dm_id),
    )
    conn.commit()
    print(f"  Set target_project={project} for DM #{dm_id} (target_projects union extended)")


def set_qualification(conn, dm_id, status, notes=None):
    if notes is not None:
        conn.execute(
            "UPDATE dms SET qualification_status = %s, qualification_notes = %s WHERE id = %s",
            (status, notes, dm_id),
        )
    else:
        conn.execute(
            "UPDATE dms SET qualification_status = %s WHERE id = %s",
            (status, dm_id),
        )
    conn.commit()
    suffix = f" (notes: {notes[:60]}...)" if notes else ""
    print(f"  Set qualification_status={status} for DM #{dm_id}{suffix}")


def mark_booking_sent(conn, dm_id):
    conn.execute("UPDATE dms SET booking_link_sent_at = NOW() WHERE id = %s", (dm_id,))
    conn.commit()
    print(f"  Set booking_link_sent_at=NOW() for DM #{dm_id}")


def mark_skipped(conn, dm_id, reason):
    conn.execute(
        "UPDATE dms SET status='skipped', skip_reason=%s WHERE id=%s AND status='pending'",
        (reason, dm_id),
    )
    conn.commit()
    print(f"  Set status=skipped (reason: {reason}) for DM #{dm_id}")


def set_icp_precheck(conn, dm_id, label, project, notes=None):
    """Upsert a per-project ICP verdict into dms.icp_matches (JSONB array).
    Does NOT gate sending. Also syncs icp_precheck TEXT with the entry matching
    the row's current target_project so legacy readers still see a label."""
    import json as _json

    if not project:
        raise ValueError("set_icp_precheck requires project (name from config.json)")

    entry = {
        "project": project,
        "label":   label,
        "notes":   notes,
        "at":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    conn.execute(
        """
        UPDATE dms
        SET icp_matches = COALESCE(
            (SELECT jsonb_agg(e) FROM jsonb_array_elements(icp_matches) e
             WHERE e->>'project' IS DISTINCT FROM %s),
            '[]'::jsonb
        ) || %s::jsonb,
            icp_precheck = CASE
                WHEN target_project = %s THEN %s
                ELSE icp_precheck
            END
        WHERE id = %s
        """,
        (project, _json.dumps(entry), project, label, dm_id),
    )

    if notes is not None:
        prefix = f"icp:{project}:{label} - {notes}"
        conn.execute(
            """
            UPDATE dms
            SET qualification_notes = CASE
                WHEN qualification_notes IS NULL OR qualification_notes = ''
                THEN %s
                ELSE qualification_notes || E'\n' || %s
            END
            WHERE id = %s
            """,
            (prefix, prefix, dm_id),
        )
    conn.commit()
    suffix = f" (notes: {notes[:60]}...)" if notes else ""
    print(f"  Upserted icp_matches[{project}]={label} for DM #{dm_id}{suffix}")


def _http_link_wrap_guard(dm_id, content):
    """Pure-Python (no DB) link-wrap guard, mirroring log_outbound's pre-pass.

    Returns True if an unwrapped project URL is present (caller must abort the
    log and print already happened here). Returns False when content is clean
    or the classifier could not load.
    """
    try:
        from dm_short_links import _classify_url, _load_projects, _URL_RE, _TRAILING_PUNCT
        _wrap_check_projects = _load_projects()
        for m in _URL_RE.finditer(content or ""):
            raw_url = m.group(0).rstrip(_TRAILING_PUNCT)
            if re.search(r'/r/[a-z0-9]{4,32}(?:[/?#]|$)', raw_url, re.IGNORECASE):
                continue
            kind, matched = _classify_url(raw_url, _wrap_check_projects)
            if kind != 'other':
                print(f"  LINK BLOCKED: DM #{dm_id} content contains unwrapped {kind} URL "
                      f"({raw_url[:80]}) for project {matched!r}. Re-send via the wrap-text "
                      f"helper (python3 scripts/dm_short_links.py wrap-text --dm-id {dm_id} "
                      f"--text '...').")
                return True
    except Exception as _wrap_err:
        print(f"  WARNING: link-wrap guard skipped due to error: {_wrap_err}", file=sys.stderr)
    return False


def _http_log_outbound(args):
    """DB-free log-outbound over the s4l.ai API.

    Preserves log_outbound's behaviour for the LinkedIn/HTTP lane: --verified
    gate, link-wrap guard, timeline gate, dedup guard, message insert,
    conversation_status='active'. The reddit-only campaign suffix attribution
    and dm_links.message_id backfill (driven by WRAP_MINTED_CODES, set only by
    reddit_browser/twitter_browser) are no-ops on this lane because LinkedIn
    never mints codes through a Python pre-pass; those rails run on the
    DB-equipped machine. Returns the process exit behaviour via sys.exit on
    block (matching the DB path's sys.exit(3))."""
    import http_api

    dm_id = args.dm_id
    content = args.content

    if not args.verified:
        print(f"  VERIFY BLOCKED: refusing to log outbound for DM #{dm_id} without "
              f"--verified. Pass it only when the browser send tool returned verified=true.")
        sys.exit(3)

    if _http_link_wrap_guard(dm_id, content):
        sys.exit(3)

    resp = http_api.api_get(f"/api/v1/dms/{dm_id}", ok_on_404=True)
    if resp.get("_not_found"):
        print(f"  ERROR: DM #{dm_id} not found")
        sys.exit(3)
    row = (resp.get("data") or {}).get("dm") or {}

    # Timeline gate (mirror log_outbound:194-201).
    cur_count = row.get("message_count") or 0
    qual_status = row.get("qualification_status") or "pending"
    icp_list = row.get("icp_matches") or []
    if cur_count >= 3 and qual_status == "pending" and not icp_list:
        print(f"  TIMELINE BLOCKED: DM #{dm_id} is at msg {cur_count} with qualification_status=pending and empty icp_matches.")
        print(f"  Run Step 2.4 (set-icp-precheck for every project in $PROJECTS) before logging this outbound.")
        print(f"  If nothing in $PROJECTS plausibly fits this prospect, call set-qualification --status disqualified --notes 'reason' and retry.")
        sys.exit(3)

    # Dedup guard: block if the last message is already outbound.
    msgs = (http_api.api_get(f"/api/v1/dms/{dm_id}/messages", {"limit": 1000}).get("data") or {}).get("messages") or []
    if msgs and msgs[-1].get("direction") == "outbound":
        print(f"  DEDUP BLOCKED: Last message to {row.get('their_author')} (DM #{dm_id}) was already outbound. Skipping.")
        sys.exit(3)

    config = load_config()
    author = args.author or get_our_account(config, row.get("platform"))
    claude_session_id = os.environ.get("CLAUDE_SESSION_ID") or None

    http_api.api_post(
        f"/api/v1/dms/{dm_id}/messages",
        {
            "direction": "outbound",
            "author": author,
            "content": content,
            "claude_session_id": claude_session_id,
            "bump_to_needs_reply": False,
        },
    )
    # log_outbound sets conversation_status='active' on every outbound.
    http_api.api_patch(f"/api/v1/dms/{dm_id}", {"conversation_status": "active"})

    print(f"  Logged outbound to {row.get('their_author')} (DM #{dm_id})")
    return True


def _http_dispatch(args):
    """Route DB-free commands through the s4l.ai HTTP API.

    Returns True when the command was handled (caller should return), False
    when the command is not yet wired for the no-DATABASE_URL lane (caller
    falls through to the clear "needs DB" error). Each branch prints the exact
    same stdout the DB path emits so downstream shell parsing is unchanged.
    """
    import http_api

    cmd = args.command
    dm_id = getattr(args, "dm_id", None)

    if cmd == "mark-skipped":
        http_api.api_patch(
            f"/api/v1/dms/{dm_id}",
            {"status": "skipped", "only_if_status": "pending", "skip_reason": args.reason},
        )
        print(f"  Set status=skipped (reason: {args.reason}) for DM #{dm_id}")
        return True

    if cmd == "set-icp-precheck":
        body = {"project": args.project, "label": args.label}
        if args.notes is not None:
            body["notes"] = args.notes
        http_api.api_post(f"/api/v1/dms/{dm_id}/icp-precheck", body)
        suffix = f" (notes: {args.notes[:60]}...)" if args.notes else ""
        print(f"  Upserted icp_matches[{args.project}]={args.label} for DM #{dm_id}{suffix}")
        return True

    if cmd == "set-tier":
        http_api.api_patch(f"/api/v1/dms/{dm_id}", {"tier": args.tier})
        print(f"  Set tier={args.tier} for DM #{dm_id}")
        return True

    if cmd == "set-status":
        http_api.api_patch(f"/api/v1/dms/{dm_id}", {"conversation_status": args.status})
        print(f"  Set conversation_status={args.status} for DM #{dm_id}")
        return True

    if cmd == "set-interest":
        http_api.api_patch(f"/api/v1/dms/{dm_id}", {"interest_level": args.interest})
        print(f"  Set interest_level={args.interest} for DM #{dm_id}")
        return True

    if cmd == "set-mode":
        http_api.api_patch(f"/api/v1/dms/{dm_id}", {"mode": args.mode})
        print(f"  Set mode={args.mode} for DM #{dm_id}")
        return True

    if cmd == "set-project":
        body = {"project_name": args.project}
        if getattr(args, "append", False):
            body["target_projects_add"] = args.project
        http_api.api_patch(f"/api/v1/dms/{dm_id}", body)
        extra = " (appended to target_projects)" if getattr(args, "append", False) else ""
        print(f"  Set project_name={args.project} for DM #{dm_id}{extra}")
        return True

    if cmd == "set-target-project":
        http_api.api_patch(
            f"/api/v1/dms/{dm_id}",
            {"target_project": args.project, "target_projects_add": args.project},
        )
        print(f"  Set target_project={args.project} for DM #{dm_id} (target_projects union extended)")
        return True

    if cmd == "set-qualification":
        body = {"qualification_status": args.status}
        if args.notes is not None:
            body["qualification_notes"] = args.notes
        http_api.api_patch(f"/api/v1/dms/{dm_id}", body)
        suffix = f" (notes: {args.notes[:60]}...)" if args.notes else ""
        print(f"  Set qualification_status={args.status} for DM #{dm_id}{suffix}")
        return True

    if cmd == "mark-booking-sent":
        http_api.api_patch(f"/api/v1/dms/{dm_id}", {"booking_link_sent_at_now": True})
        print(f"  Set booking_link_sent_at=NOW() for DM #{dm_id}")
        return True

    if cmd == "mark-inspected":
        http_api.api_patch(f"/api/v1/dms/{dm_id}", {"last_inspected_at_now": True})
        print(f"  Marked DM #{dm_id} inspected at NOW()")
        return True

    if cmd == "set-url":
        resp = http_api.api_get(f"/api/v1/dms/{dm_id}", ok_on_404=True)
        if resp.get("_not_found"):
            print(f"  ERROR: DM #{dm_id} not found")
            return True
        platform = ((resp.get("data") or {}).get("dm") or {}).get("platform")
        clean = _valid_chat_url(platform, args.url)
        if args.url and not clean:
            print(f"  ERROR: '{args.url[:120]}' is not a valid {platform} DM-thread URL; refusing to save.")
            print(f"         Expected shapes: reddit=/chat/room/!..., x=/i/chat/..., linkedin=/messaging/thread/...")
            sys.exit(2)
        http_api.api_patch(f"/api/v1/dms/{dm_id}", {"chat_url": clean})
        print(f"  Set chat_url for DM #{dm_id}")
        return True

    if cmd == "log-inbound":
        body = {"direction": "inbound", "author": args.author, "content": args.content}
        if getattr(args, "message_at", None):
            body["message_at"] = args.message_at
        if getattr(args, "event_id", None):
            body["event_id"] = args.event_id
        http_api.api_post(f"/api/v1/dms/{dm_id}/messages", body)
        print(f"  Logged inbound from {args.author} (DM #{dm_id})")
        return True

    if cmd == "log-outbound":
        return _http_log_outbound(args)

    if cmd == "ensure-dm":
        body = {"platform": args.platform, "author": args.author}
        if getattr(args, "chat_url", None):
            body["chat_url"] = args.chat_url
        if getattr(args, "lookback_hours", None) is not None:
            body["lookback_hours"] = args.lookback_hours
        resp = http_api.api_post("/api/v1/dms/ensure", body)
        data = resp.get("data") or {}
        new_id = data.get("dm_id")
        print(f"DM_ID={new_id}")
        if data.get("created"):
            linked = data.get("linked_reply_id")
            if linked:
                print(f"  created (linked to replies.id={linked})")
            else:
                print("  created (no matching replies row within lookback, reply_id/post_id NULL)")
        else:
            print("  existing")
        return True

    if cmd == "history":
        resp = http_api.api_get(f"/api/v1/dms/{dm_id}", ok_on_404=True)
        if resp.get("_not_found"):
            print(f"DM #{dm_id} not found")
            return True
        dm = (resp.get("data") or {}).get("dm") or {}
        print(f"=== DM #{dm.get('id')} with {dm.get('their_author')} [{dm.get('platform')}] ===")
        print(f"Status: {dm.get('conversation_status')}  Tier: {dm.get('tier')}  Messages: {dm.get('message_count')}")
        if dm.get("chat_url"):
            print(f"Chat URL: {dm['chat_url']}")
        if dm.get("comment_context"):
            print(f"Original context: {dm['comment_context'][:200]}...")
        print()
        msgs_resp = http_api.api_get(f"/api/v1/dms/{dm_id}/messages")
        msgs = (msgs_resp.get("data") or {}).get("messages") or []
        for m in msgs:
            arrow = ">>" if m.get("direction") == "outbound" else "<<"
            ma = m.get("message_at") or ""
            ts = str(ma)[:16].replace("T", " ") if ma else "?"
            print(f"  {arrow} [{ts}] {m.get('author')}: {m.get('content')}")
        print()
        return True

    if cmd == "pending":
        resp = http_api.api_get("/api/v1/dms/pending")
        rows = (resp.get("data") or {}).get("pending") or []
        if not rows:
            print("No conversations needing reply.")
            return True
        print(f"=== {len(rows)} conversations need reply ===\n")
        for r in rows:
            tier_label = f"T{r['tier']}" if r.get("tier") else "T1"
            ma = r.get("last_message_at") or ""
            ts = str(ma)[5:16].replace("T", " ") if ma else "?"
            last = (r.get("last_msg") or "")[:100]
            print(f"  DM #{r['id']} [{r.get('platform')}] {r.get('their_author')} ({tier_label}, {r.get('message_count')} msgs, last: {ts})")
            print(f"    Last: {last}")
            if r.get("chat_url"):
                print(f"    URL: {r['chat_url']}")
            print()
        return True

    if cmd == "show-flagged":
        resp = http_api.api_get("/api/v1/dms/flagged")
        rows = (resp.get("data") or {}).get("flagged") or []
        if not rows:
            print("No conversations flagged for human attention.")
            return True
        print(f"=== {len(rows)} conversations need HUMAN attention ===\n")
        for r in rows:
            fa = r.get("flagged_at") or ""
            ts = str(fa)[5:16].replace("T", " ") if fa else "?"
            last = (r.get("last_msg") or "")[:150]
            print(f"  DM #{r['id']} [{r.get('platform')}] {r.get('their_author')} (T{r.get('tier') or 1}, {r.get('message_count')} msgs)")
            print(f"    REASON: {r.get('human_reason')}")
            print(f"    Flagged: {ts}")
            print(f"    Last msg ({r.get('last_dir')}): {last}")
            if r.get("chat_url"):
                print(f"    URL: {r['chat_url']}")
            print()
        return True

    if cmd == "flag-human":
        resp = http_api.api_post(
            f"/api/v1/dms/{dm_id}/flag-human", {"reason": args.reason}, ok_on_conflict=True
        )
        data = resp.get("data") or {}
        if data.get("skipped"):
            print(f"  SKIP flag-human: DM #{dm_id} last message is OUTBOUND. We already replied; ball is in their court. Reason was: {args.reason}")
            return True
        print(f"  FLAGGED DM #{dm_id} for human attention: {args.reason}")
        if data.get("email_sent"):
            print(f"  Escalation email sent for DM #{dm_id}")
        else:
            print(f"  WARNING: escalation email not sent for DM #{dm_id} (no RESEND_API_KEY on server, or send failed)")
        return True

    if cmd == "backfill-urls":
        records = _load_records_arg(args)
        resp = http_api.api_post(
            "/api/v1/dms/backfill-urls",
            {"platform": args.platform, "records": records},
        )
        stats = (resp.get("data") or {}).get("stats") or {}
        print(f"  backfill-urls [{args.platform}]: updated={stats.get('updated', 0)} "
              f"already_set={stats.get('skipped_already_set', 0)} no_match={stats.get('no_match', 0)} "
              f"invalid={stats.get('skipped_invalid', 0)} ambiguous={stats.get('ambiguous', 0)}")
        return True

    if cmd == "filter-inbox":
        records = _load_records_arg(args)
        resp = http_api.api_post(
            "/api/v1/dms/filter-inbox",
            {"platform": args.platform, "records": records},
        )
        data = resp.get("data") or {}
        keep = data.get("keep") or []
        counters = data.get("counters") or {}
        norm = "x" if args.platform in ("twitter", "x") else args.platform
        total_in = data.get("in", len(records))
        total_keep = data.get("kept", len(keep))
        print(
            f"  filter-inbox [{norm}]: in={total_in} kept={total_keep} "
            f"(unread={counters.get('kept_unread', 0)}, "
            f"no_db_row={counters.get('kept_no_db_row', 0)}, "
            f"ambiguous={counters.get('kept_ambiguous', 0)}) "
            f"skipped={total_in - total_keep} "
            f"(is_from_us={counters.get('skip_is_from_us', 0)}, "
            f"we_replied_after={counters.get('skip_we_replied_after', 0)}, "
            f"recently_inspected={counters.get('skip_recently_inspected', 0)}, "
            f"needs_human={counters.get('skip_needs_human', 0)}, "
            f"closed={counters.get('skip_closed', 0)}, "
            f"invalid_url={counters.get('skip_invalid_url', 0)})",
            file=sys.stderr,
        )
        print(json.dumps(keep, default=str))
        return True

    if cmd == "find":
        resp = http_api.api_get("/api/v1/dms/find", query={"author": args.author})
        rows = (resp.get("data") or {}).get("matches") or []
        if not rows:
            print(f"No DMs found matching '{args.author}'")
            return True
        for r in rows:
            ma = r.get("last_message_at") or ""
            # API returns ISO 'YYYY-MM-DDTHH:MM:...'; DB path printed '%m/%d %H:%M'.
            ts = (str(ma)[5:16].replace("T", " ").replace("-", "/")) if ma else "never"
            print(f"  DM #{r['id']} [{r.get('platform')}] {r.get('their_author')} - "
                  f"{r.get('status')}/{r.get('conversation_status')} T{r.get('tier') or 1} "
                  f"({r.get('message_count')} msgs, last: {ts})")
            if r.get("chat_url"):
                print(f"    URL: {r['chat_url']}")
        return True

    if cmd == "summary":
        resp = http_api.api_get("/api/v1/dms/summary")
        s = (resp.get("data") or {}).get("summary") or {}
        print("=== DM Pipeline Summary ===")
        print(f"  Conversations: {s.get('total', 0)} total ({s.get('sent', 0)} sent, {s.get('skipped', 0)} skipped)")
        print(f"  Unique authors: {s.get('unique_authors', 0)}")
        print(f"  Status: {s.get('needs_reply', 0)} needs_reply, {s.get('active', 0)} active, "
              f"{s.get('converted', 0)} converted, {s.get('stale', 0)} stale")
        print(f"  Tiers: {s.get('tier2', 0)} at T2, {s.get('tier3', 0)} at T3")
        print(f"  Messages: {s.get('total_messages', 0)} total ({s.get('outbound', 0)} outbound, "
              f"{s.get('inbound', 0)} inbound)")
        print(f"  Reply rate: {s.get('conversations_with_replies', 0)}/{s.get('sent', 0)} "
              f"conversations have inbound replies")
        print()
        return True

    if cmd == "send-escalation-email":
        resp = http_api.api_post(
            f"/api/v1/dms/{dm_id}/send-escalation-email", {}, ok_on_404=True
        )
        if resp.get("_not_found"):
            print(f"ERROR: DM #{dm_id} not found")
            return True
        data = resp.get("data") or {}
        if data.get("status_warning"):
            print(f"WARNING: DM #{dm_id} is '{data.get('conversation_status')}', not 'needs_human'. Sending anyway.")
        if data.get("email_sent"):
            print(f"  Escalation email sent for DM #{dm_id}")
        else:
            print(f"  WARNING: escalation email not sent for DM #{dm_id} (no RESEND_API_KEY on server, or send failed)")
        return True

    return False


def _load_records_arg(args):
    """Read a JSON array of records from --file or stdin for the bulk commands
    (backfill-urls, filter-inbox), matching the DB-path's input handling so the
    HTTP lane accepts the exact same scanner dumps. Returns a list (possibly
    empty); exits 2 on unparseable input."""
    raw = open(args.file).read() if getattr(args, "file", None) else sys.stdin.read()
    try:
        records = json.loads(raw)
    except Exception as e:
        print(f"ERROR: could not parse JSON input: {e}", file=sys.stderr)
        sys.exit(2)
    if isinstance(records, dict):
        for k in ("conversations", "threads", "dms", "items"):
            if k in records and isinstance(records[k], list):
                return records[k]
        if records.get("ok") is False:
            return []
    if not isinstance(records, list):
        print("ERROR: expected a JSON array of records", file=sys.stderr)
        sys.exit(2)
    return records


def main():
    parser = argparse.ArgumentParser(description="DM conversation tracker")
    sub = parser.add_subparsers(dest="command")

    p_out = sub.add_parser("log-outbound", help="Log outbound message")
    p_out.add_argument("--dm-id", type=int, required=True)
    p_out.add_argument("--content", required=True)
    p_out.add_argument("--author")
    p_out.add_argument(
        "--verified",
        action="store_true",
        help="REQUIRED. Confirms the browser send_dm/compose_dm tool returned verified=true.",
    )

    p_ensure = sub.add_parser("ensure-dm",
        help="Return dm_id for (platform, author), creating the row and auto-linking reply_id/post_id from the most recent matching replies row. Prints DM_ID=<n> on stdout.")
    p_ensure.add_argument("--platform", required=True, choices=["reddit", "linkedin", "x", "twitter"])
    p_ensure.add_argument("--author", required=True)
    p_ensure.add_argument("--chat-url", default=None,
        help="Optional chat URL to stamp on the DM row (set only if currently NULL).")
    p_ensure.add_argument("--lookback-hours", type=int, default=720,
        help="How far back to search for a matching replies row when auto-linking (default 720h = 30d).")

    p_in = sub.add_parser("log-inbound", help="Log inbound message")
    p_in.add_argument("--dm-id", type=int, required=True)
    p_in.add_argument("--author", required=True)
    p_in.add_argument("--content", required=True)
    p_in.add_argument("--message-at", help="ISO timestamp (platform-provided); falls back to NOW() if omitted.")
    p_in.add_argument("--event-id", help="Platform-native unique message id (e.g., Matrix $... event_id). When supplied, dedup is by event_id instead of content match.")

    p_hist = sub.add_parser("history", help="Show conversation history")
    p_hist.add_argument("--dm-id", type=int, required=True)

    sub.add_parser("pending", help="Show conversations needing reply")

    p_find = sub.add_parser("find", help="Find DM by author")
    p_find.add_argument("--author", required=True)

    sub.add_parser("summary", help="Pipeline summary")

    p_url = sub.add_parser("set-url", help="Set chat URL")
    p_url.add_argument("--dm-id", type=int, required=True)
    p_url.add_argument("--url", required=True)

    p_backfill = sub.add_parser("backfill-urls",
        help=("Bulk-stamp chat_url onto orphan dms rows from a scanner JSON dump. "
              "Input: a JSON array of {author|handle, chat_url|thread_url} on stdin or --file."))
    p_backfill.add_argument("--platform", required=True, choices=["reddit", "linkedin", "x", "twitter"])
    p_backfill.add_argument("--file", default=None,
        help="Path to JSON file. If omitted, reads from stdin.")

    p_filter = sub.add_parser("filter-inbox",
        help=("Filter a sidebar scan dump down to threads that need inspection. "
              "Combines sidebar signals (is_from_us, has_unread, time) with the "
              "DB's last outbound message_at to drop threads where we already "
              "sent the most recent message. "
              "Input: JSON array on stdin or --file. "
              "Output: filtered JSON array on stdout, summary on stderr."))
    p_filter.add_argument("--platform", required=True, choices=["reddit", "linkedin", "x", "twitter"])
    p_filter.add_argument("--file", default=None,
        help="Path to JSON file. If omitted, reads from stdin.")

    p_inspect = sub.add_parser("mark-inspected",
        help=("Stamp NOW() onto dms.last_inspected_at after a read-conversation "
              "call confirmed there is no new content to log. The next "
              "filter-inbox run will skip this thread for 24h unless a fresh "
              "outbound or inbound is logged in the meantime."))
    p_inspect.add_argument("--dm-id", type=int, required=True)

    p_tier = sub.add_parser("set-tier", help="Set conversation tier")
    p_tier.add_argument("--dm-id", type=int, required=True)
    p_tier.add_argument("--tier", type=int, required=True, choices=[1, 2, 3])

    p_status = sub.add_parser("set-status", help="Set conversation status")
    p_status.add_argument("--dm-id", type=int, required=True)
    p_status.add_argument("--status", required=True,
                          choices=["active", "needs_reply", "stale", "converted", "closed", "needs_human"])

    p_interest = sub.add_parser("set-interest", help="Set prospect interest level for product/topic")
    p_interest.add_argument("--dm-id", type=int, required=True)
    p_interest.add_argument("--interest", required=True,
                            choices=["no_response", "general_discussion", "cold", "warm", "hot", "declined", "not_our_prospect"])

    p_mode = sub.add_parser("set-mode", help="Set per-turn conversational posture (rapport vs pitch). Reversible.")
    p_mode.add_argument("--dm-id", type=int, required=True)
    p_mode.add_argument("--mode", required=True, choices=["rapport", "pitch"])

    p_flag = sub.add_parser("flag-human", help="Flag conversation for human attention")
    p_flag.add_argument("--dm-id", type=int, required=True)
    p_flag.add_argument("--reason", required=True)

    sub.add_parser("show-flagged", help="Show conversations needing human attention")

    p_resend = sub.add_parser("send-escalation-email",
                              help="Re-send the escalation email for an already-flagged DM (for testing / manual retry)")
    p_resend.add_argument("--dm-id", type=int, required=True)

    p_proj = sub.add_parser("set-project", help="Set project_name (project we recommended)")
    p_proj.add_argument("--dm-id", type=int, required=True)
    p_proj.add_argument("--project", required=True)
    p_proj.add_argument("--append", action="store_true",
                         help="Also add to target_projects[] (the union of pursued projects)")

    p_tproj = sub.add_parser("set-target-project",
                              help="Set primary target_project AND extend target_projects[] (always)")
    p_tproj.add_argument("--dm-id", type=int, required=True)
    p_tproj.add_argument("--project", required=True)
    p_tproj.add_argument("--append", action="store_true",
                         help="Explicit caller intent (semantic no-op: union always grows)")

    p_qual = sub.add_parser("set-qualification", help="Set qualification_status and optional notes")
    p_qual.add_argument("--dm-id", type=int, required=True)
    p_qual.add_argument("--status", required=True,
                         choices=["pending", "asked", "answered", "qualified", "disqualified"])
    p_qual.add_argument("--notes", default=None)

    p_book = sub.add_parser("mark-booking-sent", help="Record that a booking link was shared")
    p_book.add_argument("--dm-id", type=int, required=True)

    p_skip = sub.add_parser("mark-skipped", help="Skip a pending outreach DM (sets status=skipped). No-op on non-pending rows.")
    p_skip.add_argument("--dm-id", type=int, required=True)
    p_skip.add_argument("--reason", required=True)

    p_icp = sub.add_parser("set-icp-precheck", help="Upsert per-project ICP verdict into icp_matches array (no filter)")
    p_icp.add_argument("--dm-id", type=int, required=True)
    p_icp.add_argument("--label", required=True,
                        choices=["icp_match", "icp_miss", "disqualified", "unknown"])
    p_icp.add_argument("--project", required=True,
                       help="Project name from config.json (e.g., 'mk0r', 'Assrt')")
    p_icp.add_argument("--notes", default=None)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    dbmod.load_env()

    # DB-free lane: when no DATABASE_URL is configured (e.g. a freshly onboarded
    # dev machine with only the s4l.ai HTTP API reachable), route through the
    # HTTP endpoints. On a DB-equipped machine this branch is skipped entirely,
    # so the reddit/twitter rails keep their full DB-side behaviour (campaign
    # suffix attribution, dm_links backfill) unchanged.
    if not os.environ.get("DATABASE_URL"):
        if _http_dispatch(args):
            return
        print(
            f"ERROR: '{args.command}' is not yet wired for the no-DATABASE_URL "
            f"(HTTP) lane. Run this command on a DB-equipped machine, or extend "
            f"_http_dispatch in dm_conversation.py.",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = dbmod.get_conn()

    if args.command == "log-outbound":
        ok = log_outbound(conn, args.dm_id, args.content, args.author,
                          verified=args.verified)
        if not ok:
            sys.exit(3)
    elif args.command == "ensure-dm":
        dm_id, created, linked_reply_id = ensure_dm(
            conn, args.platform, args.author,
            chat_url=args.chat_url, lookback_hours=args.lookback_hours,
        )
        print(f"DM_ID={dm_id}")
        if created:
            if linked_reply_id:
                print(f"  created (linked to replies.id={linked_reply_id})")
            else:
                print("  created (no matching replies row within lookback, reply_id/post_id NULL)")
        else:
            print("  existing")
    elif args.command == "log-inbound":
        log_inbound(conn, args.dm_id, args.author, args.content,
                    message_at=args.message_at, event_id=args.event_id)
    elif args.command == "history":
        show_history(conn, args.dm_id)
    elif args.command == "pending":
        show_pending(conn)
    elif args.command == "find":
        find_by_author(conn, args.author)
    elif args.command == "summary":
        show_summary(conn)
    elif args.command == "set-url":
        set_chat_url(conn, args.dm_id, args.url)
    elif args.command == "backfill-urls":
        raw = open(args.file).read() if args.file else sys.stdin.read()
        try:
            records = json.loads(raw)
        except Exception as e:
            print(f"ERROR: could not parse JSON input: {e}", file=sys.stderr)
            sys.exit(2)
        if isinstance(records, dict):
            for k in ("conversations", "threads", "dms", "items"):
                if k in records and isinstance(records[k], list):
                    records = records[k]
                    break
        if not isinstance(records, list):
            print("ERROR: expected a JSON array of {author, chat_url} records", file=sys.stderr)
            sys.exit(2)
        backfill_urls(conn, args.platform, records)
    elif args.command == "filter-inbox":
        raw = open(args.file).read() if args.file else sys.stdin.read()
        try:
            records = json.loads(raw)
        except Exception as e:
            print(f"ERROR: could not parse JSON input: {e}", file=sys.stderr)
            sys.exit(2)
        if isinstance(records, dict):
            for k in ("conversations", "threads", "dms", "items"):
                if k in records and isinstance(records[k], list):
                    records = records[k]
                    break
            else:
                # Some scanners wrap a single thread; treat as 0 records, not an error.
                if records.get("ok") is False:
                    records = []
        if not isinstance(records, list):
            print("ERROR: expected a JSON array of thread records", file=sys.stderr)
            sys.exit(2)
        filter_inbox(conn, args.platform, records)
    elif args.command == "mark-inspected":
        mark_inspected(conn, args.dm_id)
    elif args.command == "set-tier":
        set_tier(conn, args.dm_id, args.tier)
    elif args.command == "set-status":
        set_status(conn, args.dm_id, args.status)
    elif args.command == "set-interest":
        set_interest(conn, args.dm_id, args.interest)
    elif args.command == "set-mode":
        set_mode(conn, args.dm_id, args.mode)
    elif args.command == "flag-human":
        flag_human(conn, args.dm_id, args.reason)
    elif args.command == "show-flagged":
        show_flagged(conn)
    elif args.command == "send-escalation-email":
        row = conn.execute("""
            SELECT platform, their_author, human_reason, conversation_status
            FROM dms WHERE id = %s
        """, (args.dm_id,)).fetchone()
        if not row:
            print(f"ERROR: DM #{args.dm_id} not found")
        elif row["conversation_status"] != "needs_human":
            print(f"WARNING: DM #{args.dm_id} is '{row['conversation_status']}', not 'needs_human'. Sending anyway.")
            _send_escalation_email(conn, args.dm_id, row["platform"], row["their_author"],
                                   row["human_reason"] or "(no reason stored)")
        else:
            _send_escalation_email(conn, args.dm_id, row["platform"], row["their_author"],
                                   row["human_reason"] or "(no reason stored)")
    elif args.command == "set-project":
        set_project(conn, args.dm_id, args.project, append=getattr(args, "append", False))
    elif args.command == "set-target-project":
        set_target_project(conn, args.dm_id, args.project, append=getattr(args, "append", False))
    elif args.command == "set-qualification":
        set_qualification(conn, args.dm_id, args.status, args.notes)
    elif args.command == "mark-booking-sent":
        mark_booking_sent(conn, args.dm_id)
    elif args.command == "mark-skipped":
        mark_skipped(conn, args.dm_id, args.reason)
    elif args.command == "set-icp-precheck":
        set_icp_precheck(conn, args.dm_id, args.label, args.project, args.notes)

    conn.close()


if __name__ == "__main__":
    main()
