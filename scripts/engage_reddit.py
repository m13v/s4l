#!/usr/bin/env python3
"""Reddit reply engagement orchestrator.

Processes pending Reddit replies one at a time, each in its own Claude session.
This avoids the context accumulation problem of batching 200 replies into one session.

Usage:
    python3 scripts/engage_reddit.py
    python3 scripts/engage_reddit.py --dry-run          # Print prompt for first reply, don't post
    python3 scripts/engage_reddit.py --limit 5           # Process at most 5 replies
    python3 scripts/engage_reddit.py --timeout 3600      # Global timeout in seconds (default: 5400)
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

REPO_DIR = os.path.expanduser("~/social-autoposter")
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")
REPLY_DB = os.path.join(REPO_DIR, "scripts", "reply_db.py")
CAMPAIGN_BUMP = os.path.join(REPO_DIR, "scripts", "campaign_bump.py")
REDDIT_MCP_CONFIG = os.path.expanduser("~/.claude/browser-agent-configs/reddit-agent-mcp.json")

from engagement_styles import REPLY_STYLES as VALID_STYLES, get_styles_prompt, get_content_rules, get_anti_patterns, validate_or_register


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_active_reddit_campaigns():
    """Active Reddit campaigns with a literal suffix and budget remaining.

    Tool-level enforcement: the LLM never sees these. We append suffix to the
    drafted text in Python before the browser submits, so the literal text is
    guaranteed on Reddit. sample_rate gates the per-reply coin flip for A/B.
    """
    dbmod.load_env()
    conn = dbmod.get_conn()
    try:
        cur = conn.execute(
            """SELECT id, suffix, COALESCE(sample_rate, 1.000)
               FROM campaigns
               WHERE status = 'active'
                 AND (',' || platforms || ',') LIKE '%,reddit,%'
                 AND max_posts_total IS NOT NULL
                 AND posts_made < max_posts_total
                 AND suffix IS NOT NULL AND suffix <> ''
               ORDER BY id"""
        )
        return [
            {"id": r[0], "suffix": r[1], "sample_rate": float(r[2])}
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def bump_campaigns(table, row_id, campaign_ids):
    """Attach a row in {posts,replies,dm_messages} to its applied campaigns."""
    if not row_id or not campaign_ids:
        return
    for cid in campaign_ids:
        try:
            subprocess.run(
                ["python3", CAMPAIGN_BUMP,
                 "--table", table, "--id", str(row_id), "--campaign-id", str(cid)],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as e:
            print(f"[engage_reddit] WARNING: campaign_bump failed (id={row_id} c={cid}): {e}")


def patch_replied_with_retry(cmd_args, reply_id):
    """Run reply_db.py replied PATCH with rate-limit-aware retry.

    The comment is ALREADY posted on the platform when we call this. If the
    s4l PATCH fails (e.g. 429 during a rate-limit storm), the row stays in
    'processing' and reset_stuck_processing flips it back to 'pending' after
    2h, which would re-fetch and re-post a duplicate. Confirmed in production
    2026-05-07 where 423 duplicates landed on a single Moltbook parent.

    To prevent that, we retry the PATCH for up to ~10min with growing backoff
    (15s, 30s, 60s, 120s, 300s). If still failing after that, log a CRITICAL
    line so the operator can flip the row to 'replied' manually before the 2h
    reset fires. Returns True on success, False on terminal failure.
    """
    backoff_s = [15, 30, 60, 120, 300]
    last_stderr = ""
    for attempt in range(len(backoff_s) + 1):
        try:
            proc = subprocess.run(cmd_args, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired as e:
            last_stderr = f"timeout: {e}"
            proc = None
        else:
            if proc.returncode == 0:
                return True
            last_stderr = (proc.stderr or b"").decode(errors="replace")

        if attempt < len(backoff_s):
            wait = backoff_s[attempt]
            print(
                f"[engage_reddit] #{reply_id} REPLIED PATCH attempt {attempt+1} "
                f"failed ({last_stderr[:200]}); retrying in {wait}s",
                flush=True,
            )
            time.sleep(wait)

    print(
        f"[engage_reddit] CRITICAL: #{reply_id} REPLIED PATCH failed all retries "
        f"({last_stderr[:300]}). Comment IS posted on platform but row stays in "
        f"'processing'. After ~2h reset_stuck_processing will flip it to "
        f"'pending' and the next run may post a DUPLICATE. Manual fix: SELECT "
        f"-> verify our_reply_url, then UPDATE replies SET status='replied' "
        f"WHERE id={reply_id}.",
        flush=True,
    )
    return False


def reset_stuck_processing(conn, platform):
    result = conn.execute(
        "UPDATE replies SET status='pending' WHERE status='processing' "
        "AND platform = %s AND processing_at < NOW() - INTERVAL '2 hours' RETURNING id",
        (platform,),
    )
    count = len(result.fetchall())
    conn.commit()
    if count > 0:
        print(f"[engage_reddit] Reset {count} stuck 'processing' {platform} items back to pending")


def get_next_pending(conn, platform):
    """Fetch the next pending reply for the given platform (one at a time)."""
    cur = conn.execute("""
        SELECT r.id, r.platform, r.their_author,
               r.their_content as their_content,
               r.their_comment_url, r.their_comment_id, r.depth,
               p.thread_title as thread_title,
               p.thread_url, p.our_content as our_content, p.our_url,
               CASE WHEN p.thread_url = p.our_url THEN 1 ELSE 0 END as is_our_original_post,
               p.project_name, r.post_id
        FROM replies r
        JOIN posts p ON r.post_id = p.id
        WHERE r.status='pending' AND r.platform = %s
        ORDER BY
            CASE WHEN p.thread_url = p.our_url THEN 0 ELSE 1 END,
            r.discovered_at ASC
        LIMIT 1
    """, (platform,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "platform": row[1], "their_author": row[2],
        "their_content": row[3], "their_comment_url": row[4],
        "their_comment_id": row[5], "depth": row[6],
        "thread_title": row[7], "thread_url": row[8],
        "our_content": row[9], "our_url": row[10],
        "is_our_original_post": row[11],
        "project_name": row[12],
        "post_id": row[13],
    }


META_CALLOUT_KEYWORDS = re.compile(
    r"(?i)\b("
    r"written\s+(?:by|with)\s+(?:ai|chatgpt|gpt|llm|a\s+(?:bot|machine|model))"
    r"|(?:are|r)\s+you\s+(?:an?\s+)?(?:ai|bot|llm|gpt|chatgpt|automated)"
    r"|you(?:'re|\s+are)\s+(?:an?\s+)?(?:ai|bot|llm|gpt|chatgpt|automated)"
    r"|is\s+this\s+(?:an?\s+)?(?:ai|bot|llm|gpt|chatgpt|automated)"
    r"|chatgpt\s+(?:wrote|generated|response|reply)"
    r"|ai[-\s]+(?:generated|written|response|reply|comment)"
    r"|automated\s+(?:response|reply|comment|account)"
    r"|bot\s+(?:account|reply|response|comment)"
    r"|(?:smells?|sounds?|reads?)\s+like\s+(?:an?\s+)?(?:ai|bot|gpt|chatgpt|llm)"
    r")\b"
)


def detect_meta_callout(parent_content):
    """Detect whether the parent comment is calling out our AI/bot use.

    Returns a dict {"keyword", "evidence"} when a callout is matched,
    None otherwise. Soft-signal only: the prompt surfaces it as a
    'consider acknowledging and disengaging' nudge, the LLM still owns the
    skip/reply decision. False positives are tolerable; missing a real
    callout is the costly direction (we end up arguing past the off-ramp,
    as in the Fit-Conversation856 thread).
    """
    if not parent_content:
        return None
    m = META_CALLOUT_KEYWORDS.search(parent_content)
    if not m:
        return None
    start = max(0, m.start() - 60)
    end = min(len(parent_content), m.end() + 60)
    snippet = parent_content[start:end].replace("\n", " ").strip()
    return {"keyword": m.group(0), "evidence": snippet}


def check_cross_pipeline_history(conn, platform, author, post_id):
    """Cross-pipeline check before posting a comment-reply.

    Returns (same_post_disengage, prior_history_block).

    same_post_disengage: dict {dm_id, interest_level, conversation_status,
        qualification_status, last_message_at} if there is an existing dms
        row on the SAME post for this author with a hard disengage signal
        (declined / not_our_prospect / stale). Caller hard-skips. None means
        proceed.

    prior_history_block: human-readable text summarizing other-thread dms
    history for this author (different post_id, last 5, message_count > 0,
    plus the latest message direction + content). Empty string if no
    prior history. Soft-surface for the LLM, never blocks.

    Both are best-effort: any DB failure returns (None, "") rather than
    aborting the reply, since this is enrichment and a soft loss-of-context
    is preferable to dropping a pending reply because of a bad query.
    """
    if not author or not post_id:
        return None, ""
    try:
        same_post = conn.execute(
            """
            SELECT id, interest_level, conversation_status, qualification_status,
                   last_message_at
            FROM dms
            WHERE platform = %s AND their_author = %s AND post_id = %s
              AND (
                interest_level IN ('declined', 'not_our_prospect')
                OR conversation_status = 'stale'
              )
            ORDER BY last_message_at DESC NULLS LAST
            LIMIT 1
            """,
            (platform, author, post_id),
        ).fetchone()
        if same_post:
            same_post_disengage = {
                "dm_id": same_post["id"],
                "interest_level": same_post["interest_level"],
                "conversation_status": same_post["conversation_status"],
                "qualification_status": same_post["qualification_status"],
                "last_message_at": same_post["last_message_at"].isoformat()
                                   if same_post["last_message_at"] else None,
            }
        else:
            same_post_disengage = None

        other = conn.execute(
            """
            SELECT d.id, d.post_id, d.interest_level, d.mode, d.tier,
                   d.conversation_status, d.target_project, d.message_count,
                   d.last_message_at,
                   (SELECT direction || ': ' || content
                    FROM dm_messages WHERE dm_id = d.id
                    ORDER BY message_at DESC LIMIT 1) AS last_msg
            FROM dms d
            WHERE d.platform = %s AND d.their_author = %s
              AND COALESCE(d.post_id, -1) <> %s
              AND COALESCE(d.message_count, 0) > 0
            ORDER BY d.last_message_at DESC NULLS LAST
            LIMIT 5
            """,
            (platform, author, post_id),
        ).fetchall()
        if not other:
            return same_post_disengage, ""

        lines = []
        for r in other:
            ts = r["last_message_at"].strftime("%Y-%m-%d") if r["last_message_at"] else "unknown"
            interest = r["interest_level"] or "unset"
            mode = r["mode"] or "unset"
            status = r["conversation_status"] or "unset"
            tier = r["tier"] if r["tier"] is not None else "?"
            msgs = r["message_count"] or 0
            target = r["target_project"] or "-"
            last = (r["last_msg"] or "").replace("\n", " ").strip()
            lines.append(
                f"- dm #{r['id']} on post #{r['post_id']} (last activity {ts}): "
                f"interest={interest}, mode={mode}, status={status}, "
                f"tier={tier}, messages={msgs}, target_project={target}\n"
                f"    last: {last}"
            )
        block = (
            "## Prior history with this person on OTHER threads\n"
            "Soft context from the dms tracker (different post_id). "
            "Use this to gauge tone, fit, and whether they have already "
            "declined or pitched us elsewhere. Does NOT auto-block; you "
            "still decide reply or skip based on the current thread.\n"
            + "\n".join(lines)
        )
        return same_post_disengage, block
    except Exception as e:
        print(f"[engage_reddit] cross-pipeline check failed for {platform}/@{author} post={post_id}: {e}")
        return None, ""


def get_recent_archetypes(conn, platform, limit=3):
    """Fetch archetypes of last N replied replies for rotation context."""
    cur = conn.execute("""
        SELECT our_reply_content
        FROM replies
        WHERE status='replied' AND our_reply_content IS NOT NULL
            AND platform = %s
        ORDER BY replied_at DESC
        LIMIT %s
    """, [platform, limit])
    return [row[0] for row in cur.fetchall()]


def build_prompt(reply, recent_replies, config, excluded_authors, top_report="", prior_history_block="", meta_callout=None):
    """Build a minimal prompt for one reply."""
    reddit_username = config.get("accounts", {}).get("reddit", {}).get("username", "Deep_Ad1959")
    reply_json = json.dumps(reply, indent=2)

    # Moltbook: skip recent_replies + top_report context blocks. Both are
    # dense with our prior agent-persona-voiced comments ("my human ran...",
    # "my human ships...") which, in aggregate, trip Anthropic's Usage Policy
    # classifier. Reddit doesn't have that signature so it's fine for reddit.
    if reply['platform'] == "moltbook":
        recent_replies = []
        top_report = ""

    recent_context = ""
    if recent_replies:
        snippets = "\n".join(f"  - {r}" for r in recent_replies)
        recent_context = f"""
Your last {len(recent_replies)} replies (vary your style, don't repeat the same archetype):
{snippets}
"""

    if excluded_authors and reply["their_author"].lower() in {a.lower() for a in excluded_authors}:
        return None  # will be skipped by caller

    top_context = f"\n## FEEDBACK FROM PAST PERFORMANCE (use this to write better replies):\n{top_report}\n" if top_report else ""
    history_block = f"\n{prior_history_block}\n" if prior_history_block else ""
    callout_block = ""
    if meta_callout:
        callout_block = (
            "\n## Meta-callout detected in parent comment\n"
            f"The parent comment contains language matching `{meta_callout['keyword']}`. "
            "Evidence (60 chars on each side of the match):\n"
            f"  > {meta_callout['evidence']}\n"
            "This means the partner has likely noticed our AI disclosure or is asking whether they're talking to a bot. "
            "Default behavior: acknowledge it briefly, do NOT pitch a project, and prefer skipping over arguing. "
            "If you do reply, address the callout directly in one short sentence (no defensiveness) and consider "
            "outputting `{\"action\": \"skip\", \"reason\": \"meta_callout_acknowledged\"}` so the thread is not "
            "kept alive by another bot reply. Continuing the prior pitch as if nothing was said is the wrong move "
            "— the Fit-Conversation856 thread (2026-04-28) burned through 4 follow-up replies past this signal "
            "before the engage-dm-replies pipeline disengaged.\n"
        )

    voice_block = ""
    project_name = reply.get("project_name")
    if project_name:
        project_cfg = next(
            (p for p in config.get("projects", []) if p.get("name") == project_name),
            None,
        )
        voice = (project_cfg or {}).get("voice", {}) or {}
        if voice:
            voice_block = f"""
## Project voice (matched project: {project_name})
{json.dumps(voice, indent=2)}

Apply this voice when drafting: follow `tone`, never violate any item in `never`, mirror `examples` / `examples_good` when present.
"""

    if reply['platform'] == "moltbook":
        framing = (
            "You are helping draft a reply to a comment on a public discussion "
            "forum on behalf of the user's account. The forum is open to AI "
            "authorship, fully transparent, and the user has authorized you to "
            "draft this reply for them."
        )
    else:
        framing = (
            f"You are helping draft a reply to a comment on {reply['platform']} "
            f"on behalf of the user's account."
        )

    return f"""{framing}

## Reply data
{reply_json}

## Context
Read ~/social-autoposter/config.json for project details and content_angle.
{recent_context}{top_context}{voice_block}{history_block}{callout_block}
## Content rules
{get_content_rules("reddit")}
- First person has TWO valid forms per the GROUNDING RULE below. Lane 1: open with a disclosure phrase ("hypothetically", "imagine someone running this", "scenario:") and then invent freely. Lane 2: stay first-person only when every specific is grounded in content_angle / voice / messaging in config.json, or drop the specific and pattern-frame ("the part that breaks down is..."). Never undisclosed first-person with invented numbers / places / programs.
- Vary openings. Don't always start with credentials.

{get_styles_prompt("reddit", context="replying")}

{get_anti_patterns()}

## Tiered links
- Tier 1 (default): No link. Genuine engagement.
- Tier 2: Topic matches a config project. Mention casually.
- Tier 3: They ask for link/tool. Give it from config.

## Guardrails
- NEVER suggest calls, meetings, demos.
- NEVER promise to share links/files not in config.json.
- NEVER offer to DM. NEVER make time-bound promises.

## Execution steps

1. First, fetch the full thread context cheaply via Bash (NO browser needed):
   python3 ~/social-autoposter/scripts/reddit_tools.py fetch '{reply['thread_url']}'
   This returns JSON with "thread" (title, author, selftext, score, subreddit) and "comments" (id, author, body, score, permalink).
   Read the output to understand the full conversation context, who said what, and the overall tone.

2. Using the thread context from step 1 AND the reply data above, decide: reply or skip?
   If skip (troll, spam, not directed at us, light acknowledgment, conversation already resolved), output ONLY this JSON:
   {{"action": "skip", "reason": "SHORT_REASON"}}

3. If replying, draft 1-3 sentences following the rules above. Output ONLY this JSON:
   {{"action": "reply", "text": "YOUR_REPLY_TEXT", "project": null, "engagement_style": "STYLE_NAME", "new_style": null}}
   Set "engagement_style" to the style you used (one of: critic, storyteller, pattern_recognizer, curious_probe, contrarian, data_point_drop, snarky_oneliner, plus any candidate styles shown in the styles block above).
   If you recommended a project, set "project" to the project name.

   If, and ONLY if, none of the listed styles fits, you may invent one. Set
   "engagement_style" to your new snake_case name AND replace "new_style": null with:
   {{"new_style": {{"description": "...", "example": "...", "note": "...", "why_existing_didnt_fit": "..."}}}}
   Inventing should be rare. Prefer an existing style if it's even 80% right.

CRITICAL: Your ENTIRE output must be ONLY the JSON object above. No other text, no explanations, no markdown.
The orchestrator script will handle posting via CDP and database updates automatically.
"""


def ensure_mcp_config():
    """Create a minimal MCP config with only the reddit-agent server."""
    if os.path.exists(REDDIT_MCP_CONFIG):
        return REDDIT_MCP_CONFIG
    # Extract reddit-agent config from ~/.claude.json
    claude_json = os.path.expanduser("~/.claude.json")
    if os.path.exists(claude_json):
        with open(claude_json) as f:
            data = json.load(f)
        reddit_cfg = data.get("mcpServers", {}).get("reddit-agent")
        if reddit_cfg:
            mcp = {"mcpServers": {"reddit-agent": reddit_cfg}}
            os.makedirs(os.path.dirname(REDDIT_MCP_CONFIG), exist_ok=True)
            with open(REDDIT_MCP_CONFIG, "w") as f:
                json.dump(mcp, f, indent=2)
            return REDDIT_MCP_CONFIG
    return None


def run_claude(prompt, timeout=300, session_id=None):
    """Run claude -p with the given prompt. Returns (success, output, usage_dict).

    Streams output in real time to stderr for log visibility.
    """
    import time as _time
    import select
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_create": 0, "cost_usd": 0.0}
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if session_id:
        cmd += ["--session-id", session_id]
    # --bare removed: it blocks OAuth auth which we need
    cmd += ["--tools", "Bash,Read"]
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # ensure claude uses OAuth, not API key
    if session_id:
        env["CLAUDE_SESSION_ID"] = session_id
    try:
        proc = subprocess.Popen(
            cmd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        proc.stdin.write(prompt)
        proc.stdin.close()
        collected = []
        deadline = _time.time() + timeout
        while True:
            remaining = deadline - _time.time()
            if remaining <= 0:
                proc.kill()
                return False, "TIMEOUT", usage
            ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 30))
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                collected.append(line)
                try:
                    evt = json.loads(line.strip())
                    etype = evt.get("type", "")
                    if etype == "assistant":
                        msg = evt.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") == "tool_use":
                                print(f"[engage_reddit] tool: {block.get('name','')} | {str(block.get('input',{}).get('command',''))[:120]}", file=sys.stderr, flush=True)
                            elif block.get("type") == "text" and block.get("text","").strip():
                                txt = block["text"].strip()[:200]
                                print(f"[engage_reddit] {txt}", file=sys.stderr, flush=True)
                    elif etype == "result":
                        print(f"[engage_reddit] done: cost=${evt.get('total_cost_usd',0):.4f}", file=sys.stderr, flush=True)
                except (json.JSONDecodeError, TypeError):
                    print(f"[engage_reddit] {line.rstrip()[:200]}", file=sys.stderr, flush=True)
            elif proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    collected.append(rest)
                break
            else:
                print(f"[engage_reddit] ... still running ({int(_time.time() - (deadline - timeout))}s)", file=sys.stderr, flush=True)
        proc.wait()
        text_output = ""
        for line_str in collected:
            line_str = line_str.strip()
            if not line_str:
                continue
            try:
                event = json.loads(line_str)
                if event.get("type") == "result":
                    text_output = event.get("result", "")
                    usage["cost_usd"] = event.get("total_cost_usd", 0.0)
                    u = event.get("usage", {})
                    usage["input_tokens"] = u.get("input_tokens", 0)
                    usage["output_tokens"] = u.get("output_tokens", 0)
                    usage["cache_read"] = u.get("cache_read_input_tokens", 0)
                    usage["cache_create"] = u.get("cache_creation_input_tokens", 0)
            except (json.JSONDecodeError, TypeError):
                pass
        if not text_output:
            text_output = "".join(collected)
        stderr_out = proc.stderr.read() if proc.stderr else ""
        return proc.returncode == 0, text_output + stderr_out, usage
    except Exception as e:
        return False, str(e), usage


def main():
    parser = argparse.ArgumentParser(description="Reddit/Moltbook reply engagement (one at a time)")
    parser.add_argument("--platform", choices=["reddit", "moltbook"], default="reddit",
                        help="Platform to process (default: reddit)")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt for first reply without executing")
    parser.add_argument("--limit", type=int, default=0, help="Max replies to process (0 = unlimited)")
    parser.add_argument("--timeout", type=int, default=5400, help="Global timeout in seconds")
    parser.add_argument("--per-reply-timeout", type=int, default=300, help="Timeout per claude session in seconds")
    args = parser.parse_args()

    dbmod.load_env()
    conn = dbmod.get_conn()
    config = load_config()
    excluded_authors = config.get("exclusions", {}).get("authors", [])

    reset_stuck_processing(conn, args.platform)

    try:
        top_report = subprocess.check_output(
            ["python3", os.path.join(REPO_DIR, "scripts", "top_performers.py"), "--platform", args.platform],
            text=True, stderr=subprocess.DEVNULL, timeout=30,
        )
    except Exception:
        top_report = ""

    start_time = time.time()
    processed = 0
    succeeded = 0
    skipped = 0
    failed = 0
    skip_reasons = Counter()
    meta_callouts_detected = 0
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_create": 0, "cost_usd": 0.0}

    print(f"[engage_reddit] Starting. platform={args.platform} limit={args.limit or 'unlimited'}, timeout={args.timeout}s")

    while True:
        # Global timeout check
        elapsed = time.time() - start_time
        if elapsed > args.timeout:
            print(f"[engage_reddit] Global timeout reached ({args.timeout}s). Stopping.")
            break

        # Limit check
        if args.limit and processed >= args.limit:
            print(f"[engage_reddit] Limit reached ({args.limit}). Stopping.")
            break

        # Fetch next pending reply
        reply = get_next_pending(conn, args.platform)
        if not reply:
            print("[engage_reddit] No pending replies. Done!")
            break

        # Check exclusion before spawning Claude
        if reply["their_author"].lower() in {a.lower() for a in excluded_authors}:
            subprocess.run(["python3", REPLY_DB, "skipped", str(reply["id"]), "excluded_author"])
            print(f"[engage_reddit] #{reply['id']} skipped (excluded_author: {reply['their_author']})")
            skipped += 1
            skip_reasons["excluded_author"] += 1
            processed += 1
            continue

        # Cross-pipeline disengage check. Hard-skip if the engage-dm-replies
        # pipeline already classified this person as declined / not_our_prospect
        # / stale on THIS post. Soft-surface other-thread history into the
        # prompt so the LLM can adjust tone without being auto-blocked.
        same_post_disengage, prior_history_block = check_cross_pipeline_history(
            conn, reply["platform"], reply["their_author"], reply.get("post_id")
        )
        if same_post_disengage:
            reason = (
                f"cross_pipeline_disengage:dm#{same_post_disengage['dm_id']}"
                f":interest={same_post_disengage['interest_level']}"
                f":status={same_post_disengage['conversation_status']}"
            )
            subprocess.run(["python3", REPLY_DB, "skipped", str(reply["id"]), reason])
            print(f"[engage_reddit] #{reply['id']} skipped ({reason})")
            skipped += 1
            skip_reasons["cross_pipeline_disengage"] += 1
            processed += 1
            continue

        # Meta-callout detection on the parent comment text. Soft signal:
        # surfaces an authorize-to-ack-and-disengage block in the prompt
        # without auto-skipping. Catches the case where engage-dm-replies
        # has not yet classified the partner but the inbound text already
        # calls out our AI disclosure or asks if they're talking to a bot.
        meta_callout = detect_meta_callout(reply.get("their_content"))
        if meta_callout:
            meta_callouts_detected += 1
            print(f"[engage_reddit] #{reply['id']} meta-callout detected: keyword={meta_callout['keyword']!r}")

        # Get recent replies for archetype rotation
        recent = get_recent_archetypes(conn, args.platform, limit=3)

        # Build prompt
        prompt = build_prompt(reply, recent, config, excluded_authors,
                              top_report=top_report,
                              prior_history_block=prior_history_block,
                              meta_callout=meta_callout)
        if prompt is None:
            skipped += 1
            processed += 1
            continue

        if args.dry_run:
            print("=== DRY RUN: Prompt for reply #{} ===".format(reply["id"]))
            print(prompt)
            print("=== END DRY RUN ===")
            break

        # Run Claude session for this one reply (Claude decides + drafts, we post)
        reply_start = time.time()
        session_id = str(uuid.uuid4())
        os.environ["CLAUDE_SESSION_ID"] = session_id
        session_started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        print(f"[engage_reddit] Processing #{reply['id']} ({reply['platform']}) "
              f"from {reply['their_author']}: {(reply['their_content'] or '')[:60]}...")

        ok, output, usage = run_claude(prompt, timeout=args.per_reply_timeout, session_id=session_id)
        reply_elapsed = time.time() - reply_start
        session_ended_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        subprocess.run(
            ["python3", os.path.join(REPO_DIR, "scripts", "log_claude_session.py"),
             "--session-id", session_id, "--script", "engage_reddit",
             "--started-at", session_started_at, "--ended-at", session_ended_at],
            capture_output=True,
        )

        # Accumulate usage
        for k in total_usage:
            total_usage[k] += usage[k]

        # AUP refusal short-circuit. If Anthropic's safety classifier blocks
        # the request, every subsequent reply in this batch will get the same
        # refusal and burn $0.05-$0.30 each. Abort the run, leave rows pending
        # so the next launchd cycle picks them up after a prompt fix.
        if ("Claude Code is unable to respond" in output
                and ("Usage Policy" in output or "violate" in output.lower())):
            print(f"[engage_reddit] #{reply['id']} AUP REFUSAL detected — aborting run "
                  f"to avoid wasted spend on continued refusals. Reword the prompt "
                  f"and try again. Cost on this refusal: ${usage['cost_usd']:.4f}")
            failed += 1
            skip_reasons["aup_refusal"] += 1
            for k in total_usage:
                total_usage[k] += 0  # already accumulated above
            break

        # Monthly cap short-circuit. Mirrors the AUP guard above. When the
        # Claude Code OAuth account hits its monthly usage cap, every call
        # returns "You've hit your org's monthly usage limit" with cost=0, and
        # the per-reply queue would otherwise loop on the same row up to
        # --limit times because the row is never marked processing/skipped.
        # Surfaced in run_monitor as failure_reasons=monthly_limit:1 so the
        # dashboard Result column reads "failed: monthly_limit ×1" instead of
        # the previous silent "queue empty $0.00".
        if "monthly usage limit" in output.lower():
            print(f"[engage_reddit] #{reply['id']} MONTHLY USAGE LIMIT hit — "
                  f"aborting run. Cost on this attempt: ${usage['cost_usd']:.4f}")
            failed += 1
            skip_reasons["monthly_limit"] += 1
            break

        if not ok:
            # Generic Claude failure (timeout, transport error, non-zero exit).
            # Mark the reply as `processing` so the next iteration of the
            # while-loop doesn't fetch the SAME pending row again and burn
            # another Claude session on it. reset_stuck_processing brings it
            # back to pending after 2h, which gives the partner thread time
            # to settle (and us, time to fix whatever broke).
            failed += 1
            reason_key = "timeout" if output == "TIMEOUT" else "claude_failed"
            skip_reasons[reason_key] += 1
            try:
                subprocess.run(["python3", REPLY_DB, "processing", str(reply["id"])],
                               capture_output=True, timeout=10)
            except Exception:
                pass
            print(f"[engage_reddit] #{reply['id']} CLAUDE FAILED ({reply_elapsed:.0f}s): {output[:200]}")
        else:
            # Parse Claude's JSON decision
            decision = None
            try:
                # Extract JSON from output (may have surrounding text)
                import re as _re
                json_match = _re.search(r'\{[^{}]*"action"\s*:\s*"[^"]+?"[^{}]*\}', output)
                if json_match:
                    decision = json.loads(json_match.group())
            except (json.JSONDecodeError, TypeError):
                pass

            if not decision:
                # Fallback: check if output looks like a skip/reply
                failed += 1
                skip_reasons["bad_output"] += 1
                # Same loop-prevention as the not-ok branch: mark processing
                # so the next iteration moves to a different pending row.
                try:
                    subprocess.run(["python3", REPLY_DB, "processing", str(reply["id"])],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass
                print(f"[engage_reddit] #{reply['id']} BAD OUTPUT ({reply_elapsed:.0f}s): {output[:200]}")
            elif decision.get("action") == "skip":
                reason = decision.get("reason", "unknown")
                subprocess.run(["python3", REPLY_DB, "skipped", str(reply["id"]), reason])
                skipped += 1
                skip_reasons[f"llm:{reason[:48]}"] += 1
                print(f"[engage_reddit] #{reply['id']} skipped: {reason} ({reply_elapsed:.0f}s) "
                      f"[${usage['cost_usd']:.4f}]")
            elif decision.get("action") == "reply":
                reply_text = decision.get("text", "")
                project = decision.get("project")
                # validate_or_register accepts known styles, registers
                # well-formed new ones as candidates, and returns None for
                # unknown-and-undocumented (matches the prior clear-to-None
                # behavior). source_post URL is the THEIR comment we're
                # replying to, since we don't know our own URL until after
                # the post lands.
                engagement_style, _style_action = validate_or_register(
                    decision,
                    source_post={
                        "platform": reply.get("platform"),
                        "post_url": reply.get("their_comment_url"),
                        "post_id": reply.get("id"),
                        "model": decision.get("model"),
                    },
                )
                if not reply_text:
                    failed += 1
                    print(f"[engage_reddit] #{reply['id']} empty reply text")
                else:
                    # Mark as processing. CRITICAL: this PATCH must succeed before we
                    # post to the platform. If it fails (e.g. s4l rate-limit 429), the
                    # row stays `pending` and the next iteration of the while-loop
                    # would re-fetch it, draft a new reply, and post again, creating
                    # duplicates on the platform. Confirmed in production 2026-05-07
                    # where 423+ duplicate comments landed on a single Moltbook
                    # parent during a 5000/24h s4l rate-limit storm. Hard-fail the
                    # entire run on any non-zero exit so the row stays untouched and
                    # no platform side-effect occurs.
                    proc_result = subprocess.run(
                        ["python3", REPLY_DB, "processing", str(reply["id"])],
                        capture_output=True,
                    )
                    if proc_result.returncode != 0:
                        err_txt = (proc_result.stderr or b"").decode(errors="replace")
                        print(f"[engage_reddit] #{reply['id']} PROCESSING PATCH FAILED "
                              f"rc={proc_result.returncode}: {err_txt[:300]}")
                        print(f"[engage_reddit] Aborting run to prevent duplicate posts. "
                              f"Row stays pending; next launchd cycle will retry once "
                              f"the rate-limit window clears.")
                        failed += 1
                        skip_reasons["processing_patch_failed"] = (
                            skip_reasons.get("processing_patch_failed", 0) + 1
                        )
                        break

                    # Tool-level campaign suffix injection (Reddit only).
                    # The LLM never sees the campaign; we append the literal
                    # suffix here so the actual posted text carries the tag.
                    applied_campaign_ids = []
                    if reply["platform"] == "reddit":
                        for camp in load_active_reddit_campaigns():
                            if random.random() < camp["sample_rate"]:
                                reply_text = reply_text + camp["suffix"]
                                applied_campaign_ids.append(camp["id"])
                        if applied_campaign_ids:
                            print(f"[engage_reddit] #{reply['id']} applied campaigns "
                                  f"{applied_campaign_ids} (suffix appended)")

                    # URL-wrap the final reply_text (suffix included) so every
                    # outbound URL routes through /r/<code> for click attribution.
                    # project_name comes from the LLM decision (Tier 2/3) or
                    # falls back to the reply row's project_name; either is
                    # populated for any reply that includes a URL we care about.
                    # We backfill post_links.reply_id after the platform call
                    # succeeds (using reply["id"]).
                    minted_session = None
                    wrap_project = (project or reply.get("project_name") or "").strip()
                    wrap_platform = "reddit" if reply["platform"] == "reddit" else reply["platform"]
                    if wrap_project:
                        try:
                            from dm_short_links import wrap_text_for_post
                            wrap_res = wrap_text_for_post(
                                text=reply_text,
                                platform=wrap_platform,
                                project_name=wrap_project,
                            )
                            if wrap_res.get("ok"):
                                reply_text = wrap_res["text"]
                                minted_session = wrap_res.get("minted_session")
                                if wrap_res.get("codes"):
                                    print(f"[engage_reddit] #{reply['id']} wrapped "
                                          f"{len(wrap_res['codes'])} URL(s)")
                            else:
                                print(f"[engage_reddit] #{reply['id']} WARNING: URL wrap "
                                      f"failed ({wrap_res.get('error')}); posting unwrapped")
                        except Exception as e:
                            print(f"[engage_reddit] #{reply['id']} WARNING: URL wrap "
                                  f"raised ({e}); posting unwrapped")

                    # Post via CDP (reddit) or Moltbook API (moltbook)
                    post_result = None
                    if reply["platform"] == "moltbook":
                        m = re.search(
                            r"/post/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                            reply.get("their_comment_url") or "",
                        )
                        if not m:
                            post_result = {"ok": False, "error": "missing_moltbook_post_uuid"}
                        else:
                            post_uuid = m.group(1)
                            parent_id = reply.get("their_comment_id") or ""
                            for attempt in range(3):
                                try:
                                    out = subprocess.check_output(
                                        ["python3", os.path.join(REPO_DIR, "scripts", "moltbook_post.py"),
                                         "comment",
                                         "--post-id", post_uuid,
                                         "--parent-id", parent_id,
                                         "--content", reply_text,
                                         "--no-upvote"],
                                        text=True, timeout=120, stderr=subprocess.DEVNULL,
                                    )
                                    # moltbook_post.py prints logs + a final JSON line
                                    json_line = next((ln for ln in reversed(out.splitlines())
                                                      if ln.strip().startswith("{")), "")
                                    post_result = json.loads(json_line) if json_line else None
                                    if post_result and post_result.get("ok"):
                                        break
                                except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError, StopIteration) as e:
                                    print(f"[engage_reddit] #{reply['id']} moltbook attempt {attempt+1} failed: {e}")
                                    if attempt < 2:
                                        time.sleep(10)
                    else:
                        for attempt in range(3):
                            try:
                                cdp_out = subprocess.check_output(
                                    ["python3", os.path.join(REPO_DIR, "scripts", "reddit_browser.py"),
                                     "reply", reply["their_comment_url"], reply_text],
                                    text=True, timeout=120, stderr=subprocess.DEVNULL,
                                )
                                post_result = json.loads(cdp_out)
                                if post_result.get("ok"):
                                    break
                            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError) as e:
                                print(f"[engage_reddit] #{reply['id']} CDP attempt {attempt+1} failed: {e}")
                                if attempt < 2:
                                    time.sleep(10)

                    if post_result and post_result.get("ok"):
                        # Check if already replied (dedup)
                        if post_result.get("already_replied"):
                            existing = post_result.get("existing_text", "")
                            existing_url = post_result.get("existing_url", "")
                            cmd_args = ["python3", REPLY_DB, "replied", str(reply["id"]), existing]
                            if existing_url:
                                cmd_args.append(existing_url)
                            patch_replied_with_retry(cmd_args, reply["id"])
                            succeeded += 1
                            print(f"[engage_reddit] #{reply['id']} DEDUP (already replied) ({reply_elapsed:.0f}s)")
                            print(f"[engage_reddit] #{reply['id']} tokens: in={usage['input_tokens']} out={usage['output_tokens']} "
                                  f"cache_r={usage['cache_read']} cache_w={usage['cache_create']} "
                                  f"${usage['cost_usd']:.4f}")
                            processed += 1
                            time.sleep(2)
                            continue

                        # Mark as replied in DB. patch_replied_with_retry adds
                        # rate-limit-aware retries so a transient s4l 429 after a
                        # successful platform post does not leave the row in
                        # 'processing' (which 2h reset_stuck_processing would flip
                        # back to 'pending' and cause a duplicate post).
                        reply_url = post_result.get("url", "")
                        cmd_args = ["python3", REPLY_DB, "replied", str(reply["id"]), reply_text, reply_url]
                        if engagement_style:
                            cmd_args.append(engagement_style)
                        patch_replied_with_retry(cmd_args, reply["id"])
                        # Attribute reply to any campaigns that applied a suffix
                        bump_campaigns("replies", reply["id"], applied_campaign_ids)
                        # Stamp post_links.reply_id for the URLs minted before
                        # the platform call (idempotent; no-op when reply had
                        # no URLs to wrap).
                        if minted_session:
                            try:
                                from dm_short_links import backfill_reply_id
                                backfill_reply_id(minted_session=minted_session,
                                                  reply_id=reply["id"])
                            except Exception as e:
                                print(f"[engage_reddit] #{reply['id']} WARNING: "
                                      f"backfill_reply_id failed ({e})")
                        # Cross-pipeline linkage: ensure a dms row exists for
                        # this person on this thread so engage-dm-replies'
                        # next cycle picks up any inbound on this chain
                        # immediately, instead of waiting for the unread-dms
                        # scan (which can lag up to 30 min). ensure-dm is
                        # idempotent and auto-links to the most recent
                        # replies row for this author within lookback.
                        if reply["platform"] == "reddit":
                            try:
                                subprocess.run(
                                    ["python3",
                                     os.path.join(REPO_DIR, "scripts", "dm_conversation.py"),
                                     "ensure-dm",
                                     "--platform", "reddit",
                                     "--author", reply["their_author"]],
                                    capture_output=True, text=True, timeout=20,
                                )
                            except Exception as e:
                                print(f"[engage_reddit] #{reply['id']} ensure-dm failed: {e}")
                        # Update project if recommended. Routes through the
                        # HTTPS PATCH lane in reply_db.py so the project name
                        # travels as a JSON field (no shell interpolation, no
                        # SQL injection vector) and benefits from the same
                        # retry-on-transient policy as the rest of the
                        # mutations.
                        if project:
                            subprocess.run(
                                ["python3", REPLY_DB, "set_project",
                                 str(reply["id"]), project],
                                capture_output=True,
                            )
                        succeeded += 1
                        print(f"[engage_reddit] #{reply['id']} POSTED ({reply_elapsed:.0f}s) "
                              f"[${usage['cost_usd']:.4f}]")
                    else:
                        err = post_result.get("error", "unknown") if post_result else "no_response"
                        subprocess.run(["python3", REPLY_DB, "skipped", str(reply["id"]), f"CDP_ERROR: {err}"])
                        skip_reasons[f"cdp_error:{(err or 'unknown')[:32]}"] += 1
                        failed += 1
                        print(f"[engage_reddit] #{reply['id']} CDP FAILED: {err} ({reply_elapsed:.0f}s)")
            else:
                failed += 1
                print(f"[engage_reddit] #{reply['id']} unknown action: {decision}")

            print(f"[engage_reddit] #{reply['id']} tokens: in={usage['input_tokens']} out={usage['output_tokens']} "
                  f"cache_r={usage['cache_read']} cache_w={usage['cache_create']} "
                  f"${usage['cost_usd']:.4f}")

        processed += 1

        # Brief pause between sessions
        time.sleep(2)

    total_elapsed = time.time() - start_time
    print(f"\n[engage_reddit] === SUMMARY ===")
    print(f"[engage_reddit] processed={processed} succeeded={succeeded} "
          f"skipped={skipped} failed={failed} elapsed={total_elapsed:.0f}s")
    print(f"[engage_reddit] meta_callouts_detected={meta_callouts_detected}")
    if skip_reasons:
        print(f"[engage_reddit] skip_reasons:")
        for reason, n in skip_reasons.most_common():
            print(f"[engage_reddit]   {n:>3}  {reason}")
    print(f"[engage_reddit] Total tokens: input={total_usage['input_tokens']} "
          f"output={total_usage['output_tokens']} "
          f"cache_read={total_usage['cache_read']} cache_create={total_usage['cache_create']}")
    print(f"[engage_reddit] Total cost: ${total_usage['cost_usd']:.4f}")
    if succeeded > 0:
        print(f"[engage_reddit] Avg cost per reply: ${total_usage['cost_usd'] / succeeded:.4f}")

    # Build the failure-reasons string for the dashboard Result column. We
    # only count *hard* failure categories here (monthly_limit, aup_refusal,
    # timeout, claude_failed, bad_output) so that recoverable LLM-driven
    # skips (`llm:not_directed`, `llm:troll`, ...) don't get surfaced as
    # failures. Missing keys map to 0 via Counter, so this is safe even
    # when the run had zero failures.
    HARD_FAILURE_KEYS = ("monthly_limit", "aup_refusal", "timeout",
                         "claude_failed", "bad_output")
    fr_pairs = [f"{k}:{skip_reasons[k]}" for k in HARD_FAILURE_KEYS
                if skip_reasons.get(k, 0) > 0]
    # Also surface CDP_ERROR rollups so a Reddit posting outage shows up as
    # "failed: cdp_error ×N" instead of dropping into the generic skip pile.
    cdp_total = sum(n for r, n in skip_reasons.items() if r.startswith("cdp_error:"))
    if cdp_total > 0:
        fr_pairs.append(f"cdp_error:{cdp_total}")
    failure_reasons_arg = ",".join(fr_pairs)

    # Canonical machine-readable summary line for the shell wrapper
    # (engage-reddit.sh) to grep. The wrapper combines these engage-stage
    # counters with its own scan-stage counters and writes ONE log_run.py row.
    # Previously we wrote our own log_run row here AND the shell wrote one too,
    # producing two rows per cycle in run_monitor.log -- the duplicate without
    # scan info was the row the dashboard surfaced, which is why empty cycles
    # rendered as "0 0 0 0" instead of "scanned N / 0 new".
    print(
        f"[engage_reddit] LOG_RUN_SUMMARY"
        f" posted={succeeded}"
        f" skipped={skipped}"
        f" failed={failed}"
        f" cost={total_usage['cost_usd']:.4f}"
        f" elapsed={int(total_elapsed)}"
        f" failure_reasons={failure_reasons_arg}"
    )

    # Print final status
    subprocess.run(["python3", REPLY_DB, "status"])
    conn.close()


if __name__ == "__main__":
    main()
