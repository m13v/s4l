#!/usr/bin/env python3
"""Classify a Claude CLI run envelope or shell log into a single failure reason key.

Reads stdin OR a file path, prints ONE snake_case reason key on stdout (or empty
line if nothing matched). Used by run-twitter-cycle.sh and peer pipelines so
dashboard "failed: <reason>" pills carry the *actual* error class instead of
collapsing every Claude-side death to a generic 'phase1_no_tweets'.

Patterns ordered most-specific first; the first match wins. Order matters:
- monthly_limit must beat the generic rate_limit fallback
- credit_balance must beat the generic api_error fallback
- stream_idle_timeout must beat the generic api_error fallback

Reason keys mirror existing dashboard conventions (`bin/server.js` renders them
as "failed: <reason>"). Do not rename without sweeping the dashboard's tooltip
copy.

Usage:
    cat run.log | classify_run_error.py
    classify_run_error.py /path/to/log.txt
    classify_run_error.py --quiet  # exit 1 if no match (for grep-style chaining)
"""
import re
import sys
from pathlib import Path

# Order: most-specific first. Each entry is (reason_key, list_of_regex_patterns).
# Patterns are matched case-insensitive against the full text.
PATTERNS = [
    # Anthropic billing / quota hits. Single source of truth so dashboard
    # tooltip copy stays consistent across every pipeline.
    ("monthly_limit", [
        r'"api_error_status":\s*429',
        r"monthly usage limit",
        r"hit your org'?s monthly",
        r"monthly limit reached",
        r"month'?s (?:usage|allowance|cap)",
        r"\"hit your limit\"",
        r"hit your limit",
    ]),
    ("daily_limit", [
        r"daily rate limit",
        r"daily usage limit",
        r"daily limit reached",
    ]),
    ("rate_limit_5h", [
        r"5[\s-]?hour (?:rate )?limit",
        r"rate_limit_5h",
        r"per[\s-]?5h (?:rate )?limit",
    ]),
    ("credit_balance", [
        r"credit balance is too low",
        r"insufficient credit",
        r"out of credits",
    ]),
    # Anthropic streaming / transient errors. These are recoverable on retry
    # but burn the whole cycle's spend when they happen, so surface them with
    # a distinct key (operators care about the difference between "we hit the
    # cap" and "Anthropic's stream blinked").
    ("stream_idle_timeout", [
        r"stream idle timeout",
        r"partial response received",
    ]),
    ("api_overloaded", [
        r'"api_error_status":\s*529',
        r"overloaded_error",
        r"\"type\":\s*\"overloaded\"",
    ]),
    ("api_unavailable", [
        r'"api_error_status":\s*503',
        r"service unavailable",
    ]),
    ("api_error_500", [
        r'"api_error_status":\s*500',
        r"internal server error",
    ]),
    ("context_overflow", [
        r"context[\s_-]?length (?:exceeded|too long)",
        r"context[\s_-]?window (?:exceeded|too long)",
        r"prompt is too long",
        r"max(?:imum)? context (?:length|window)",
    ]),
    # Claude CLI is not authenticated. On a fresh machine the standalone
    # `claude` CLI that the pipelines shell out to (via run_claude.sh) has its
    # own credential store (~/.claude/.credentials.json / keychain) SEPARATE
    # from Claude Desktop, so it can be logged out even when the MCP host is
    # logged in. The CLI then returns a result envelope with is_error:true and
    # result:"Not logged in Â· Please run /login". Must beat the generic
    # api_error fallback so the dashboard + MCP can tell the user to run
    # `claude /login` instead of mis-reporting a benign empty plan. The two
    # phrases below are CLI-specific and don't appear in scraped tweet bodies.
    ("claude_not_logged_in", [
        r"not logged in",
        r"please run /login",
    ]),
    # Generic Anthropic-side error fallback. Fires when `is_error:true` shows
    # up in the JSONL envelope but none of the more specific patterns matched.
    # Lets the operator see "failed: api_error" instead of nothing at all.
    #
    # NOTE: platform/browser-level errors (auth redirects, RATE_LIMITED_TWITTER,
    # browser navigation timeouts, posting_blocked) are deliberately NOT
    # classified here — they're already covered by per-pipeline cascades in
    # run-twitter-cycle.sh / run-reddit-search.sh / engage_reddit.py with
    # stable historical reason keys the dashboard already renders. Keeping
    # those out of this classifier prevents double-counting.
    ("api_error", [
        r'"is_error":\s*true',
        r"api error:",
        r"anthropic.*error",
    ]),
]


def classify(text: str) -> str:
    """Return the first matching reason key, or '' if nothing matched."""
    if not text:
        return ""
    # Pre-lower once; all patterns are case-insensitive anyway, but a single
    # lowercase pass lets us skip the re.IGNORECASE flag overhead on every
    # match attempt against multi-MB log buffers.
    low = text.lower()
    for reason, regexes in PATTERNS:
        for rx in regexes:
            if re.search(rx, low):
                return reason
    return ""


def _read_input(path: str) -> str:
    """Read text from a path or '-' (stdin). Errors degrade to empty string."""
    if path == "-" or path == "":
        return sys.stdin.read()
    try:
        return Path(path).read_text(errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""


def main() -> int:
    quiet = False
    args = list(sys.argv[1:])
    if "--quiet" in args:
        quiet = True
        args.remove("--quiet")
    path = args[0] if args else "-"
    text = _read_input(path)
    reason = classify(text)
    if reason:
        print(reason)
        return 0
    if quiet:
        return 1
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
