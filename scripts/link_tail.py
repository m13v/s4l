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

APPEND-ONLY, deterministically (2026-07-07): the model is asked for the
bridge sentence ONLY, never the full reply. main() concatenates it onto the
UNMODIFIED reply_text in Python, so the model's output is never substituted
for the drafted reply, only appended after it — it cannot rewrite the
drafted body no matter what it returns. The bridge prompt also embeds the
matched project's learned_preferences (draft_style_notes / edit_examples,
same as the main drafting prompt) so human-review feedback reaches the
bridge sentence too, not just the initial draft.

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

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import learned_preferences  # noqa: E402

REPO_DIR = os.path.expanduser("~/social-autoposter")
RUN_CLAUDE_SH = os.path.join(REPO_DIR, "scripts", "run_claude.sh")
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")

# --- X/Twitter length budget -------------------------------------------------
# X charges a FLAT 23 characters for any http/https URL (t.co wrapping),
# regardless of the link's real length. So the budget is fixed: text + 23 <= 280
# => at most 257 characters of text before the link. We only enforce this for
# twitter; reddit/linkedin have far larger ceilings and need no tail trim.
TWEET_LIMIT = 280
URL_WEIGHT = 23
TWITTER_TEXT_BUDGET = TWEET_LIMIT - URL_WEIGHT  # 257 chars for everything but the URL
_URL_RE = re.compile(r"https?://\S+")

# The model is asked for the BRIDGE SENTENCE ONLY, never a rewrite of the
# already-drafted reply (see build_prompt / main). These bound that sentence:
# below MIN_BRIDGE_CHARS of budget there's no room to say anything meaningful
# before the URL, so main() skips the Claude call and falls straight to a
# mechanical concat; above MAX_BRIDGE_CHARS the model has ignored the
# "sentence only" contract and is attempting a full rewrite, so the quality
# gate rejects it as a structural violation, not a phrasing nitpick.
MIN_BRIDGE_CHARS = 20
MAX_BRIDGE_CHARS = 400


def x_weighted_len(text: str) -> int:
    """Character count the way X computes it: every URL counts as 23."""
    if not text:
        return 0
    return len(_URL_RE.sub("x" * URL_WEIGHT, text))


def _trim_to_chars(s: str, max_chars: int) -> str:
    """Trim `s` to at most `max_chars`, backing off to a word boundary so we
    never chop mid-word, then stripping trailing punctuation/space."""
    s = s.strip()
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars].rstrip()
    sp = cut.rfind(" ")
    # only back off to the word boundary when it doesn't gut more than half
    if sp > max_chars * 0.5:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-")


def enforce_budget(text: str, link_url: str,
                   limit: int = TWEET_LIMIT) -> tuple[str, bool]:
    """Guarantee x_weighted_len(text) <= limit by trimming the BODY, never the
    link. The link is the most important part of the reply and always stays at
    the end. Returns (text, was_trimmed)."""
    if x_weighted_len(text) <= limit:
        return text, False
    if link_url and link_url in text:
        head, _, tail = text.rpartition(link_url)
        head = head.rstrip()
        tail = tail.strip()  # normally empty; the URL ends the reply
        # Budget left for the body after reserving the URL (23) + a joining
        # space + any (rare) trailing chars after the URL.
        max_head = limit - URL_WEIGHT - 1 - len(tail)
        trimmed_head = _trim_to_chars(head, max_head)
        joined = (trimmed_head + " " + link_url).strip()
        if tail:
            joined = (joined + " " + tail).strip()
        return joined, True
    # No link present (shouldn't happen on the twitter path): hard word-trim.
    return _trim_to_chars(text, limit), True


def _load_project_entry(project: str) -> dict:
    """Load the matched project's raw config.json entry (case-insensitive
    name match), or {} if config.json is unreadable or the project is
    missing. Shared by voice_relationship + learned_preferences lookups so
    both read config.json once via the same path. The link_tail subprocess
    runs with --disallowed-tools that bans Read/Glob/MCP, so these values
    must be resolved here in Python and handed to the prompt as plain text.
    """
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except Exception:
        return {}
    name_lc = (project or "").lower()
    for p in cfg.get("projects", []):
        if (p.get("name") or "").lower() == name_lc:
            return p
    return {}


def resolve_voice_relationship(project_entry: dict) -> str:
    """Returns "first_party" or "third_party" from the project's
    `voice_relationship` field. Defaults to "first_party" if the field is
    absent, matching the historical pre-2026-05-27 behavior so we never
    silently mute first-party voice."""
    val = (project_entry.get("voice_relationship") or "").strip().lower()
    return val if val in ("first_party", "third_party") else "first_party"


def resolve_learned_preferences_block(project_entry: dict) -> str:
    """Render this project's learned_preferences (draft_style_notes,
    edit_examples, audience/thread avoid-prefer) via the shared
    learned_preferences.prompt_block() renderer — the same text the main
    drafting prompt embeds. Empty string when the block is disabled or has
    no entries yet."""
    return learned_preferences.prompt_block(project_entry)

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
                 project: str, platform: str,
                 voice_relationship: str = "first_party",
                 learned_prefs_block: str = "",
                 bridge_budget: int | None = None) -> str:
    """Compose the one-shot prompt for the bridge sentence ONLY.

    The model is never asked to reproduce or rewrite the reply already
    drafted; it's shown that text for context and told explicitly not to
    repeat it. main() appends whatever comes back to the UNMODIFIED
    reply_text in Python afterward — the model's output is never substituted
    for the drafted reply, only concatenated after it, so it structurally
    cannot rewrite the drafted body regardless of what it outputs.

    `voice_relationship` ("first_party" | "third_party") is resolved by the
    caller from config.json and selects the example sentences + voice rule
    embedded in the prompt. third_party projects (Agora, Runner, Podlog,
    studyly, NightOwl, PieLine as of 2026-05-27) MUST be referred to in
    third-person; first_party projects own the "we ship / we built" voice.

    `learned_prefs_block` is the project's rendered learned_preferences
    (learned_preferences.prompt_block()), the same distilled human-review
    feedback the main drafting prompt treats as mandatory; empty when the
    project has no notes yet.
    """
    if voice_relationship == "third_party":
        voice_rule = (
            f"VOICE RELATIONSHIP — third_party (HIGHEST PRIORITY):\n"
            f"You are an OUTSIDE OBSERVER of {project}, NOT its team. Refer "
            f"to the product in third-person, by name or by 'their': "
            f"\"{project} ships X\", \"their Y\", \"the {project} team rolled "
            f"out Z\". Acceptable narrator framings: \"I tried {project}\", "
            f"\"I noticed {project} does X\", \"{project} ships Y\". BANNED: "
            f"\"we ship\", \"our product\", \"we built\", \"I built this\", "
            f"\"what we offer\". A bridge that uses first-person plural about "
            f"{project} impersonates the client and is a hard failure."
        )
        example_block = (
            f"  - \"fwiw the rubric scoring on rephrased stems is what {project} ships, {link_url}\"\n"
            f"  - \"{project} runs the same recall-on-revisit pattern, scores against a 4-axis rubric, {link_url}\""
        )
    else:
        voice_rule = (
            f"VOICE RELATIONSHIP — first_party:\n"
            f"You speak AS the builder / maker of {project}. First-person "
            f"plural about the product (\"we ship X\", \"our Y\", \"we built "
            f"Z\") is allowed. Recommendation-style framing is also fine."
        )
        example_block = (
            f"  - \"fwiw the rubric scoring on rephrased stems is what we built {project} for, {link_url}\"\n"
            f"  - \"we ship the same recall-on-revisit pattern in {project}, scores against a 4-axis rubric, {link_url}\""
        )

    # X-specific hard length budget. The reply's body is FIXED (never resized
    # by the model); only the bridge sentence's own budget matters, computed
    # by the caller from how much room the already-drafted body leaves.
    # Other platforms (reddit/linkedin) have far larger ceilings, so we add
    # no tight cap there.
    length_rule = ""
    if platform == "twitter" and bridge_budget is not None:
        length_rule = (
            f"HARD LENGTH LIMIT for YOUR SENTENCE ONLY (X counts EVERY link as "
            f"exactly {URL_WEIGHT} characters, no matter how long it looks; "
            f"that's already reserved below):\n"
            f"- Everything you write BEFORE the URL must be \u2264 {max(bridge_budget, 0)} "
            f"characters. The reply above is fixed length; you are not resizing "
            f"it, only adding a short sentence after it.\n"
        )

    learned_prefs_section = ""
    if learned_prefs_block:
        learned_prefs_section = (
            learned_prefs_block.strip() + "\n"
            "The draft_style_notes / edit_examples entries above are MANDATORY "
            "for how you phrase your bridge sentence, same as they are for the "
            "original draft; on conflict they override the example sentences "
            "below. The audience/thread avoid-prefer entries are about "
            "candidate judging, already applied earlier in the pipeline \u2014 not "
            "your job here.\n"
        )

    return f"""You are writing ONLY the bridge sentence that gets APPENDED after a social media reply we already drafted (shown below). This is a one-shot task. Output ONLY the bridge sentence itself: no preamble, no explanation, no quotes, and do NOT repeat, paraphrase, or rewrite any part of the reply below \u2014 it is already final and will be appended before your output automatically, verbatim.

PLATFORM: {platform}
PROJECT: {project}
LANDING PAGE URL: {link_url}

{voice_rule}

{learned_prefs_section}

{length_rule}

ORIGINAL THREAD WE ARE REPLYING TO:
{thread_text}

REPLY ALREADY DRAFTED (fixed, DO NOT repeat or modify \u2014 your sentence is appended after this automatically):
{reply_text}

YOUR TASK:
Write ONE bridge sentence (\u2264 22 words) that:
  1. References the SINGLE strongest specific claim, mechanism, or detail from the reply above (e.g. "rephrasing on revisit", "a 4-axis rubric", "200ms p95", "automatic distractor scoring") \u2014 pick ONE concrete thing, not a category.
  2. Names a CONCRETE PRODUCT MECHANISM that delivers it (verb + noun, inferred from the URL slug + project context). Do NOT say "a tool for this", "something that helps", "made this for it" \u2014 those are banned.
  3. Ends with the URL exactly as given. No period after. No "click here", "check it out", "give it a try".
  4. Reads in the voice of the reply above (lowercase if reply is lowercase, casual if reply is casual).
  5. Obeys the VOICE RELATIONSHIP rule above. This rule overrides any default phrasing instinct.
  6. Obeys the LEARNED PREFERENCES above when present. This rule overrides the example sentences below on conflict.

OUTPUT FORMAT (strict):
Output ONLY your bridge sentence, ending in the URL, on a single line. Nothing else: no JSON, no markdown, no quotes, and absolutely none of the reply text shown above.

Example bridge sentences (do NOT copy verbatim \u2014 these are FORM examples, voice-matched to this project):
{example_block}

Write the bridge sentence now."""


def _unwrap_queue_envelope(text: str) -> str:
    """Unwrap claude_job.py's queue-provider envelope if present.

    'twitter-link-tail' is a queue-mapped tag (scripts/claude_job.py
    TAG_TO_TYPE, since 2026-07-06), so run_claude.sh routes it through
    claude_job.py's provider instead of exec'ing `claude` directly. On
    success that provider ALWAYS emits a claude `--output-format json`-shaped
    envelope on stdout: {"type":"result","subtype":"success","is_error":false,
    "structured_output":<answer>,"result":<answer>} — regardless of whether
    the caller's prompt asked for JSON. This prompt explicitly asks for bare
    text ("no JSON, no quotes"), so without this unwrap the WHOLE envelope
    string was fed straight into clean_output()/enforce_budget(), which
    produced garbage: the raw JSON leaking into the reply, or the link URL
    (present twice in the envelope: once in structured_output, once in
    result) getting doubled up when enforce_budget's trim-and-reappend ran
    (2026-07-06 regression, first release rc.22).

    A direct (non-queued) `claude -p` response is plain text and never
    matches this shape, so this is a no-op for that path.
    """
    t = (text or "").strip()
    if not (t.startswith("{") and t.endswith("}")):
        return text
    try:
        obj = json.loads(t)
    except Exception:
        return text
    if not isinstance(obj, dict) or obj.get("type") != "result":
        return text
    payload = obj.get("structured_output")
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("text", "reply", "answer", "output"):
            v = payload.get(key)
            if isinstance(v, str):
                return v
    result = obj.get("result")
    if isinstance(result, str):
        return result
    return text


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
        return (True, _unwrap_queue_envelope(out), "")
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


THIRD_PARTY_VOICE_VIOLATIONS = (
    re.compile(r"\bwe ship\b", re.IGNORECASE),
    re.compile(r"\bwe built\b", re.IGNORECASE),
    re.compile(r"\bwe made\b", re.IGNORECASE),
    re.compile(r"\bwe offer\b", re.IGNORECASE),
    re.compile(r"\bour product\b", re.IGNORECASE),
    re.compile(r"\bI built (?:this|it)\b", re.IGNORECASE),
    re.compile(r"\bwhat we (?:ship|build|offer|make)\b", re.IGNORECASE),
)


def passes_quality_gate(bridge: str, link_url: str,
                        voice_relationship: str = "first_party",
                        ) -> tuple[bool, str]:
    """Return (passes, reason_if_not).

    Validates the model-written BRIDGE SENTENCE ALONE. reply_text is trusted,
    already-vetted content that this function never sees and never judges —
    the append-only contract means reply_text can't fail this gate, only the
    newly generated bridge can.

    Hard rules:
      - must contain link_url
      - must end with link_url (allow trailing whitespace, nothing else)
      - must NOT contain banned phrases
      - must not be wildly longer than a sentence (MAX_BRIDGE_CHARS): the
        model ignored the "bridge sentence only" contract and is attempting
        a full rewrite — a structural violation, not a phrasing nitpick
      - on third_party projects, must NOT use first-person-plural product
        ownership phrases ("we ship", "we built", "our product", ...). The
        link_tail prompt selects voice-matched examples but the model can
        still drift; on violation we fall back to the mechanical concat so
        the post still ships without impersonating the client (root cause of
        the 2026-05-27 Agora OODAO incident).
    """
    if not bridge:
        return (False, "empty")
    if link_url not in bridge:
        return (False, "no_url")
    # Trailing-URL check (nothing meaningful after URL, optional ./! is fine
    # to strip; but our prompt forbids trailing period — so just check no
    # alphanumeric content follows).
    tail = bridge.split(link_url, 1)[1].strip()
    if tail and re.search(r"[A-Za-z0-9]", tail):
        return (False, f"content_after_url: {tail[:40]!r}")
    lower = bridge.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            return (False, f"banned_phrase: {phrase!r}")
    if voice_relationship == "third_party":
        for rx in THIRD_PARTY_VOICE_VIOLATIONS:
            m = rx.search(bridge)
            if m:
                return (False, f"third_party_voice_violation: {m.group(0)!r}")
    if len(bridge) > MAX_BRIDGE_CHARS:
        return (False, f"bridge_too_long:{len(bridge)}>{MAX_BRIDGE_CHARS}")
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
    ap.add_argument("--voice-relationship", default=None,
                    choices=["first_party", "third_party"],
                    help="Override the voice_relationship lookup. Defaults to "
                         "the value in config.json for --project, or "
                         "first_party if missing.")
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

    # Plugin (MCP post_drafts) flow sets S4L_SKIP_LINK_TAIL=1. The bridge only
    # rewords prose around the URL — the minted short link is produced by a
    # separate deterministic wrap step in twitter_post_plan.py — so the Claude
    # call buys nothing there, and on .mcpb customer boxes (no `claude` binary)
    # it burns ~35s of run_claude.sh retry backoff per post before falling back
    # to this exact mechanical concat. Short-circuit straight to the concat.
    # The local cron/plist autopilot leaves this env unset and still generates
    # the bridge sentence.
    if os.environ.get("S4L_SKIP_LINK_TAIL") == "1":
        limit = TWEET_LIMIT if args.platform == "twitter" else None
        fb_text, fb_trim = enforce_budget(
            mechanical_fallback(reply_text, link_url), link_url,
            limit if limit is not None else TWEET_LIMIT * 100)
        out = {
            "ok": True,
            "text": fb_text,
            "tail": link_url,
            "model_call_ok": False,
            "fallback_used": True,
            "budget_trimmed": fb_trim,
            "error": "skipped_plugin_flow",
            "elapsed_sec": 0.0,
        }
        print(json.dumps(out), flush=True)
        return 0

    project_entry = _load_project_entry(args.project)
    voice_relationship = args.voice_relationship or resolve_voice_relationship(project_entry)
    learned_prefs_block = resolve_learned_preferences_block(project_entry)
    # Length cap is X-specific; reddit/linkedin pass None (no tail trim).
    limit = TWEET_LIMIT if args.platform == "twitter" else None

    # Deterministic no-room guard: if the already-drafted reply alone leaves
    # no meaningful space for a bridge sentence before the (flat-weighted)
    # URL, skip the Claude call outright — a squeezed-in fragment is worse
    # than no bridge — and fall back to the mechanical concat like any other
    # failure path.
    bridge_budget = None
    if limit is not None:
        bridge_budget = limit - URL_WEIGHT - 1 - x_weighted_len(reply_text)
        if bridge_budget < MIN_BRIDGE_CHARS:
            fb_text, fb_trim = enforce_budget(
                mechanical_fallback(reply_text, link_url), link_url, limit)
            out = {
                "ok": True,
                "text": fb_text,
                "tail": link_url,
                "model_call_ok": False,
                "fallback_used": True,
                "budget_trimmed": fb_trim,
                "error": f"no_room_for_bridge:{bridge_budget}",
                "elapsed_sec": 0.0,
            }
            print(json.dumps(out), flush=True)
            return 0

    prompt = build_prompt(
        reply_text=reply_text, link_url=link_url,
        thread_text=(args.thread_text or "").strip()[:2000],
        project=args.project, platform=args.platform,
        voice_relationship=voice_relationship,
        learned_prefs_block=learned_prefs_block,
        bridge_budget=bridge_budget,
    )

    started = time.time()
    ok, raw, err = call_claude(prompt, timeout_sec=args.timeout,
                               use_run_claude_sh=not args.no_wrapper)
    elapsed = round(time.time() - started, 2)

    if not ok:
        fb_text, fb_trim = enforce_budget(
            mechanical_fallback(reply_text, link_url), link_url,
            limit if limit is not None else TWEET_LIMIT * 100)
        out = {
            "ok": True,
            "text": fb_text,
            "tail": link_url,
            "model_call_ok": False,
            "fallback_used": True,
            "budget_trimmed": fb_trim,
            "error": err or "model_call_failed",
            "elapsed_sec": elapsed,
        }
        print(json.dumps(out), flush=True)
        return 0

    bridge = clean_output(raw)
    passes, reason = passes_quality_gate(bridge, link_url,
                                         voice_relationship=voice_relationship)
    if not passes:
        fb_text, fb_trim = enforce_budget(
            mechanical_fallback(reply_text, link_url), link_url,
            limit if limit is not None else TWEET_LIMIT * 100)
        out = {
            "ok": True,
            "text": fb_text,
            "tail": link_url,
            "model_call_ok": True,
            "fallback_used": True,
            "budget_trimmed": fb_trim,
            "error": f"quality_gate_failed:{reason}",
            "raw_model_output": raw[:500],
            "elapsed_sec": elapsed,
        }
        print(json.dumps(out), flush=True)
        return 0

    # Successful path: append the model-written bridge to the UNMODIFIED
    # reply_text. This is the deterministic append-only guarantee — the
    # model's output was validated above as a standalone sentence and is
    # never substituted for reply_text, only concatenated after it.
    final_text = f"{reply_text.rstrip()} {bridge}".strip()
    budget_trimmed = False
    if limit is not None:
        # Belt-and-suspenders: should be a no-op given the bridge_budget
        # precheck above, unless the model ignored the length rule. Trims
        # from the END of the pre-URL text (the model's own bridge, appended
        # last), so a well-behaved reply_text is only touched in the
        # degenerate case where reply_text alone already exceeds the limit.
        final_text, budget_trimmed = enforce_budget(final_text, link_url, limit)

    out = {
        "ok": True,
        "text": final_text,
        "tail": bridge,
        "model_call_ok": True,
        "fallback_used": False,
        "budget_trimmed": budget_trimmed,
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
