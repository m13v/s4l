#!/usr/bin/env python3
"""
twitter_post_plan.py — Phase 2b-post helper for run-twitter-cycle.sh.

Reads the candidate plan JSON file (already enriched with link_url by
twitter_gen_links.py), and for each candidate:

  1. Calls scripts/twitter_browser.py reply <candidate_url> "<reply_text> <link_url>"
  2. Logs the post via scripts/log_post.py (INSERT mode), captures post_id
  3. Bumps every campaign in applied_campaigns via scripts/campaign_bump.py
  4. Marks link_edited_at via scripts/log_post.py --mark-self-reply
     (the link is embedded in the primary reply; no self-reply will follow)
  5. UPDATE twitter_candidates SET status='posted', posted_at=NOW(), post_id=...

Browser lock IS expected to be held by the caller (run-twitter-cycle.sh
re-acquires twitter-browser before invoking this script). twitter_browser.py
attaches to the twitter-harness Chrome via CDP on the browser-harness
profile, so the exclusive lock matters.

The script exits 0 unless it can't even load the plan; per-candidate failures
are recorded in twitter_candidates.status (skipped|failed) and a JSON summary
is written to stdout for the caller to read counts back.

Stdout summary (one JSON object on the last line):
    {"posted": N, "skipped": N, "failed": N,
     "failure_reasons": "timeout:1,log_post_no_id:1,...",
     "skip_reasons":    "duplicate_thread_pre_post:3,empty_reply_text:1,..."}

`failure_reasons` is real failures only (the dashboard renders it as a
"failed: <reason>" pill, so dedup skips do NOT belong here). `skip_reasons`
captures the per-skip breakdown (duplicate_thread_pre_post,
empty_reply_text, rate_limited, tweet_not_found, reply_box_not_found,
no_reply_url_captured) without misclassifying them as failures.

Usage:
    python3 twitter_post_plan.py --plan /tmp/twitter_cycle_plan_<batch>.json
"""

from __future__ import annotations  # PEP 604 unions (int | None) for Python 3.9 launchd

import argparse
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _neuter_stream(stream) -> None:
    """Point a dead pipe's fd at /dev/null so later writes (including the
    interpreter-shutdown flush) can't raise BrokenPipeError again."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, stream.fileno())
        os.close(devnull)
    except Exception:
        pass


_builtin_print = print


def print(*args, **kwargs):  # noqa: A001 -- deliberate builtins.print shadow
    # When the parent (cycle shell tree or MCP server) is killed mid-batch, our
    # stdout/stderr pipes close and the next print raises BrokenPipeError. The
    # old behavior unwound through excepthook (Sentry S4L-8), killing the batch
    # BETWEEN posting a reply and recording it — which is how live tweets ended
    # up unlogged and candidates re-feedable. Output to a dead parent is
    # worthless; the bookkeeping (log_post, update_candidate, audit JSONL) is
    # not. Swallow the error, neuter the fds, keep going.
    try:
        _builtin_print(*args, **kwargs)
    except BrokenPipeError:
        _neuter_stream(sys.stdout)
        _neuter_stream(sys.stderr)


# Graceful SIGTERM: the MCP post_drafts timeout (and anything else that asks
# nicely before SIGKILL) sends SIGTERM. Dying instantly reopens the same
# posted-but-unrecorded window as the broken pipe, so instead flag the loop to
# stop at the next candidate boundary: current candidate finishes its
# bookkeeping, the summary and audit line still get written, exit stays 0.
_terminate_requested = False


def _on_sigterm(signum, frame):
    global _terminate_requested
    _terminate_requested = True
    print("[post] SIGTERM received; stopping after current candidate", flush=True)


signal.signal(signal.SIGTERM, _on_sigterm)

# This pipeline ONLY posts (never scans), so mark every twitter_browser.py reply
# subprocess it spawns as the high-priority "post" lock role. run_subprocess
# inherits this process env, so the child twitter_browser.py reads S4L_LOCK_ROLE
# at import and will PREEMPT a live scan holding the browser lock instead of
# losing the 45s wait. Covers BOTH the MCP approve path and the cron post path,
# since both shell out to this script. Set before any child is spawned.
os.environ["S4L_LOCK_ROLE"] = "post"

REPO_DIR = os.path.expanduser("~/social-autoposter")
TWITTER_BROWSER = os.path.join(REPO_DIR, "scripts", "twitter_browser.py")
LOG_POST = os.path.join(REPO_DIR, "scripts", "log_post.py")
CAMPAIGN_BUMP = os.path.join(REPO_DIR, "scripts", "campaign_bump.py")
LINK_TAIL = os.path.join(REPO_DIR, "scripts", "link_tail.py")

# Interpreter every child subprocess (twitter_browser.py reply, log_post.py,
# campaign_bump.py, link_tail.py) must run under. The reply path is the only
# Playwright importer in the pipeline, so a bare "python3" here silently
# resolved to the user's system python (no Playwright) and every post died
# with no_reply_json (Karol, 2026-06-22). Honor the authoritative pin the rest
# of the runtime uses — S4L_PYTHON (set by the launchd plist) — then fall back
# to sys.executable (the interpreter THIS process already runs under, which the
# MCP's runPython resolves to the owned uv runtime). Never the literal
# "python3": that re-rolls the PATH dice. Re-exported so grandchildren inherit.
PYTHON = os.environ.get("S4L_PYTHON") or sys.executable
os.environ["S4L_PYTHON"] = PYTHON

# DATABASE_URL was previously used to issue ad-hoc `psql -c "..."` calls for
# the pre-post dedup probe and the candidate status updates. As of the
# 2026-05-18 routes migration both lanes go through the s4l.ai HTTP API
# (/api/v1/posts/lookup + /api/v1/twitter-candidates/by-id) via http_api, so
# we no longer need the raw connection string at this layer. Kept around as
# a no-op constant in case downstream tooling reads it from the environment.
sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))
from http_api import api_get, api_patch, api_post  # noqa: E402
try:
    from account_resolver import resolve as _resolve_account  # noqa: E402
except Exception:
    def _resolve_account(_platform):  # type: ignore[unused-arg]
        return None

# Engagement-style enforcement (2026-05-22 cutover): the Twitter post path
# now calls validate_or_register exactly like Reddit/GitHub/Moltbook so
# (a) USE-mode drift gets coerced back to the picker's assigned style and
# (b) INVENT-mode inventions land in engagement_styles_registry via the
# /api/v1/engagement-styles/registry POST. The picker assignment is read
# from the plan envelope (run-twitter-cycle.sh writes assigned_style +
# assigned_mode into the same JSON file that already carries session_id).
# The model's optional new_style block per candidate is read from the
# candidate dict itself. Soft import so the post path still runs if the
# module is unavailable for some reason (we fall back to the raw
# engagement_style string from the model).
try:
    from engagement_styles import validate_or_register  # noqa: E402
except Exception:
    validate_or_register = None  # type: ignore[assignment]

# Reasons that signal an OPERATIONAL post failure (browser/session/API broke),
# as opposed to a content-judgment skip ("off-topic ..."). Some of these are
# bucketed as `skipped` in the run summary (e.g. reply_box_not_found) so they do
# not pollute the dashboard "failed" pill, but for remote observability they ARE
# the signal that a user "approved but couldn't post" — so we capture them to
# Sentry regardless of which summary bucket they land in.
MACHINE_FAIL_REASONS = {
    "no_reply_json", "reply_failed", "timeout", "unknown", "exception",
    "log_post_no_id", "reply_box_not_found", "rate_limited", "tweet_not_found",
    "no_reply_url_captured", "empty_reply_text", "session_invalid",
}

REPLY_URL_RE = re.compile(r"^https?://(?:x\.com|twitter\.com)/[^/]+/status/\d+")
TOP_LEVEL_OBJ_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)

def parse_last_json_object(text):  # -> dict | None; bare hint kept off the signature for Python 3.9 compatibility (PEP 604 union requires 3.10+)
    """Extract the last balanced top-level JSON object from a string.

    twitter_browser.py prints log lines to stderr and one JSON object to
    stdout via json.dumps(indent=2); but capture_output=True merges nothing
    by default. We still scan defensively for the last `{...}` block in case
    the caller passes combined output.
    """
    text = text.strip()
    if not text:
        return None
    # Fast path: single object.
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            pass
    # Fallback: find all top-level balanced objects.
    matches = []
    depth = 0
    start = None
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


def run_subprocess(cmd: list[str], timeout_sec: int = 600) -> tuple[int, str, str]:
    """Run a subprocess; return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        return (r.returncode, r.stdout or "", r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return (-1, e.stdout or "", f"TIMEOUT after {timeout_sec}s")


def update_candidate(cid: int, status: str, reason: str | None = None) -> None:
    """Flip candidate status (skipped/posted/expired) via the HTTP API.

    Server-side WHERE: `status != 'posted'` so we never stomp the posted
    state — mirrors the old psql guard exactly. The route returns 404 when
    the row IS already posted (or absent); we treat that as success here
    since the caller's intent ("don't retry this row") is already met.

    IMPORTANT: the DB CHECK constraint twitter_candidates_status_check only
    allows pending/posted/skipped/expired. There is NO 'failed' status — a
    reply that fails (timeout, exception, missing reply_url, lost log row)
    is recorded as 'skipped' with a descriptive skip_reason so the row is
    not retried (re-trying a landed reply double-posts on x.com). The
    run-summary 'failed' count is derived from each post_one() return value,
    NOT from the DB status, so the dashboard signal is unaffected. Writing
    'failed' here used to 500 against the check constraint on every failure.

    When status='skipped' and a reason is given, route through the
    mark_skipped action so skip_reason + skipped_at are stamped; otherwise
    use the generic set_status override.
    """
    if status == "posted":
        # Caller will set post_id separately on success path; here we just
        # mark intermediate states.
        return
    try:
        if status == "skipped" and reason:
            payload = {
                "id": int(cid),
                "action": "mark_skipped",
                "reason": str(reason)[:500],
            }
        else:
            payload = {"id": int(cid), "action": "set_status", "status": status}
        resp = api_patch(
            "/api/v1/twitter-candidates/by-id",
            payload,
            ok_on_404=True,
        )
        if resp.get("_not_found"):
            # Either row was already posted (allow_overwrite_posted=false
            # default blocks it / mark_skipped only touches pending) or it
            # doesn't exist. Either way, no further action is needed.
            return
    except SystemExit as e:
        print(f"[post] candidate {cid} status update failed: {e}", flush=True)


def already_posted_to_thread(thread_url: str) -> tuple[bool, int | None]:
    """Pre-post dedup race guard.

    Returns (True, post_id) if posts already has a row for
    (platform='twitter', thread_url=<thread_url>), else (False, None).

    Why this exists: cycles overlap. Phase 0 of cycle B can salvage a
    candidate while cycle A is still in its T1 wait window — cycle A
    hasn't INSERTed into posts yet, so salvage's
    `tweet_url NOT IN (SELECT thread_url FROM posts)` guard lets the
    same row through. Both cycles then call reply_to_tweet, the second
    one gets DUPLICATE_THREAD from log_post.py only AFTER the second
    reply is already on X. Real double-post observed 2026-05-01:
    posts #22317 (cycle 14:23, our_url ...4034) AND a second reply
    ...8891 (cycle 14:38, never logged).

    This SELECT runs ~26s after the peer cycle's INSERT in the observed
    race, so it would have caught the duplicate. It does not eliminate
    the race entirely — two cycles SELECTing in the same ms would both
    pass — but advisory-lock-grade atomicity is overkill for an event
    that fires once per cycle. log_post.py's post-INSERT dedup is still
    the final backstop.
    """
    # Scope MUST match the server-side insert dedup, which is keyed on
    # (platform, thread_url) ONLY -- NOT our_account (see social-autoposter-
    # website /api/v1/posts route: "Enforces dedup on (platform, thread_url)").
    # The old per-account scoping here made the probe NARROWER than the server:
    # it passed when a post existed under a different/placeholder our_account,
    # so the cycle posted a SECOND reply to a thread the server then rejected
    # with duplicate_thread -- after the reply was already live on X. Querying
    # thread-only makes the pre-post guard catch exactly what the insert would
    # reject, so we never burn that wasted second reply. (2026-06-02)
    dedupe_q = {"platform": "twitter", "thread_url": thread_url}
    try:
        resp = api_get(
            "/api/v1/posts/lookup",
            query=dedupe_q,
            ok_on_404=True,
        )
    except SystemExit as e:
        print(f"[post] dedup pre-check API call failed: {e}", flush=True)
        return (False, None)
    if resp.get("_not_found"):
        return (False, None)
    data = resp.get("data") or {}
    post = data.get("post") or {}
    pid = post.get("id")
    if pid is None:
        return (False, None)
    try:
        return (True, int(pid))
    except (TypeError, ValueError):
        return (True, None)


def fetch_thread_engagement_snapshot(cid: int) -> str | None:
    """Fetch the T0 engagement snapshot the discovery pipeline recorded for
    this candidate, serialised as a compact JSON string ready for the
    posts.thread_engagement TEXT column.

    Reads from /api/v1/twitter-candidates/by-id?id=<cid>, which returns the
    *_t0 columns score_twitter_candidates.py stamps at scrape time. No live
    refresh, no fxtwitter call: this is the snapshot Twitter showed when the
    candidate was first discovered.

    Returns:
      - JSON string like '{"likes":42,"retweets":3,"replies":12,"views":8100,"bookmarks":1,"source":"discovery_t0"}'
        when at least one engagement field was present on the candidate row.
      - None when the row is missing or every engagement field is NULL (no
        signal worth storing; column stays NULL on posts).

    Failure mode: any error logs a warning and returns None. We never block
    the post on this; missing one row of snapshot data is preferable to
    losing the post.
    """
    try:
        resp = api_get(
            "/api/v1/twitter-candidates/by-id",
            query={"id": int(cid)},
            ok_on_404=True,
        )
    except SystemExit as e:
        print(f"[post] candidate {cid} thread_engagement fetch failed: {e}", flush=True)
        return None
    if resp.get("_not_found"):
        return None
    data = resp.get("data") or {}
    cand = data.get("candidate") or {}
    if not cand:
        return None

    def _pick(t0_key: str, live_key: str):
        # Prefer the T0 snapshot (captured at discovery, the user's explicit
        # requirement: scrape-time engagement, not live). Fall back to the
        # live column only when T0 is missing AND live is present, which
        # happens on very old candidate rows that pre-date the T0 backfill.
        v0 = cand.get(t0_key)
        if v0 is not None:
            return v0
        return cand.get(live_key)

    snap = {
        "likes": _pick("likes_t0", "likes"),
        "retweets": _pick("retweets_t0", "retweets"),
        "replies": _pick("replies_t0", "replies"),
        "views": _pick("views_t0", "views"),
        "bookmarks": _pick("bookmarks_t0", "bookmarks"),
    }
    # Skip when every field is NULL/missing — nothing worth recording.
    if not any(v is not None for v in snap.values()):
        return None
    snap["source"] = "discovery_t0"
    discovered = cand.get("discovered_at")
    if discovered:
        snap["snapshot_at"] = str(discovered)
    return json.dumps(snap, separators=(",", ":"))


def fetch_thread_media_snapshot(cid: int) -> str | None:
    """Fetch the candidate's thread_media (captured in Phase 2b-prep by
    capture_thread_media.py) as a compact JSON-array string, ready for the
    posts.thread_media JSONB column (2026-06-03 thread-media feature).

    Reads from /api/v1/twitter-candidates/by-id?id=<cid>, the same endpoint the
    engagement snapshot uses. No browser, no live refresh: whatever media the
    prep step persisted onto the candidate row is what gets frozen onto the
    post as an immutable audit record of what the thread visually showed.

    Returns:
      - A JSON array string (e.g. '[{"url":"...","alt":"...","type":"image"}]')
        when the candidate has a non-empty thread_media array.
      - '[]' when media was captured but the thread had none (captured-none is
        meaningful and worth recording, distinct from never-captured).
      - None when the row is missing, thread_media is NULL (never captured), or
        any error occurs. We never block the post on this.
    """
    try:
        resp = api_get(
            "/api/v1/twitter-candidates/by-id",
            query={"id": int(cid)},
            ok_on_404=True,
        )
    except SystemExit as e:
        print(f"[post] candidate {cid} thread_media fetch failed: {e}", flush=True)
        return None
    if resp.get("_not_found"):
        return None
    cand = (resp.get("data") or {}).get("candidate") or {}
    media = cand.get("thread_media")
    # NULL on the row = never captured (capture disabled or pre-feature row):
    # leave posts.thread_media NULL too. An empty list = captured-none: record it.
    if media is None:
        return None
    if not isinstance(media, list):
        return None
    return json.dumps(media, separators=(",", ":"))


def update_candidate_posted(cid: int, post_id: int,
                            matched_project=None, search_topic=None) -> None:
    """Mark the candidate posted via /api/v1/twitter-candidates/by-id.

    Re-stamps batch_id to the executing cycle's BATCH_ID alongside the
    status='posted' flip. Belt-and-suspenders against peer-cycle Phase 0
    salvage races: salvage can rewrite our candidate's batch_id while we are
    mid-Phase-2b (observed 2026-05-15 with twcycle-20260515-171505's 6 posts
    mis-attributed to twcycle-20260515-180005 after the latter salvaged them
    while 171505 was queued behind 173005's 42-min Phase 1 lock-hold).
    When BATCH_ID env is unset (manual replays, ad-hoc runs), fall back to
    leaving batch_id alone so we never NULL-out a live attribution.

    Cross-route writeback (2026-05-29): the Phase 2b prep step can re-route a
    candidate to a better-fitting project than the Phase 1 query that surfaced
    it. matched_project carries the project the post actually landed on; it is
    sent on EVERY post (not just re-routes) so twitter_candidates.matched_project
    always equals posts.project_name. search_topic is the plan's topic, which is
    "" on a re-route (the by-id route clears "" to NULL, because the origin
    query's topic does not belong to the routed project). Both are honoured by
    the by-id route as of 2026-05-29; older deploys ignore unknown body fields
    harmlessly, so this is safe to ship ahead of the route.
    """
    body = {
        "id": int(cid),
        "action": "mark_posted",
        "post_id": int(post_id),
    }
    batch_id = (os.environ.get("BATCH_ID") or "").strip()
    if batch_id:
        body["batch_id"] = batch_id
    if matched_project:
        body["matched_project"] = matched_project
    # Send even when empty: "" tells the route to CLEAR search_topic to NULL on
    # a re-route. Only omit when the caller passed nothing at all (None).
    if search_topic is not None:
        body["search_topic"] = search_topic
    try:
        api_patch("/api/v1/twitter-candidates/by-id", body)
    except SystemExit as e:
        print(f"[post] candidate {cid} -> posted update failed: {e}", flush=True)


def post_one(c: dict, picker_assignment: dict | None = None) -> tuple[str, str]:
    """Post a single candidate. Returns (outcome, reason).

    outcome: 'posted' | 'skipped' | 'failed'
    reason:  short failure key when outcome != 'posted', else ''.

    picker_assignment: optional {assigned_style, assigned_mode} dict for
        THIS candidate (per-candidate override when the plan carries one,
        else the plan-level fallback, see call site in main()). When
        present, drives the validate_or_register call below so USE-mode
        drift coerces back and INVENT-mode new_style blocks land in
        engagement_styles_registry. None means legacy behaviour
        (uncoerced; whatever the model said is what gets logged).
    """
    cid = int(c["candidate_id"])
    candidate_url = c["candidate_url"]
    reply_text = (c.get("reply_text") or "").strip()
    link_url = (c.get("link_url") or "").strip()
    project = c["matched_project"]
    thread_author = c.get("thread_author") or ""
    thread_text = c.get("thread_text") or ""
    # Engagement-style enforcement (2026-05-22 cutover). Twitter is now
    # symmetric with Reddit/GitHub/Moltbook: the draft phase pre-picks an
    # assignment via s4l_pick_style; the post phase calls
    # validate_or_register(decision, assigned_style=..., assigned_mode=...)
    # which coerces USE drift back to the assigned name OR registers
    # INVENT inventions into engagement_styles_registry via
    # POST /api/v1/engagement-styles/registry. The picker assignment flows
    # in via the plan envelope (picker_assignment param); the model's
    # optional new_style block flows in via the candidate dict itself.
    raw_style = (c.get("engagement_style") or "").strip()
    new_style_block = c.get("new_style") if isinstance(c.get("new_style"), dict) else None
    if validate_or_register is not None and raw_style:
        assigned_style = (picker_assignment or {}).get("assigned_style") or None
        assigned_mode = (picker_assignment or {}).get("assigned_mode") or None
        decision = {
            "engagement_style": raw_style,
            # Only attach new_style when the model actually shipped one;
            # validate_or_register treats None as "no new_style block"
            # and never registers anything in that case.
            **({"new_style": new_style_block} if new_style_block else {}),
        }
        try:
            coerced_style, action = validate_or_register(
                decision,
                source_post={
                    "platform": "twitter",
                    "post_url": candidate_url,
                    "post_id": None,
                    "model": None,
                },
                assigned_style=assigned_style,
                assigned_mode=assigned_mode,
            )
        except Exception as e:
            # Never let a registry/API hiccup block posting. Fall back to
            # the raw model output; the post still lands, just without
            # picker coercion for this one row.
            print(f"[post] candidate {cid}: validate_or_register raised {e!r}; "
                  f"falling back to raw style={raw_style!r}", flush=True)
            coerced_style, action = raw_style, "rejected"
        if action == "coerced" and coerced_style != raw_style:
            print(f"[post] candidate {cid}: engagement_style coerced "
                  f"{raw_style!r} -> {coerced_style!r} (assigned={assigned_style!r})",
                  flush=True)
        elif action == "registered":
            print(f"[post] candidate {cid}: registered new engagement_style "
                  f"{coerced_style!r} into engagement_styles_registry",
                  flush=True)
        style = (coerced_style or raw_style or "").strip()
    else:
        style = raw_style
    # target_chars SNAPSHOT: freeze the assigned style's target comment length
    # onto this post so style_length_report can compare realized-vs-target
    # without being fooled by later registry drift (the human_derived
    # synthesizer retunes targets daily). Resolve from the FINAL coerced style
    # name via the registry; fall back to DEFAULT_TARGET_CHARS, then to None
    # (column is nullable; the report falls back to the live target for NULL).
    target_chars = None
    if style:
        try:
            from engagement_styles import get_all_styles, DEFAULT_TARGET_CHARS
            meta = get_all_styles().get(style) or {}
            target_chars = meta.get("target_chars") or DEFAULT_TARGET_CHARS
        except Exception as e:
            print(f"[post] candidate {cid}: target_chars lookup failed ({e}); "
                  f"leaving NULL", flush=True)
            target_chars = None
    language = (c.get("language") or "").strip()
    link_source = (c.get("link_source") or "").strip()
    # search_topic flows from twitter_candidates -> Phase 2b prompt
    # ("Search query: <topic>") -> prep envelope -> here. Stamped on
    # posts.search_topic so top_search_topics.py can aggregate per-topic
    # conversion (clicks / likes / views) and feed the next cycle's Phase 1
    # which topics to favour or drop. Reddit/GitHub already populate this;
    # Twitter was a coverage gap (0/3,280 rows) until the 2026-05-25 wiring.
    search_topic = (c.get("search_topic") or "").strip()

    if not reply_text:
        print(f"[post] candidate {cid}: empty reply_text; skipping", flush=True)
        update_candidate(cid, "skipped")
        return ("skipped", "empty_reply_text")

    # Pre-post dedup race guard. See already_posted_to_thread() docstring
    # for the full failure mode this closes (overlapping cycles double-
    # posting because Phase 0 salvage runs before the peer cycle has
    # INSERTed into posts). Skip without calling reply_to_tweet so we
    # don't burn a second reply tweet on a thread we've already engaged.
    pre_dup, pre_dup_pid = already_posted_to_thread(candidate_url)
    if pre_dup:
        print(
            f"[post] candidate {cid}: pre-post dedup hit "
            f"(existing post_id={pre_dup_pid}, thread={candidate_url}); "
            f"skipping reply call",
            flush=True,
        )
        update_candidate(cid, "skipped")
        return ("skipped", "duplicate_thread_pre_post")

    # CTA bridge generation. 2026-07-06: moved from HERE (post time) to
    # twitter_gen_links.py's Phase 2b-gen step (draft time). Phase 2b-gen runs
    # BEFORE the DRAFT_ONLY gate for every candidate, so both the review-card
    # path and the autonomous-post path already carry a stamped
    # tail_link_variant + finalized reply_text by the time they reach here.
    # Post time is a bad fit for the queue-backed Claude call: post_drafts is
    # a synchronous MCP call the user is actively waiting on after clicking
    # Approve, while the s4l-worker scheduled task claims one job per minute
    # and doesn't overlap a multi-minute drafting turn, so a queue-routed call
    # here could stall an approval for minutes. This block is now a FALLBACK
    # for a candidate that somehow reaches post time unstamped (e.g. a plan
    # already in flight from before this change).
    tail_link_variant = c.get("tail_link_variant")
    full_text = reply_text
    if tail_link_variant is not None:
        link_tail_outcome = c.get("link_tail_outcome") or "applied_at_draft_time"
        print(f"[post] candidate {cid} link_tail: {link_tail_outcome} "
              f"(already finalized at draft time, tail_link_variant={tail_link_variant})",
              flush=True)
    else:
        # AB TEST — tail link on/off:
        # TWITTER_TAIL_LINK_RATE (float 0..1, default 0.5) controls the fraction
        # of posts that receive a tail link. Setting it to 1.0 restores old
        # behavior (always add link). Setting it to 0.0 disables links entirely.
        # tail_link_variant is logged to posts.tail_link_variant so the dashboard
        # can compare engagement across arms.
        _tail_link_rate = float(os.environ.get("TWITTER_TAIL_LINK_RATE", "0.5"))
        _add_tail_link = link_url and (random.random() < _tail_link_rate)
        if link_url:
            tail_link_variant = "link" if _add_tail_link else "no_link"
        link_tail_outcome = "skipped_no_link"
        if _add_tail_link:
            rc, out, err = run_subprocess(
                [PYTHON, LINK_TAIL,
                 "--reply-text", reply_text,
                 "--link-url", link_url,
                 "--thread-text", thread_text or "",
                 "--project", project,
                 "--platform", "twitter",
                 "--timeout", "120"],
                timeout_sec=180,
            )
            tail_obj = parse_last_json_object(out) or {}
            if tail_obj.get("text"):
                full_text = tail_obj["text"]
                if tail_obj.get("model_call_ok") and not tail_obj.get("fallback_used"):
                    link_tail_outcome = "bridge_generated"
                else:
                    link_tail_outcome = f"fallback:{tail_obj.get('error', 'unknown')[:60]}"
            else:
                # link_tail.py is supposed to ALWAYS return JSON; if we got
                # nothing, hard-fall-back to the mechanical concat to preserve
                # prior behavior (post still ships, link still on the wire).
                full_text = f"{reply_text} {link_url}".strip()
                link_tail_outcome = f"hard_fallback_no_json:rc={rc}"
            print(f"[post] candidate {cid} link_tail: {link_tail_outcome} "
                  f"(elapsed={tail_obj.get('elapsed_sec')}s)", flush=True)
        elif link_url and not _add_tail_link:
            # No-link arm of the AB test: post the reply text as-is (no CTA bridge,
            # no URL). Log the outcome so the dashboard can tally the arm.
            link_tail_outcome = "ab_no_link"
            print(f"[post] candidate {cid} link_tail: {link_tail_outcome} "
                  f"(tail_link_variant=no_link, rate={_tail_link_rate})", flush=True)

    # URL-wrap the text BEFORE handing it to twitter_browser. The browser
    # script appends the campaign suffix internally; suffixes are plain
    # text in practice, so URLs in the suffix won't be wrapped (documented
    # caveat). All URLs in reply_text + link_url get minted into post_links
    # with NULL post_id; we backfill with post_id below after log_post.py
    # returns.
    minted_session = None
    try:
        from dm_short_links import wrap_text_for_post, utm_only_text
        wrap_res = wrap_text_for_post(text=full_text, platform="twitter",
                                        project_name=project)
        if wrap_res.get("ok"):
            full_text = wrap_res["text"]
            minted_session = wrap_res.get("minted_session")
            if wrap_res.get("codes"):
                print(f"[post] candidate {cid} wrapped {len(wrap_res['codes'])} URL(s): "
                      f"{wrap_res['codes']}", flush=True)
        else:
            print(f"[post] candidate {cid} WARNING: URL wrap failed "
                  f"({wrap_res.get('error')}); falling back to UTM-only", flush=True)
            full_text = utm_only_text(text=full_text, platform="twitter", project_name=project)
    except Exception as e:
        print(f"[post] candidate {cid} WARNING: URL wrap raised ({e}); "
              f"falling back to UTM-only", flush=True)
        try:
            from dm_short_links import utm_only_text
            full_text = utm_only_text(text=full_text, platform="twitter", project_name=project)
        except Exception as ee:
            print(f"[post] candidate {cid} WARNING: UTM-only fallback also failed ({ee}); "
                  f"posting unwrapped", flush=True)

    print(f"[post] candidate {cid} -> posting (link={link_url!r})", flush=True)
    rc, out, err = run_subprocess(
        [PYTHON, TWITTER_BROWSER, "reply", candidate_url, full_text],
        timeout_sec=600,
    )
    if err:
        # Surface stderr verbatim for the cycle log; reply_to_tweet logs to
        # stderr extensively so this is intentional debugging context.
        print(f"[post][reply.stderr]\n{err}", flush=True)
    if out:
        print(f"[post][reply.stdout]\n{out}", flush=True)

    parsed = parse_last_json_object(out) or {}
    if not parsed.get("ok"):
        reason = parsed.get("error") or "no_reply_json"
        print(f"[post] candidate {cid} reply failed: {reason}", flush=True)
        if reason in ("rate_limited", "tweet_not_found", "reply_box_not_found",
                      "reply_restricted", "tweet_unavailable", "blocked_by_author"):
            # reply_restricted / tweet_unavailable / blocked_by_author are
            # PERMANENT, thread-intrinsic conditions (the author limits who can
            # reply, the tweet is gone, or the author has blocked our account):
            # record the specific skip_reason so discovery can suppress the thread
            # (and, for restrictions/blocks, the author) and never burn another
            # draft re-attempting it.
            update_candidate(cid, "skipped", reason)
            return ("skipped", reason)
        # everything else (incl. timeout, parse errors): the reply did NOT
        # land, so mark skipped (NOT a DB 'failed' status — that violates the
        # check constraint) with the reason, but report 'failed' to the run
        # summary so the dashboard reflects the real failure.
        update_candidate(cid, "skipped", reason if reason else "reply_failed")
        return ("failed", reason if reason else "unknown")

    reply_url = parsed.get("reply_url") or ""
    final_text = parsed.get("final_text") or full_text
    # Edited-tweet redirect (2026-07-06): reply_to_tweet may have followed the
    # "See the latest post" banner and replied to a NEWER status id than the
    # candidate's. Its payload carries the URL it actually replied to; adopt it
    # so posts.thread_url, the top-replies snapshot, and the
    # already_posted_to_thread dedup guard all key to the REAL thread instead
    # of the stale pre-edit permalink.
    _replied_url = parsed.get("tweet_url") or ""
    if _replied_url and _replied_url != candidate_url:
        print(f"[post] candidate {cid} reply landed on edited-tweet latest "
              f"version: {_replied_url} (candidate had {candidate_url})",
              flush=True)
        candidate_url = _replied_url
    applied_campaigns = parsed.get("applied_campaigns") or []
    # Snapshot the top human replies on the thread at post-success time.
    # twitter_browser.reply_to_tweet scrapes them while the page is still on
    # the candidate URL with replies visible. List is already filtered (self
    # + thread author removed), sorted by likes DESC, capped at 3.
    top_replies = parsed.get("top_replies") or []

    # Auto-like outcome (reply_to_tweet likes the parent tweet after the reply
    # lands). Log pass/fail to the cycle log so we have a record on our end.
    # A like failure is non-fatal: the reply already landed.
    like_result = parsed.get("like_result") or {}
    if parsed.get("liked"):
        print(
            f"[like] candidate {cid} parent tweet liked "
            f"(already_liked={like_result.get('already_liked', False)})",
            flush=True,
        )
    else:
        err = str(like_result.get("error", "unknown")).splitlines()[0]
        print(f"[like] candidate {cid} parent tweet not liked (non-fatal): {err}", flush=True)

    if not reply_url or not REPLY_URL_RE.match(reply_url):
        # Reply was likely sent (browser action returned ok=True with verified)
        # but the URL capture in twitter_browser.py couldn't pin it down — CDP
        # network interception missed the CreateTweet response and the DOM diff
        # found no new /m13v_/status link. Method 3 (profile-page scrape) was
        # removed 2026-05-01 because it cross-contaminated under parallel
        # cycles. Mark SKIPPED, not FAILED, so the candidate is NOT re-tried
        # next cycle — re-trying when the prior reply already landed creates
        # a duplicate on Twitter. Salvage's posts.thread_url guard would catch
        # it eventually but only after the candidate sat through one more
        # cycle of wasted Claude work.
        print(f"[post] candidate {cid} reply succeeded but reply_url invalid: {reply_url!r}",
              flush=True)
        update_candidate(cid, "skipped", "no_reply_url_captured")
        return ("skipped", "no_reply_url_captured")

    # Stash the live URL on the candidate NOW, before the posts-row INSERT:
    # main() needs it for candidate_results and the durable audit line even
    # when log_post.py fails below (the reply is already live on X).
    c["our_url"] = reply_url

    # Insert the post row.
    # Pass --account explicitly so log_post.py stamps posts.our_account with
    # this machine's configured Twitter handle (e.g. `m13v_` on the local
    # cron, `matt_diak` on the VM). Without this, log_post.py falls back
    # through twitter_account.resolve_handle() to the same value, but
    # forwarding it here makes the per-machine identity visible in the
    # subprocess argv (useful for grep'ing run logs to confirm scoping).
    sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))
    from twitter_account import resolve_handle as _resolve_twitter_handle

    log_args = [
        PYTHON, LOG_POST,
        "--platform", "twitter",
        "--thread-url", candidate_url,
        "--our-url", reply_url,
        "--our-content", final_text,
        "--project", project,
        "--thread-author", thread_author,
        "--thread-title", thread_text,
    ]
    twitter_handle = _resolve_twitter_handle()
    if twitter_handle:
        log_args += ["--account", twitter_handle]
    if style:
        log_args += ["--engagement-style", style]
    if target_chars:
        log_args += ["--target-chars", str(target_chars)]
    if language:
        log_args += ["--language", language]
    if link_source:
        log_args += ["--link-source", link_source]
    if search_topic:
        log_args += ["--search-topic", search_topic]
    if tail_link_variant:
        log_args += ["--tail-link-variant", tail_link_variant]
    # Draft-prompt A/B arm: assigned ONCE per cycle in run-twitter-cycle.sh and
    # persisted onto the plan candidate's `experiments` dict at plan-write time
    # (scripts/active_experiments.py). That stamp is the ONLY source here — env
    # is deliberately NOT consulted (2026-07-07): this poster runs in two homes,
    # inside the cycle (autopilot) and spawned by the MCP server for approved
    # cards (no cycle env), and reading env made the queue-review lane stamp
    # NULL while autopilot stamped the arm, silently dropping the human-review
    # lane from the experiment readout.
    draft_prompt_variant = (c.get("experiments") or {}).get("draft_prompt")
    if draft_prompt_variant:
        log_args += ["--draft-prompt-variant", draft_prompt_variant]
    # LENGTH A/B concluded 2026-06-04; future production posts are no longer
    # stamped into posts.length_arm so the archived experiment readout stays
    # frozen to the actual test window.
    # Generation trace: run-twitter-cycle.sh writes a snapshot of the
    # cycle's few-shot context (top_performers, top_queries, supply
    # signal, dud queries) to a tempfile and exports the path via
    # S4L_TWITTER_GEN_TRACE_PATH. Forward to log_post.py so every
    # post landed this cycle gets posts.generation_trace JSONB pointing
    # to the same snapshot. Same trace for every post in this run
    # because they all saw the same Phase 2b-prep context. The env var
    # is missing/empty when run-twitter-cycle.sh's trace step failed —
    # in that case we just skip the flag and the row gets NULL trace.
    trace_path = os.environ.get("S4L_TWITTER_GEN_TRACE_PATH") or ""
    if trace_path and os.path.isfile(trace_path):
        log_args += ["--generation-trace", trace_path]

    # T0 engagement of the original thread (captured at discovery, NOT live).
    # Read from twitter_candidates via the by-id GET endpoint. No fxtwitter
    # call, no extra page-load: whatever score_twitter_candidates.py stamped
    # into *_t0 at scrape time is what we record. Stored as a JSON string
    # in posts.thread_engagement (TEXT). Silently skip on any failure;
    # losing one snapshot row is preferable to losing the post.
    thread_engagement_json = fetch_thread_engagement_snapshot(cid)
    if thread_engagement_json:
        log_args += ["--thread-engagement", thread_engagement_json]
        print(f"[post] candidate {cid} thread_engagement snapshot: "
              f"{thread_engagement_json}", flush=True)
    else:
        print(f"[post] candidate {cid} thread_engagement snapshot: none "
              f"(no T0 data on candidate row)", flush=True)

    # Thread media snapshot (2026-06-03): freeze the candidate's captured media
    # onto posts.thread_media. Reads thread_media off the candidate row (set in
    # Phase 2b-prep by capture_thread_media.py). None when capture was disabled
    # or the row pre-dates the feature; '[]' when the thread genuinely had none.
    thread_media_json = fetch_thread_media_snapshot(cid)
    if thread_media_json is not None:
        log_args += ["--thread-media", thread_media_json]
        print(f"[post] candidate {cid} thread_media snapshot: "
              f"{thread_media_json}", flush=True)

    rc, out, err = run_subprocess(log_args, timeout_sec=60)
    if err:
        print(f"[post][log_post.stderr]\n{err}", flush=True)
    if out:
        print(f"[post][log_post.stdout]\n{out}", flush=True)
    log_obj = parse_last_json_object(out) or {}
    post_id = log_obj.get("post_id")
    if not post_id:
        print(f"[post] candidate {cid} log_post.py did not return post_id; raw={out!r}",
              flush=True)
        # The reply IS posted; the data layer just lost the row. We MUST keep
        # the candidate's DB status as 'skipped' so it isn't retried (which
        # would double-post on x.com). But the run-summary outcome should be
        # 'failed' so the dashboard reflects reality: posted=0, failed=N.
        # Previously this returned 'skipped', which silently hid backend
        # logging outages (e.g. the /api/v1/posts 5000/24h rate-limit cap)
        # behind a benign-looking metric.
        update_candidate(cid, "skipped", "log_post_no_id")
        # 2026-07-06 incident hardening: the server-side 'skipped' flip alone
        # did NOT stop retries — the menu-bar card lane re-fed the batch
        # because nothing told it the reply was live (8 approved cards were
        # re-posted into X's duplicate guard after a posts-API 400).
        # Three belts now:
        #   1. Stash the exact log_post.py argv so the posts row can be
        #      replayed once the API accepts it (restores the
        #      already_posted_to_thread dedup guard).
        #   2. Print the "posted as" line the mcp outcome parsers (old and
        #      new) recognize, so the CARD is marked posted and never re-fed.
        #   3. Return the posted_unlogged sentinel: main() still counts it in
        #      `failed` (the dashboard must surface the logging outage) but
        #      reports the card as posted in candidate_results.
        _stash_pending_post_log(cid, reply_url, log_args, out, err)
        print(f"[post] candidate {cid} posted as {reply_url} "
              f"(post_id=unlogged; posts-row backfill pending)", flush=True)
        return ("posted_unlogged", "log_post_no_id")

    # Stamp post_links.post_id for the URLs minted at wrap time. Idempotent;
    # no-op when minted_session is None (no URLs in the original text).
    if minted_session:
        try:
            from dm_short_links import backfill_post_id
            backfill_post_id(minted_session=minted_session, post_id=post_id)
        except Exception as e:
            print(f"[post] candidate {cid} WARNING: backfill_post_id failed ({e})",
                  flush=True)

    # Campaign attribution.
    for ccid in applied_campaigns:
        rc, out, err = run_subprocess(
            [PYTHON, CAMPAIGN_BUMP, "--table", "posts",
             "--id", str(post_id), "--campaign-id", str(ccid)],
            timeout_sec=30,
        )
        if err:
            print(f"[post][campaign_bump.stderr] cid={ccid} {err}", flush=True)
        if out:
            print(f"[post][campaign_bump.stdout] cid={ccid} {out}", flush=True)

    # Mark link_edited_at: link is embedded in primary reply, no self-reply
    # will follow. Prevents link-edit-twitter sweep from re-attempting.
    rc, out, err = run_subprocess(
        [PYTHON, LOG_POST,
         "--mark-self-reply",
         "--post-id", str(post_id),
         "--self-reply-url", reply_url,
         "--self-reply-content", final_text],
        timeout_sec=30,
    )
    if err:
        print(f"[post][mark-self-reply.stderr] {err}", flush=True)
    if out:
        print(f"[post][mark-self-reply.stdout] {out}", flush=True)

    update_candidate_posted(cid, post_id,
                            matched_project=project, search_topic=search_topic)
    print(f"[post] candidate {cid} posted as {reply_url} (post_id={post_id})",
          flush=True)
    # (our_url was already stashed on the candidate right after reply_url
    # validation, so the audit line has it even on log_post failure paths.)

    # Persist the human-top-replies snapshot via the s4l.ai routes. We POST
    # even when top_replies is empty so posts.top_replies_captured_at is
    # stamped and the "did we attempt capture?" gate doesn't keep retrying
    # threads that had genuinely zero competitor replies. Failure here is
    # non-fatal: the reply IS posted and logged; missing snapshot only loses
    # one row of benchmark data, not the run.
    try:
        ttr_payload = {
            "post_id": post_id,
            "platform": "twitter",
            "thread_url": candidate_url,
            "replies": [
                {
                    "rank": rank,
                    "reply_url": r.get("reply_url"),
                    "reply_tweet_id": r.get("reply_tweet_id"),
                    "reply_author": r.get("reply_author"),
                    "reply_author_handle": r.get("reply_author_handle"),
                    "reply_content": r.get("reply_content"),
                    "likes": r.get("likes"),
                    "replies": r.get("replies"),
                    "retweets": r.get("retweets"),
                    "views": r.get("views"),
                    # Link metadata (2026-05-22). reply_link_url is the t.co
                    # shortlink twitter wraps every external URL with;
                    # reply_link_display is what the user sees in the tweet
                    # (e.g. "deno.com/blog/agents"). Either may be null when
                    # the reply contains no outbound link (the typical case
                    # for rank=1; the typical NON-null case for rank=2).
                    "reply_link_url": r.get("reply_link_url"),
                    "reply_link_display": r.get("reply_link_display"),
                }
                for rank, r in enumerate(top_replies, start=1)
                if r.get("reply_url")
            ],
        }
        ttr_res = api_post("/api/v1/thread-top-replies", ttr_payload)
        print(f"[post] candidate {cid} thread_top_replies "
              f"inserted={ttr_res.get('inserted_count')} "
              f"requested={ttr_res.get('requested_count')}",
              flush=True)
    except Exception as e:
        print(f"[post] candidate {cid} WARNING: thread_top_replies POST failed ({e})",
              flush=True)

    return ("posted", "")


def _stash_pending_post_log(cid, reply_url, log_args, out, err) -> None:
    """The reply landed on X but the posts-API INSERT failed, so the posts row
    (and with it the already_posted_to_thread dedup guard + stats) is missing.
    Persist the exact log_post.py argv so the row can be replayed later —
    log_post.py is idempotent per thread (DUPLICATE_THREAD returns the existing
    post_id), so replaying is always safe. Best-effort: never affects the run.

    Replay: <runtime python> scripts/log_post.py <argv...> from the package dir.
    """
    try:
        path = os.path.join(REPO_DIR, "skill", "logs", "pending-post-logs.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(),
                "candidate_id": cid,
                "reply_url": reply_url,
                # Drop the interpreter; keep script path + flags.
                "log_post_argv": [str(a) for a in log_args[1:]],
                "stdout_tail": (out or "")[-500:],
                "stderr_tail": (err or "")[-500:],
            }) + "\n")
        print(f"[post] candidate {cid} pending posts-row logged to {path}",
              flush=True)
    except Exception as e:
        print(f"[post] candidate {cid} WARNING: pending-post-log stash failed "
              f"({e})", flush=True)


def _s4l_state_dir() -> str:
    return os.environ.get("S4L_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp")


def _write_activity(label: str) -> None:
    """Best-effort live status for the S4L menu bar, which polls
    <state_dir>/activity.json. Mirrors the Node server's writeActivity shape so
    the menu-bar spinner renders our per-post progress ("posting +3 · 4/10",
    then "posting +3 ✓ · 4/10"). Purely cosmetic: a failure here never affects
    posting."""
    try:
        sd = _s4l_state_dir()
        os.makedirs(sd, exist_ok=True)
        payload = {"state": "working", "label": label,
                   "since": datetime.now(timezone.utc).isoformat()}
        with open(os.path.join(sd, "activity.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass


def _clear_activity() -> None:
    """Remove our status so neither an early exit nor the cron path (which does
    NOT go through the MCP runTool's clear) leaves a stale 'posting/posted' stuck
    in the menu bar. Double-clearing with runTool is harmless."""
    try:
        p = os.path.join(_s4l_state_dir(), "activity.json")
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True,
                    help="Path to the plan JSON file (read-only here)")
    ap.add_argument("--post-unapproved", action="store_true",
                    help="Post candidates even when the plan marks them "
                         "approved=false. The MCP review path already filters to "
                         "approved-only, and autopilot/legacy plans omit the key; "
                         "this is the explicit override for an intentional direct run.")
    args = ap.parse_args()

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"[post] plan file not found: {plan_path}", file=sys.stderr)
        return 2
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[post] plan file unreadable: {e}", file=sys.stderr)
        return 2

    candidates = plan.get("candidates") or []

    # Re-export the prep session id into env so log_post.py stamps
    # posts.claude_session_id and the dashboard activity feed can join to
    # claude_sessions for cost. The parent shell pre-assigns this in Phase
    # 2b-prep and writes it into the plan JSON; the env var doesn't survive
    # the prep command-substitution subshell, so we restore it here.
    plan_session_id = plan.get("session_id")
    if plan_session_id:
        os.environ["CLAUDE_SESSION_ID"] = plan_session_id

    # Pull the picker assignment from the plan envelope (written by
    # run-twitter-cycle.sh after s4l_pick_style). This is the PLAN-LEVEL
    # fallback, used verbatim for legacy plans (pre-2026-05-22 envelopes, or
    # any candidate missing a per-candidate assignment) and as the last
    # resort in the per-candidate override below. Falls back to None on
    # legacy plans that don't carry these keys; post_one then runs the
    # legacy uncoerced path. Empty assigned_style + assigned_mode='invent'
    # means the picker rolled INVENT this cycle; validate_or_register treats
    # that as "register if the model produced a well-formed new_style block".
    picker_assignment = {
        "assigned_style": plan.get("assigned_style") or None,
        "assigned_mode":  plan.get("assigned_mode")  or None,
    }
    if picker_assignment["assigned_mode"]:
        print(f"[post] picker assignment for batch: "
              f"mode={picker_assignment['assigned_mode']} "
              f"style={picker_assignment['assigned_style'] or '(invent)'}",
              flush=True)

    posted = skipped = failed = 0
    # Per-candidate outcome rows for the summary JSON. The MCP layer
    # (mcp/src/index.ts post_drafts) prefers summary.candidate_results over
    # regex-parsing our stdout; this is the authoritative channel that marks
    # review-queue cards posted/terminal so a card is never re-fed to a poster
    # after its reply already landed (2026-07-06 duplicate-retry incident).
    candidate_results: list[dict] = []
    # Split skip vs fail reasons. The dashboard renders `failure_reasons` as
    # a "failed: <reason>" pill, so intentional skips (duplicate_thread_pre_post,
    # empty_reply_text, rate_limited, tweet_not_found, reply_box_not_found,
    # no_reply_url_captured) MUST NOT land in this bucket; otherwise a clean
    # dedup-only cycle (posted=2, failed=0) misrenders as
    # "failed: duplicate_thread_pre_post 3" which is exactly the wrong signal.
    fail_reasons: dict[str, int] = {}
    skip_reasons: dict[str, int] = {}

    # Approval gate. A plan that went through the MCP review carries an
    # `approved` flag per candidate (set in mcp/dist/index.js). Honor it here so
    # a DIRECT `--plan` run — bypassing the elicitation form — can't publish
    # drafts the user never ticked. Plans that never had review (autopilot,
    # legacy) omit the key entirely and pass through untouched. Override with
    # --post-unapproved.
    if not args.post_unapproved:
        _kept = []
        for c in candidates:
            if "approved" in c and not c.get("approved"):
                skipped += 1
                skip_reasons["not_approved"] = skip_reasons.get("not_approved", 0) + 1
            else:
                _kept.append(c)
        if skip_reasons.get("not_approved"):
            print(f"[post] {skip_reasons['not_approved']} candidate(s) skipped: not "
                  f"approved in plan (pass --post-unapproved to override)", flush=True)
        candidates = _kept

    # Hard preflight: the reply path (twitter_browser.py) imports Playwright,
    # the only such importer in the pipeline. If the resolved interpreter can't
    # import it, EVERY post dies with no_reply_json because the owned runtime is
    # missing or half-provisioned (Karol, 2026-06-22). Fail LOUD here with a
    # distinct signal instead of attempting posts that silently no-op. Gated on
    # there being real work, so a no-op / all-skipped plan still exits clean.
    if candidates:
        _chk = subprocess.run(
            [PYTHON, "-c", "import playwright"],
            capture_output=True, text=True,
        )
        if _chk.returncode != 0:
            print(f"[post] FATAL runtime_incomplete: interpreter {PYTHON!r} cannot "
                  f"import playwright — the owned Python runtime is missing or "
                  f"unprovisioned. Run the `runtime` install (action:'install') "
                  f"before posting. stderr: {(_chk.stderr or '').strip()[:300]}",
                  file=sys.stderr, flush=True)
            print(json.dumps({
                "posted": 0,
                "skipped": 0,
                "failed": len(candidates),
                "failure_reasons": "runtime_incomplete",
                "skip_reasons": "",
            }), flush=True)
            return 3

    _total = len(candidates)

    # ---- Batch-level browser-lock hold (cross-process posting priority) --------
    # Hold the Twitter browser lock for the WHOLE approved batch instead of
    # re-acquiring it per candidate. Per-candidate acquisition freed the lock in
    # the gap between replies (dominated by link_tail's `claude -p` call, ~5-20s),
    # and the autopilot scan fires every 60s, so a scan kept slipping into that gap
    # and seizing the browser mid-batch -- the exact "posting gets cut off" symptom
    # on the remote box. Acquiring ONCE here PREEMPTS any live scan (role:"post"
    # priority) and, by exporting our session id as S4L_LOCK_OWNER, makes every
    # child twitter_browser.py reply INHERIT this hold rather than contend for it,
    # closing the gap. The hold is bounded by the posting-specific POST_LOCK_EXPIRY
    # failsafe in twitter_browser (a hung poster self-clears in <=180s; a crashed
    # one frees instantly via dead-pid reclaim), so it can never wedge the browser
    # indefinitely. Best-effort: if the import or acquire fails we simply fall back
    # to the legacy per-candidate acquisition (children still preempt scans one by
    # one), so posting degrades gracefully and is never blocked by this addition.
    _tb = None
    _batch_lock_held = False
    if candidates:
        try:
            import twitter_browser as _tb  # S4L_LOCK_ROLE=post already set above
            _tb._acquire_browser_lock()    # preempts a live scan; sys.exit(1) if contended
            os.environ["S4L_LOCK_OWNER"] = _tb._LOCK_SESSION_ID
            _batch_lock_held = True
            print(f"[post] batch lock held by {_tb._LOCK_SESSION_ID} (role=post); "
                  f"{_total} candidate(s) inherit it", flush=True)
        except SystemExit:
            # _acquire_browser_lock exits when a LIVE non-preemptable peer (another
            # poster) holds the lock past LOCK_WAIT_MAX. Don't abort the whole run:
            # drop to per-candidate acquisition (each child still preempts scans).
            print("[post] batch lock contended; per-candidate acquisition in effect",
                  flush=True)
        except Exception as _e:
            print(f"[post] batch lock setup skipped ({_e}); per-candidate "
                  "acquisition in effect", flush=True)

    try:
        for _idx, c in enumerate(candidates, start=1):
            if _terminate_requested:
                print(f"[post] stopping early on SIGTERM: {_idx - 1}/{_total} "
                      f"candidates processed, posted={posted}", flush=True)
                break
            # Live per-post status for the S4L menu bar. LEAD with `posted` (the
            # REAL count of replies that actually landed), not `_idx` (the loop
            # position). _idx races through already-posted / deleted cards as instant
            # dedup-skips, so a bare "posting 88/139" looked like 88 were sent when
            # 0 were — misleading on every restart. "+{posted}" keeps the honest
            # number in front; the position is secondary context. The label MUST
            # keep the "posting" prefix: the menu bar detects a live drain by that
            # substring (s4l_menubar _compute_posting_label / _spin).
            _write_activity(f"posting +{posted} · {_idx}/{_total}")
            # Re-stamp the batch hold at each candidate boundary so the
            # POST_LOCK_EXPIRY failsafe measures silence from the LAST real
            # progress, not from batch start. Insurance on top of the child's own
            # inherit-refresh: keeps the hold fresh even across a candidate that
            # skips before ever spawning a reply subprocess (empty_reply_text,
            # pre-post dedup). Cheap; never raises.
            if _batch_lock_held and _tb is not None:
                try:
                    _tb._refresh_browser_lock()
                except Exception:
                    pass
            try:
                # Two-draft cards (2026-07-07): each candidate may carry its OWN
                # (assigned_style, assigned_mode) reflecting whichever draft is
                # actually posting, either the recommended one (stamped at
                # plan-write time) or the other one (stamped by the MCP
                # post_drafts edits path when a human switched drafts on the
                # review card). Without this, every card would coerce back to
                # ONE cycle-wide style even when it posted under the other
                # style, silently corrupting the engagement_style label the
                # picker's performance stats are learned from. Falls back to
                # the plan-level assignment for any candidate/plan that
                # predates this (assigned_mode key absent).
                if "assigned_mode" in c:
                    _cand_assignment = {
                        "assigned_style": c.get("assigned_style"),
                        "assigned_mode": c.get("assigned_mode"),
                    }
                else:
                    _cand_assignment = picker_assignment
                outcome, reason = post_one(c, picker_assignment=_cand_assignment)
            except Exception as e:
                print(f"[post] candidate {c.get('candidate_id')} crashed: {e}",
                      flush=True)
                outcome, reason = ("failed", "exception")
                cid = c.get("candidate_id")
                if isinstance(cid, int):
                    update_candidate(cid, "skipped", "exception")
            if outcome == "posted":
                posted += 1
                # Flash the confirmation with a short dwell so the menu bar shows
                # it before the next iteration's "posting" overwrites the label.
                # `posted` was just incremented, so it reflects the reply that landed.
                _write_activity(f"posting +{posted} ✓ · {_idx}/{_total}")
                time.sleep(0.6)
            elif outcome == "skipped":
                skipped += 1
                if reason:
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            else:
                # 'failed' and 'posted_unlogged' both count as failed for the
                # dashboard: posted_unlogged means the reply IS live on X but
                # the posts-row INSERT failed, which is a logging outage that
                # must stay visible (posted=0, failed=N), not a silent skip.
                failed += 1
                if reason:
                    fail_reasons[reason] = fail_reasons.get(reason, 0) + 1
            # Card-level outcome: posted_unlogged maps to 'posted' so the MCP
            # layer marks the card done and never re-posts a live reply.
            candidate_results.append({
                "candidate_id": c.get("candidate_id"),
                "outcome": "posted" if outcome == "posted_unlogged" else outcome,
                "reason": reason or "",
                "our_url": c.get("our_url") or "",
            })
            # Per-candidate durable record: the run-level audit at the bottom of
            # main() is lost if this process is SIGKILLed mid-batch (browser-lock
            # hijack), leaving no local trace of what already posted. Separate
            # file from post-results.jsonl on purpose: that one is run-level and
            # the menu bar/dashboard parse it; don't mix schemas.
            try:
                _prog_path = os.path.join(
                    REPO_DIR, "skill", "logs", "post-candidates.jsonl")
                with open(_prog_path, "a", encoding="utf-8") as _pf:
                    _pf.write(json.dumps({
                        "at": datetime.now(timezone.utc).isoformat(),
                        "plan": plan_path.name,
                        **candidate_results[-1],
                    }) + "\n")
            except Exception:
                pass
    finally:
        _clear_activity()
        # Release the batch hold so the next scan/post can take the browser
        # immediately (don't make peers wait out POST_LOCK_EXPIRY). _tb's atexit
        # is a backstop if we somehow skip this; clearing S4L_LOCK_OWNER stops a
        # late child from re-inheriting a lock we just dropped.
        if _batch_lock_held and _tb is not None:
            try:
                _tb._release_browser_lock()
            except Exception:
                pass
            os.environ.pop("S4L_LOCK_OWNER", None)
            print("[post] batch lock released", flush=True)

    summary = {
        "posted": posted,
        "skipped": skipped,
        "failed": failed,
        "failure_reasons": ",".join(f"{k}:{v}" for k, v in fail_reasons.items()),
        "skip_reasons":    ",".join(f"{k}:{v}" for k, v in skip_reasons.items()),
        # Authoritative per-candidate outcomes for the MCP card layer (see the
        # candidate_results declaration above). Outcome 'posted' here includes
        # posted-but-unlogged replies so cards are never re-fed.
        "candidate_results": candidate_results,
    }
    # Remote observability: a handled post failure returns a reason instead of
    # raising, so the global Sentry excepthook never sees it. On customer .mcpb
    # installs the cycle log lives only on their machine, so an explicit capture
    # here is the ONLY channel that surfaces "approved but didn't post" to us.
    # Fires only on operational/machine reasons (content-judgment skips like
    # "off-topic ..." are excluded), so it never alerts on a healthy dedup cycle.
    try:
        machine = dict(fail_reasons)
        for _k, _v in skip_reasons.items():
            if _k in MACHINE_FAIL_REASONS:
                machine[_k] = machine.get(_k, 0) + _v
        if machine:
            import sentry_init
            _top = max(machine, key=machine.get)
            sentry_init.capture_message(
                "twitter post pipeline issues: "
                f"posted={posted} failed={failed} attempted={len(candidates)} "
                f"reasons={','.join(f'{k}:{v}' for k, v in machine.items())}",
                # error only when a REAL failure happened (failed > 0). A batch
                # that's all benign operational skips (tweet_not_found because
                # the target vanished before we replied, rate_limited, etc.)
                # with posted==0 is not a problem, it's a batch of 1 that had
                # nothing postable. Conflating "nothing posted" with "broken"
                # used to file a fresh Sentry error for every such skip.
                level=("error" if failed > 0 else "warning"),
                tags={
                    "component": "twitter_post",
                    "posted": str(posted),
                    "failed": str(failed),
                    "attempted": str(len(candidates)),
                    "top_reason": _top,
                },
            )
            sentry_init.flush(2.0)
    except Exception:
        pass

    # Persist a durable audit line so "did it post, and how many — and where?" is
    # answerable after the fact. The shell harvests the json on stdout, but when
    # the menu bar launches this directly it captures-then-discards stdout, leaving
    # no record of what posted. Append a timestamped JSONL row (with the live URLs)
    # the menu bar / dashboard can read. Best-effort: never affects the post outcome.
    try:
        posted_urls = [c.get("our_url") for c in candidates if c.get("our_url")]
        audit = dict(summary)
        audit["plan"] = plan_path.name
        audit["at"] = datetime.now(timezone.utc).isoformat()
        audit["urls"] = posted_urls
        audit_path = os.path.join(REPO_DIR, "skill", "logs", "post-results.jsonl")
        os.makedirs(os.path.dirname(audit_path), exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(audit) + "\n")
        print(f"[post] result: posted={posted} skipped={skipped} failed={failed}"
              f"{' urls=' + ','.join(posted_urls) if posted_urls else ''} "
              f"(audit: {audit_path})", flush=True)
    except Exception as e:
        print(f"[post] audit-log write failed (non-fatal): {e}", flush=True)
    # The shell harvests this as the last json line in our stdout.
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
