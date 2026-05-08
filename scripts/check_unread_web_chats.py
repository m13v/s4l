#!/usr/bin/env python3
"""Check Neon for web-chat threads with unread visitor messages.

Mirror of ~/fazm/inbox/scripts/check-unread-chats.js but reads/writes Neon
Postgres web_chat_threads / web_chat_messages tables instead of Firestore.

Returns JSON array of:
  { thread_id, project, visitor_email, visitor_name, unread, last_message,
    page_url, messages: [{ id, text, sender, sender_name, created_at, read }] }

Also recovers stuck threads (last_message_sender='visitor' but
unread_by_founder=0 with expired claimed_until) by re-flagging them as unread.
This catches Claude sessions that died without unclaiming.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


def main():
    dbmod.load_env()
    conn = dbmod.get_conn()

    # Recover stuck threads first.
    conn.execute(
        """
        UPDATE web_chat_threads
           SET unread_by_founder = 1
         WHERE unread_by_founder = 0
           AND last_message_sender = 'visitor'
           AND (claimed_until IS NULL OR claimed_until < NOW())
           AND (rate_limited_until IS NULL OR rate_limited_until < NOW())
           AND last_message_at > NOW() - INTERVAL '24 hours'
        """
    )
    conn.commit()

    rows = conn.execute(
        """
        SELECT thread_id, project_name, visitor_email, visitor_name,
               unread_by_founder, last_message_text, page_url, last_message_at
          FROM web_chat_threads
         WHERE unread_by_founder > 0
           AND (claimed_until IS NULL OR claimed_until < NOW())
           AND (rate_limited_until IS NULL OR rate_limited_until < NOW())
         ORDER BY last_message_at ASC NULLS LAST
        """
    ).fetchall()

    if not rows:
        print("[]")
        return

    out = []
    for row in rows:
        thread_id = row["thread_id"]
        msgs_cur = conn.execute(
            """
            SELECT id, sender, sender_name, text, read_by_founder, created_at
              FROM web_chat_messages
             WHERE thread_id = %s
             ORDER BY created_at ASC
             LIMIT 200
            """,
            (thread_id,),
        ).fetchall()
        messages = [
            {
                "id": m["id"],
                "text": m["text"] or "",
                "sender": m["sender"],
                "sender_name": m["sender_name"] or "",
                "created_at": m["created_at"].isoformat() if m["created_at"] else "",
                "read": bool(m["read_by_founder"]),
            }
            for m in msgs_cur
        ]
        out.append(
            {
                "thread_id": thread_id,
                "project": row["project_name"],
                "visitor_email": row["visitor_email"] or "",
                "visitor_name": row["visitor_name"] or "",
                "unread": int(row["unread_by_founder"] or 0),
                "last_message": row["last_message_text"] or "",
                "page_url": row["page_url"] or "",
                "messages": messages,
            }
        )

    print(json.dumps(out))


if __name__ == "__main__":
    main()
