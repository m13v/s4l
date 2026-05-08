#!/usr/bin/env python3
"""
link_tail.py — Generate a context-aware bridge sentence that folds a landing
page URL into a Twitter (or other social) reply.

Replaces the mechanical concat `f"{reply_text} {link_url}"` in
twitter_post_plan.py with a one-shot Claude call (default smart model, NOT
Haiku) that:

  1. Re-reads the original thread + the reply we already drafted
  2. Identifies the strongest claim/mechanism in our reply
  3. Looks at the landing page URL's slug for a hint about what's there
  4. Writes 1 short bridge sentence that names a concrete benefit and
     ends with the URL — no period after, no "click here".

Why not Haiku: bridge writing requires reading two pieces of context (thread
+ our reply) and producing language that doesn't read as bolted-on. The
cheap model fails this; tested via the existing studyly Twitter dataset
(see CLAUDE memory `feedback_link_tail_default_model`).

Usage (CLI / from twitter_post_plan.py):
    python3 link_tail.py \\
        --reply-text "Step 2 CK was the one that burned me out worst..." \\
        --link-url "https://studyly.io/t/active-recall-question-generator" \\
        --thread-text "huge milestone, just passed step 2..." \\
        --project "studyly" \\
        --platform "twitter"

Stdout (single JSON object):
    {"ok": true, "text": "<reply_text with bridge tail + URL>",
     "tail": "<just the bridge sentence with URL>",
     "model_call_ok": true, "fallback_used": false}

On any failure (claude errored, returned empty, returned a sentence that
fails sanity checks) the script falls back to the mechanical concat:
    {"ok": true, "text": "<reply_text> <link_url>",
     "tail": "<link_url>", "model_call_ok": false,
     "fallback_used": true, "error": "<short reason>"}

Exit codes:
    0 — wrote a JSON object to stdout (whether smart or fallback)
    2 — argparse / IO failure before we could write any JSON
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

REPO_DIR = os.path.expanduser("~/social-autoposter")
RUN_CLAUDE_SH = os.path.join(REPO_DIR, "scripts", "run_claude.sh")

# Paths to the Claude Code CLI in order of preference. run_claude.sh resolves
# `claude` from PATH; we fall back to a direct nvm path if PATH lookup fails
# (twitter_post_plan.py is invoked from a launchd shell that may have a thin
# PATH).
CLAUDE_CLI_CANDIDATES = [
    "/Users/matthewdi/.nvm/versions/node/v20.19.4/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
]


def resolve_claude_cli() -> str:
    for p in CLAUDE_CLI_CANDIDATES:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    # Last resort: ask `which`. If this also fails the caller falls back to
    # mechanical concat — link_tail.py is always tolerant of model failure.
    try:
        out = subprocess.check_output(
            ["/usr/bin/env", "which", "claude"],
            text=True, timeout=5,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return ""


def build_prompt(*, reply_text: str, link_url: str, thread_text: str,
                 project: str, platform: str) -> str:
    """Compose the one-shot prompt for the bridge sentence.

    Kept tight on purpose: the model gets only the four pieces of context it
    needs, plus a precise output contract. No tools, no MCP, no file access.
    """
    return f"""You are writing the FINAL bridge sentence that folds a product link into a social media reply we already drafted. This is a one-shot task. Output ONLY the bridge sentence (no preamble, no explanation, no quotes).

PLATFORM: {platform}
PROJECT: {project}
LANDING PAGE URL: {link_url}

ORIGINAL THREAD WE ARE REPLYING TO:
{thread_text}

REPLY WE ALREADY DRAFTED (its last sentence is what your bridge will REPLACE / EXTEND):
{reply_text}

YOUR TASK:
Rewrite the reply so the LAST sentence is a 1-sentence (≤ 22 words) bridge that:
  1. References the SINGLE strongest specific claim, mechanism, or detail from the existing reply (e.g. "rephrasing on revisit", "a 4-axis rubric", "200ms p95", "automatic distractor scoring") — pick ONE concrete thing, not a category.
  2. Names a CONCRETE PRODUCT MECHANISM that delivers it (verb + noun, inferred from the URL slug + project context). Do NOT say "a tool for this", "something that helps", "made this for it" — those are banned.
  3. Frames the project as OUR OWN creation (first-person ownership: "we built", "I shipped", "we made"), NEVER as third-party ("I found", "there's a tool", "came across").
  4. Ends with the URL exactly as given. No period after. No "click here", "check it out", "give it a try".
  5. Reads in the voice of the reply (lowercase if reply is lowercase, casual if reply is casual).

REPLACEMENT RULE:
- If the reply has a clear empathy/advice body, KEEP that body verbatim and append the bridge as a new sentence (separated by a single space).
- If the reply already trails off with weak filler, REPLACE just the trailing weak portion with the bridge.

OUTPUT FORMAT (strict):
Output the FULL FINAL REPLY TEXT (body + bridge sentence ending in URL) on a single line. Nothing else. No JSON, no markdown, no quotes.

Example bridge sentences (do NOT copy verbatim — these are FORM examples):
  - "fwiw the rubric scoring on rephrased stems is what we built {project} for, {link_url}"
  - "we ship the same recall-on-revisit pattern in {project}, scores against a 4-axis rubric, {link_url}"

Write the final reply now."""


def call_claude(prompt: str, *, timeout_sec: int = 120,
                use_run_claude_sh: bool = True) -> tuple[bool, str, str]:
    """Run claude -p in headless mode. Returns (ok, stdout_text, error_msg).

    Uses run_claude.sh for cost tracking under script_tag 'twitter-link-tail'
    so the cost rolls into the dashboard claude_sessions table. Falls back
    to direct claude invocation if run_claude.sh is missing.
    """
    use_wrapper = use_run_claude_sh and os.path.exists(RUN_CLAUDE_SH)
    cli = resolve_claude_cli()
    if not cli and not use_wrapper:
        return (False, "", "no_claude_cli")

    if use_wrapper:
        cmd = [
            "bash", RUN_CLAUDE_SH, "twitter-link-tail",
            "-p", prompt,
            "--max-turns", "1",
            "--disallowed-tools",
            "ScheduleWakeup,CronCreate,CronDelete,CronList,EnterPlanMode,EnterWorktree,Bash,Edit,Write,Read,Grep,Glob,WebFetch,WebSearch,Agent,TodoWrite,NotebookEdit,LSP,Monitor,PushNotification,RemoteTrigger,TaskOutput,TaskStop,ListMcpResourcesTool,ReadMcpResourceTool",
        ]
    else:
        cmd = [
            cli, "-p", prompt,
            "--max-turns", "1",
            "--disallowed-tools",
            "ScheduleWakeup,CronCreate,CronDelete,CronList,EnterPlanMode,EnterWorktree,Bash,Edit,Write,Read,Grep,Glob,WebFetch,WebSearch,Agent,TodoWrite,NotebookEdit,LSP,Monitor,PushNotification,RemoteTrigger,TaskOutput,TaskStop,ListMcpResourcesTool,ReadMcpResourceTool",
        ]

    # Pre-strip MCP config (we don't need any tools for plain text gen). Some
    # claude installs auto-load MCP from ~/.claude/mcp.json — pass an empty
    # JSON config to force-disable. /dev/null doesn't parse as JSON, so we
    # use a real file written once into /tmp.
    empty_mcp = "/tmp/.link_tail_empty_mcp.json"
    if not os.path.exists(empty_mcp):
        try:
            Path(empty_mcp).write_text('{"mcpServers": {}}', encoding="utf-8")
        except Exception:
            empty_mcp = ""
    if empty_mcp:
        cmd += ["--strict-mcp-config", "--mcp-config", empty_mcp]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec,
            cwd=REPO_DIR,
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            return (False, out, f"rc={r.returncode}: {err[:300]}")
        if not out:
            return (False, "", f"empty_stdout: {err[:200]}")
        return (True, out, "")
    except subprocess.TimeoutExpired:
        return (False, "", f"timeout_{timeout_sec}s")
    except FileNotFoundError as e:
        return (False, "", f"file_not_found: {e}")


# Sanity guards. The model occasionally returns extra commentary; strip it.
PREAMBLE_RES = [
    re.compile(r"^(here(?:'s| is)|here you go|sure|okay|ok|got it|the (?:final )?reply(?: is)?:?)\s*[,:.\-]?\s*", re.IGNORECASE),
    re.compile(r"^[\"'`]+"),
    re.compile(r"[\"'`]+$"),
]
BANNED_PHRASES = [
    "click here", "check it out", "give it a try",
    "i found this", "there's a tool", "came across this",
    "saw this manual", "found this guide",
    # Generic-verb-no-object failures.
    "a tool for exactly this", "made this for it",
]


def clean_output(text: str) -> str:
    """Strip preamble and surrounding quotes; collapse whitespace."""
    t = text.strip()
    # If the model returned multiple lines, take the LAST non-empty line — the
    # actual reply is at the bottom (preamble like "Here's the reply:" is on
    # earlier lines).
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if not lines:
        return ""
    candidate = lines[-1]
    # Strip wrapping quotes / markdown.
    for rx in PREAMBLE_RES:
        candidate = rx.sub("", candidate).strip()
    # Collapse internal whitespace.
    candidate = re.sub(r"\s+", " ", candidate).strip()
    return candidate


def passes_quality_gate(final_text: str, link_url: str) -> tuple[bool, str]:
    """Return (passes, reason_if_not).

    Hard rules:
      - must contain link_url
      - must end with link_url (allow trailing whitespace, nothing else)
      - must NOT contain banned phrases
      - must not be shorter than reply text would have been (silly model fail)
    """
    if not final_text:
        return (False, "empty")
    if link_url not in final_text:
        return (False, "no_url")
    # Trailing-URL check (nothing meaningful after URL, optional ./! is fine
    # to strip; but our prompt forbids trailing period — so just check no
    # alphanumeric content follows).
    tail = final_text.split(link_url, 1)[1].strip()
    if tail and re.search(r"[A-Za-z0-9]", tail):
        return (False, f"content_after_url: {tail[:40]!r}")
    lower = final_text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            return (False, f"banned_phrase: {phrase!r}")
    # Length sanity: model returning a 5-word stub is a fail.
    if len(final_text.split()) < 8:
        return (False, "too_short")
    return (True, "")


def mechanical_fallback(reply_text: str, link_url: str) -> str:
    """The pre-existing concat behavior. Identical to the line we replace
    in twitter_post_plan.py."""
    return f"{reply_text} {link_url}".strip() if link_url else reply_text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reply-text", required=True,
                    help="The reply we already drafted (no link).")
    ap.add_argument("--link-url", required=True,
                    help="The landing page URL to fold in.")
    ap.add_argument("--thread-text", default="",
                    help="The original thread / tweet we are replying to.")
    ap.add_argument("--project", required=True,
                    help="Project name (e.g. 'studyly', 'fazm').")
    ap.add_argument("--platform", default="twitter",
                    help="Platform (twitter, reddit, linkedin).")
    ap.add_argument("--timeout", type=int, default=120,
                    help="Hard timeout for the claude call (seconds).")
    ap.add_argument("--no-wrapper", action="store_true",
                    help="Skip run_claude.sh; call claude directly. For testing.")
    args = ap.parse_args()

    reply_text = (args.reply_text or "").strip()
    link_url = (args.link_url or "").strip()
    if not reply_text or not link_url:
        # Garbage in → mechanical concat (which respects empty link_url).
        out = {
            "ok": True,
            "text": mechanical_fallback(reply_text, link_url),
            "tail": link_url,
            "model_call_ok": False,
            "fallback_used": True,
            "error": "missing_input",
        }
        print(json.dumps(out), flush=True)
        return 0

    prompt = build_prompt(
        reply_text=reply_text, link_url=link_url,
        thread_text=(args.thread_text or "").strip()[:2000],
        project=args.project, platform=args.platform,
    )

    started = time.time()
    ok, raw, err = call_claude(prompt, timeout_sec=args.timeout,
                               use_run_claude_sh=not args.no_wrapper)
    elapsed = round(time.time() - started, 2)

    if not ok:
        out = {
            "ok": True,
            "text": mechanical_fallback(reply_text, link_url),
            "tail": link_url,
            "model_call_ok": False,
            "fallback_used": True,
            "error": err or "model_call_failed",
            "elapsed_sec": elapsed,
        }
        print(json.dumps(out), flush=True)
        return 0

    cleaned = clean_output(raw)
    passes, reason = passes_quality_gate(cleaned, link_url)
    if not passes:
        out = {
            "ok": True,
            "text": mechanical_fallback(reply_text, link_url),
            "tail": link_url,
            "model_call_ok": True,
            "fallback_used": True,
            "error": f"quality_gate_failed:{reason}",
            "raw_model_output": raw[:500],
            "elapsed_sec": elapsed,
        }
        print(json.dumps(out), flush=True)
        return 0

    # Successful path: extract the bridge tail (everything after the original
    # reply body's prefix, OR the last sentence containing the URL).
    tail = ""
    # Heuristic: the bridge is the last sentence in cleaned. Split on ". " or
    # "! " or "? "; take the last chunk.
    chunks = re.split(r"(?<=[.!?])\s+", cleaned)
    for c in reversed(chunks):
        if link_url in c:
            tail = c.strip()
            break
    if not tail:
        tail = cleaned

    out = {
        "ok": True,
        "text": cleaned,
        "tail": tail,
        "model_call_ok": True,
        "fallback_used": False,
        "elapsed_sec": elapsed,
    }
    print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted"}), flush=True)
        sys.exit(2)
    except Exception as e:
        # Last-resort safety net so callers never get a non-JSON crash.
        print(json.dumps({
            "ok": True,
            "text": "",
            "tail": "",
            "model_call_ok": False,
            "fallback_used": True,
            "error": f"unhandled:{type(e).__name__}:{e}",
        }), flush=True)
        sys.exit(0)
