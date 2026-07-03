#!/usr/bin/env python3
"""GitHub issues reply engagement orchestrator.

Processes pending GitHub issue replies one at a time, each in its own Claude session.
Before deciding, fetches the full issue thread via gh CLI so Claude can see the
entire conversation (title, body, every comment, our own prior replies) and make
a thread-aware reply-or-skip decision with a JSON escape hatch.

This replaces the batched inline prompt in skill/github-engage.sh, which fed
truncated snippets to Claude with a "Process EVERY reply" directive. That design
produced spammy self-promotion comments that got flagged on fastrepl/char#4881.

Usage:
    python3 scripts/engage_github.py
    python3 scripts/engage_github.py --dry-run          # Print prompt for first reply, don't post
    python3 scripts/engage_github.py --limit 5           # Process at most 5 replies
    python3 scripts/engage_github.py --timeout 3600      # Global timeout in seconds
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get
from engagement_styles import get_styles_prompt, get_anti_patterns, get_voice_relationship_rule

REPO_DIR = os.path.expanduser("~/social-autoposter")
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")
REPLY_DB = os.path.join(REPO_DIR, "scripts", "reply_db.py")
SKILL_FILE = os.path.join(REPO_DIR, "SKILL.md")

# Interpreter every child subprocess must run under. A bare PYTHON resolved
# to the user's system python, which lacks the pipeline deps that live only in
# the owned uv runtime — the same fresh-box failure class that broke the Twitter
# poster (Karol, 2026-06-22). The GitHub rail posts via the REST API (no browser,
# so no Playwright dep), but its util/DB children still need the owned venv, so
# pin the interpreter here too. Honor S4L_PYTHON (set by the launchd plist),
# else sys.executable; never the literal PYTHON.
PYTHON = os.environ.get("S4L_PYTHON") or sys.executable
os.environ["S4L_PYTHON"] = PYTHON

# Cap the thread JSON we pass to Claude. Long issues with 100+ comments would
# otherwise blow the prompt budget. 12k chars is ~3k tokens, enough for most
# threads while leaving headroom for the rules and output.
THREAD_CHAR_CAP = 12000


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_next_pending():
    """Fetch the next pending GitHub reply (one at a time, oldest first).

    Routes through /api/v1/replies/next-pending, which LEFT-JOINs posts +
    mentions server-side and filters orphans. GitHub replies are always
    post-rooted, so the post-side fields (thread_title/thread_url/our_content/
    our_url) come back populated exactly as the old INNER JOIN produced.
    """
    resp = api_get("/api/v1/replies/next-pending",
                   query={"platform": "github", "limit": 1})
    rows = ((resp or {}).get("data") or {}).get("replies") or []
    if not rows:
        return None
    r = rows[0]
    return {
        "id": r.get("id"), "platform": r.get("platform"),
        "their_author": r.get("their_author"),
        "their_content": r.get("their_content"),
        "their_comment_url": r.get("their_comment_url"),
        "their_comment_id": r.get("their_comment_id"),
        "depth": r.get("depth"),
        "thread_title": r.get("thread_title"),
        "thread_url": r.get("thread_url"),
        "our_content": r.get("our_content"),
        "our_url": r.get("our_url"),
    }


def get_recent_archetypes(limit=3):
    """Fetch our last N GitHub replies so Claude can vary style across threads.

    Routes through /api/v1/replies (status=replied, has_our_reply_content,
    ordered by replied_at DESC)."""
    resp = api_get("/api/v1/replies", query={
        "platform": "github",
        "status": "replied",
        "has_our_reply_content": "true",
        "order_by": "replied_at",
        "limit": int(limit),
    })
    rows = ((resp or {}).get("data") or {}).get("replies") or []
    return [r.get("our_reply_content") for r in rows if r.get("our_reply_content")]


def parse_issue_url(url):
    """Extract (owner, repo, number) from a github.com issue or PR URL."""
    if not url:
        return None, None, None
    m = re.search(r"github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)", url)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), int(m.group(3))


def fetch_thread(owner, repo, number):
    """Fetch full issue thread via gh CLI. Returns dict with title, body, comments."""
    try:
        out = subprocess.check_output(
            ["gh", "issue", "view", str(number), "-R", f"{owner}/{repo}",
             "--json", "title,body,author,state,comments,url"],
            text=True, timeout=30, stderr=subprocess.STDOUT,
        )
        return json.loads(out)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        err = e.output if hasattr(e, "output") and e.output else str(e)
        return {"_error": str(err)[:300]}
    except json.JSONDecodeError as e:
        return {"_error": f"json_decode: {e}"}


def summarize_thread_for_prompt(thread, our_username):
    """Compact the gh issue view JSON into a human-readable string for the prompt.

    The raw JSON is noisy (association, reactionGroups, etc). We want Claude to
    see a clean chronological transcript: issue body first, then each comment
    with author and body. We tag our own comments explicitly so Claude knows
    what we've already said.
    """
    if "_error" in thread:
        return f"[thread fetch failed: {thread['_error']}]"

    lines = []
    lines.append(f"Title: {thread.get('title', '(no title)')}")
    lines.append(f"State: {thread.get('state', '?')}")
    author = (thread.get("author") or {}).get("login", "?")
    lines.append(f"Opened by: @{author}")
    lines.append("")
    lines.append("=== Issue body ===")
    lines.append(thread.get("body", "") or "(empty)")
    lines.append("")
    lines.append("=== Comments (chronological) ===")

    comments = thread.get("comments", []) or []
    for i, c in enumerate(comments, 1):
        c_author = (c.get("author") or {}).get("login", "?")
        is_us = c_author == our_username
        tag = " [THIS IS US]" if is_us else ""
        body = c.get("body", "") or ""
        lines.append(f"\n--- Comment {i} by @{c_author}{tag} ---")
        lines.append(body)

    text = "\n".join(lines)
    if len(text) > THREAD_CHAR_CAP:
        text = text[:THREAD_CHAR_CAP] + f"\n\n[... truncated, {len(text) - THREAD_CHAR_CAP} chars cut ...]"
    return text


def build_prompt(reply, thread_summary, recent_replies, our_username, owner, repo, number):
    reply_json = json.dumps(reply, indent=2, default=str)

    recent_context = ""
    if recent_replies:
        snippets = "\n".join(f"  - {r}" for r in recent_replies)
        recent_context = f"""
## Your last {len(recent_replies)} GitHub replies (vary your style, don't repeat yourself)
{snippets}
"""

    return f"""You are the Social Autoposter GitHub issues engagement bot.

Your GitHub username is: {our_username}
Target issue: {owner}/{repo}#{number}

## The triggering comment we need to decide about
{reply_json}

## Full issue thread
The entire conversation is below. Our own prior comments are tagged [THIS IS US].
Read it carefully before deciding anything.

{thread_summary}
{recent_context}
{get_styles_prompt("github", context="replying")}

## Content rules
- Write like a technical peer in the thread, not a marketer.
- NO em dashes. Use commas, periods, or regular dashes.
- Match the length and register of the thread. Short threads get short replies.
- Do not promote. Voice (whether you speak AS the maker or as an outside observer) is governed by the VOICE RELATIONSHIP section below; do not override it here.
- Never link to your own repo or product in a thread that is a bug report for someone else's project. Ever.

## Bot / engagement-loop escape hatch (use sparingly, but use it)
We maintain a universal author blocklist in Postgres (`author_blocklist`),
consulted at /api/v1/replies POST time. A single block recorded by ANY of
our accounts/installs applies to EVERY future engagement from EVERY of our
accounts — universal scope, by design. The velocity gate already covers
"this handle has gotten too many replies from us in 24h/7d"; this lane is
for the LLM-judgment cases velocity cannot catch.

When to add a block (your judgment, exercised CONSERVATIVELY):
- The GitHub handle is plainly an AI/bot account: templated phrasing across
  unrelated issues, generic filler answers, account name pattern like
  `*-bot` / `Foo-AI`, comments are repository drive-by promo
- We are clearly stuck in a reciprocal engagement loop with this account
- The handle is comment-spamming across many repos (drive-by self-promo on
  every issue, not actually engaging with the bug)

DO NOT block: a maintainer we disagree with, a hostile-but-human critic,
a low-quality but human comment, or a single bad interaction. Skip those
(action='skip') — blocking is permanent until manually removed and applies
to all our accounts.

How to use it: do NOT emit a reply for this row. Instead, output the skip
JSON with reason='blocklist_added:HANDLE', and the orchestrator's
post-decision step will run reply_db.py blocklist add for you when the
reason starts with `blocklist_added:`. The handle to pass is the GitHub
login (e.g. for github.com/octocat, pass octocat). If you also want a
classification, output reason='blocklist_added:HANDLE:bot' or
'blocklist_added:HANDLE:engagement_loop'.

{get_voice_relationship_rule()}

{get_anti_patterns()}

## Tiered link strategy (DEFAULT TO TIER 1)
- Tier 1 (default): No link, no project mention. Just substance that helps the reader.
- Tier 2: Only if the thread is explicitly about a topic one of our projects solves AND nobody has offered a comparable tool yet AND the maintainer hasn't already resolved it. Mention casually.
- Tier 3: Only if someone explicitly asks "what do you use" / "any tools for this" / "link?". Then give it directly.

## Decision step: reply or skip?

Read the FULL thread above. There is NO cap on how many times we can reply to a thread. Active back-and-forth is encouraged when the conversation keeps developing and we have something useful to contribute. Do not skip just because we have prior comments in the thread. Skip only when one of the specific conditions below is clearly true.

DEFAULT TO REPLY when you have substance. Lean toward engagement, not silence.

SKIP (output action=skip) only when one of these is clearly true:
- light_acknowledgment: the triggering comment is just thanks, emoji, +1, or other content-free acknowledgment
- not_directed_at_us: the comment is in a conversation between two other people in the thread and does not ask us anything. Prefer this reason whenever the comment is addressed to someone else by @mention or context, regardless of how many prior comments we've made.
- no_value_to_add: the specific question or point has already been answered in the thread by someone else, or our reply would just repeat something we or others already said. This is about content, not count.
- conversation_concluded: the issue has been resolved, a fix has shipped, the maintainer closed it with an answer, and there is nothing substantive left to discuss. This is about thread state, not count.
- hostile_or_flagged: our prior comments in this thread were flagged as spam, someone called us a bot, or we are being accused of shilling. Back off.
- off_topic_for_us: the discussion is outside our expertise or unrelated to anything in config.json
- self_promo_risk: any honest reply would inevitably sound like self-promotion and there is no way to be genuinely helpful without it

REPLY (output action=reply with text) when any of these is true:
- The comment asks a direct question we can answer with useful insight
- We have specific technical substance to contribute that is not already in the thread
- The conversation is still alive and a peer reading it would find our next reply useful
- It is fine to be the 5th, 10th, or 20th reply from our account. Count does not matter. Substance does.

## Output format
Output ONLY ONE JSON object. No markdown, no prose, no explanations, no code fences.

For skip:
{{"action": "skip", "reason": "REASON_FROM_LIST_ABOVE"}}

For reply:
{{"action": "reply", "text": "YOUR_REPLY_TEXT", "project": null, "engagement_style": "STYLE_NAME"}}

Set "engagement_style" to the style you chose from the list above. Every reply MUST have an engagement_style. If none of the listed styles fit, you may invent a new one: set engagement_style to your new name AND include a `new_style` block (description, example, note, why_existing_didnt_fit) inside the same JSON object, per the "Inventing a new style" instructions above.
If you recommended a project from config.json in the reply text, set "project" to that project name.
The orchestrator posts the reply via gh CLI and updates the database. You only decide and draft.
"""


def run_claude(prompt, timeout=300, session_id=None):
    """Run claude -p with the given prompt. Returns (success, output, usage_dict).

    Streams output in real time to stderr for log visibility. Mirrors
    engage_reddit.py exactly.
    """
    import time as _time
    import select
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_create": 0, "cost_usd": 0.0}
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if session_id:
        cmd += ["--session-id", session_id]
    cmd += ["--tools", "Read"]
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # use OAuth, not API key
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
                                tool_name = block.get("name", "")
                                tool_in = str(block.get("input", {}))[:120]
                                print(f"[engage_github] tool: {tool_name} | {tool_in}",
                                      file=sys.stderr, flush=True)
                            elif block.get("type") == "text" and block.get("text", "").strip():
                                txt = block["text"].strip()[:200]
                                print(f"[engage_github] {txt}", file=sys.stderr, flush=True)
                    elif etype == "result":
                        print(f"[engage_github] done: cost=${evt.get('total_cost_usd', 0):.4f}",
                              file=sys.stderr, flush=True)
                except (json.JSONDecodeError, TypeError):
                    print(f"[engage_github] {line.rstrip()[:200]}", file=sys.stderr, flush=True)
            elif proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    collected.append(rest)
                break
            else:
                elapsed_s = int(_time.time() - (deadline - timeout))
                print(f"[engage_github] ... still running ({elapsed_s}s)",
                      file=sys.stderr, flush=True)
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


def parse_decision(output):
    """Extract the action JSON object from Claude's output. Returns dict or None."""
    # Try strict object first: balanced braces containing "action":"..."
    # Claude may wrap in ``` or add prose; scan for any {...} containing "action"
    candidates = re.findall(r'\{[^{}]*"action"\s*:\s*"[^"]+?"[^{}]*\}', output, re.DOTALL)
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
    # Fallback: find the last JSON-looking object
    try:
        start = output.rfind("{")
        end = output.rfind("}")
        if start != -1 and end > start:
            return json.loads(output[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def post_comment(owner, repo, number, body):
    """Post a comment via gh CLI. Returns (ok, url_or_error_string)."""
    try:
        out = subprocess.check_output(
            ["gh", "issue", "comment", str(number), "-R", f"{owner}/{repo}", "--body", body],
            text=True, timeout=60, stderr=subprocess.STDOUT,
        )
        url = None
        for line in out.strip().splitlines():
            if line.startswith("https://github.com"):
                url = line.strip()
                break
        return True, url
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        err = e.output if hasattr(e, "output") and e.output else str(e)
        return False, str(err)[:300]


def main():
    parser = argparse.ArgumentParser(description="GitHub issues engagement (one at a time, thread-aware)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print prompt for first pending reply without executing Claude")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max replies to process (0 = unlimited)")
    parser.add_argument("--timeout", type=int, default=3600,
                        help="Global timeout in seconds")
    parser.add_argument("--per-reply-timeout", type=int, default=300,
                        help="Timeout per claude session in seconds")
    args = parser.parse_args()

    config = load_config()
    excluded_authors = {a.lower() for a in config.get("exclusions", {}).get("authors", [])}
    excluded_repos = {r.lower() for r in config.get("exclusions", {}).get("github_repos", [])}
    # Auto-blocklist: owners with >=2 moderated posts in last 90 days. Same
    # source of truth as github_tools.py cmd_search uses for new candidates.
    # (HTTP-only now; per-account scoping is resolved inside the helper.)
    from github_tools import _dynamic_owner_blocklist
    excluded_repos = excluded_repos | _dynamic_owner_blocklist()
    our_username = config.get("accounts", {}).get("github", {}).get("username", "m13v")

    start_time = time.time()
    processed = 0
    succeeded = 0
    skipped = 0
    failed = 0
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_create": 0, "cost_usd": 0.0}

    consecutive_failures = 0
    last_failed_id = None

    print(f"[engage_github] Starting. limit={args.limit or 'unlimited'}, timeout={args.timeout}s, user={our_username}")

    while True:
        if time.time() - start_time > args.timeout:
            print(f"[engage_github] Global timeout reached ({args.timeout}s). Stopping.")
            break
        if args.limit and processed >= args.limit:
            print(f"[engage_github] Limit reached ({args.limit}). Stopping.")
            break
        if consecutive_failures >= 3:
            print(f"[engage_github] 3 consecutive Claude failures (likely rate limit). Stopping.")
            break

        reply = get_next_pending()
        if not reply:
            print("[engage_github] No pending replies. Done!")
            break

        # Exclusion: author
        if (reply["their_author"] or "").lower() in excluded_authors:
            subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]), "excluded_author"])
            print(f"[engage_github] #{reply['id']} skipped (excluded_author: {reply['their_author']})")
            skipped += 1
            processed += 1
            continue

        # Parse owner/repo/number from thread_url
        owner, repo, number = parse_issue_url(reply["thread_url"] or "")
        if not owner:
            subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]), "bad_thread_url"])
            print(f"[engage_github] #{reply['id']} skipped (bad_thread_url: {reply['thread_url']})")
            skipped += 1
            processed += 1
            continue

        # Exclusion: repo
        repo_key = f"{owner}/{repo}".lower()
        if repo_key in excluded_repos or owner.lower() in excluded_repos:
            subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]), "excluded_repo"])
            print(f"[engage_github] #{reply['id']} skipped (excluded_repo: {repo_key})")
            skipped += 1
            processed += 1
            continue

        # Fetch the full thread
        print(f"[engage_github] Fetching thread for {owner}/{repo}#{number}")
        thread = fetch_thread(owner, repo, number)
        if "_error" in thread:
            subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]),
                            f"fetch_error: {thread['_error']}"])
            print(f"[engage_github] #{reply['id']} skipped (fetch_error: {thread['_error'][:100]})")
            skipped += 1
            processed += 1
            continue

        thread_summary = summarize_thread_for_prompt(thread, our_username)
        recent = get_recent_archetypes(limit=3)
        prompt = build_prompt(reply, thread_summary, recent, our_username, owner, repo, number)

        if args.dry_run:
            print(f"=== DRY RUN: Prompt for reply #{reply['id']} ===")
            print(prompt)
            print("=== END DRY RUN ===")
            break

        reply_start = time.time()
        session_id = str(uuid.uuid4())
        os.environ["CLAUDE_SESSION_ID"] = session_id
        session_started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        print(f"[engage_github] Processing #{reply['id']} from @{reply['their_author']} "
              f"on {owner}/{repo}#{number}")

        ok, output, usage = run_claude(prompt, timeout=args.per_reply_timeout, session_id=session_id)
        reply_elapsed = time.time() - reply_start
        session_ended_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log_args = [PYTHON, os.path.join(REPO_DIR, "scripts", "log_claude_session.py"),
             "--session-id", session_id, "--script", "engage_github",
             "--started-at", session_started_at, "--ended-at", session_ended_at]
        orch_cost = usage.get("cost_usd")
        if isinstance(orch_cost, (int, float)) and orch_cost > 0:
            log_args.extend(["--orchestrator-cost-usd", str(orch_cost)])
        subprocess.run(log_args, capture_output=True)

        for k in total_usage:
            total_usage[k] += usage[k]

        if not ok:
            failed += 1
            consecutive_failures += 1
            # Mark as skipped so the loop advances to the next pending reply
            subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]), "claude_error"],
                           capture_output=True)
            print(f"[engage_github] #{reply['id']} CLAUDE FAILED ({reply_elapsed:.0f}s): {output[:200]}")
        else:
            consecutive_failures = 0
            decision = parse_decision(output)
            if not decision:
                failed += 1
                print(f"[engage_github] #{reply['id']} BAD OUTPUT ({reply_elapsed:.0f}s): {output[:300]}")
            elif decision.get("action") == "skip":
                reason = decision.get("reason", "unknown") or "unknown"
                # Bot/engagement-loop escape hatch. The github engage prompt
                # is run with --tools Read only (no Bash), so the model
                # cannot shell out to reply_db.py blocklist add itself.
                # Instead it signals via the skip reason pattern
                # `blocklist_added:HANDLE[:classification]` and the
                # orchestrator records the block here. HANDLE defaults to
                # the reply's their_author when the model omits it (which
                # is the common case for github since the author IS the
                # GitHub login).
                if reason.startswith("blocklist_added"):
                    parts = reason.split(":")
                    handle = parts[1].strip() if len(parts) > 1 and parts[1].strip() else (reply["their_author"] or "").strip()
                    classification = parts[2].strip() if len(parts) > 2 and parts[2].strip() in ("bot", "engagement_loop") else "bot"
                    if handle:
                        bl_cmd = [
                            PYTHON, REPLY_DB, "blocklist", "add",
                            "github_issues", handle,
                            "--reason", f"engage_llm judgment: {reason}",
                            "--classification", classification,
                            "--severity", "hard",
                            "--source-reply-id", str(reply["id"]),
                        ]
                        bl_res = subprocess.run(bl_cmd, capture_output=True, text=True)
                        if bl_res.returncode == 0:
                            print(f"[engage_github] blocklist add github_issues/{handle} cls={classification}")
                        else:
                            print(f"[engage_github] blocklist add FAILED for {handle}: {bl_res.stderr[:200]}")
                subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]), reason])
                skipped += 1
                print(f"[engage_github] #{reply['id']} SKIPPED: {reason} ({reply_elapsed:.0f}s) "
                      f"[${usage['cost_usd']:.4f}]")
            elif decision.get("action") == "reply":
                reply_text = (decision.get("text") or "").strip()
                project = decision.get("project")
                if not reply_text:
                    subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]), "empty_reply_text"])
                    failed += 1
                    print(f"[engage_github] #{reply['id']} empty reply text, marked skipped")
                else:
                    subprocess.run([PYTHON, REPLY_DB, "processing", str(reply["id"])])
                    ok_post, url_or_err = post_comment(owner, repo, number, reply_text)
                    if ok_post:
                        cmd_args = [PYTHON, REPLY_DB, "replied", str(reply["id"]), reply_text]
                        if url_or_err:
                            cmd_args.append(url_or_err)
                        style = decision.get("engagement_style", "")
                        if style:
                            if not url_or_err:
                                cmd_args.append("")  # placeholder for url
                            cmd_args.append(style)
                        subprocess.run(cmd_args)
                        if project:
                            subprocess.run(
                                [PYTHON, REPLY_DB, "set_project", str(reply["id"]), project],
                                capture_output=True,
                            )
                        succeeded += 1
                        print(f"[engage_github] #{reply['id']} POSTED ({reply_elapsed:.0f}s) "
                              f"[${usage['cost_usd']:.4f}] -> {url_or_err or '(no url)'}")
                    else:
                        subprocess.run([PYTHON, REPLY_DB, "skipped", str(reply["id"]),
                                        f"post_error: {url_or_err}"])
                        failed += 1
                        print(f"[engage_github] #{reply['id']} POST FAILED: {url_or_err}")
            else:
                failed += 1
                print(f"[engage_github] #{reply['id']} unknown action: {decision}")

            print(f"[engage_github] #{reply['id']} tokens: in={usage['input_tokens']} "
                  f"out={usage['output_tokens']} cache_r={usage['cache_read']} "
                  f"cache_w={usage['cache_create']} ${usage['cost_usd']:.4f}")

        processed += 1
        time.sleep(2)

    total_elapsed = time.time() - start_time
    print(f"\n[engage_github] === SUMMARY ===")
    print(f"[engage_github] processed={processed} succeeded={succeeded} "
          f"skipped={skipped} failed={failed} elapsed={total_elapsed:.0f}s")
    print(f"[engage_github] Total tokens: input={total_usage['input_tokens']} "
          f"output={total_usage['output_tokens']} "
          f"cache_read={total_usage['cache_read']} cache_create={total_usage['cache_create']}")
    print(f"[engage_github] Total cost: ${total_usage['cost_usd']:.4f}")
    if succeeded > 0:
        print(f"[engage_github] Avg cost per reply: ${total_usage['cost_usd'] / succeeded:.4f}")

    # Canonical machine-readable summary line. github-engage.sh greps this and
    # writes ONE log_run.py row that also carries Phase A scan counters. See
    # the comment in engage_reddit.py for the duplicate-row history.
    print(
        f"[engage_github] LOG_RUN_SUMMARY"
        f" posted={succeeded}"
        f" skipped={skipped}"
        f" failed={failed}"
        f" cost={total_usage['cost_usd']:.4f}"
        f" elapsed={int(total_elapsed)}"
    )

    subprocess.run([PYTHON, REPLY_DB, "status"])


if __name__ == "__main__":
    main()
