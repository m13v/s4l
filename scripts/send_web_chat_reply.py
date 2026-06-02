#!/usr/bin/env python3
"""Send a founder reply on a web-chat thread (HTTP-only DB layer).

Mirror of ~/fazm/inbox/scripts/send-chat-reply.js. The DB work (dedup guard,
insert the agent/founder message, mark visitor messages read, bump thread
metadata) runs through POST /api/v1/web-chat/threads/<id>/reply. The Resend
email forward to the visitor stays local (send-email.js), and the resulting
Resend id is stamped back onto the message via PATCH
/api/v1/web-chat/messages/<id>.

Dedup guard (server-side): skip if a founder/agent message was already sent in
the last 60 seconds (matches Fazm behaviour).

Usage:
  python3 send_web_chat_reply.py --thread <thread_id> --text "reply" [--name "matt"]
                                 [--sender agent|founder] [--no-email]
                                 [--ingested-gmail-id <gmail_id>]
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_post, api_patch

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
    parser.add_argument("--ingested-gmail-id", default=None,
                        help="Stamp this Gmail id on the inserted message (Gmail-ingest rail dedup)")
    args = parser.parse_args()

    # 1-3 (dedup + insert + mark-read + bump) all happen server-side.
    body = {
        "text": args.text,
        "name": args.name,
        "sender": args.sender,
    }
    if args.ingested_gmail_id:
        body["ingested_gmail_id"] = args.ingested_gmail_id

    resp = api_post(
        f"/api/v1/web-chat/threads/{args.thread}/reply", body, ok_on_404=True,
    )
    if resp.get("_not_found"):
        print(f"ERROR: thread {args.thread} not found", file=sys.stderr)
        sys.exit(1)

    data = resp.get("data") or {}
    if data.get("deduped"):
        print(f"dedup: founder/agent message sent {int(data.get('age_s', 0))}s ago, skipping")
        return

    msg_id = data.get("message_id")
    visitor_email = data.get("visitor_email") or ""
    project_name = data.get("project_name")

    # 4. Forward to visitor email so they get the reply when widget is closed.
    visitor_resend_id = None
    if not args.no_email and visitor_email:
        cfg = project_config(project_name)
        visitor_resend_id = email_visitor(visitor_email, project_name, args.text, cfg)
        if visitor_resend_id and msg_id:
            api_patch(
                f"/api/v1/web-chat/messages/{msg_id}",
                {"visitor_email_id": visitor_resend_id},
            )

    print(f"sent reply (msg #{msg_id}, visitor_email_id={visitor_resend_id or '-'})")


if __name__ == "__main__":
    main()
