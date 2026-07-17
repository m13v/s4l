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
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")


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
    # No hardcoded fallback on any platform: a default handle/username/name stamped
    # on a DM silently mis-attributes it to the repo owner. Resolve from config / env
    # through the one resolver; "" means unknown (caller decides how to degrade).
    from account_resolver import resolve as _resolve_account
    if platform == "reddit":
        return accounts.get("reddit", {}).get("username") or _resolve_account("reddit") or ""
    elif platform == "linkedin":
        return accounts.get("linkedin", {}).get("name") or _resolve_account("linkedin") or ""
    elif platform == "x":
        # Twitter fails LOUD if unresolved (an outbound DM must carry a real
        # sender); reddit/linkedin above degrade to "" since their callers can.
        h = _resolve_account("twitter")
        if not h:
            raise RuntimeError(
                "no Twitter handle configured (accounts.twitter.handle / "
                "AUTOPOSTER_TWITTER_HANDLE); refusing to stamp a fallback account "
                "on an outbound DM to avoid wrong-attribution. Run connect_x first.")
        return h.lstrip("@")
    return "unknown"


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

    # HTTP-only lane: every command routes through the s4l.ai HTTP API. The
    # direct-Postgres lane was removed 2026-06-01 — there is NO database-driven
    # path any more, not as primary, not as fallback. DATABASE_URL, if present
    # in the environment, is deliberately ignored; all reads/writes go through
    # _http_dispatch against /api/v1/*.
    if _http_dispatch(args):
        return
    print(
        f"ERROR: '{args.command}' is not wired for the HTTP API lane. "
        f"Extend _http_dispatch in dm_conversation.py — there is no DB fallback.",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
