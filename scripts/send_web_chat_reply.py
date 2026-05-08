#!/usr/bin/env python3
"""Send a founder reply on a web-chat thread.

Mirror of ~/fazm/inbox/scripts/send-chat-reply.js. Three things:
  1. INSERT a new web_chat_messages row with sender='agent' (or 'founder' if
     the override-via-Gmail rail produced it; that path is handled in
     ingest_web_chat_replies.py instead).
  2. Mark all visitor messages on this thread as read_by_founder=true.
  3. Bump web_chat_threads metadata: last_message_*, unread_by_founder=0,
     unread_by_visitor +=1.

Also fires a Resend email to the visitor's email so they receive the reply
even when the widget is closed (this is the key UX win over the Fazm in-app
chat: visitors don't need to be on the page to see the answer).

Dedup guard: skip if a founder/agent message was already sent in the last 60
seconds (matches Fazm behaviour).

Usage:
  python3 send_web_chat_reply.py --thread <thread_id> --text "reply" [--name "matt"]
                                 [--sender agent|founder] [--no-email]
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

SEND_EMAIL_SCRIPT = os.path.expanduser("~/analytics/scripts/send-email.js")
NODE_BIN = os.path.expanduser("~/.nvm/versions/node/v20.19.4/bin/node")
NODE_PATH = os.path.expanduser("~/analytics/node_modules")


def project_config(project_name):
    """Read the project entry from ~/social-autoposter/config.json."""
    import json

    cfg_path = os.path.expanduser("~/social-autoposter/config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    for p in cfg.get("projects", []):
        if p.get("name") == project_name:
            return p
    return {}


def email_visitor(visitor_email, project_name, text, project_cfg):
    """Forward founder reply to visitor's email via Resend (send-email.js)."""
    if not visitor_email or not SEND_EMAIL_SCRIPT or not os.path.exists(SEND_EMAIL_SCRIPT):
        return None

    web_chat_cfg = (project_cfg or {}).get("web_chat", {}) or {}
    from_email = web_chat_cfg.get("from_email") or "Matt <matt@mail.omi.me>"
    site_pretty = (project_cfg.get("website") or "").replace("https://", "").replace("http://", "").rstrip("/")
    subject = f"re: your message on {site_pretty}" if site_pretty else "re: your message"

    env = os.environ.copy()
    env.setdefault("NODE_PATH", NODE_PATH)

    args = [
        NODE_BIN, SEND_EMAIL_SCRIPT,
        "--to", visitor_email,
        "--subject", subject,
        "--body", text,
        "--from", from_email,
        "--no-db",  # web_chat has its own DB rail
    ]
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
        # send-email.js prints "Sent! Resend ID: <id>"
        for line in (result.stdout or "").splitlines():
            if "Resend ID:" in line:
                return line.split("Resend ID:")[-1].strip()
    except Exception as e:
        print(f"  WARN: visitor email send failed: {e}", file=sys.stderr)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--name", default="matt")
    parser.add_argument("--sender", default="agent", choices=["agent", "founder"])
    parser.add_argument("--no-email", action="store_true",
                        help="Skip Resend forward to visitor (used when ingest already emailed)")
    args = parser.parse_args()

    dbmod.load_env()
    conn = dbmod.get_conn()

    # Dedup guard: skip if any founder/agent message in last 60s.
    recent = conn.execute(
        """
        SELECT id, EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_s
          FROM web_chat_messages
         WHERE thread_id = %s
           AND sender IN ('agent', 'founder')
           AND created_at > NOW() - INTERVAL '60 seconds'
         ORDER BY created_at DESC
         LIMIT 1
        """,
        (args.thread,),
    ).fetchone()
    if recent:
        print(f"dedup: founder/agent message sent {int(recent['age_s'])}s ago, skipping")
        return

    # Look up thread metadata for visitor email forwarding.
    thread = conn.execute(
        """
        SELECT visitor_email, visitor_name, project_name
          FROM web_chat_threads
         WHERE thread_id = %s
        """,
        (args.thread,),
    ).fetchone()
    if not thread:
        print(f"ERROR: thread {args.thread} not found", file=sys.stderr)
        sys.exit(1)

    # 1. Insert agent/founder message.
    msg_cur = conn.execute(
        """
        INSERT INTO web_chat_messages (thread_id, sender, sender_name, text)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (args.thread, args.sender, args.name, args.text),
    )
    msg_id = msg_cur.fetchone()["id"]

    # 2. Mark visitor messages read.
    conn.execute(
        """
        UPDATE web_chat_messages
           SET read_by_founder = TRUE
         WHERE thread_id = %s
           AND sender = 'visitor'
           AND read_by_founder = FALSE
        """,
        (args.thread,),
    )

    # 3. Bump thread metadata.
    conn.execute(
        """
        UPDATE web_chat_threads
           SET last_message_text = %s,
               last_message_at = NOW(),
               last_message_sender = %s,
               unread_by_founder = 0,
               unread_by_visitor = unread_by_visitor + 1
         WHERE thread_id = %s
        """,
        (args.text[:300], args.sender, args.thread),
    )
    conn.commit()

    # 4. Forward to visitor email so they get reply when widget is closed.
    visitor_resend_id = None
    if not args.no_email and thread["visitor_email"]:
        cfg = project_config(thread["project_name"])
        visitor_resend_id = email_visitor(
            thread["visitor_email"], thread["project_name"], args.text, cfg
        )
        if visitor_resend_id:
            conn.execute(
                "UPDATE web_chat_messages SET visitor_email_id = %s WHERE id = %s",
                (visitor_resend_id, msg_id),
            )
            conn.commit()

    print(f"sent reply (msg #{msg_id}, visitor_email_id={visitor_resend_id or '-'})")


if __name__ == "__main__":
    main()
