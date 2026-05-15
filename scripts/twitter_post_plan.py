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
uses the twitter-agent persistent profile + CDP, so the exclusive lock
matters.

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
import re
import subprocess
import sys
from pathlib import Path

REPO_DIR = os.path.expanduser("~/social-autoposter")
TWITTER_BROWSER = os.path.join(REPO_DIR, "scripts", "twitter_browser.py")
LOG_POST = os.path.join(REPO_DIR, "scripts", "log_post.py")
CAMPAIGN_BUMP = os.path.join(REPO_DIR, "scripts", "campaign_bump.py")
LINK_TAIL = os.path.join(REPO_DIR, "scripts", "link_tail.py")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

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


def update_candidate(cid: int, status: str) -> None:
    if not DATABASE_URL:
        print("[post] DATABASE_URL not set; skipping candidate update", flush=True)
        return
    sql_status = status.replace("'", "''")
    if status == "posted":
        # Caller will set post_id separately on success path; here we just
        # mark intermediate states.
        return
    cmd = [
        "psql", DATABASE_URL, "-c",
        f"UPDATE twitter_candidates SET status='{sql_status}' WHERE id={cid} AND status != 'posted'",
    ]
    rc, out, err = run_subprocess(cmd, timeout_sec=30)
    if rc != 0:
        print(f"[post] candidate {cid} status update failed: {err}", flush=True)


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
    if not DATABASE_URL:
        return (False, None)
    # Use psql -t -A for tab-separated, no header, no padding output.
    # Quote thread_url for SQL literal; thread_url is captured from the
    # tweet URL which only ever contains [A-Za-z0-9_/:.] so single-quote
    # escaping is sufficient.
    safe_url = thread_url.replace("'", "''")
    cmd = [
        "psql", DATABASE_URL, "-t", "-A", "-c",
        "SELECT id FROM posts "
        f"WHERE platform='twitter' AND thread_url='{safe_url}' LIMIT 1",
    ]
    rc, out, err = run_subprocess(cmd, timeout_sec=15)
    if rc != 0:
        print(f"[post] dedup pre-check psql failed (rc={rc}): {err}", flush=True)
        return (False, None)
    out = (out or "").strip()
    if not out:
        return (False, None)
    try:
        return (True, int(out.splitlines()[0].strip()))
    except (ValueError, IndexError):
        return (True, None)


def update_candidate_posted(cid: int, post_id: int) -> None:
    if not DATABASE_URL:
        print("[post] DATABASE_URL not set; cannot mark candidate posted", flush=True)
        return
    cmd = [
        "psql", DATABASE_URL, "-c",
        f"UPDATE twitter_candidates SET status='posted', posted_at=NOW(), post_id={int(post_id)} WHERE id={int(cid)}",
    ]
    rc, out, err = run_subprocess(cmd, timeout_sec=30)
    if rc != 0:
        print(f"[post] candidate {cid} -> posted update failed: {err}", flush=True)


def post_one(c: dict) -> tuple[str, str]:
    """Post a single candidate. Returns (outcome, reason).

    outcome: 'posted' | 'skipped' | 'failed'
    reason:  short failure key when outcome != 'posted', else ''.
    """
    cid = int(c["candidate_id"])
    candidate_url = c["candidate_url"]
    reply_text = (c.get("reply_text") or "").strip()
    link_url = (c.get("link_url") or "").strip()
    project = c["matched_project"]
    thread_author = c.get("thread_author") or ""
    thread_text = c.get("thread_text") or ""
    style = (c.get("engagement_style") or "").strip()
    language = (c.get("language") or "").strip()
    link_source = (c.get("link_source") or "").strip()

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

    # CTA bridge generation: instead of bolting `link_url` onto `reply_text`
    # with a space (the old `f"{reply_text} {link_url}"`), call link_tail.py
    # which spawns one Claude call (default smart model, NOT Haiku) to write
    # a 1-sentence bridge that names a concrete benefit and ends in the URL.
    # On any failure (timeout, model error, output fails sanity gate) the
    # script returns the mechanical concat as a fallback, so this code path
    # is always tolerant of model failure.
    full_text = reply_text
    link_tail_outcome = "skipped_no_link"
    if link_url:
        rc, out, err = run_subprocess(
            ["python3", LINK_TAIL,
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
        ["python3", TWITTER_BROWSER, "reply", candidate_url, full_text],
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
        if reason in ("rate_limited", "tweet_not_found", "reply_box_not_found"):
            update_candidate(cid, "skipped")
            return ("skipped", reason)
        # everything else (incl. timeout, parse errors) -> failed
        update_candidate(cid, "failed")
        return ("failed", reason if reason else "unknown")

    reply_url = parsed.get("reply_url") or ""
    final_text = parsed.get("final_text") or full_text
    applied_campaigns = parsed.get("applied_campaigns") or []

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
        update_candidate(cid, "skipped")
        return ("skipped", "no_reply_url_captured")

    # Insert the post row.
    log_args = [
        "python3", LOG_POST,
        "--platform", "twitter",
        "--thread-url", candidate_url,
        "--our-url", reply_url,
        "--our-content", final_text,
        "--project", project,
        "--thread-author", thread_author,
        "--thread-title", thread_text,
    ]
    if style:
        log_args += ["--engagement-style", style]
    if language:
        log_args += ["--language", language]
    if link_source:
        log_args += ["--link-source", link_source]
    # Generation trace: run-twitter-cycle.sh writes a snapshot of the
    # cycle's few-shot context (top_performers, top_queries, supply
    # signal, dud queries) to a tempfile and exports the path via
    # SAPS_TWITTER_GEN_TRACE_PATH. Forward to log_post.py so every
    # post landed this cycle gets posts.generation_trace JSONB pointing
    # to the same snapshot. Same trace for every post in this run
    # because they all saw the same Phase 2b-prep context. The env var
    # is missing/empty when run-twitter-cycle.sh's trace step failed —
    # in that case we just skip the flag and the row gets NULL trace.
    trace_path = os.environ.get("SAPS_TWITTER_GEN_TRACE_PATH") or ""
    if trace_path and os.path.isfile(trace_path):
        log_args += ["--generation-trace", trace_path]

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
        update_candidate(cid, "skipped")
        return ("failed", "log_post_no_id")

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
            ["python3", CAMPAIGN_BUMP, "--table", "posts",
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
        ["python3", LOG_POST,
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

    update_candidate_posted(cid, post_id)
    print(f"[post] candidate {cid} posted as {reply_url} (post_id={post_id})",
          flush=True)
    return ("posted", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True,
                    help="Path to the plan JSON file (read-only here)")
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

    posted = skipped = failed = 0
    # Split skip vs fail reasons. The dashboard renders `failure_reasons` as
    # a "failed: <reason>" pill, so intentional skips (duplicate_thread_pre_post,
    # empty_reply_text, rate_limited, tweet_not_found, reply_box_not_found,
    # no_reply_url_captured) MUST NOT land in this bucket; otherwise a clean
    # dedup-only cycle (posted=2, failed=0) misrenders as
    # "failed: duplicate_thread_pre_post 3" which is exactly the wrong signal.
    fail_reasons: dict[str, int] = {}
    skip_reasons: dict[str, int] = {}

    for c in candidates:
        try:
            outcome, reason = post_one(c)
        except Exception as e:
            print(f"[post] candidate {c.get('candidate_id')} crashed: {e}",
                  flush=True)
            outcome, reason = ("failed", "exception")
            cid = c.get("candidate_id")
            if isinstance(cid, int):
                update_candidate(cid, "failed")
        if outcome == "posted":
            posted += 1
        elif outcome == "skipped":
            skipped += 1
            if reason:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        else:
            failed += 1
            if reason:
                fail_reasons[reason] = fail_reasons.get(reason, 0) + 1

    summary = {
        "posted": posted,
        "skipped": skipped,
        "failed": failed,
        "failure_reasons": ",".join(f"{k}:{v}" for k, v in fail_reasons.items()),
        "skip_reasons":    ",".join(f"{k}:{v}" for k, v in skip_reasons.items()),
    }
    # The shell harvests this as the last json line in our stdout.
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
