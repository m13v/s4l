#!/usr/bin/env python3
"""early_reply_cycle.py — convert observed early_reply_candidates into Fazm replies.

A separate posting rail from the twitter discovery cycle. Reads rows from
`early_reply_candidates` (populated by the twitterapi.io webhook in
bin/server.js when filter rules fire on @mckaywrigley/@yuchenj_uw/@simonw/
@alexalbert__/@ericzakariasson tweets), filters/scores them, asks Claude to
judge whether a single best candidate is worth replying to for Fazm, and on
draft posts the reply via twitter_browser.reply_to_tweet.

Why a separate cycle (not folded into run-twitter-cycle.sh): the discovery
pipeline pulls from search; this rail pushes from webhook. Different source,
different cadence (5 min vs 60 s), different scoring (view_count / minutes,
not the long discovery curve), different daily cap. Sharing the cycle would
have meant 50+ extra columns on early_reply_candidates and broken the
analytics scoping (`source LIKE 'twitterapi_webhook_early_reply'`). Cleaner
to have two narrow rails that share the lock, the engagement-style picker,
the URL wrapper, and the log_post.py writer.

Pipeline:

  1. Pre-filter every status='observed' row:
       - is_reply OR is RT: skip (we want top-level tweets only)
       - older than EARLY_REPLY_MAX_AGE_MIN minutes: skip (early-reply is
         only useful while reply_count is still low)
       - already in posts.thread_url (any source): cross-rail dedup, skip
       - already in twitter_candidates.tweet_url with status='posted':
         discovery rail already engaged this thread, skip
     Each skip flips status='filtered' with skip_reason.

  2. Daily cap: if status='posted' rows from this rail in the last 24h
     >= EARLY_REPLY_DAILY_CAP, exit early (0 picks).

  3. Score remaining rows by view_count_at_arrival / minutes_since_post.
     Pick the top 1.

  4. Pick engagement style via engagement_styles.pick_style_for_post(
     "twitter", context="replying"). Build the Claude prompt.

  5. Call Claude (via scripts/run_claude.sh for session/cost logging) with
     a JSON schema: {decision: 'draft'|'skip', skip_reason, draft_text,
     engagement_style}. Strict format; no MCP needed (the post is done by
     twitter_browser.py, not by Claude tool-calls).

  6. On decision='skip': flip status='skipped', persist skip_reason.

  7. On decision='draft':
       a. validate_or_register the engagement style.
       b. wrap_text_for_post via dm_short_links.
       c. acquire twitter-browser lock, ensure harness up.
       d. twitter_browser.reply_to_tweet(tweet_url, full_text).
       e. log_post.py INSERT (platform=twitter, engagement_style, language).
       f. flip early_reply_candidates status='posted' with posted_at,
          our_reply_url, engagement_style, post_id, batch_id.

  --dry-run flag short-circuits steps 7c-f: prints the wrapped draft text
  + picked engagement style + scored candidate and exits 0 without holding
  the lock or touching the browser.

Locked file caveats: this file is NEW; engagement_styles.py and
twitter_browser.py are locked and used as APIs only. dm_short_links.py
ditto.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_DIR = os.path.expanduser("~/social-autoposter")
SCRIPTS_DIR = os.path.join(REPO_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

# ── Tunables ────────────────────────────────────────────────────────────────
EARLY_REPLY_MAX_AGE_MIN = int(os.environ.get("EARLY_REPLY_MAX_AGE_MIN", "30"))
EARLY_REPLY_DAILY_CAP   = int(os.environ.get("EARLY_REPLY_DAILY_CAP", "3"))
EARLY_REPLY_PROJECT     = os.environ.get("EARLY_REPLY_PROJECT", "fazm")
EARLY_REPLY_BATCH_ID    = os.environ.get("BATCH_ID") or os.environ.get(
    "EARLY_REPLY_BATCH_ID"
) or f"earlyreply-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

REPLY_URL_RE = re.compile(r"^https?://(?:x\.com|twitter\.com)/[^/]+/status/\d+")


# ── DB ──────────────────────────────────────────────────────────────────────

def _connect():
    """Direct psycopg2 connection. The s4l.ai HTTP API doesn't yet have
    early_reply_candidates routes (test-mode-only when written 2026-05-23);
    rather than waiting on a server-side route bump we just go direct. Use
    DATABASE_URL from .env, same as every other script."""
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_DIR, ".env"))
    return psycopg2.connect(os.environ["DATABASE_URL"])


def fetch_observed_rows(conn) -> list[dict]:
    """All status='observed' rows, joined with derived minutes_old."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, monitored_handle, author_handle, tweet_id, tweet_url,
                   tweet_text, tweet_posted_at, is_reply, in_reply_to_id,
                   author_followers,
                   reply_count_at_arrival, like_count_at_arrival,
                   retweet_count_at_arrival, view_count_at_arrival,
                   bookmark_count_at_arrival, quote_count_at_arrival,
                   project, raw_payload,
                   EXTRACT(EPOCH FROM (NOW() - tweet_posted_at))/60 AS mins_old
              FROM early_reply_candidates
             WHERE status = 'observed'
               AND project = %s
             ORDER BY received_at DESC
            """,
            (EARLY_REPLY_PROJECT,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def thread_already_in_posts(conn, tweet_url: str) -> bool:
    """Cross-rail dedup against `posts` (any source, twitter platform).
    Mirrors twitter_post_plan.already_posted_to_thread but goes direct so
    we don't depend on the s4l.ai API being up."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM posts WHERE platform='twitter' AND thread_url=%s LIMIT 1",
            (tweet_url,),
        )
        return cur.fetchone() is not None


def thread_already_in_twitter_candidates(conn, tweet_url: str) -> bool:
    """Cross-rail dedup against the discovery pipeline's twitter_candidates.
    Only fires on rows already FLIPPED to status='posted' — observed/pending
    candidates that haven't gone out yet are OK to race; whoever posts first
    wins, the loser hits log_post.py's DUPLICATE_THREAD."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM twitter_candidates "
            "WHERE tweet_url=%s AND status='posted' LIMIT 1",
            (tweet_url,),
        )
        return cur.fetchone() is not None


def daily_posted_count(conn) -> int:
    """Count of THIS rail's posted-in-last-24h rows. Drives the cap."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM early_reply_candidates
             WHERE status='posted'
               AND posted_at > NOW() - INTERVAL '24 hours'
               AND project = %s
            """,
            (EARLY_REPLY_PROJECT,),
        )
        return cur.fetchone()[0]


def mark_filtered(conn, row_id: int, skip_reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE early_reply_candidates
               SET status='filtered', skip_reason=%s, processed_at=NOW(),
                   batch_id=COALESCE(batch_id, %s)
             WHERE id=%s AND status='observed'
            """,
            (skip_reason, EARLY_REPLY_BATCH_ID, row_id),
        )
    conn.commit()


def mark_skipped(conn, row_id: int, skip_reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE early_reply_candidates
               SET status='skipped', skip_reason=%s, processed_at=NOW(),
                   batch_id=%s
             WHERE id=%s
            """,
            (skip_reason, EARLY_REPLY_BATCH_ID, row_id),
        )
    conn.commit()


def mark_posted(conn, row_id: int, *, our_reply_url: str,
                engagement_style: str, post_id: int | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE early_reply_candidates
               SET status='posted', posted_at=NOW(),
                   our_reply_url=%s, engagement_style=%s,
                   post_id=%s, batch_id=%s, processed_at=NOW()
             WHERE id=%s
            """,
            (our_reply_url, engagement_style, post_id,
             EARLY_REPLY_BATCH_ID, row_id),
        )
    conn.commit()


def mark_failed(conn, row_id: int, err: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE early_reply_candidates
               SET status='failed', last_error=%s, processed_at=NOW(),
                   batch_id=%s
             WHERE id=%s
            """,
            (err[:500], EARLY_REPLY_BATCH_ID, row_id),
        )
    conn.commit()


# ── Pre-filter + scoring ────────────────────────────────────────────────────

def prefilter(conn, rows: list[dict]) -> list[dict]:
    """Return rows that survive every filter. Filtered rows are committed
    as status='filtered' with skip_reason before this function returns."""
    keep: list[dict] = []
    for r in rows:
        rid = r["id"]
        if r.get("is_reply"):
            mark_filtered(conn, rid, "is_reply")
            continue
        text = (r.get("tweet_text") or "").strip()
        if text.startswith("RT @"):
            mark_filtered(conn, rid, "is_retweet")
            continue
        mins_old = float(r.get("mins_old") or 0)
        if mins_old > EARLY_REPLY_MAX_AGE_MIN:
            mark_filtered(conn, rid, f"too_old_{int(mins_old)}min")
            continue
        if not r.get("tweet_url"):
            mark_filtered(conn, rid, "missing_tweet_url")
            continue
        if thread_already_in_posts(conn, r["tweet_url"]):
            mark_filtered(conn, rid, "already_in_posts")
            continue
        if thread_already_in_twitter_candidates(conn, r["tweet_url"]):
            mark_filtered(conn, rid, "already_in_twitter_candidates_posted")
            continue
        keep.append(r)
    return keep


def score(row: dict) -> float:
    """views / minute. Linear is fine; we only need ranking, not absolute."""
    mins = max(float(row.get("mins_old") or 0.0), 0.5)  # avoid divide-by-zero
    views = float(row.get("view_count_at_arrival") or 0)
    return views / mins


# ── Claude judgment ─────────────────────────────────────────────────────────

JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision"],
    "properties": {
        "decision": {"type": "string", "enum": ["draft", "skip"]},
        "skip_reason": {"type": ["string", "null"]},
        "draft_text": {"type": ["string", "null"]},
        "engagement_style": {"type": ["string", "null"]},
        "new_style": {
            "type": ["object", "null"],
            "additionalProperties": True,
        },
        "reasoning": {"type": ["string", "null"]},
    },
}


FAZM_BLURB = (
    "Fazm is a native macOS app that wraps Claude Code and Codex via ACP. "
    "Persistent sessions across machine restarts, one-click chat forking "
    "for parallel exploration, no auto-compacting (you control the context "
    "window). Built for devs who already live in Claude Code / Codex and "
    "want a real desktop client instead of a terminal pane. https://fazm.ai"
)


def build_prompt(picked: dict, styles_block: str, assignment: dict) -> str:
    """Build the judge-and-draft prompt. Single Claude call: decide whether
    this tweet is worth replying to AS Fazm, and if yes draft the reply."""
    text = (picked.get("tweet_text") or "").strip()
    handle = picked.get("monitored_handle") or picked.get("author_handle") or "?"
    mins_old = float(picked.get("mins_old") or 0)
    views = picked.get("view_count_at_arrival") or 0
    replies = picked.get("reply_count_at_arrival") or 0
    likes = picked.get("like_count_at_arrival") or 0

    assigned_style = assignment.get("style") or "(invent — see styles block)"

    return f"""You are deciding whether to post a Twitter reply on behalf of Fazm.

# About Fazm

{FAZM_BLURB}

Voice: Write like a fellow developer sharing honest notes from building and
using the tool, specific and technical, skeptical of hype. Never use:
marketing language, exclamation points, pitch closers.

# The tweet you might reply to

Author: @{handle}
Posted: {mins_old:.0f} min ago
Engagement at webhook arrival: views={views} replies={replies} likes={likes}
URL: {picked.get("tweet_url")}

Tweet text:
\"\"\"
{text}
\"\"\"

# Engagement style assignment

{styles_block}

# Your task

Decide if this tweet is a good fit for a Fazm reply. A good fit means:

- The tweet is about coding agents, dev tooling, terminal/CLI UX, Claude/
  Codex/Cursor, AI coding workflows, prompt engineering for code, or
  similar developer-facing AI topics.
- A short Fazm-flavored reply (one of the listed engagement styles, written
  as a fellow dev, NOT marketing) would plausibly add value to the
  conversation, not just inject a product mention.
- The tweet is NOT a personal/political/off-topic post, NOT a reply to
  someone else (already filtered, but double-check), and NOT something
  where any product mention would be cringe.

If yes -> decision="draft", set draft_text to your reply (max 270 chars to
leave room for the wrapped URL the orchestrator appends), set
engagement_style to "{assigned_style}" (or your invented snake_case name
if the style block said INVENT). Do NOT include any URL in draft_text;
the orchestrator wraps and appends a Fazm URL automatically.

If no -> decision="skip", set skip_reason to a short snake_case reason
like "off_topic", "personal_post", "not_dev_focused", "would_be_cringe",
"too_political", "no_natural_angle". Leave draft_text and
engagement_style null.

Return ONE JSON object matching the schema. No prose outside the JSON.
"""


def run_claude_judge(prompt: str, schema: dict) -> dict | None:
    """Invoke the Claude CLI via scripts/run_claude.sh for session/cost
    logging. Strict JSON schema, no MCP. Returns the parsed JSON or None
    on any failure."""
    run_claude = os.path.join(SCRIPTS_DIR, "run_claude.sh")
    schema_path = f"/tmp/early_reply_judge_schema_{os.getpid()}.json"
    with open(schema_path, "w") as f:
        json.dump(schema, f)
    try:
        cmd = [
            "/bin/bash", run_claude, "early-reply-judge",
            "--output-format", "json",
            "--json-schema", schema_path,
            "-p", prompt,
        ]
        # Inherit CLAUDE_MODEL via parent env; run_claude.sh handles the
        # --model flag when CLAUDE_MODEL is set, falls back to settings.json.
        env = os.environ.copy()
        # Allow ~5 minutes — judgment is cheap but Claude can occasionally
        # be slow on the first call after a long idle period.
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=420, env=env)
        if r.returncode != 0:
            print(f"[early-reply] judge exit={r.returncode} stderr={r.stderr[:500]}",
                  file=sys.stderr)
            return None
        out = (r.stdout or "").strip()
        if not out:
            print("[early-reply] judge: empty stdout", file=sys.stderr)
            return None
        # Claude --output-format json emits an envelope; the model's
        # JSON-schema result lives in result.result (when schema specified)
        # OR result itself depending on CLI version. Be defensive.
        envelope = json.loads(out)
        if isinstance(envelope, dict):
            inner = envelope.get("result")
            if isinstance(inner, str):
                try:
                    return json.loads(inner)
                except Exception:
                    pass
            if isinstance(inner, dict):
                return inner
            # Maybe the envelope IS already the decision.
            if "decision" in envelope:
                return envelope
        print(f"[early-reply] judge: unrecognized envelope shape: {out[:300]}",
              file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("[early-reply] judge: timeout", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[early-reply] judge: exception {e!r}", file=sys.stderr)
        return None
    finally:
        try:
            os.remove(schema_path)
        except OSError:
            pass


# ── Posting ─────────────────────────────────────────────────────────────────

def post_reply(picked: dict, draft_text: str, project: str,
               engagement_style: str) -> dict:
    """Wrap URL, call twitter_browser.reply_to_tweet, log_post.py INSERT.
    Returns a result dict with shape:
      {ok: bool, reply_url, post_id, final_text, error}
    """
    # 1. URL-wrap. dm_short_links may add a project URL into the text via
    #    minting, but our draft_text per the prompt shouldn't have any URLs.
    #    Still go through the wrapper so we get consistent behavior.
    from dm_short_links import wrap_text_for_post, utm_only_text  # type: ignore
    full_text = draft_text
    minted_session = None
    # Append the project URL on a new line so the model's reply text stays
    # clean; wrap_text_for_post then turns the bare URL into /r/<code>.
    base_text_with_url = f"{draft_text}\n\nhttps://fazm.ai"
    try:
        wrap_res = wrap_text_for_post(text=base_text_with_url,
                                      platform="twitter", project_name=project)
        if wrap_res.get("ok"):
            full_text = wrap_res["text"]
            minted_session = wrap_res.get("minted_session")
        else:
            print(f"[early-reply] URL wrap failed ({wrap_res.get('error')}); "
                  f"falling back to UTM-only", flush=True)
            full_text = utm_only_text(text=base_text_with_url,
                                      platform="twitter", project_name=project)
    except Exception as e:
        print(f"[early-reply] URL wrap raised ({e}); falling back to UTM-only",
              flush=True)
        try:
            full_text = utm_only_text(text=base_text_with_url,
                                      platform="twitter", project_name=project)
        except Exception as ee:
            print(f"[early-reply] UTM-only fallback also failed ({ee}); "
                  f"posting unwrapped", flush=True)
            full_text = base_text_with_url

    # 2. Call twitter_browser.py reply via subprocess. apply_campaigns=True
    #    is the default; we want suffix attribution for this rail too.
    twitter_browser = os.path.join(SCRIPTS_DIR, "twitter_browser.py")
    print(f"[early-reply] posting to {picked['tweet_url']}", flush=True)
    print(f"[early-reply] full_text={full_text!r}", flush=True)
    r = subprocess.run(
        ["python3", twitter_browser, "reply", picked["tweet_url"], full_text],
        capture_output=True, text=True, timeout=600,
    )
    if r.stderr:
        print(f"[early-reply][reply.stderr]\n{r.stderr}", flush=True)
    if r.stdout:
        print(f"[early-reply][reply.stdout]\n{r.stdout}", flush=True)

    # twitter_browser prints one JSON object to stdout on success/failure.
    parsed = _parse_last_json_object(r.stdout) or {}
    if not parsed.get("ok"):
        return {"ok": False, "error": parsed.get("error") or "no_reply_json",
                "minted_session": minted_session, "full_text": full_text}

    reply_url = parsed.get("reply_url") or ""
    final_text = parsed.get("final_text") or full_text
    if not reply_url or not REPLY_URL_RE.match(reply_url):
        return {"ok": False, "error": "no_reply_url_captured",
                "minted_session": minted_session, "full_text": final_text}

    # 3. log_post.py INSERT.
    log_post = os.path.join(SCRIPTS_DIR, "log_post.py")
    try:
        from twitter_account import resolve_handle as _resolve_twitter_handle
        twitter_handle = _resolve_twitter_handle()
    except Exception:
        twitter_handle = None

    log_args = [
        "python3", log_post,
        "--platform", "twitter",
        "--thread-url", picked["tweet_url"],
        "--our-url", reply_url,
        "--our-content", final_text,
        "--project", project,
        "--thread-author", picked.get("author_handle") or "",
        "--thread-title", (picked.get("tweet_text") or "")[:200],
        "--thread-content", picked.get("tweet_text") or "",
        "--engagement-style", engagement_style,
    ]
    if twitter_handle:
        log_args += ["--account", twitter_handle]
    # Snapshot thread engagement from webhook arrival (NOT live).
    try:
        snap = {
            "likes": picked.get("like_count_at_arrival"),
            "retweets": picked.get("retweet_count_at_arrival"),
            "replies": picked.get("reply_count_at_arrival"),
            "views": picked.get("view_count_at_arrival"),
            "bookmarks": picked.get("bookmark_count_at_arrival"),
            "source": "twitterapi_webhook_t0",
        }
        if any(v is not None for v in snap.values() if v != "twitterapi_webhook_t0"):
            log_args += ["--thread-engagement", json.dumps(snap, separators=(",", ":"))]
    except Exception:
        pass

    r2 = subprocess.run(log_args, capture_output=True, text=True, timeout=60)
    if r2.stderr:
        print(f"[early-reply][log_post.stderr]\n{r2.stderr}", flush=True)
    if r2.stdout:
        print(f"[early-reply][log_post.stdout]\n{r2.stdout}", flush=True)
    log_obj = _parse_last_json_object(r2.stdout) or {}
    post_id = log_obj.get("post_id")

    # 4. Backfill short-link post_id.
    if minted_session and post_id:
        try:
            from dm_short_links import backfill_post_id
            backfill_post_id(minted_session=minted_session, post_id=post_id)
        except Exception as e:
            print(f"[early-reply] backfill_post_id failed: {e}", flush=True)

    return {
        "ok": True,
        "reply_url": reply_url,
        "post_id": post_id,
        "final_text": final_text,
        "minted_session": minted_session,
    }


def _parse_last_json_object(text: str) -> dict | None:
    """Same logic as twitter_post_plan.parse_last_json_object."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            pass
    depth = 0
    start = None
    matches: list[str] = []
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    matches.append(text[start:i + 1])
                    start = None
    for cand in reversed(matches):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Do everything except actually posting / updating "
                         "early_reply_candidates. Prints the picked row, "
                         "the engagement-style assignment, the Claude "
                         "decision, and the wrapped draft text.")
    ap.add_argument("--limit", type=int, default=1,
                    help="Max picks per invocation (default 1). Daily cap "
                         "is enforced independently.")
    args = ap.parse_args()

    conn = _connect()

    # Daily cap
    if not args.dry_run:
        today_count = daily_posted_count(conn)
        if today_count >= EARLY_REPLY_DAILY_CAP:
            print(json.dumps({
                "status": "cap_hit",
                "posted_24h": today_count,
                "cap": EARLY_REPLY_DAILY_CAP,
            }))
            return 0

    rows = fetch_observed_rows(conn)
    if not rows:
        print(json.dumps({"status": "no_observed_rows"}))
        return 0
    print(f"[early-reply] observed rows in queue: {len(rows)}", flush=True)

    # Pre-filter (commits status='filtered' on misses)
    survivors = prefilter(conn, rows)
    if not survivors:
        print(json.dumps({
            "status": "all_filtered",
            "scanned": len(rows),
        }))
        return 0
    print(f"[early-reply] survivors after pre-filter: {len(survivors)}", flush=True)

    # Score + pick top N
    survivors.sort(key=score, reverse=True)
    picks = survivors[: max(args.limit, 1)]
    print(f"[early-reply] picked top {len(picks)} by views/min: "
          f"{[(p['id'], p['monitored_handle'], round(score(p), 2)) for p in picks]}",
          flush=True)

    # Engagement style picker — once for the whole batch (only 1 pick today).
    from engagement_styles import (
        pick_style_for_post, get_assigned_style_prompt,
        validate_or_register,
    )
    assignment = pick_style_for_post("twitter", context="replying")
    styles_block = get_assigned_style_prompt(
        "twitter", assignment, context="replying"
    )
    print(f"[early-reply] assignment: mode={assignment['mode']} "
          f"style={assignment.get('style') or '(invent)'}", flush=True)

    posted = skipped = failed = 0
    for picked in picks:
        rid = picked["id"]
        prompt = build_prompt(picked, styles_block, assignment)
        decision = run_claude_judge(prompt, JUDGE_SCHEMA)
        if not decision or "decision" not in decision:
            print(f"[early-reply] row {rid}: judge returned no decision; skipping",
                  flush=True)
            if not args.dry_run:
                mark_failed(conn, rid, "judge_no_decision")
            failed += 1
            continue
        print(f"[early-reply] row {rid}: decision={decision.get('decision')} "
              f"skip_reason={decision.get('skip_reason')!r} "
              f"engagement_style={decision.get('engagement_style')!r}",
              flush=True)
        if decision["decision"] == "skip":
            reason = decision.get("skip_reason") or "judge_skipped"
            if not args.dry_run:
                mark_skipped(conn, rid, reason)
            skipped += 1
            continue

        draft_text = (decision.get("draft_text") or "").strip()
        if not draft_text:
            print(f"[early-reply] row {rid}: draft decision but empty draft_text",
                  flush=True)
            if not args.dry_run:
                mark_failed(conn, rid, "judge_empty_draft")
            failed += 1
            continue

        # Engagement-style coercion / registration. Mirrors the Twitter
        # post path (twitter_post_plan.post_one).
        raw_style = (decision.get("engagement_style") or "").strip()
        new_style_block = decision.get("new_style") \
            if isinstance(decision.get("new_style"), dict) else None
        coerced_style = raw_style
        try:
            decision_for_validator = {
                "engagement_style": raw_style,
                **({"new_style": new_style_block} if new_style_block else {}),
            }
            coerced_style, action = validate_or_register(
                decision_for_validator,
                source_post={
                    "platform": "twitter",
                    "post_url": picked["tweet_url"],
                    "post_id": None,
                    "model": None,
                },
                assigned_style=assignment.get("style") or None,
                assigned_mode=assignment.get("mode") or None,
            )
            if action == "coerced" and coerced_style != raw_style:
                print(f"[early-reply] row {rid}: engagement_style coerced "
                      f"{raw_style!r} -> {coerced_style!r}", flush=True)
            elif action == "registered":
                print(f"[early-reply] row {rid}: registered new style "
                      f"{coerced_style!r}", flush=True)
        except Exception as e:
            print(f"[early-reply] row {rid}: validate_or_register raised {e!r}; "
                  f"falling back to raw style", flush=True)
            coerced_style = raw_style or "balanced_advisor"
        style_for_log = coerced_style or "balanced_advisor"

        if args.dry_run:
            # Show wrapped text so we can eyeball what would have shipped.
            from dm_short_links import wrap_text_for_post
            base_text_with_url = f"{draft_text}\n\nhttps://fazm.ai"
            try:
                wrap_res = wrap_text_for_post(text=base_text_with_url,
                                              platform="twitter",
                                              project_name=EARLY_REPLY_PROJECT)
                wrapped = wrap_res.get("text") if wrap_res.get("ok") else "(wrap failed)"
            except Exception as e:
                wrapped = f"(wrap raised {e!r})"
            print(json.dumps({
                "dry_run": True,
                "row_id": rid,
                "monitored_handle": picked.get("monitored_handle"),
                "author_handle": picked.get("author_handle"),
                "tweet_url": picked.get("tweet_url"),
                "tweet_text_preview": (picked.get("tweet_text") or "")[:120],
                "score": round(score(picked), 2),
                "engagement_style": style_for_log,
                "draft_text": draft_text,
                "wrapped_text": wrapped,
                "reasoning": decision.get("reasoning"),
            }, indent=2))
            posted += 1
            continue

        # Real post path.
        try:
            res = post_reply(picked, draft_text, EARLY_REPLY_PROJECT, style_for_log)
        except Exception as e:
            print(f"[early-reply] row {rid}: post_reply raised {e!r}",
                  flush=True)
            mark_failed(conn, rid, f"post_exception:{e!r}"[:500])
            failed += 1
            continue
        if not res.get("ok"):
            err = res.get("error") or "unknown"
            print(f"[early-reply] row {rid}: post failed: {err}", flush=True)
            mark_failed(conn, rid, f"post_failed:{err}")
            failed += 1
            continue
        mark_posted(
            conn, rid,
            our_reply_url=res["reply_url"],
            engagement_style=style_for_log,
            post_id=res.get("post_id"),
        )
        posted += 1
        print(f"[early-reply] row {rid} POSTED as {res['reply_url']} "
              f"(post_id={res.get('post_id')})", flush=True)

    summary = {
        "status": "done",
        "batch_id": EARLY_REPLY_BATCH_ID,
        "scanned": len(rows),
        "survivors": len(survivors),
        "picks": len(picks),
        "posted": posted,
        "skipped": skipped,
        "failed": failed,
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
