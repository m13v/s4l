#!/usr/bin/env python3
"""Daily reply-risk digest.

Scans recent inbound replies to our social replies, keeps the model's
existing skip/status classification in view, adds surrounding DB context, and
emails a concise daily digest of risks, learnings, and drafting suggestions.

The core pass is deterministic and read-only:
  - replies: inbound text, our follow-up, status, skip_reason, engagement
  - parent replies: context for true depth-2+ replies under our reply
  - posts/mentions: stored thread and notification context
  - author_blocklist + author history: account-level risk context

By default the script asks Claude to summarize the compact JSON envelope into
a plain-text operator email. If Claude is unavailable, it falls back to a
deterministic report so the daily pipeline still produces something useful.

Usage:
    python3 scripts/reply_risk_digest.py --dry-run --no-claude
    python3 scripts/reply_risk_digest.py --hours 24
    python3 scripts/reply_risk_digest.py --platform all --dry-run
"""

from __future__ import annotations

import argparse
import atexit
import base64
import html
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

REPO_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = REPO_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from db import load_env  # noqa: E402
from db_direct import get_conn  # noqa: E402

RUN_STARTED = time.time()
SCRIPT_TAG = "reply-risk-digest"
OPERATOR_EMAIL = "i@m13v.com"
GMAIL_TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
GMAIL_SCOPES = ["https://mail.google.com/"]
RUN_CLAUDE_PATH = REPO_DIR / "scripts" / "run_claude.sh"

RISK_RULES = [
    ("bot_callout", re.compile(r"\b(ai reply|ai bot|nice ai bot|bot account|are you a bot|automated ai|automated reply|fully automated|llm write|llm[- ]generated)\b", re.I), 7),
    ("ai_slop", re.compile(r"\b(ai slop|slop reply|slop\b|clanker response|awful ai reply)\b", re.I), 7),
    ("spam_callout", re.compile(r"\b(spam people|spam your|spamming|nobody needs your ai replies|treat me like a human|blocked for ai reply|auto[- ]reply block)\b", re.I), 8),
    ("hostile", re.compile(r"\b(fuck|fucking|bullshit|\bbs\b|scam|grift|trash|garbage|red flags?|what the hell|did i ask|stop it|lying|mentir|faux|fake)\b", re.I), 5),
    ("bot_detection_bait", re.compile(r"\b(write me a poem|ignore previous|prompt injection|banana|bananas)\b", re.I), 6),
    ("product_takedown", re.compile(r"\b(red flags?|no usen este producto|inferior|landing gen[eé]rica|page objects|propuesta de valor)\b", re.I), 5),
]

SKIP_RISK_HINTS = [
    ("hostile_user", 7),
    ("human_called_out_ai_reply", 9),
    ("ai_bot_callout", 9),
    ("user_explicitly_objected_to_ai_replies", 9),
    ("reply_hidden_flagged_as_ai", 9),
    ("llm_accusation_bait", 8),
    ("troll/bot-detection bait", 8),
    ("drive_by_mock", 6),
    ("templated_bot_reply", 6),
    ("author_blocked_us", 5),
    ("blocklist_added", 5),
    ("engagement_loop", 4),
    ("hostile_unsubstantive_rant", 4),
]

POSITIVE_RE = re.compile(
    r"\b(thanks|thank you|appreciate|agree|exactly|makes sense|fair point|"
    r"good point|smart|interesting|love|cool|great|true|correct)\b",
    re.I,
)
QUESTION_RE = re.compile(r"\?")
LINK_RE = re.compile(r"https?://", re.I)
AI_DISCLOSURE_RE = re.compile(r"\bwritten with (ai|s4lai)\b", re.I)
PRODUCT_RE = re.compile(
    r"\b(fazm|assrt|s4l|s4lai|podlog|claude-meter|runner|nightowl|cyrano|blurt)\b",
    re.I,
)


def _emit_run_log() -> None:
    elapsed = max(0, int(time.time() - RUN_STARTED))
    subprocess.run(
        [
            "python3",
            str(REPO_DIR / "scripts" / "log_run.py"),
            "--script",
            SCRIPT_TAG,
            "--posted",
            "0",
            "--skipped",
            "0",
            "--failed",
            "0",
            "--cost",
            "0",
            "--elapsed",
            str(elapsed),
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


atexit.register(_emit_run_log)


def _clip(text: str | None, limit: int = 700) -> str:
    if not text:
        return ""
    one_line = re.sub(r"\s+", " ", str(text)).strip()
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1].rstrip() + "…"


def _as_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _gmail_service():
    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _send_email(to_addr: str, subject: str, body: str):
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = to_addr
    msg["from"] = OPERATOR_EMAIL
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return (
        _gmail_service()
        .users()
        .messages()
        .send(userId="me", body={"raw": raw})
        .execute()
    )


def fetch_reply_rows(db, platform: str, hours: int, limit: int) -> list[dict[str, Any]]:
    where = ["r.discovered_at >= NOW() - (%s * INTERVAL '1 hour')"]
    params: list[Any] = [int(hours)]
    if platform != "all":
        where.append("r.platform = %s")
        params.append(platform)
    params.append(int(limit))
    sql = f"""
        SELECT
          r.id, r.platform, r.depth, r.parent_reply_id, r.post_id, r.mention_id,
          r.status, r.skip_reason, r.their_author, r.their_content,
          r.their_comment_url, r.our_reply_id, r.our_reply_content,
          r.our_reply_url, r.our_account, r.thread_author_handle,
          r.discovered_at, r.replied_at, r.processing_at, r.project_name,
          r.engagement_style, r.language, r.model, r.claude_session_id,
          r.is_recommendation, r.campaign_id, r.upvotes, r.comments_count,
          r.views, r.engagement_updated_at, r.autoposter_version,
          p.thread_url, p.thread_author, p.thread_author_handle AS post_thread_author_handle,
          p.thread_title, p.thread_content, p.thread_engagement,
          p.top_comment_author, p.top_comment_content, p.top_comment_url,
          p.our_content AS original_our_content, p.our_url AS original_our_url,
          p.project_name AS post_project_name, p.search_topic,
          m.mentioning_url, m.mentioning_handle, m.mentioning_text,
          m.parent_views, m.parent_likes, m.parent_retweets,
          pr.their_author AS parent_their_author,
          pr.their_content AS parent_their_content,
          pr.their_comment_url AS parent_their_comment_url,
          pr.our_reply_content AS parent_our_reply_content,
          pr.our_reply_url AS parent_our_reply_url,
          pr.status AS parent_status,
          pr.skip_reason AS parent_skip_reason,
          pr.engagement_style AS parent_engagement_style
        FROM replies r
        LEFT JOIN posts p ON p.id = r.post_id
        LEFT JOIN mentions m ON m.id = r.mention_id
        LEFT JOIN replies pr ON pr.id = r.parent_reply_id
        WHERE {" AND ".join(where)}
        ORDER BY r.discovered_at DESC NULLS LAST, r.id DESC
        LIMIT %s
    """
    cur = db.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def fetch_author_history(
    db, rows: list[dict[str, Any]], platform: str, days: int = 30
) -> dict[str, dict[str, Any]]:
    handles = {
        (r.get("their_author") or "").lower()
        for r in rows
        if r.get("their_author")
    }
    if not handles:
        return {}

    where = ["discovered_at >= NOW() - (%s * INTERVAL '1 day')"]
    params: list[Any] = [int(days)]
    if platform != "all":
        where.append("platform = %s")
        params.append(platform)

    cur = db.execute(
        f"""
        SELECT platform, their_author, status, skip_reason, discovered_at,
               upvotes, comments_count, views
        FROM replies
        WHERE {" AND ".join(where)}
        """,
        params,
    )
    history: dict[str, dict[str, Any]] = {
        h: {
            "last_30d": 0,
            "replied": 0,
            "skipped": 0,
            "riskish_skips": 0,
            "child_replies": 0,
            "upvotes": 0,
            "views": 0,
            "last_seen_at": None,
        }
        for h in handles
    }
    for raw in cur.fetchall():
        r = dict(raw)
        handle = (r.get("their_author") or "").lower()
        if handle not in history:
            continue
        h = history[handle]
        h["last_30d"] += 1
        if r.get("status") == "replied":
            h["replied"] += 1
        if r.get("status") == "skipped":
            h["skipped"] += 1
        if _skip_reason_risk_score(r.get("skip_reason") or "") >= 4:
            h["riskish_skips"] += 1
        h["child_replies"] += int(r.get("comments_count") or 0)
        h["upvotes"] += int(r.get("upvotes") or 0)
        h["views"] += int(r.get("views") or 0)
        seen = _as_iso(r.get("discovered_at"))
        if seen and (not h["last_seen_at"] or seen > h["last_seen_at"]):
            h["last_seen_at"] = seen

    block_where = []
    block_params: list[Any] = []
    if platform != "all":
        block_where.append("platform = %s")
        block_params.append(platform)
    block_sql = "SELECT platform, handle, classification, severity, reason, source_reply_id, created_at, updated_at, hit_count FROM author_blocklist"
    if block_where:
        block_sql += " WHERE " + " AND ".join(block_where)
    try:
        cur = db.execute(block_sql, block_params if block_params else None)
        for raw in cur.fetchall():
            b = dict(raw)
            handle = (b.get("handle") or "").lower()
            if handle not in history:
                continue
            history[handle]["blocklist"] = {
                "platform": b.get("platform"),
                "classification": b.get("classification"),
                "severity": b.get("severity"),
                "reason": b.get("reason"),
                "source_reply_id": b.get("source_reply_id"),
                "hit_count": b.get("hit_count"),
                "created_at": _as_iso(b.get("created_at")),
                "updated_at": _as_iso(b.get("updated_at")),
            }
    except Exception as e:
        for h in history.values():
            h["blocklist_error"] = str(e)
    return history


def _skip_reason_risk_score(skip_reason: str) -> int:
    reason = (skip_reason or "").lower()
    score = 0
    for marker, points in SKIP_RISK_HINTS:
        if marker.lower() in reason:
            score += points
    return score


def classify_row(row: dict[str, Any], author_history: dict[str, dict[str, Any]]):
    text = " ".join(
        [
            row.get("their_content") or "",
            row.get("skip_reason") or "",
            row.get("our_reply_content") or "",
            row.get("parent_our_reply_content") or "",
        ]
    )
    risk_score = _skip_reason_risk_score(row.get("skip_reason") or "")
    insight_score = 0
    tags: list[str] = []

    for tag, pattern, points in RISK_RULES:
        if pattern.search(text):
            tags.append(tag)
            risk_score += points

    if row.get("status") == "skipped" and row.get("skip_reason"):
        tags.append("model_skipped")
        risk_score += 1
    if row.get("parent_reply_id") is not None or int(row.get("depth") or 1) > 1:
        tags.append("true_nested_followup")
    else:
        tags.append("notification_capture")

    our_text = row.get("our_reply_content") or ""
    parent_our_text = row.get("parent_our_reply_content") or ""
    if AI_DISCLOSURE_RE.search(our_text) or AI_DISCLOSURE_RE.search(parent_our_text):
        tags.append("ai_disclosure_present")
        risk_score += 1
    if LINK_RE.search(our_text) or PRODUCT_RE.search(our_text):
        tags.append("our_followup_productish")
        risk_score += 1
    if LINK_RE.search(parent_our_text) or PRODUCT_RE.search(parent_our_text):
        tags.append("trigger_parent_productish")
        risk_score += 1

    if QUESTION_RE.search(row.get("their_content") or ""):
        tags.append("question")
        insight_score += 2
    if POSITIVE_RE.search(row.get("their_content") or ""):
        tags.append("positive_signal")
        insight_score += 1
    child_replies = int(row.get("comments_count") or 0)
    likes = int(row.get("upvotes") or 0)
    views = int(row.get("views") or 0)
    if child_replies:
        tags.append("our_followup_got_child_reply")
        insight_score += min(5, child_replies + 1)
    if likes:
        insight_score += min(3, likes)
    if views >= 50:
        insight_score += 1

    hist = author_history.get((row.get("their_author") or "").lower()) or {}
    if hist.get("blocklist"):
        tags.append("author_blocklisted")
        risk_score += 5
    if hist.get("riskish_skips", 0) >= 2:
        tags.append("repeat_risk_author")
        risk_score += 2
    if hist.get("last_30d", 0) >= 4 and hist.get("riskish_skips", 0) == 0:
        tags.append("repeat_constructive_author")
        insight_score += 1

    return {
        "risk_score": risk_score,
        "insight_score": insight_score,
        "tags": sorted(set(tags)),
    }


def compact_row(row: dict[str, Any], author_history: dict[str, dict[str, Any]]):
    handle = (row.get("their_author") or "").lower()
    return {
        "id": row.get("id"),
        "platform": row.get("platform"),
        "discovered_at": _as_iso(row.get("discovered_at")),
        "depth": row.get("depth"),
        "parent_reply_id": row.get("parent_reply_id"),
        "status": row.get("status"),
        "skip_reason": row.get("skip_reason"),
        "classification": row.get("_classification"),
        "author": row.get("their_author"),
        "author_history": author_history.get(handle) or {},
        "inbound_reply": {
            "text": _clip(row.get("their_content"), 900),
            "url": row.get("their_comment_url"),
            "views": int(row.get("views") or 0),
            "likes": int(row.get("upvotes") or 0),
            "child_replies": int(row.get("comments_count") or 0),
        },
        "our_followup_to_inbound": {
            "text": _clip(row.get("our_reply_content"), 900),
            "url": row.get("our_reply_url"),
            "style": row.get("engagement_style"),
            "model": row.get("model"),
            "replied_at": _as_iso(row.get("replied_at")),
            "campaign_id": row.get("campaign_id"),
            "autoposter_version": row.get("autoposter_version"),
        },
        "stored_parent_context": {
            "parent_author": row.get("parent_their_author"),
            "parent_inbound_text": _clip(row.get("parent_their_content"), 600),
            "parent_inbound_url": row.get("parent_their_comment_url"),
            "parent_our_reply_text": _clip(row.get("parent_our_reply_content"), 900),
            "parent_our_reply_url": row.get("parent_our_reply_url"),
            "parent_status": row.get("parent_status"),
            "parent_skip_reason": row.get("parent_skip_reason"),
            "parent_style": row.get("parent_engagement_style"),
        },
        "thread_context": {
            "thread_url": row.get("thread_url"),
            "thread_author": row.get("thread_author"),
            "thread_author_handle": (
                row.get("thread_author_handle") or row.get("post_thread_author_handle")
            ),
            "thread_title": _clip(row.get("thread_title"), 240),
            "thread_content": _clip(row.get("thread_content"), 800),
            "thread_engagement": row.get("thread_engagement"),
            "top_comment_author": row.get("top_comment_author"),
            "top_comment_content": _clip(row.get("top_comment_content"), 450),
            "top_comment_url": row.get("top_comment_url"),
            "original_our_content": _clip(row.get("original_our_content"), 700),
            "original_our_url": row.get("original_our_url"),
            "search_topic": row.get("search_topic"),
        },
        "mention_context": {
            "mentioning_url": row.get("mentioning_url"),
            "mentioning_handle": row.get("mentioning_handle"),
            "mentioning_text": _clip(row.get("mentioning_text"), 500),
            "parent_views": row.get("parent_views"),
            "parent_likes": row.get("parent_likes"),
            "parent_retweets": row.get("parent_retweets"),
        },
        "context_gaps": [
            gap
            for gap, missing in [
                ("no_post_thread_context", not row.get("post_id")),
                ("no_parent_reply_context", not row.get("parent_reply_id")),
                ("no_our_followup_because_skipped", row.get("status") == "skipped" and not row.get("our_reply_content")),
            ]
            if missing
        ],
    }


def build_envelope(rows: list[dict[str, Any]], author_history: dict[str, dict[str, Any]], args):
    for row in rows:
        row["_classification"] = classify_row(row, author_history)

    platform_counts = Counter(r.get("platform") or "unknown" for r in rows)
    status_counts = Counter(r.get("status") or "unknown" for r in rows)
    depth_counts = Counter(str(r.get("depth") or 1) for r in rows)
    tag_counts = Counter(
        tag for r in rows for tag in (r.get("_classification") or {}).get("tags", [])
    )

    risk_rows = sorted(
        [r for r in rows if r["_classification"]["risk_score"] >= args.min_risk_score],
        key=lambda r: (
            r["_classification"]["risk_score"],
            r.get("comments_count") or 0,
            r.get("views") or 0,
        ),
        reverse=True,
    )
    insight_rows = sorted(
        [
            r
            for r in rows
            if r["_classification"]["insight_score"] >= args.min_insight_score
            and r["_classification"]["risk_score"] < args.min_risk_score
        ],
        key=lambda r: (
            r["_classification"]["insight_score"],
            r.get("comments_count") or 0,
            r.get("upvotes") or 0,
            r.get("views") or 0,
        ),
        reverse=True,
    )
    repeated_authors = []
    for handle, h in author_history.items():
        if h.get("riskish_skips", 0) or h.get("last_30d", 0) >= 4 or h.get("blocklist"):
            repeated_authors.append({"handle": handle, **h})
    repeated_authors.sort(
        key=lambda h: (
            bool(h.get("blocklist")),
            h.get("riskish_skips", 0),
            h.get("last_30d", 0),
        ),
        reverse=True,
    )

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": args.hours,
            "platform": args.platform,
            "rows_scanned": len(rows),
            "risk_threshold": args.min_risk_score,
            "insight_threshold": args.min_insight_score,
            "note": (
                "notification_capture rows may lack the exact parent tweet that "
                "triggered the inbound reply; true_nested_followup rows include "
                "stored parent reply context via parent_reply_id."
            ),
        },
        "counts": {
            "platforms": dict(platform_counts),
            "statuses": dict(status_counts),
            "depths": dict(depth_counts),
            "tags": dict(tag_counts.most_common()),
            "risk_items": len(risk_rows),
            "insight_items": len(insight_rows),
            "true_nested_followups": sum(
                1
                for r in rows
                if r.get("parent_reply_id") is not None or int(r.get("depth") or 1) > 1
            ),
        },
        "risk_items": [
            compact_row(r, author_history) for r in risk_rows[: args.risk_limit]
        ],
        "insight_items": [
            compact_row(r, author_history) for r in insight_rows[: args.insight_limit]
        ],
        "author_watchlist": repeated_authors[: args.author_limit],
    }


def build_prompt(envelope: dict[str, Any]) -> str:
    compact = json.dumps(envelope, ensure_ascii=False, indent=2)
    return f"""You are writing a daily operator email for Matt about replies TO our social replies.

Use ONLY the JSON context below. Do not invent thread context when the JSON says it is missing.
The `skip_reason` is valuable: it is the model's existing assessment after reading the reply.
Preserve row IDs and URLs for any concrete examples.

Write a concise plain-text email body with these sections:

1. Executive summary: 3-5 bullets with counts and the day's risk level.
2. Risk replies: the most important bot/spam/hostility/product-trust risks. Explain what triggered them.
3. Learnings: what worked, what patterns generated constructive replies, and what reply shapes should be avoided.
4. Suggested changes: concrete drafting/skip/feedback-loop suggestions.
5. Rows to inspect: 3-8 row IDs/URLs with one-line reasons.

Rules:
- Focus on "what was the thread / what was our reply / what did they reply / how did we classify it".
- If stored parent/thread context is missing, say so briefly; do not pretend we know it.
- Distinguish true nested follow-ups from notification-captured replies.
- Quote only short snippets, never long tweet bodies.
- Keep the email under about 900 words.

JSON context:
{compact}
"""


def summarize_with_claude(envelope: dict[str, Any], timeout: int) -> str | None:
    if not RUN_CLAUDE_PATH.exists():
        return None
    prompt = build_prompt(envelope)
    try:
        proc = subprocess.run(
            [
                str(RUN_CLAUDE_PATH),
                SCRIPT_TAG,
                "--output-format",
                "json",
                "-p",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_DIR),
        )
    except Exception as e:
        print(f"[reply_risk_digest] Claude summarizer failed to start: {e}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(
            f"[reply_risk_digest] Claude summarizer exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[-1200:]}",
            file=sys.stderr,
        )
        return None
    try:
        data = json.loads(proc.stdout)
        result = (data.get("result") or "").strip()
        return result or None
    except Exception:
        text = proc.stdout.strip()
        if text:
            return text
        return None


def fallback_report(envelope: dict[str, Any]) -> str:
    meta = envelope["meta"]
    counts = envelope["counts"]
    lines = [
        f"Reply Risk Digest ({meta['platform']}, last {meta['window_hours']}h)",
        "",
        "Executive summary",
        f"- Scanned {meta['rows_scanned']} replies.",
        f"- Statuses: {counts['statuses']}",
        f"- Risk items above threshold: {counts['risk_items']}. Insight items: {counts['insight_items']}.",
        f"- True nested follow-ups with stored parent context: {counts['true_nested_followups']}.",
        "",
        "Risk replies",
    ]
    if not envelope["risk_items"]:
        lines.append("- No risk rows crossed the threshold.")
    for item in envelope["risk_items"][:8]:
        c = item["classification"]
        lines.append(
            f"- #{item['id']} @{item['author']} score={c['risk_score']} "
            f"tags={','.join(c['tags'])}: "
            f"{_clip(item['inbound_reply']['text'], 180)}"
        )
        if item.get("skip_reason"):
            lines.append(f"  skip_reason: {_clip(item['skip_reason'], 220)}")
        lines.append(f"  url: {item['inbound_reply']['url']}")
    lines.extend(["", "Learnings / constructive replies"])
    if not envelope["insight_items"]:
        lines.append("- No insight rows crossed the threshold.")
    for item in envelope["insight_items"][:8]:
        c = item["classification"]
        followup = item["our_followup_to_inbound"]
        lines.append(
            f"- #{item['id']} @{item['author']} score={c['insight_score']} "
            f"style={followup.get('style')}: {_clip(followup.get('text'), 200)}"
        )
        lines.append(f"  inbound: {_clip(item['inbound_reply']['text'], 160)}")
    lines.extend(["", "Author watchlist"])
    for author in envelope["author_watchlist"][:8]:
        lines.append(
            f"- @{author['handle']}: last_30d={author.get('last_30d')} "
            f"riskish_skips={author.get('riskish_skips')} "
            f"blocklist={'yes' if author.get('blocklist') else 'no'}"
        )
    lines.extend(["", "Generated by scripts/reply_risk_digest.py"])
    return "\n".join(lines)


def build_subject(envelope: dict[str, Any]) -> str:
    counts = envelope["counts"]
    platform = envelope["meta"]["platform"]
    risk = counts["risk_items"]
    rows = envelope["meta"]["rows_scanned"]
    day = datetime.now(timezone.utc).date().isoformat()
    if risk >= 10:
        level = "HIGH"
    elif risk >= 3:
        level = "WARN"
    else:
        level = "OK"
    return f"[reply-risk] {level} {platform} {day} ({risk} risk / {rows} scanned)"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="x", help="'x' by default, or 'all'")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--risk-limit", type=int, default=25)
    parser.add_argument("--insight-limit", type=int, default=18)
    parser.add_argument("--author-limit", type=int, default=12)
    parser.add_argument("--min-risk-score", type=int, default=5)
    parser.add_argument("--min-insight-score", type=int, default=3)
    parser.add_argument("--to", default=None, help="Recipient; defaults to NOTIFICATION_EMAIL or i@m13v.com")
    parser.add_argument("--dry-run", action="store_true", help="Print email instead of sending")
    parser.add_argument("--no-claude", action="store_true", help="Use deterministic fallback report")
    parser.add_argument("--claude-timeout", type=int, default=420)
    parser.add_argument("--send-empty", action="store_true", help="Send even when no rows were scanned")
    parser.add_argument("--json-out", default=None, help="Write the compact JSON envelope to this path")
    return parser.parse_args()


def main():
    args = parse_args()
    load_env()
    recipient = args.to or os.environ.get("NOTIFICATION_EMAIL") or OPERATOR_EMAIL
    platform = args.platform.lower().strip()
    if platform == "twitter":
        platform = "x"
    args.platform = platform

    db = get_conn()
    try:
        rows = fetch_reply_rows(db, platform, args.hours, args.limit)
        author_history = fetch_author_history(db, rows, platform)
    finally:
        db.close()

    envelope = build_envelope(rows, author_history, args)
    if args.json_out:
        path = Path(args.json_out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")

    if not rows and not args.send_empty:
        print("[reply_risk_digest] no rows in window; no email sent")
        return

    body = None if args.no_claude else summarize_with_claude(envelope, args.claude_timeout)
    if not body:
        body = fallback_report(envelope)
    subject = build_subject(envelope)
    footer = (
        "\n\n---\n"
        f"Generated by {html.escape(str(REPO_DIR / 'scripts' / 'reply_risk_digest.py'))}\n"
        f"Window: last {args.hours}h, platform={args.platform}, scanned={len(rows)}\n"
    )
    if "Generated by scripts/reply_risk_digest.py" not in body:
        body = body.rstrip() + footer

    if args.dry_run:
        print(f"To: {recipient}")
        print(f"Subject: {subject}")
        print("")
        print(body)
        return

    result = _send_email(recipient, subject, body)
    print(f"[reply_risk_digest] sent to {recipient} id={result.get('id')} subject={subject!r}")


if __name__ == "__main__":
    main()
