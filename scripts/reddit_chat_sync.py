#!/usr/bin/env python3
"""Read Reddit Chat state directly from the matrix-js-sdk IndexedDB cache.

Reddit Chat is a Matrix (vanilla v3) client. The entire joined-rooms state,
including per-room unread counts, member displaynames, and recent timeline
events, is persisted client-side in IndexedDB under
`matrix-js-sdk:reddit-chat-sync` -> `sync` store.

Reading that store lets us answer "which rooms have unread messages and what
do they contain" WITHOUT scrolling the virtual sidebar and WITHOUT originating
any API calls. We only read state that the Reddit client itself already
fetched as part of its normal page hydration. This matches the passive CDP
pattern in twitter_browser.py's reply_to_tweet() and stays well inside the
"don't originate calls the human wouldn't" line that took LinkedIn down on
2026-04-17.

Usage:
    python3 reddit_chat_sync.py list-unread             # JSON to stdout
    python3 reddit_chat_sync.py list-unread --pretty    # formatted JSON

The output is an array of records with fields:
    room_id, chat_url, unread_count, room_name, partner_username,
    partner_mxid, last_event_id, last_event_ts, last_event_body,
    last_event_from_us, timeline (array of recent events).

This command is strictly read-only. DB writes come in a later subcommand.

Requires: pip install playwright && playwright install chromium
Shares the reddit-agent Chromium profile + lock used by reddit_browser.py.
"""

import argparse
import atexit
import json
import os
import sys
import time
from datetime import datetime, timezone

PROFILE_DIR = os.path.expanduser("~/.claude/browser-profiles/reddit")
LOCK_FILE = os.path.expanduser("~/.claude/reddit-agent-lock.json")
LOCK_EXPIRY = 300
LOCK_WAIT_MAX = 45
LOCK_POLL_INTERVAL = 2
VIEWPORT = {"width": 911, "height": 1016}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Our Reddit username via the ONE resolver (env -> reddit_account login truth ->
# accounts.reddit.username). "" means "unknown account" rather than impersonating
# the repo owner when no reddit account is configured.
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from account_resolver import resolve as _resolve_account
    OUR_USERNAME = _resolve_account("reddit") or ""
except Exception:
    OUR_USERNAME = ""

_LOCK_SESSION_ID = f"python:{os.getpid()}"


def _release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                lock = json.load(f)
            if lock.get("session_id") == _LOCK_SESSION_ID:
                os.remove(LOCK_FILE)
    except (json.JSONDecodeError, OSError):
        pass


atexit.register(_release_lock)


def _acquire_lock():
    deadline = time.time() + LOCK_WAIT_MAX
    while True:
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE) as f:
                    lock = json.load(f)
                age = time.time() - lock.get("timestamp", 0)
                if age >= LOCK_EXPIRY:
                    break
                holder = lock.get("session_id", "unknown")
                if time.time() >= deadline:
                    print(
                        json.dumps(
                            {
                                "success": False,
                                "error": f"Reddit browser locked by session {holder} ({int(age)}s); waited {LOCK_WAIT_MAX}s, giving up.",
                            }
                        )
                    )
                    sys.exit(1)
                time.sleep(LOCK_POLL_INTERVAL)
                continue
            except (json.JSONDecodeError, OSError):
                pass
        break
    with open(LOCK_FILE, "w") as f:
        json.dump(
            {"session_id": _LOCK_SESSION_ID, "timestamp": int(time.time())}, f
        )


# JS we run inside the page. Extracts every joined room that has
# unread_notifications.notification_count > 0, plus enough context to
# reconstruct the conversation.
_EXTRACT_JS = r"""
async () => {
  const REDDIT_SYSTEM_BOT = '@t2_1qwk:reddit.com';

  const openReq = indexedDB.open('matrix-js-sdk:reddit-chat-sync');
  const conn = await new Promise((res, rej) => {
    openReq.onsuccess = () => res(openReq.result);
    openReq.onerror = () => rej(openReq.error);
  });

  const row = await new Promise((res, rej) => {
    const tx = conn.transaction('sync', 'readonly');
    const req = tx.objectStore('sync').getAll();
    req.onsuccess = () => res(req.result[0] || null);
    req.onerror = () => rej(req.error);
  });
  conn.close();

  if (!row || !row.roomsData || !row.roomsData.join) {
    return { ok: false, error: 'no_sync_row', total_joined: 0, unread: [] };
  }

  const join = row.roomsData.join;
  const unread = [];

  for (const [roomId, r] of Object.entries(join)) {
    const nc = (r.unread_notifications && r.unread_notifications.notification_count) || 0;
    const hc = (r.unread_notifications && r.unread_notifications.highlight_count) || 0;
    if (nc === 0 && hc === 0) continue;

    const stateEvents = (r.state && r.state.events) || [];
    const memberEvents = stateEvents.filter(e => e.type === 'm.room.member');
    const nameEv = stateEvents.find(e => e.type === 'm.room.name');
    const roomName = nameEv ? (nameEv.content && nameEv.content.name) || null : null;

    // Identify our mxid and the partner mxid. The Reddit system bot
    // (@t2_1qwk:reddit.com) is a member of every room and must be excluded
    // from partner resolution. Our mxid is identified by displayname match
    // to OUR_USERNAME.
    const ourMxid = memberEvents.find(m =>
      m.content && m.content.displayname === %OUR_USERNAME_LITERAL%
    )?.state_key || null;

    const partnerMember = memberEvents.find(m =>
      m.state_key !== ourMxid &&
      m.state_key !== REDDIT_SYSTEM_BOT &&
      m.content && m.content.displayname
    );

    const timeline = (r.timeline && r.timeline.events) || [];
    // Last human message
    const lastMsg = [...timeline]
      .reverse()
      .find(e => e.type === 'm.room.message');

    // Return the last ~30 timeline events so the caller has enough context
    // to log each new message without re-fetching. We don't return every
    // event to keep the payload reasonable on old rooms.
    const recentTimeline = timeline.slice(-30).map(e => ({
      event_id: e.event_id,
      ts: e.origin_server_ts,
      sender: e.sender,
      type: e.type,
      body: (e.content && e.content.body) || null,
      msgtype: (e.content && e.content.msgtype) || null,
      from_us: e.sender === ourMxid,
    }));

    unread.push({
      room_id: roomId,
      chat_url: 'https://www.reddit.com/chat/room/' + encodeURIComponent(roomId),
      unread_count: nc,
      highlight_count: hc,
      room_name: roomName,
      partner_username: (partnerMember && partnerMember.content && partnerMember.content.displayname) || null,
      partner_mxid: (partnerMember && partnerMember.state_key) || null,
      our_mxid: ourMxid,
      last_event_id: (lastMsg && lastMsg.event_id) || null,
      last_event_ts: (lastMsg && lastMsg.origin_server_ts) || null,
      last_event_body: (lastMsg && lastMsg.content && lastMsg.content.body) || null,
      last_event_from_us: (lastMsg && lastMsg.sender === ourMxid) || false,
      timeline: recentTimeline,
    });
  }

  // Sort by unread_count desc then last_event_ts desc so operators see the
  // loudest threads first.
  unread.sort((a, b) =>
    (b.unread_count - a.unread_count) ||
    ((b.last_event_ts || 0) - (a.last_event_ts || 0))
  );

  return {
    ok: true,
    total_joined: Object.keys(join).length,
    unread_room_count: unread.length,
    total_unread_messages: unread.reduce((s, r) => s + r.unread_count, 0),
    next_batch: row.nextBatch || null,
    unread,
  };
}
""".replace("%OUR_USERNAME_LITERAL%", json.dumps(OUR_USERNAME))


# Minimal extraction: every joined room, just partner resolution + room_id.
# Used for the full chat_url backfill across the ~737 rooms the user has ever
# joined, not just the unread subset. No timeline, no message bodies.
_EXTRACT_ALL_ROOMS_JS = r"""
async () => {
  const REDDIT_SYSTEM_BOT = '@t2_1qwk:reddit.com';
  const openReq = indexedDB.open('matrix-js-sdk:reddit-chat-sync');
  const conn = await new Promise((res, rej) => {
    openReq.onsuccess = () => res(openReq.result);
    openReq.onerror = () => rej(openReq.error);
  });
  const row = await new Promise((res, rej) => {
    const tx = conn.transaction('sync', 'readonly');
    const req = tx.objectStore('sync').getAll();
    req.onsuccess = () => res(req.result[0] || null);
    req.onerror = () => rej(req.error);
  });
  conn.close();
  if (!row || !row.roomsData || !row.roomsData.join) {
    return { ok: false, error: 'no_sync_row', rooms: [] };
  }
  const join = row.roomsData.join;
  const rooms = [];
  for (const [roomId, r] of Object.entries(join)) {
    const stateEvents = (r.state && r.state.events) || [];
    const memberEvents = stateEvents.filter(e => e.type === 'm.room.member');
    const ourMxid = memberEvents.find(m =>
      m.content && m.content.displayname === %OUR_USERNAME_LITERAL%
    )?.state_key || null;
    const partner = memberEvents.find(m =>
      m.state_key !== ourMxid &&
      m.state_key !== REDDIT_SYSTEM_BOT &&
      m.content && m.content.displayname
    );
    const nc = (r.unread_notifications && r.unread_notifications.notification_count) || 0;
    rooms.push({
      room_id: roomId,
      chat_url: 'https://www.reddit.com/chat/room/' + encodeURIComponent(roomId),
      partner_username: (partner && partner.content && partner.content.displayname) || null,
      partner_mxid: (partner && partner.state_key) || null,
      unread_count: nc,
    });
  }
  return { ok: true, total_joined: rooms.length, rooms };
}
""".replace("%OUR_USERNAME_LITERAL%", json.dumps(OUR_USERNAME))


def _open_and_evaluate(js_code, hydration_wait_ms=8000, nav_retries=2):
    """Shared scaffolding: open /chat in a headless Chromium on the reddit
    profile, let matrix-js-sdk finish incremental sync, then run the given
    JS and return its result.

    Returns either the parsed JS return value or an {ok: false, error} record.
    """
    from playwright.sync_api import sync_playwright

    _acquire_lock()
    try:
        with sync_playwright() as p:
            deadline = time.time() + LOCK_WAIT_MAX
            context = None
            while True:
                try:
                    context = p.chromium.launch_persistent_context(
                        PROFILE_DIR,
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled"],
                        viewport=VIEWPORT,
                        user_agent=USER_AGENT,
                    )
                    break
                except Exception as e:
                    if time.time() >= deadline:
                        return {
                            "ok": False,
                            "error": f"chromium profile locked by another process; waited {LOCK_WAIT_MAX}s: {e}",
                        }
                    time.sleep(LOCK_POLL_INTERVAL)

            try:
                page = context.new_page()
                for attempt in range(nav_retries + 1):
                    try:
                        page.goto("https://www.reddit.com/chat", wait_until="domcontentloaded", timeout=30000)
                        break
                    except Exception as e:
                        if attempt == nav_retries:
                            return {"ok": False, "error": f"navigate_failed: {e}"}
                        time.sleep(2)

                page.wait_for_timeout(hydration_wait_ms)
                return page.evaluate(js_code)
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    finally:
        _release_lock()


def list_unread(hydration_wait_ms=8000, nav_retries=2):
    """Return every Matrix room with notification_count > 0 along with its
    partner, last message, and last ~30 timeline events."""
    return _open_and_evaluate(_EXTRACT_JS, hydration_wait_ms, nav_retries)


def list_all_rooms(hydration_wait_ms=8000, nav_retries=2):
    """Return every joined room with {room_id, chat_url, partner_username,
    unread_count}. Same IndexedDB source, no timeline payload. Used for the
    full chat_url backfill that fills rows the unread-only scan misses."""
    return _open_and_evaluate(_EXTRACT_ALL_ROOMS_JS, hydration_wait_ms, nav_retries)


def ingest_unread(hydration_wait_ms=8000, dry_run=False):
    """Scan every unread Reddit chat room, upsert a dms row (backfilling
    chat_url), and log each inbound m.room.message to dm_messages with its
    Matrix event_id as the dedup key.

    Returns a structured summary of what happened.

    Progress chatter from the helpers is squelched because we emit a single
    JSON doc at the end and don't want it corrupted. The migration to HTTP
    (2026-05-12) removed direct psycopg2 access from this script entirely;
    DM lookups + chat_url updates go through /api/v1/dms*, ensure_dm goes
    through a subprocess to scripts/dm_conversation.py ensure-dm (which is
    the same precedent engage_reddit.py uses), and per-event inbound
    messages POST directly to /api/v1/dms/[id]/messages with event_id
    dedup handled server-side.
    """
    # Lazy import so list-unread doesn't pay for it.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import contextlib  # noqa: E402

    scan = list_unread(hydration_wait_ms=hydration_wait_ms)
    if not scan.get("ok"):
        return scan

    stats = {
        "rooms_scanned": len(scan["unread"]),
        "rooms_new_dms": 0,
        "rooms_existing_dms": 0,
        "chat_urls_backfilled": 0,
        "inbound_inserted": 0,
        "inbound_deduped": 0,
        "skipped_non_message_events": 0,
        "skipped_our_events": 0,
        "rooms_without_partner": 0,
        "errors": [],
    }
    per_room = []

    # Route any stray helper prints to stderr for the whole loop. We emit
    # one JSON doc at the end on stdout and that's it.
    _redirect_cm = contextlib.redirect_stdout(sys.stderr)
    _redirect_cm.__enter__()

    try:
        _ingest_rooms(scan, dry_run, stats, per_room)
    finally:
        _redirect_cm.__exit__(None, None, None)

    return {
        "ok": True,
        "dry_run": dry_run,
        "matrix_total_joined": scan.get("total_joined"),
        "matrix_unread_rooms": scan.get("unread_room_count"),
        "matrix_total_unread_messages": scan.get("total_unread_messages"),
        "matrix_next_batch": scan.get("next_batch"),
        "stats": stats,
        "per_room": per_room,
    }


def _http_lookup_reddit_dm(partner):
    """Return the most-recent reddit dms row for `partner`, or None.

    Replaces the legacy
        SELECT id, chat_url FROM dms WHERE platform='reddit' AND their_author=%s
        ORDER BY id DESC LIMIT 1
    via GET /api/v1/dms?platform=reddit&their_author=partner&limit=1. The
    route's their_author filter is case-insensitive, matching what the
    legacy LOWER(...) backfill clause expected.
    """
    from http_api import api_get  # local import keeps list-unread cheap
    resp = api_get(
        "/api/v1/dms",
        query={"platform": "reddit", "their_author": partner, "limit": 1},
    )
    data = (resp or {}).get("data") or {}
    rows = data.get("dms") or []
    return rows[0] if rows else None


def _http_ensure_reddit_dm(partner, chat_url):
    """Shell out to dm_conversation.py ensure-dm (matches engage_reddit.py
    precedent, which also subprocess-calls the CLI rather than re-implementing
    the cross-link logic over HTTP). Parses `DM_ID=<n>\\n  created (...)` from
    stdout. Returns (dm_id:int, created:bool, error:str|None).
    """
    import subprocess
    cmd = [
        "python3",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "dm_conversation.py"),
        "ensure-dm",
        "--platform", "reddit",
        "--author", partner,
    ]
    if chat_url:
        cmd.extend(["--chat-url", chat_url])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return None, False, f"subprocess failed: {e}"
    if result.returncode != 0:
        return None, False, (result.stderr or "ensure-dm rc != 0").strip()
    out = result.stdout.strip().splitlines()
    dm_id = None
    created = False
    for line in out:
        if line.startswith("DM_ID="):
            try:
                dm_id = int(line.split("=", 1)[1])
            except Exception:
                pass
        elif "created" in line.lower():
            created = True
    if dm_id is None:
        return None, False, f"could not parse DM_ID from stdout: {out!r}"
    return dm_id, created, None


def _http_log_inbound(dm_id, partner, body, message_at_iso, event_id):
    """POST /api/v1/dms/<id>/messages with event_id dedup handled server-side.
    Returns ("inserted", None) on insert, ("deduped", dedup_key) on duplicate,
    ("error", reason) on failure.
    """
    from http_api import api_post
    body_payload = {
        "direction": "inbound",
        "author": partner,
        "content": body,
    }
    if message_at_iso:
        body_payload["message_at"] = message_at_iso
    if event_id:
        body_payload["event_id"] = event_id
    try:
        resp = api_post(f"/api/v1/dms/{dm_id}/messages", body_payload)
    except SystemExit as e:
        # http_api raises SystemExit on terminal HTTP failure
        return "error", f"http_api SystemExit: {e}"
    except Exception as e:
        return "error", str(e)
    data = (resp or {}).get("data") or {}
    if data.get("deduped"):
        return "deduped", data.get("dedup_key") or "unknown"
    return "inserted", None


def _ingest_rooms(scan, dry_run, stats, per_room):
    for room in scan["unread"]:
        partner = room.get("partner_username")
        chat_url = room.get("chat_url")
        if not partner:
            stats["rooms_without_partner"] += 1
            continue

        try:
            existing = _http_lookup_reddit_dm(partner)
        except Exception as e:
            stats["errors"].append({"room_id": room["room_id"], "lookup_error": str(e)})
            continue
        had_chat_url = bool(existing and existing.get("chat_url"))

        if dry_run:
            dm_id = existing["id"] if existing else None
            created = not existing
        else:
            dm_id, created, err = _http_ensure_reddit_dm(partner, chat_url)
            if err is not None:
                stats["errors"].append({"room_id": room["room_id"], "ensure_dm_error": err})
                continue

        if existing and not had_chat_url and chat_url:
            stats["chat_urls_backfilled"] += 1
        if created:
            stats["rooms_new_dms"] += 1
        else:
            stats["rooms_existing_dms"] += 1

        inserted_this_room = 0
        deduped_this_room = 0
        for ev in room.get("timeline", []):
            if ev.get("type") != "m.room.message":
                stats["skipped_non_message_events"] += 1
                continue
            if ev.get("from_us"):
                stats["skipped_our_events"] += 1
                continue
            body = ev.get("body")
            if not body:
                continue
            ts_ms = ev.get("ts")
            message_at_iso = None
            if ts_ms:
                message_at_iso = datetime.fromtimestamp(
                    ts_ms / 1000.0, tz=timezone.utc
                ).isoformat()
            event_id = ev.get("event_id")

            if dry_run:
                # We can't predict dedup without a query; approximate by
                # counting all events as would-insert. The dry-run path is
                # dev-only and the inflated insert count is acceptable.
                inserted_this_room += 1
                continue

            outcome, detail = _http_log_inbound(
                dm_id, partner, body, message_at_iso, event_id,
            )
            if outcome == "inserted":
                inserted_this_room += 1
            elif outcome == "deduped":
                deduped_this_room += 1
            else:
                stats["errors"].append({
                    "room_id": room["room_id"],
                    "log_inbound_error": detail,
                    "event_id": event_id,
                })

        stats["inbound_inserted"] += inserted_this_room
        stats["inbound_deduped"] += deduped_this_room
        per_room.append({
            "room_id": room["room_id"],
            "partner": partner,
            "unread_count_matrix": room["unread_count"],
            "inserted": inserted_this_room,
            "deduped": deduped_this_room,
            "created_new_dm": created if not dry_run else (not existing),
            "chat_url_backfilled": bool(existing and not had_chat_url and chat_url),
        })

    return {
        "ok": True,
        "dry_run": dry_run,
        "matrix_total_joined": scan.get("total_joined"),
        "matrix_unread_rooms": scan.get("unread_room_count"),
        "matrix_total_unread_messages": scan.get("total_unread_messages"),
        "matrix_next_batch": scan.get("next_batch"),
        "stats": stats,
        "per_room": per_room,
    }


def backfill_chat_urls(hydration_wait_ms=8000, dry_run=False):
    """For every joined Reddit chat room, if a platform='reddit' dms row
    exists for that partner with chat_url IS NULL, stamp it with the
    room's chat_url. Read-only lookup when --dry-run is set.

    Unlike ingest-unread (which only processes rooms with unread notifs),
    this walks ALL joined rooms so we can catch historical DMs that have
    been quiet for months but are still in the sidebar.

    Routes used:
      GET   /api/v1/dms?platform=reddit&their_author=<partner>&limit=200
            (their_author filter is case-insensitive, matching the legacy
             `LOWER(their_author)=LOWER(%s)` clause)
      PATCH /api/v1/dms/<id>  with { chat_url }
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from http_api import api_get, api_patch  # noqa: E402

    scan = list_all_rooms(hydration_wait_ms=hydration_wait_ms)
    if not scan.get("ok"):
        return scan

    stats = {
        "rooms_in_sidebar": len(scan["rooms"]),
        "rooms_without_partner": 0,
        "matched_dms_already_filled": 0,
        "matched_dms_filled_now": 0,
        "no_matching_dm": 0,
        "multiple_matching_dms": 0,
    }
    filled = []

    for room in scan["rooms"]:
        partner = room.get("partner_username")
        chat_url = room.get("chat_url")
        if not partner or not chat_url:
            stats["rooms_without_partner"] += 1
            continue

        resp = api_get(
            "/api/v1/dms",
            query={"platform": "reddit", "their_author": partner, "limit": 200},
        )
        rows = ((resp or {}).get("data") or {}).get("dms") or []
        # Legacy SELECT used ORDER BY id DESC; the route orders by
        # discovered_at DESC which picks the same "most recent" row for
        # our backfill purpose (rows[0]).

        if not rows:
            stats["no_matching_dm"] += 1
            continue
        if len(rows) > 1:
            stats["multiple_matching_dms"] += 1
            # Fall through and backfill the most recent row only (rows[0]).

        target = rows[0]
        if target.get("chat_url"):
            stats["matched_dms_already_filled"] += 1
            continue

        if not dry_run:
            api_patch(f"/api/v1/dms/{target['id']}", {"chat_url": chat_url})
        stats["matched_dms_filled_now"] += 1
        filled.append({
            "dm_id": target["id"],
            "partner": partner,
            "chat_url": chat_url,
        })

    return {
        "ok": True,
        "dry_run": dry_run,
        "matrix_total_joined": scan.get("total_joined"),
        "stats": stats,
        "filled_sample": filled[:25],
    }


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command")

    p_list = sub.add_parser(
        "list-unread",
        help="Emit JSON of every Matrix room with unread notifications (read-only).",
    )
    p_list.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON (human-readable). Default is compact.",
    )
    p_list.add_argument(
        "--hydration-ms",
        type=int,
        default=8000,
        help="Milliseconds to wait after /chat navigation for matrix-js-sdk to incremental-sync (default 8000).",
    )

    p_ing = sub.add_parser(
        "ingest-unread",
        help="Upsert every unread Reddit chat room into dms + log each new inbound message into dm_messages.",
    )
    p_ing.add_argument("--dry-run", action="store_true", help="Simulate only; no DB writes.")
    p_ing.add_argument("--pretty", action="store_true")
    p_ing.add_argument("--hydration-ms", type=int, default=8000)

    p_bf = sub.add_parser(
        "backfill-chat-urls",
        help="Walk all joined Reddit chat rooms and fill any platform=reddit dms row with chat_url IS NULL whose author matches.",
    )
    p_bf.add_argument("--dry-run", action="store_true", help="Simulate only; no DB writes.")
    p_bf.add_argument("--pretty", action="store_true")
    p_bf.add_argument("--hydration-ms", type=int, default=8000)

    args = ap.parse_args()

    if args.command == "list-unread":
        result = list_unread(hydration_wait_ms=args.hydration_ms)
    elif args.command == "ingest-unread":
        result = ingest_unread(hydration_wait_ms=args.hydration_ms, dry_run=args.dry_run)
    elif args.command == "backfill-chat-urls":
        result = backfill_chat_urls(hydration_wait_ms=args.hydration_ms, dry_run=args.dry_run)
    else:
        ap.print_help()
        sys.exit(2)

    if args.pretty:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
