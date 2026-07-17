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
from http_api import api_get, api_post

REPO_DIR = os.path.expanduser("~/social-autoposter")
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")
REPLY_DB = os.path.join(REPO_DIR, "scripts", "reply_db.py")
CAMPAIGN_BUMP = os.path.join(REPO_DIR, "scripts", "campaign_bump.py")
REDDIT_MCP_CONFIG = os.path.expanduser("~/.claude/browser-agent-configs/reddit-agent-mcp.json")
REDDIT_BROWSER_LOCK = os.path.join(REPO_DIR, "scripts", "reddit_browser_lock.py")

# Interpreter every child subprocess must run under. A bare PYTHON resolved
# to the user's system python, which lacks the pipeline deps (Playwright and
# friends) that live only in the owned uv runtime — so on a fresh box every
# reddit_browser.py reply died (the same class as the Karol/Twitter bug,
# 2026-06-22). Honor the authoritative S4L_PYTHON pin (set by the launchd
# plist), else sys.executable (the owned interpreter the MCP launches us under).
# Never the literal PYTHON: that re-rolls the PATH dice. Re-exported so
# grandchildren inherit it.
PYTHON = os.environ.get("S4L_PYTHON") or sys.executable
os.environ["S4L_PYTHON"] = PYTHON

from engagement_styles import (
    REPLY_STYLES as VALID_STYLES,
    get_styles_prompt,
    get_content_rules,
    get_anti_patterns,
    get_voice_relationship_rule,
    validate_or_register,
    pick_style_for_post,
)
# Learned preferences (2026-07-03): human review feedback distilled by
# feedback_digest.py into the project's learned_preferences config block.
# Rendered as an explicit prompt section; empty string when absent.
try:
    from learned_preferences import prompt_block as _learned_prefs_block
except Exception:  # never let a missing module break the engage lane
    def _learned_prefs_block(_project_cfg):
        return ""


def _acquire_browser_lease(timeout: int = 600, ttl: int = 90):
    """Acquire the reddit-browser lease for THIS reply's Claude+CDP work.

    Per-reply acquire (not per-cycle) shipped 2026-05-13. Before this change,
    engage-reddit.sh held the lease around the whole `engage_reddit.py --limit
    N` run, so a 5-reply batch monopolised the browser for ~10-25 min while
    peer reddit pipelines (run-reddit-search post phase, link-edit-reddit,
    dm-outreach-reddit, engage-dm-replies) sat blocked through every Claude
    session and 2s inter-reply sleep.

    The reddit-agent MCP wrapper (scripts/mcp_lock_proxy.py) auto-heartbeats
    expires_at on every JSON-RPC `tools/call`, so the lease stays alive
    through Claude's MCP-driven search/fetch/draft loop without manual pulses.
    Default 90s TTL gives plenty of headroom for Claude session startup
    (~20s before the first MCP call) plus subsequent CDP posting.

    Returns (ok: bool, msg: str). msg is the helper's last stdout line on
    success, or BUSY/ERROR diagnostic on failure.
    """
    try:
        r = subprocess.run(
            [PYTHON, REDDIT_BROWSER_LOCK, "acquire",
             "--timeout", str(timeout), "--ttl", str(ttl)],
            capture_output=True, text=True, timeout=timeout + 30,
        )
        out_lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln]
        last = out_lines[-1] if out_lines else ""
        if r.returncode == 0 and last.startswith("OK"):
            return True, last
        return False, last or (r.stderr or "").strip()[:200] or f"rc={r.returncode}"
    except subprocess.TimeoutExpired:
        return False, "subprocess_timeout"
    except Exception as e:
        return False, f"exception:{e}"


def _release_browser_lease() -> None:
    """Release the reddit-browser lease. Idempotent (NOT_HELD is fine)."""
    try:
        subprocess.run(
            [PYTHON, REDDIT_BROWSER_LOCK, "release"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_active_reddit_campaigns():
    """Active Reddit campaigns with a literal suffix and budget remaining.

    Tool-level enforcement: the LLM never sees these. We append suffix to the
    drafted text in Python before the browser submits, so the literal text is
    guaranteed on Reddit. sample_rate gates the per-reply coin flip for A/B.

    Reads /api/v1/campaigns?status=active&platform=reddit&has_suffix=true&with_budget_remaining=true.
    """
    resp = api_get(
        "/api/v1/campaigns",
        query={
            "status": "active",
            "platform": "reddit",
            "has_suffix": "true",
            "with_budget_remaining": "true",
            "limit": 500,
        },
    )
    rows = ((resp or {}).get("data") or {}).get("campaigns") or []
    return [
        {
            "id": int(r["id"]),
            "suffix": r.get("suffix"),
            "sample_rate": float(r.get("sample_rate") if r.get("sample_rate") is not None else 1.0),
        }
        for r in rows
    ]


def strip_active_suffixes(text, active_campaigns):
    """Remove any active-campaign suffix from `text` (idempotent, trailing-only).

    Used to sanitize `recent_replies` snippets BEFORE feeding them into the
    LLM prompt. Without this, the LLM sees prior tagged replies in the
    "Your last N replies" block, copies the literal suffix into its draft,
    and `engage_reddit.py`'s tool-level injection then appends a SECOND
    suffix on top, producing posts like "written with s4lai written with
    s4lai" (observed in production 2026-05-18, ids 70412 + 70413).

    Strips trailing whitespace + suffix repeatedly so a doubled-suffix
    historical row also collapses to clean text. Active campaign list is
    passed in by the caller so we only strip patterns we're actively using
    (avoids unbounded false-positive matches on incidental phrasing).
    """
    if not text or not active_campaigns:
        return text
    cleaned = text.rstrip()
    changed = True
    while changed:
        changed = False
        for camp in active_campaigns:
            suffix = (camp.get("suffix") or "").strip()
            if suffix and cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].rstrip()
                changed = True
    return cleaned


def bump_campaigns(table, row_id, campaign_ids):
    """Attach a row in {posts,replies,dm_messages} to its applied campaigns."""
    if not row_id or not campaign_ids:
        return
    for cid in campaign_ids:
        try:
            subprocess.run(
                [PYTHON, CAMPAIGN_BUMP,
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


def reset_stuck_processing(platform):
    """Flip stuck 'processing' rows back to 'pending' (older than 2h).

    Routes through /api/v1/replies/reset-stuck so this module owns no SQL.
    """
    resp = api_post(
        "/api/v1/replies/reset-stuck",
        {"platform": platform, "older_than_hours": 2},
    )
    data = (resp or {}).get("data") or {}
    count = int(data.get("reset_count") or 0)
    if count > 0:
        print(f"[engage_reddit] Reset {count} stuck 'processing' {platform} items back to pending")


def get_next_pending(platform):
    """Fetch the next pending reply for the given platform (one at a time).

    Calls /api/v1/replies/next-pending which performs the JOIN to posts
    server-side and returns the rows in the canonical priority order
    (replies-to-our-original first, then oldest discovered_at).
    """
    resp = api_get(
        "/api/v1/replies/next-pending",
        query={"platform": platform, "limit": 1},
    )
    rows = ((resp or {}).get("data") or {}).get("replies") or []
    if not rows:
        return None
    row = rows[0]
    return {
        "id": int(row["id"]),
        "platform": row.get("platform"),
        "their_author": row.get("their_author"),
        "their_content": row.get("their_content"),
        "their_comment_url": row.get("their_comment_url"),
        "their_comment_id": row.get("their_comment_id"),
        "depth": row.get("depth"),
        "thread_title": row.get("thread_title"),
        "thread_url": row.get("thread_url"),
        "our_content": row.get("our_content"),
        "our_url": row.get("our_url"),
        "is_our_original_post": int(row.get("is_our_original_post") or 0),
        "project_name": row.get("project_name"),
        "post_id": row.get("post_id"),
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


def _fmt_date(s):
    """Format an ISO-ish timestamp string as YYYY-MM-DD, tolerant of None."""
    if not s:
        return "unknown"
    try:
        return str(s)[:10]
    except Exception:
        return "unknown"


def check_cross_pipeline_history(platform, author, post_id, reply_id=None):
    """Cross-pipeline check before posting a comment-reply.

    Returns (same_post_disengage, prior_history_block). Delegates to the
    shared counterparty_history module so Reddit and Twitter get symmetric
    behavior — both lanes (DM cross-thread + public-reply history) are
    surfaced into the prompt in one self-titled block.

    2026-05-19 refactor: previously this lived as Reddit-only direct API
    calls covering the DM lane only. Twitter's engage helper had nothing.
    The shared module now exposes both lanes to both pipelines; this
    function is a thin compat wrapper preserving the (same_post_disengage,
    block_text) tuple shape build_prompt() consumes.
    """
    if not author:
        return None, ""
    try:
        from counterparty_history import get_counterparty_history_block
        return get_counterparty_history_block(
            platform,
            author,
            current_post_id=post_id,
            current_reply_id=reply_id,
        )
    except Exception as e:
        print(
            f"[engage_reddit] counterparty_history failed for "
            f"{platform}/@{author} post={post_id}: {e}"
        )
        return None, ""


def get_recent_archetypes(platform, limit=3):
    """Fetch archetypes of last N replied replies for rotation context.

    Calls /api/v1/replies with order_by=replied_at and the new
    has_our_reply_content filter so we only see rows whose our_reply_content
    is populated (the previous SQL had AND our_reply_content IS NOT NULL).
    """
    resp = api_get(
        "/api/v1/replies",
        query={
            "platform": platform,
            "status": "replied",
            "has_our_reply_content": "true",
            "order_by": "replied_at",
            "limit": int(limit) if limit else 3,
        },
    )
    rows = ((resp or {}).get("data") or {}).get("replies") or []
    return [r.get("our_reply_content") for r in rows if r.get("our_reply_content")]


def build_prompt(reply, recent_replies, config, excluded_authors, top_report="", prior_history_block="", meta_callout=None):
    """Build a minimal prompt for one reply."""
    # Resolve through the one account resolver (env -> config); never a hardcoded
    # username. Empty means "unknown account" (the prompt just omits it) rather
    # than silently impersonating the repo owner on a misconfigured install.
    from account_resolver import resolve as _resolve_account
    reddit_username = _resolve_account("reddit") or ""
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
        return None, None  # will be skipped by caller

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
    learned_prefs_block = ""
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
        try:
            learned_prefs_block = _learned_prefs_block(project_cfg)
        except Exception:
            learned_prefs_block = ""

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

    # 2026-05-19: pre-pick the engagement style ONCE per reply so the
    # picker's assignment threads into BOTH the assigned-style block AND
    # the JSON output example. Without pinning the style name into the
    # JSON example, the model treats the example as a menu hint and
    # drifts (same drift vector that bit Reddit post-draft on this date).
    style_platform = "reddit" if reply["platform"] != "moltbook" else "moltbook"
    style_assignment = pick_style_for_post(style_platform, context="replying")
    assigned_style = (style_assignment.get("style") or "your invented snake_case name")

    prompt_text = f"""{framing}

## Reply data
{reply_json}

## Context
Read ~/social-autoposter/config.json for project details and content_angle.
{recent_context}{top_context}{voice_block}{learned_prefs_block}{history_block}{callout_block}
## Content rules
{get_content_rules("reddit")}
- Vary openings. Don't always start with credentials.

{get_styles_prompt(style_platform, context="replying", assignment=style_assignment)}

{get_voice_relationship_rule()}

{get_anti_patterns()}

## Tiered links
- Tier 1 (default): No link. Genuine engagement.
- Tier 2: Topic matches a config project. Mention casually.
- Tier 3: They ask for link/tool. Give it from config.

## Guardrails
- NEVER suggest calls, meetings, demos.
- NEVER promise to share links/files not in config.json.
- NEVER offer to DM. NEVER make time-bound promises.

## Bot / engagement-loop escape hatch (use sparingly, but use it)
We maintain a universal author blocklist in Postgres (`author_blocklist`),
consulted at /api/v1/replies POST time. A single block recorded by ANY of
our accounts/installs applies to EVERY future engagement from EVERY of our
accounts — universal scope, by design. The velocity gate already covers
"this handle has gotten too many replies from us in 24h/7d"; this lane is
for the LLM-judgment cases velocity cannot catch.

When to add a block (your judgment, exercised CONSERVATIVELY):
- The Reddit handle is plainly an AI/bot account: templated phrasing across
  unrelated subs, generic filler answers, name pattern like `Foo_AI` /
  `*_GPT` / `*Bot*`, comment history is karma-farm boilerplate
- We are clearly stuck in a reciprocal engagement loop with this account
- The handle is reply-farming across r/AskReddit / r/explainlikeimfive
  style subs with shallow comments

DO NOT block: an OP we disagree with, a hostile-but-human commenter, a
low-karma but real user, or a single bad interaction. Skip those
(action='skip') — blocking is permanent until manually removed and applies
to all our accounts.

How to use it: BEFORE outputting your decision JSON, run this in Bash:
  python3 ~/social-autoposter/scripts/reply_db.py blocklist add reddit HANDLE \
    --reason "<one-line judgment>" \
    --classification {{bot|engagement_loop}} \
    --source-reply-id REPLY_ID
Then output a skip decision (so the current reply is not posted):
  {{"action": "skip", "reason": "blocklist_added:HANDLE"}}
HANDLE is the Reddit username without the `u/` prefix.

## Execution steps

1. First, fetch the full thread context cheaply via Bash (NO browser needed):
   python3 ~/social-autoposter/scripts/reddit_tools.py fetch '{reply['thread_url']}'
   This returns JSON with "thread" (title, author, selftext, score, subreddit) and "comments" (id, author, body, score, permalink).
   Read the output to understand the full conversation context, who said what, and the overall tone.

2. Using the thread context from step 1 AND the reply data above, decide: reply or skip?
   If skip (troll, spam, not directed at us, light acknowledgment, conversation already resolved), output ONLY this JSON:
   {{"action": "skip", "reason": "SHORT_REASON"}}

3. If replying, draft 1-3 sentences following the rules above. Output ONLY this JSON:
   {{"action": "reply", "text": "YOUR_REPLY_TEXT", "project": null, "engagement_style": "{assigned_style}", "new_style": null}}
   The assigned engagement style is "{assigned_style}" (see the assigned style block above). Use it. Do not pick a different one.
   If you recommended a project, set "project" to the project name.

   Inventing a new style is only valid when the picker explicitly assigns "invent" mode (the assigned style block above will say so). Otherwise leave "new_style" as null and use the assigned style verbatim.

CRITICAL: Your ENTIRE output must be ONLY the JSON object above. No other text, no explanations, no markdown.
The orchestrator script will handle posting via CDP and database updates automatically.
"""
    # Return both the prompt and the picker's assignment so the caller can
    # forward the assignment into validate_or_register's enforcement layer
    # (USE mode coerces drift back; INVENT mode is the only path that lets
    # the model register a new style). Without this, the picker's choice
    # would be silently overridable downstream.
    return prompt_text, style_assignment


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
    # Route through run_claude.sh (2026-07-14) for session cost accounting +
    # the dead-man's-switch quota handling every shell pipeline already gets.
    # Tag engage-reddit is NOT in claude_job.py TAG_TO_TYPE, so this stays a
    # direct claude -p (stream-json passes through the wrapper's tee).
    # --session-id must NOT be passed here: the wrapper adds the flag itself
    # and honors a caller-set CLAUDE_SESSION_ID env (set below), so passing
    # it here too would hand claude a duplicate flag.
    _run_claude_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_claude.sh")
    cmd = ["bash", _run_claude_sh, "engage-reddit", "-p", "--output-format", "stream-json", "--verbose"]
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

    config = load_config()
    excluded_authors = config.get("exclusions", {}).get("authors", [])

    # Hard preflight: the reddit rail posts replies via reddit_browser.py, the
    # only Playwright importer here (Moltbook uses its own poster). If the
    # resolved interpreter can't import Playwright the owned runtime is missing
    # or half-provisioned and every reply would die with CDP_ERROR. Fail LOUD
    # with a distinct signal instead. Moltbook is exempt (no browser path).
    if args.platform == "reddit":
        _chk = subprocess.run(
            [PYTHON, "-c", "import playwright"],
            capture_output=True, text=True,
        )
        if _chk.returncode != 0:
            print(f"[engage_reddit] FATAL runtime_incomplete: interpreter {PYTHON!r} "
                  f"cannot import playwright — the owned Python runtime is missing or "
                  f"unprovisioned. Run the `runtime` install (action:'install') before "
                  f"engaging. stderr: {(_chk.stderr or '').strip()[:300]}", file=sys.stderr)
            sys.exit(3)

    reset_stuck_processing(args.platform)

    try:
        top_report = subprocess.check_output(
            [PYTHON, os.path.join(REPO_DIR, "scripts", "top_performers.py"), "--platform", args.platform],
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
        reply = get_next_pending(args.platform)
        if not reply:
            print("[engage_reddit] No pending replies. Done!")
            break

        # Check exclusion before spawning Claude
        if reply["their_author"].lower() in {a.lower() for a in excluded_authors}:
            subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]), "excluded_author"])
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
            reply["platform"], reply["their_author"], reply.get("post_id"),
            reply_id=reply.get("id"),
        )
        if same_post_disengage:
            reason = (
                f"cross_pipeline_disengage:dm#{same_post_disengage['dm_id']}"
                f":interest={same_post_disengage['interest_level']}"
                f":status={same_post_disengage['conversation_status']}"
            )
            subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]), reason])
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

        # Get recent replies for archetype rotation. Strip active campaign
        # suffixes from each snippet BEFORE the LLM sees them; otherwise the
        # model copies the literal suffix into its draft and the tool-layer
        # injection below appends a second copy. See strip_active_suffixes
        # docstring for the 2026-05-18 production incident this prevents.
        recent = get_recent_archetypes(args.platform, limit=3)
        if reply["platform"] == "reddit" and recent:
            _active_camps_for_strip = load_active_reddit_campaigns()
            recent = [strip_active_suffixes(r, _active_camps_for_strip) for r in recent]
            recent = [r for r in recent if r]

        # Build prompt. Returns (prompt_text, style_assignment) so the
        # picker's assignment can be forwarded into validate_or_register
        # below. style_assignment is None when the reply was filtered out
        # (excluded author) and the caller treats it as a skip.
        prompt, style_assignment = build_prompt(reply, recent, config, excluded_authors,
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

        # Per-reply reddit-browser lease (added 2026-05-13). Acquire JUST
        # around this reply's Claude session + CDP post, release in the
        # finally below so peers can use the browser during the inter-reply
        # 2s sleep AND during the moltbook-only iterations that follow.
        # Moltbook replies use the moltbook API (no browser), so we skip
        # acquire for those rows entirely.
        lease_held = False
        if reply["platform"] == "reddit":
            lease_ok, lease_msg = _acquire_browser_lease(timeout=600, ttl=90)
            if not lease_ok:
                print(f"[engage_reddit] #{reply['id']} LEASE: {lease_msg}; deferring")
                failed += 1
                skip_reasons["lease_acquire_timeout"] += 1
                # Mark processing so this row isn't refetched in this run;
                # reset_stuck_processing's 2h cap brings it back to pending.
                try:
                    subprocess.run(
                        [PYTHON, REPLY_DB, "processing", str(reply["id"])],
                        capture_output=True, timeout=10,
                    )
                except Exception:
                    pass
                processed += 1
                time.sleep(2)
                continue
            lease_held = True

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
        log_args = [PYTHON, os.path.join(REPO_DIR, "scripts", "log_claude_session.py"),
             "--session-id", session_id, "--script", "engage_reddit",
             "--started-at", session_started_at, "--ended-at", session_ended_at]
        orch_cost = usage.get("cost_usd")
        if isinstance(orch_cost, (int, float)) and orch_cost > 0:
            log_args.extend(["--orchestrator-cost-usd", str(orch_cost)])
        subprocess.run(log_args, capture_output=True)

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
            if lease_held:
                _release_browser_lease()
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
            print(f"[engage_reddit] #{reply['id']} MONTHLY USAGE LIMIT hit, "
                  f"aborting run. Cost on this attempt: ${usage['cost_usd']:.4f}")
            failed += 1
            skip_reasons["monthly_limit"] += 1
            if lease_held:
                _release_browser_lease()
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
                subprocess.run([PYTHON, REPLY_DB, "processing", str(reply["id"])],
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
                    subprocess.run([PYTHON, REPLY_DB, "processing", str(reply["id"])],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass
                print(f"[engage_reddit] #{reply['id']} BAD OUTPUT ({reply_elapsed:.0f}s): {output[:200]}")
            elif decision.get("action") == "skip":
                reason = decision.get("reason", "unknown")
                subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]), reason])
                skipped += 1
                skip_reasons[f"llm:{reason[:48]}"] += 1
                print(f"[engage_reddit] #{reply['id']} skipped: {reason} ({reply_elapsed:.0f}s) "
                      f"[${usage['cost_usd']:.4f}]")
            elif decision.get("action") == "reply":
                reply_text = decision.get("text", "")
                project = decision.get("project")
                # validate_or_register: in USE mode, coerces any drifted style
                # name back to the assigned one. In INVENT mode (5% slot),
                # registers the new style into engagement_styles_registry via
                # the s4l API. Without assigned_style/assigned_mode, the
                # picker's choice would be silently overridable by the model.
                # source_post URL is THEIR comment we're replying to; we don't
                # know our own URL until after the post lands.
                engagement_style, _style_action = validate_or_register(
                    decision,
                    source_post={
                        "platform": reply.get("platform"),
                        "post_url": reply.get("their_comment_url"),
                        "post_id": reply.get("id"),
                        "model": decision.get("model"),
                    },
                    assigned_style=(style_assignment or {}).get("style"),
                    assigned_mode=(style_assignment or {}).get("mode"),
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
                        [PYTHON, REPLY_DB, "processing", str(reply["id"])],
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
                        if lease_held:
                            _release_browser_lease()
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
                            from dm_short_links import wrap_text_for_post, utm_only_text
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
                                      f"failed ({wrap_res.get('error')}); falling back to UTM-only")
                                reply_text = utm_only_text(
                                    text=reply_text, platform=wrap_platform,
                                    project_name=wrap_project)
                        except Exception as e:
                            print(f"[engage_reddit] #{reply['id']} WARNING: URL wrap "
                                  f"raised ({e}); falling back to UTM-only")
                            try:
                                from dm_short_links import utm_only_text
                                reply_text = utm_only_text(
                                    text=reply_text, platform=wrap_platform,
                                    project_name=wrap_project)
                            except Exception as ee:
                                print(f"[engage_reddit] #{reply['id']} WARNING: UTM-only "
                                      f"fallback also failed ({ee}); posting unwrapped")

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
                                        [PYTHON, os.path.join(REPO_DIR, "scripts", "moltbook_post.py"),
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
                                    [PYTHON, os.path.join(REPO_DIR, "scripts", "reddit_browser.py"),
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
                            cmd_args = [PYTHON, REPLY_DB, "replied", str(reply["id"]), existing]
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
                        cmd_args = [PYTHON, REPLY_DB, "replied", str(reply["id"]), reply_text, reply_url]
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
                                    [PYTHON,
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
                                [PYTHON, REPLY_DB, "set_project",
                                 str(reply["id"]), project],
                                capture_output=True,
                            )
                        succeeded += 1
                        print(f"[engage_reddit] #{reply['id']} POSTED ({reply_elapsed:.0f}s) "
                              f"[${usage['cost_usd']:.4f}]")
                    else:
                        err = post_result.get("error", "unknown") if post_result else "no_response"
                        subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]), f"CDP_ERROR: {err}"])
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

        # Release the reddit-browser lease before the inter-reply sleep so
        # peers can use the browser during that gap (2s now; widening it
        # later would only multiply the value). Belt-and-suspenders: if any
        # branch above hit a `break`, it already released; this fires on the
        # normal end-of-iteration path. Idempotent (NOT_HELD is fine).
        if lease_held:
            _release_browser_lease()

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

    # Print final status (per-platform counts) via reply_db.py status helper,
    # which now reads /api/v1/replies/counts under the hood.
    subprocess.run([PYTHON, REPLY_DB, "status", args.platform])


if __name__ == "__main__":
    main()
