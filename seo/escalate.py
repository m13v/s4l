#!/usr/bin/env python3
"""SEO generation escalation rail.

Mirrors the DM escalation pattern in scripts/dm_conversation.py +
scripts/ingest_human_seo_replies.py:

  1. open    -- insert a row into seo_escalations, send email to i@m13v.com
               with subject [SEO #N] product/keyword: reason. Caller can
               supply --session-id / --log-path for full audit trail.
  2. list    -- list pending / replied / all escalations.
  3. show    -- print a single escalation with full history.
  4. cancel  -- close out an escalation without resuming (manual override).
  5. mark-resumed -- called by generate_page.py --resume-escalation after
                    a successful re-run; flips status='resumed', stores
                    the new run log path + outcome.

Usage:
    python3 seo/escalate.py open --product fde10x --keyword "ai phone agent" \
        --slug ai-phone-agent --reason "consumer setup missing 4 phases" \
        --trigger-kind setup_gate --source-table seo_keywords --source-id 123 \
        [--session-id <uuid>] [--log-path /abs/path]
    python3 seo/escalate.py list [--status pending] [--product X]
    python3 seo/escalate.py show --id 7
    python3 seo/escalate.py cancel --id 7 --note "fixed manually"
    python3 seo/escalate.py mark-resumed --id 7 \
        --log-path /abs/path --outcome success

Per-(product, keyword) 24h debounce: open will refuse a second escalation
within 24h of an earlier one for the same pair, regardless of its status.
This is enforced in Python because the unique partial index in the schema
only blocks two simultaneous pending rows; we want to also block churn.
"""

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "scripts"))
from http_api import api_get, api_patch, api_post, load_env  # noqa: E402

load_env()

GMAIL_TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
GMAIL_SCOPES = ["https://mail.google.com/"]
NOTIFICATION_EMAIL = os.environ.get("NOTIFICATION_EMAIL", "i@m13v.com")
ESCALATION_LOG = SCRIPT_DIR / "escalations.log"


def _scrub_dashes(s):
    """Replace em/en dashes with commas. Same rule as DM pipeline; em
    dashes in subjects garble in some email clients, and the user has a
    no-dashes preference globally."""
    if not s:
        return s
    return s.replace("\u2014", ",").replace("\u2013", ",")


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


def _append_log(line):
    """Append a single line to seo/escalations.log. Greppable timeline
    independent of the DB; survives even if Postgres is unreachable later."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(ESCALATION_LOG, "a") as f:
            f.write(f"{ts} {line}\n")
    except OSError:
        pass


def _send_escalation_email(escalation_id, product, keyword, slug, reason,
                           trigger_kind, run_log_path, claude_session_id,
                           source_table, source_id):
    """Send the escalation email. Subject embeds [SEO #N] so the ingest
    script can match the reply back. Body includes everything a human
    needs to decide what to do without opening any other tool."""
    repo_hint = ""
    project_path = ""
    try:
        cfg_path = SCRIPT_DIR.parent / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        for p in cfg.get("projects", []):
            if (p.get("name") or "").lower() == (product or "").lower():
                lp = p.get("landing_pages") or {}
                project_path = lp.get("repo") or ""
                repo_hint = f"Consumer repo: {project_path}\n" if project_path else ""
                break
    except Exception:
        pass

    log_block = f"Run log: {run_log_path}\n" if run_log_path else ""
    session_block = f"Claude session: {claude_session_id}\n" if claude_session_id else ""

    body = (
        f"SEO #{escalation_id} [{trigger_kind}] for {product} / {keyword}\n\n"
        f"Reason: {reason}\n"
        f"Slug: {slug or '(unset)'}\n"
        f"Source row: {source_table}#{source_id if source_id else '?'}\n"
        f"{repo_hint}{session_block}{log_block}\n"
        f"---\n"
        f"Reply to this email to unblock the run. Your reply will be picked\n"
        f"up by scripts/ingest_human_seo_replies.py and prepended to the\n"
        f"next generation attempt under === HUMAN GUIDANCE ===.\n"
        f"Keep the [SEO #{escalation_id}] token in the subject line so the\n"
        f"pipeline can route it.\n"
        f"\n"
        f"To inspect: python3 seo/escalate.py show --id {escalation_id}\n"
        f"To cancel:  python3 seo/escalate.py cancel --id {escalation_id} --note \"...\"\n"
    )

    subject = _scrub_dashes(
        f"[SEO #{escalation_id}] {product}/{keyword}: {(reason or '')}"
    )
    body = _scrub_dashes(body)

    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = NOTIFICATION_EMAIL
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    try:
        service = _gmail_service()
        result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return result.get("id", "")
    except Exception as e:
        print(f"  WARNING: Failed to send escalation email for #{escalation_id}: {e}",
              file=sys.stderr)
        return None


def cmd_open(args):
    # 2026-05-16: model_initiated trigger is disabled. The pipeline policy
    # is now "if the model is unsure, mark the keyword skip and move on"
    # rather than email the human. We still accept the call from the model
    # (so old prompts don't crash) but instead of emailing, we flip the
    # seo_keywords / gsc_queries row to status='skip' with the reason as
    # notes, then exit successfully. setup_gate and reaper_stuck still
    # escalate normally because those are operational, not model-judgment,
    # blockers.
    if args.trigger_kind == "model_initiated":
        conn = db_helpers.get_conn()
        cur = conn.cursor()
        skip_note = f"auto-skip (model_initiated, no escalation): {args.reason}"[:1000]
        # seo_keywords path
        cur.execute(
            "UPDATE seo_keywords SET status='skip', "
            "notes = COALESCE(notes,'') || E'\\n' || %s, "
            "updated_at = NOW() "
            "WHERE product = %s AND keyword = %s AND status != 'done' "
            "RETURNING id",
            (skip_note, args.product, args.keyword),
        )
        sk_rows = cur.fetchall()
        # gsc_queries path (same product+keyword may live here too)
        cur.execute(
            "UPDATE gsc_queries SET status='skip', "
            "notes = COALESCE(notes,'') || E'\\n' || %s, "
            "updated_at = NOW() "
            "WHERE product = %s AND query = %s AND status != 'done' "
            "RETURNING id",
            (skip_note, args.product, args.keyword),
        )
        gq_rows = cur.fetchall()
        conn.commit()
        _append_log(
            f"auto_skip model_initiated product={args.product} "
            f"keyword=\"{args.keyword}\" seo_keywords_rows={len(sk_rows)} "
            f"gsc_queries_rows={len(gq_rows)}"
        )
        print(json.dumps({
            "ok": True,
            "action": "auto_skip",
            "reason": "model_initiated trigger is disabled; keyword marked skip instead of emailing",
            "product": args.product,
            "keyword": args.keyword,
            "seo_keywords_updated": len(sk_rows),
            "gsc_queries_updated": len(gq_rows),
        }))
        cur.close(); conn.close()
        return 0

    conn = db_helpers.get_conn()
    cur = conn.cursor()

    existing = _recent_open_or_replied(cur, args.product, args.keyword, hours=24)
    if existing and not args.force:
        eid, status, asked_at = existing
        print(json.dumps({
            "ok": False,
            "reason": "debounced",
            "existing_id": eid,
            "existing_status": status,
            "asked_at": asked_at.isoformat() if asked_at else None,
        }))
        cur.close(); conn.close()
        sys.exit(2)

    cur.execute(
        """
        INSERT INTO seo_escalations
            (source_table, source_id, product, keyword, slug,
             claude_session_id, run_log_path, reason, trigger_kind)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            args.source_table, args.source_id, args.product, args.keyword,
            args.slug, args.session_id, args.log_path, args.reason,
            args.trigger_kind,
        ),
    )
    escalation_id = cur.fetchone()[0]

    note = f"\n[escalated #{escalation_id} {args.trigger_kind}]: {args.reason}"
    _stamp_source_row(
        cur, args.source_table, args.source_id, escalation_id,
        args.product, args.keyword,
        mark_status="escalated" if args.set_status_escalated else None,
        append_note=note,
    )
    conn.commit()

    gmail_id = _send_escalation_email(
        escalation_id=escalation_id,
        product=args.product,
        keyword=args.keyword,
        slug=args.slug,
        reason=args.reason,
        trigger_kind=args.trigger_kind,
        run_log_path=args.log_path,
        claude_session_id=args.session_id,
        source_table=args.source_table,
        source_id=args.source_id,
    )
    if gmail_id:
        cur.execute(
            "UPDATE seo_escalations SET gmail_outbound_id = %s, updated_at = NOW() WHERE id = %s",
            (gmail_id, escalation_id),
        )
        conn.commit()

    _append_log(
        f"open #{escalation_id} product={args.product} keyword=\"{args.keyword}\" "
        f"trigger={args.trigger_kind} gmail={gmail_id or 'NONE'} "
        f"log={args.log_path or 'NONE'}"
    )
    print(json.dumps({
        "ok": True,
        "escalation_id": escalation_id,
        "gmail_outbound_id": gmail_id,
        "notification_email": NOTIFICATION_EMAIL,
    }))
    cur.close(); conn.close()


def cmd_list(args):
    conn = db_helpers.get_conn()
    cur = conn.cursor()
    where = []
    vals: list = []
    if args.status:
        where.append("status = %s"); vals.append(args.status)
    if args.product:
        where.append("product = %s"); vals.append(args.product)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    cur.execute(
        f"""
        SELECT id, status, trigger_kind, product, keyword, slug,
               asked_at, replied_at, resumed_at, reason
        FROM seo_escalations
        {where_sql}
        ORDER BY asked_at DESC
        LIMIT %s
        """,
        vals + [args.limit],
    )
    rows = cur.fetchall()
    if args.json:
        out = []
        for r in rows:
            out.append({
                "id": r[0], "status": r[1], "trigger_kind": r[2],
                "product": r[3], "keyword": r[4], "slug": r[5],
                "asked_at": r[6].isoformat() if r[6] else None,
                "replied_at": r[7].isoformat() if r[7] else None,
                "resumed_at": r[8].isoformat() if r[8] else None,
                "reason": r[9],
            })
        print(json.dumps(out, indent=2))
    else:
        print(f"{'ID':<5} {'STATUS':<10} {'TRIGGER':<16} {'PRODUCT':<14} {'KEYWORD':<40} ASKED")
        for r in rows:
            asked = r[6].strftime('%Y-%m-%d %H:%M') if r[6] else '?'
            kw = (r[4] or '')[:40]
            print(f"{r[0]:<5} {r[1]:<10} {r[2]:<16} {r[3]:<14} {kw:<40} {asked}")
    cur.close(); conn.close()


def cmd_show(args):
    conn = db_helpers.get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM seo_escalations WHERE id = %s",
        (args.id,),
    )
    row = cur.fetchone()
    if not row:
        print(f"ERROR: escalation #{args.id} not found", file=sys.stderr)
        sys.exit(1)
    cols = [d[0] for d in cur.description]
    out = {}
    for c, v in zip(cols, row):
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        out[c] = v
    print(json.dumps(out, indent=2, default=str))
    cur.close(); conn.close()


def cmd_cancel(args):
    conn = db_helpers.get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE seo_escalations
        SET status = 'cancelled',
            resume_outcome = COALESCE(%s, resume_outcome),
            updated_at = NOW()
        WHERE id = %s AND status IN ('pending','replied')
        RETURNING product, keyword
        """,
        (args.note, args.id),
    )
    row = cur.fetchone()
    if not row:
        print(f"ERROR: escalation #{args.id} not in cancellable state", file=sys.stderr)
        sys.exit(1)
    cur.execute(
        """
        UPDATE seo_keywords SET open_escalation_id = NULL, updated_at = NOW()
        WHERE open_escalation_id = %s
        """, (args.id,))
    cur.execute(
        """
        UPDATE gsc_queries SET open_escalation_id = NULL, updated_at = NOW()
        WHERE open_escalation_id = %s
        """, (args.id,))
    conn.commit()
    _append_log(f"cancel #{args.id} note=\"{(args.note or '')[:120]}\"")
    print(json.dumps({"ok": True, "id": args.id, "product": row[0], "keyword": row[1]}))
    cur.close(); conn.close()


def cmd_mark_resumed(args):
    conn = db_helpers.get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE seo_escalations
        SET status = 'resumed',
            resumed_at = NOW(),
            resumed_run_log_path = %s,
            resume_outcome = %s,
            updated_at = NOW()
        WHERE id = %s AND status = 'replied'
        RETURNING product, keyword
        """,
        (args.log_path, args.outcome, args.id),
    )
    row = cur.fetchone()
    if not row:
        print(f"ERROR: escalation #{args.id} not in 'replied' state", file=sys.stderr)
        sys.exit(1)
    cur.execute(
        "UPDATE seo_keywords SET open_escalation_id = NULL, updated_at = NOW() WHERE open_escalation_id = %s",
        (args.id,))
    cur.execute(
        "UPDATE gsc_queries SET open_escalation_id = NULL, updated_at = NOW() WHERE open_escalation_id = %s",
        (args.id,))
    conn.commit()
    _append_log(f"resumed #{args.id} outcome={args.outcome} log={args.log_path or 'NONE'}")
    print(json.dumps({"ok": True, "id": args.id, "product": row[0], "keyword": row[1]}))
    cur.close(); conn.close()


def main():
    p = argparse.ArgumentParser(description="SEO escalation rail")
    sub = p.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="Open a new escalation + send email")
    p_open.add_argument("--product", required=True)
    p_open.add_argument("--keyword", required=True)
    p_open.add_argument("--slug", default=None)
    p_open.add_argument("--reason", required=True)
    p_open.add_argument("--trigger-kind", required=True,
                        choices=["model_initiated", "setup_gate", "reaper_stuck"])
    p_open.add_argument("--source-table", required=True,
                        choices=["seo_keywords", "gsc_queries"])
    p_open.add_argument("--source-id", type=int, default=None)
    p_open.add_argument("--session-id", default=None,
                        help="Claude session UUID from the run that escalated")
    p_open.add_argument("--log-path", default=None,
                        help="Absolute path to the .log file from the run")
    p_open.add_argument("--set-status-escalated", action="store_true",
                        help="Flip the source row's status to 'escalated' too")
    p_open.add_argument("--force", action="store_true",
                        help="Skip the 24h dedupe guard")
    p_open.set_defaults(func=cmd_open)

    p_list = sub.add_parser("list", help="List escalations")
    p_list.add_argument("--status", choices=["pending", "replied", "resumed", "cancelled"])
    p_list.add_argument("--product")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show one escalation in full")
    p_show.add_argument("--id", type=int, required=True)
    p_show.set_defaults(func=cmd_show)

    p_cancel = sub.add_parser("cancel", help="Cancel an open escalation")
    p_cancel.add_argument("--id", type=int, required=True)
    p_cancel.add_argument("--note", default=None)
    p_cancel.set_defaults(func=cmd_cancel)

    p_resumed = sub.add_parser("mark-resumed",
                               help="Mark an escalation resumed after a successful re-run")
    p_resumed.add_argument("--id", type=int, required=True)
    p_resumed.add_argument("--log-path", default=None)
    p_resumed.add_argument("--outcome", default="success")
    p_resumed.set_defaults(func=cmd_mark_resumed)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
