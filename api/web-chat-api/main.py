"""web-chat-api — Public ingress for the FounderChatPanel widget.

Sits in front of Neon Postgres so the website widget never holds DB creds.
Validates `project` against config.json on every write, rate-limits per IP,
notifies the founder via Resend on the first message in a thread.

Endpoints
---------

POST /api/web-chat/send
  body: { project, visitorId, threadId?, text, email?, name?, pageUrl?, referrer? }
  - validates project against config.json projects[].name + web_chat.enabled
  - if no threadId, creates one ("wc_<nanoid>")
  - inserts visitor message into web_chat_messages
  - bumps thread (unread_by_founder++, last_message_*)
  - if first message in thread, fires Resend notification to project notify_email
  - returns { threadId, messageId }

GET /api/web-chat/thread/:threadId
  - returns the thread + message history (visitor poll)
  - sets read_by_visitor=true on agent/founder messages

Deploy: see Dockerfile + cloudbuild.yaml. Run locally: uvicorn main:app --reload
"""
from __future__ import annotations

import json
import os
import re
import secrets
import string
import time
from typing import Any, Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

# ---------- Config ----------
DATABASE_URL = os.environ.get("DATABASE_URL")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
CONFIG_JSON_RAW = os.environ.get("SOCIAL_AUTOPOSTER_CONFIG_JSON", "")
NOTIFY_FROM_EMAIL = os.environ.get("NOTIFY_FROM_EMAIL", "Web Chat Agent <matt@mail.omi.me>")
DEFAULT_NOTIFY_EMAIL = os.environ.get("DEFAULT_NOTIFY_EMAIL", "i@m13v.com")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

# Load config.json once at startup (passed in via env var as raw JSON; on
# Cloud Run we'll mount it via Secret Manager or pass at deploy time).
_PROJECTS_BY_NAME: dict[str, dict[str, Any]] = {}
_CORS_ORIGINS: list[str] = []
if CONFIG_JSON_RAW:
    cfg = json.loads(CONFIG_JSON_RAW)
    for p in cfg.get("projects", []):
        name = p.get("name")
        if not name:
            continue
        _PROJECTS_BY_NAME[name] = p
        site = (p.get("website") or "").rstrip("/")
        if site.startswith("http"):
            _CORS_ORIGINS.append(site)
            # also allow www.* / non-www variant
            if "://www." in site:
                _CORS_ORIGINS.append(site.replace("://www.", "://"))
            else:
                _CORS_ORIGINS.append(site.replace("://", "://www."))

# Always allow localhost for dev.
_CORS_ORIGINS.extend(["http://localhost:3000", "http://localhost:3001", "http://127.0.0.1:3000"])

app = FastAPI(title="web-chat-api", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(set(_CORS_ORIGINS)) or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Naive in-process rate limiter (good enough for first-deploy; swap for Redis
# or Cloud Memorystore once volume justifies it).
_RATE_BUCKET: dict[str, list[float]] = {}
_RATE_LIMIT_MAX = 5     # messages
_RATE_LIMIT_WINDOW = 60 # seconds


def _rate_check(ip: str) -> None:
    now = time.time()
    bucket = _RATE_BUCKET.setdefault(ip, [])
    cutoff = now - _RATE_LIMIT_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= _RATE_LIMIT_MAX:
        raise HTTPException(429, "rate limit: max 5 messages per minute per IP")
    bucket.append(now)


def _conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)


_NANOID_ALPHABET = string.ascii_letters + string.digits


def _nanoid(prefix: str, n: int = 16) -> str:
    return prefix + "".join(secrets.choice(_NANOID_ALPHABET) for _ in range(n))


_VISITOR_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")
_THREAD_ID_RE = re.compile(r"^wc_[A-Za-z0-9_\-]{4,64}$")


# ---------- Schemas ----------
class SendIn(BaseModel):
    project: str
    visitorId: str
    threadId: Optional[str] = None
    text: str = Field(min_length=1, max_length=4000)
    email: Optional[EmailStr] = None
    name: Optional[str] = Field(default=None, max_length=120)
    pageUrl: Optional[str] = Field(default=None, max_length=2048)
    referrer: Optional[str] = Field(default=None, max_length=2048)


class SendOut(BaseModel):
    threadId: str
    messageId: int


class ThreadOut(BaseModel):
    threadId: str
    project: str
    messages: list[dict[str, Any]]


# ---------- Resend notify ----------
def _resend_notify(thread_db_id: int, project_name: str, project_cfg: dict, visitor_email: Optional[str], page_url: Optional[str], text: str) -> None:
    """Email founder on first visitor message in a thread."""
    if not RESEND_API_KEY:
        return
    web_chat_cfg = (project_cfg or {}).get("web_chat", {}) or {}
    notify_email = web_chat_cfg.get("notify_email") or DEFAULT_NOTIFY_EMAIL
    site = (project_cfg.get("website") or project_name).replace("https://", "").replace("http://", "").rstrip("/")
    subject = f"[WEB-CHAT #{thread_db_id}] {project_name}: {visitor_email or 'anonymous'}"
    body_lines = [
        f"New web chat on {site}",
        f"Project: {project_name}",
        f"Visitor: {visitor_email or 'anonymous'}",
        f"Page: {page_url or '(unknown)'}",
        "",
        "Message:",
        text,
        "",
        "(The agent will pick this up within ~15s and reply automatically. Reply to this email to override.)",
    ]
    body = "\n".join(body_lines)
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps({
                "from": NOTIFY_FROM_EMAIL,
                "to": [notify_email],
                "subject": subject,
                "text": body,
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        # Notification failure must not block the message ingest.
        print(f"[notify] Resend failed: {e}")


# ---------- Endpoints ----------
@app.get("/healthz")
def healthz():
    return {"ok": True, "projects_loaded": len(_PROJECTS_BY_NAME)}


@app.post("/api/web-chat/send", response_model=SendOut)
def send(payload: SendIn, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
    ip = (ip or "unknown").split(",")[0].strip()
    _rate_check(ip)

    if payload.project not in _PROJECTS_BY_NAME:
        raise HTTPException(404, f"unknown project: {payload.project}")
    project_cfg = _PROJECTS_BY_NAME[payload.project]
    web_chat_cfg = (project_cfg or {}).get("web_chat", {}) or {}
    if not web_chat_cfg.get("enabled"):
        raise HTTPException(404, f"web_chat not enabled for {payload.project}")

    if not _VISITOR_ID_RE.match(payload.visitorId):
        raise HTTPException(400, "invalid visitorId")
    if payload.threadId is not None and not _THREAD_ID_RE.match(payload.threadId):
        raise HTTPException(400, "invalid threadId")

    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "empty text")

    conn = _conn()
    try:
        cur = conn.cursor()
        first_message = False
        thread_db_id: int | None = None
        thread_id = payload.threadId
        if thread_id:
            cur.execute(
                "SELECT id, project_name FROM web_chat_threads WHERE thread_id = %s",
                (thread_id,),
            )
            row = cur.fetchone()
            if not row:
                # Treat as new thread (visitor cleared localStorage between sessions).
                thread_id = None
            elif row["project_name"] != payload.project:
                raise HTTPException(409, "thread project mismatch")
            else:
                thread_db_id = row["id"]
        if thread_id is None:
            thread_id = _nanoid("wc_", 16)
            cur.execute(
                """
                INSERT INTO web_chat_threads
                  (thread_id, project_name, visitor_id, visitor_email, visitor_name,
                   page_url, user_agent, referrer,
                   unread_by_founder, last_message_sender, last_message_text, last_message_at)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, 1, 'visitor', %s, NOW())
                RETURNING id
                """,
                (
                    thread_id, payload.project, payload.visitorId,
                    payload.email, payload.name,
                    payload.pageUrl, request.headers.get("user-agent", "")[:500], payload.referrer,
                    text[:300],
                ),
            )
            thread_db_id = cur.fetchone()["id"]
            first_message = True
        else:
            # Existing thread: backfill email/name if newly provided.
            cur.execute(
                """
                UPDATE web_chat_threads
                   SET visitor_email = COALESCE(NULLIF(visitor_email, ''), %s),
                       visitor_name  = COALESCE(NULLIF(visitor_name, ''), %s),
                       page_url      = COALESCE(NULLIF(page_url, ''), %s),
                       unread_by_founder = unread_by_founder + 1,
                       last_message_sender = 'visitor',
                       last_message_text = %s,
                       last_message_at = NOW()
                 WHERE thread_id = %s
                """,
                (payload.email, payload.name, payload.pageUrl, text[:300], thread_id),
            )

        cur.execute(
            """
            INSERT INTO web_chat_messages (thread_id, sender, sender_name, text)
            VALUES (%s, 'visitor', %s, %s)
            RETURNING id
            """,
            (thread_id, payload.email or payload.name or "visitor", text),
        )
        message_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    # Fire-and-forget notify (only on first message; subsequent visitor msgs in
    # the same thread are picked up by the agent via the unread_by_founder
    # counter and the agent's own poll loop, no need to spam founder email).
    if first_message and thread_db_id is not None:
        _resend_notify(thread_db_id, payload.project, project_cfg, payload.email, payload.pageUrl, text)

    return SendOut(threadId=thread_id, messageId=message_id)


@app.get("/api/web-chat/thread/{thread_id}", response_model=ThreadOut)
def get_thread(thread_id: str):
    if not _THREAD_ID_RE.match(thread_id):
        raise HTTPException(400, "invalid threadId")
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT thread_id, project_name FROM web_chat_threads WHERE thread_id = %s",
            (thread_id,),
        )
        thread = cur.fetchone()
        if not thread:
            raise HTTPException(404, "thread not found")
        cur.execute(
            """
            SELECT id, sender, sender_name, text, created_at
              FROM web_chat_messages
             WHERE thread_id = %s
             ORDER BY created_at ASC
            """,
            (thread_id,),
        )
        msgs = cur.fetchall()
        # Mark visitor read on agent/founder messages.
        cur.execute(
            """
            UPDATE web_chat_messages
               SET read_by_visitor = TRUE
             WHERE thread_id = %s
               AND sender IN ('agent', 'founder')
               AND read_by_visitor = FALSE
            """,
            (thread_id,),
        )
        cur.execute(
            "UPDATE web_chat_threads SET unread_by_visitor = 0 WHERE thread_id = %s",
            (thread_id,),
        )
        conn.commit()
    finally:
        conn.close()

    return ThreadOut(
        threadId=thread["thread_id"],
        project=thread["project_name"],
        messages=[
            {
                "id": m["id"],
                "sender": m["sender"],
                "sender_name": m["sender_name"] or "",
                "text": m["text"] or "",
                "createdAt": m["created_at"].isoformat() if m["created_at"] else "",
            }
            for m in msgs
        ],
    )
