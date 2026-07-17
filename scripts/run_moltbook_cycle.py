#!/usr/bin/env python3
"""run_moltbook_cycle.py — phased MoltBook posting cycle.

Reduces volume by gating on:
  1. Historical (project, style) engagement signal injected into the drafter prompt.
  2. T0 -> T1 momentum gate: scan threads now, sleep 10 min, re-poll, compute delta.
  3. Adaptive cap: default 2 posts/cycle, bump to 5 only when >=3 candidates
     show real-time momentum (delta >= threshold).

Phase 1: scan hot + new via API, snapshot T0 engagement (in-memory)
Sleep:   --sleep seconds (default 600)
Phase 2a: re-poll same threads, compute delta
Phase 2b: Claude picks from top-N pre-filtered candidates, drafts, Python posts

Usage:
    python3 scripts/run_moltbook_cycle.py
    python3 scripts/run_moltbook_cycle.py --sleep 300 --dry-run
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post, load_env
from account_resolver import resolve as _resolve_account
from moltbook_tools import fetch_moltbook_json, MoltbookRateLimitedError
from engagement_styles import validate_or_register, pick_style_for_post
from version import read_version as read_autoposter_version
from project_topics import topics_for_project

REPO_DIR = os.path.expanduser("~/social-autoposter")
SCRIPTS = os.path.join(REPO_DIR, "scripts")
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")
SKILL_FILE = os.path.join(REPO_DIR, "SKILL.md")
MOLTBOOK_POST = os.path.join(SCRIPTS, "moltbook_post.py")
RUN_CLAUDE = os.path.join(SCRIPTS, "run_claude.sh")
HISTORICAL = os.path.join(SCRIPTS, "historical_engagement.py")

# --- Momentum + cap thresholds (single source of truth, tune here) ----------
DELTA_THRESHOLD = 5.0          # candidate counts as "high momentum" if delta_score >= this
HIGH_DELTA_BUMP = 3            # need this many high-momentum candidates to bump cap
CAP_DEFAULT = 1
CAP_BUMPED = 1
CLAUDE_CANDIDATE_LIMIT = 15    # show at most this many candidates to Claude


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def api_key():
    k = os.environ.get("MOLTBOOK_API_KEY")
    if k:
        return k
    env_file = os.path.join(REPO_DIR, ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                if line.startswith("MOLTBOOK_API_KEY="):
                    return line.strip().split("=", 1)[1]
    print("ERROR: MOLTBOOK_API_KEY not set", file=sys.stderr)
    sys.exit(1)


def fetch_sorted(kind, api_key_, limit=50):
    """kind: 'hot' or 'new'. Returns list of post dicts."""
    url = f"https://www.moltbook.com/api/v1/posts?sort={kind}&limit={limit}"
    data = fetch_moltbook_json(url, api_key=api_key_)
    if not data:
        return []
    return data.get("posts", []) or data.get("data", []) or []


def fetch_one(post_id, api_key_):
    """Re-fetch a single post for T1 measurement."""
    url = f"https://www.moltbook.com/api/v1/posts/{post_id}"
    data = fetch_moltbook_json(url, api_key=api_key_)
    if not data:
        return None
    return data.get("post") or data


def already_posted_thread_ids(thread_ids):
    """Return the subset we've already commented on, to exclude.

    The old single SQL OR-LIKE query is replaced by one posts GET per
    thread_id (thread_url_contains). thread_ids is this cycle's candidate
    set (scan-limit, ~50), so the request count stays bounded.
    """
    if not thread_ids:
        return set()
    hit = set()
    for tid in thread_ids:
        resp = api_get(
            "/api/v1/posts",
            query={
                "platform": "moltbook",
                "thread_url_contains": tid,
                "limit": 1,
            },
        )
        rows = ((resp or {}).get("data") or {}).get("posts") or []
        if rows:
            hit.add(tid)
    return hit


def snapshot(post):
    pid = post.get("id")
    return {
        "id": pid,
        "title": post.get("title", ""),
        "content": (post.get("content") or ""),
        "author": (post.get("user") or {}).get("username") or post.get("author") or "",
        "submolt": (post.get("submolt") or {}).get("name") or post.get("submolt_name") or "",
        "url": f"https://www.moltbook.com/post/{pid}",
        "upvotes_t0": int(post.get("upvote_count") or post.get("upvotes") or 0),
        "comments_t0": int(post.get("comment_count") or post.get("comments_count") or 0),
        "created_at": post.get("created_at") or "",
    }


def delta_score(t0_up, t0_cm, t1_up, t1_cm):
    """Weight comments higher than upvotes (rarer, stronger signal)."""
    return 2.0 * max(t1_up - t0_up, 0) + 5.0 * max(t1_cm - t0_cm, 0)


def build_prompt(candidates, cap, history_block, styles_block, projects_json):
    cand_block = []
    for i, c in enumerate(candidates, 1):
        cand_block.append(
            f"--- #{i} id={c['id']} delta={c['delta_score']:.1f} "
            f"(up {c['upvotes_t0']}->{c['upvotes_t1']}, "
            f"cm {c['comments_t0']}->{c['comments_t1']}) ---\n"
            f"submolt: {c['submolt']}  author: {c['author']}\n"
            f"title: {c['title']}\n"
            f"body: {c['content']}\n"
            f"url: {c['url']}\n"
        )
    candidates_text = "\n".join(cand_block)

    return f"""You are the Social Autoposter reviewing MoltBook candidates for commenting.

Read {SKILL_FILE} for content rules (agent voice, no em dashes, anti-AI).

## Pre-filtered candidates (top {len(candidates)} by 10-minute engagement delta)

{candidates_text}

## Project configs
{projects_json}

{styles_block}

{history_block}

## YOUR JOB

Pick AT MOST {cap} candidates and draft a comment for each. **Post fewer than {cap} if
fewer than {cap} are genuinely on-brand.** Better to skip than to force a comment.

Rules:
- Skip candidates whose submolt/title are mbc20/crypto/spam or have no plausible angle.
- For each kept candidate, pick the ONE best-fit project from the config.
- Choose an engagement_style from the styles block.
- **Consult the historical engagement table above.** If a (project, style) pair has
  the [dead] label (>=5 past posts, median engagement 0), avoid that pair unless the
  thread is an unusually good fit. Prefer [good] pairs when plausible.
- Draft the comment in agent voice (\"my human\" not \"I\"), match the thread's language.
- Apply the matched project's `voice` block: follow `voice.tone`, never violate `voice.never`, mirror `voice.examples` / `voice.examples_good` when present.
- Comments must add a concrete, thread-relevant point. Do not paste generic product pitches.

## OUTPUT FORMAT

Return ONLY a single JSON object, no prose, with this exact shape:

```json
{{
  "posts": [
    {{
      "thread_id": "<candidate id>",
      "thread_url": "<candidate url>",
      "thread_title": "<candidate title>",
      "thread_author": "<candidate author>",
      "matched_project": "<project name from config>",
      "engagement_style": "<one of the valid styles, or your invented name>",
      "new_style": null,
      "language": "<detected language, e.g. en>",
      "comment_text": "<the actual comment to post>"
    }}
  ],
  "skipped": [
    {{ "thread_id": "<id>", "reason": "<short reason>" }}
  ]
}}
```

If, and ONLY if, none of the listed styles fits, you may invent a new style.
To do so, set `engagement_style` to your new name (snake_case) AND replace the
`new_style: null` with a populated block:

```json
"new_style": {{
  "description": "<what this style is, in one sentence>",
  "example": "<a short example utterance>",
  "note": "<when to use, when not to>",
  "why_existing_didnt_fit": "<which existing style was closest, and why it didn't fit>"
}}
```

If the engagement_style matches one of the listed styles, leave `new_style` as null.
Inventing should be rare; prefer an existing style if it's even 80% right.

CRITICAL: Do NOT call moltbook_post.py or any Bash tool. Only return the JSON.
The orchestrator will post and log."""


def parse_claude_json(output):
    # Claude's JSON sits inside a "result" field of its structured output.
    try:
        outer = json.loads(output)
        result = outer.get("result", "") if isinstance(outer, dict) else ""
    except Exception:
        result = output
    # result is a string containing either a JSON object or a fenced ```json block
    m = result
    start = m.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    end = -1
    for i in range(start, len(m)):
        ch = m[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None
    try:
        return json.loads(m[start : end + 1])
    except Exception:
        return None


def post_and_log(decisions, claude_session_id):
    """Iterate Claude's picks, call moltbook_post.py, log each to DB."""
    posted = 0
    failed = 0

    for p in decisions.get("posts", []):
        tid = p.get("thread_id")
        text = p.get("comment_text", "").strip()
        if not tid or not text:
            failed += 1
            continue

        try:
            proc = subprocess.run(
                ["python3", MOLTBOOK_POST, "comment", "--post-id", tid, "--content", text],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            log(f"  post error for {tid}: {e}")
            failed += 1
            continue

        if proc.returncode != 0:
            log(f"  post failed rc={proc.returncode} for {tid}: {proc.stderr.strip()[:200]}")
            failed += 1
            continue

        # moltbook_post prints a final JSON line with url + comment_id
        our_url = ""
        for line in reversed(proc.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    js = json.loads(line)
                    our_url = js.get("url", "")
                    break
                except Exception:
                    continue

        # Validate or register the engagement_style. In USE mode any drifted
        # style label is coerced back to style_assignment["style"]; in INVENT
        # mode the new_style block is registered into
        # engagement_styles_registry via the s4l API (replaces the legacy
        # file-based sidecar). The picker's choice is set once for the whole
        # batch above.
        validated_style, style_action = validate_or_register(
            p,
            source_post={
                "platform": "moltbook",
                "post_url": our_url or p.get("thread_url", ""),
                "post_id": None,
                "model": p.get("model"),
            },
            assigned_style=(style_assignment or {}).get("style"),
            assigned_mode=(style_assignment or {}).get("mode"),
        )

        # POST /api/v1/posts requires a valid http(s) our_url for active rows
        # (it derives thread_author_handle from thread_author and hardcodes
        # feedback_report_used=TRUE, so those are omitted here). A blank
        # our_url would 400 and crash the loop; the comment is already live,
        # so on the rare parse miss we log a warning and skip the DB row
        # rather than abort the cycle. `project` is the endpoint's key name.
        if not our_url:
            log(f"  WARNING: posted to {tid} but could not parse our_url; "
                f"skipping DB log for this row")
            posted += 1
            continue
        api_post(
            "/api/v1/posts",
            {
                "platform": "moltbook",
                "thread_url": p.get("thread_url", ""),
                "thread_author": p.get("thread_author", "various"),
                "thread_title": p.get("thread_title", ""),
                "thread_content": "",
                "our_url": our_url,
                "our_content": text,
                "our_account": _resolve_account("moltbook") or "",
                "source_summary": "moltbook cycle comment",
                "project": p.get("matched_project", ""),
                "engagement_style": validated_style or "",
                "language": p.get("language", "en"),
                "status": "active",
                "claude_session_id": claude_session_id,
                "autoposter_version": read_autoposter_version(),
            },
            ok_on_conflict=True,
        )
        posted += 1
        style_tag = validated_style or "(none)"
        if style_action == "registered":
            style_tag += " [REGISTERED candidate]"
        log(f"  posted to {tid}  project={p.get('matched_project')}  style={style_tag}")

    return posted, failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sleep", type=int, default=600)
    parser.add_argument("--scan-limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_start = time.time()
    log(f"=== MoltBook Cycle: sleep={args.sleep}s, scan-limit={args.scan_limit} ===")

    # --- Phase 0: context ---------------------------------------------------
    config = load_config()
    def _project_record(p):
        rec = {k: p.get(k) for k in ("description", "website", "voice")}
        rec["search_topics"] = list(topics_for_project(p.get("name") or ""))
        return rec
    projects_json = json.dumps(
        {p["name"]: _project_record(p)
         for p in config.get("projects", [])
         if p.get("weight", 0) > 0
         and "moltbook" not in (p.get("platforms_disabled") or [])},
        indent=2,
    )

    try:
        history_block = subprocess.run(
            ["python3", HISTORICAL, "--platform", "moltbook"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        history_block = "## Historical engagement\n(unavailable)\n"

    try:
        styles_block = subprocess.run(
            ["bash", "-c", f"source {REPO_DIR}/skill/styles.sh && generate_styles_block moltbook posting"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        styles_block = ""

    key = api_key()

    # --- Phase 1: scan T0 ---------------------------------------------------
    log("Phase 1: scanning MoltBook hot + new...")
    try:
        hot = fetch_sorted("hot", key, limit=args.scan_limit)
        new = fetch_sorted("new", key, limit=args.scan_limit)
    except MoltbookRateLimitedError as e:
        log(f"MoltBook rate-limited, aborting cycle: {e.reset_seconds}s")
        return 2

    seen = {}
    for p in (hot + new):
        snap = snapshot(p)
        if snap["id"] and snap["id"] not in seen:
            seen[snap["id"]] = snap

    candidates = list(seen.values())
    log(f"Phase 1: {len(candidates)} unique candidates scanned.")

    # Exclude threads we've already commented on
    posted_before = already_posted_thread_ids([c["id"] for c in candidates])
    candidates = [c for c in candidates if c["id"] not in posted_before]
    log(f"Phase 1: {len(candidates)} after excluding already-posted ({len(posted_before)} filtered).")

    if not candidates:
        log("No candidates. Exiting.")
        return 0

    # --- Sleep --------------------------------------------------------------
    log(f"Sleeping {args.sleep}s before T1 re-measurement...")
    time.sleep(args.sleep)

    # --- Phase 2a: re-poll T1 ----------------------------------------------
    log("Phase 2a: re-polling T1 engagement...")
    for c in candidates:
        try:
            t1 = fetch_one(c["id"], key)
        except MoltbookRateLimitedError as e:
            log(f"  rate-limited mid re-poll ({e.reset_seconds}s), using T0 data for remaining")
            break
        if not t1:
            c["upvotes_t1"] = c["upvotes_t0"]
            c["comments_t1"] = c["comments_t0"]
            c["delta_score"] = 0.0
            continue
        c["upvotes_t1"] = int(t1.get("upvote_count") or t1.get("upvotes") or c["upvotes_t0"])
        c["comments_t1"] = int(t1.get("comment_count") or t1.get("comments_count") or c["comments_t0"])
        c["delta_score"] = delta_score(c["upvotes_t0"], c["comments_t0"], c["upvotes_t1"], c["comments_t1"])

    for c in candidates:
        c.setdefault("upvotes_t1", c["upvotes_t0"])
        c.setdefault("comments_t1", c["comments_t0"])
        c.setdefault("delta_score", 0.0)

    # --- Phase 2b: adaptive cap + Claude ------------------------------------
    high_delta = [c for c in candidates if c["delta_score"] >= DELTA_THRESHOLD]
    cap = CAP_BUMPED if len(high_delta) >= HIGH_DELTA_BUMP else CAP_DEFAULT
    log(f"Phase 2b: {len(high_delta)} candidates with delta >= {DELTA_THRESHOLD} "
        f"-> cap = {cap}")

    candidates.sort(key=lambda c: c["delta_score"], reverse=True)
    top = candidates[:CLAUDE_CANDIDATE_LIMIT]
    log(f"Phase 2b: showing Claude top {len(top)} by delta, cap = {cap}")
    for c in top:
        log(f"  #{c['id']} delta={c['delta_score']:.1f} "
            f"t0={c['upvotes_t0']}up/{c['comments_t0']}cm "
            f"t1={c['upvotes_t1']}up/{c['comments_t1']}cm")

    if args.dry_run:
        log("Dry run: skipping Claude + post.")
        for c in top[:cap]:
            log(f"  would consider #{c['id']} delta={c['delta_score']:.1f} title={c['title'][:60]}")
        return 0

    claude_session_id = str(uuid.uuid4())
    os.environ["CLAUDE_SESSION_ID"] = claude_session_id
    # 2026-05-22: pick the engagement style for this draft batch so
    # validate_or_register can coerce any drifted engagement_style label
    # back to the picker's choice. Moltbook batches share one assignment
    # per cycle (same pattern as github batches; cycles run often enough
    # that the picker's distribution averages out). The styles_block in
    # the prompt still shows the legacy menu because the prompt is built
    # by a shell helper; the enforcement happens at the validate step.
    style_assignment = pick_style_for_post("moltbook", context="posting")
    log(f"Style assignment for this batch: mode={style_assignment.get('mode')} "
        f"style={style_assignment.get('style') or '(invent)'}")
    prompt = build_prompt(top, cap, history_block, styles_block, projects_json)

    log("Phase 2b: invoking Claude for drafting...")
    try:
        proc = subprocess.run(
            [RUN_CLAUDE, "run-moltbook-cycle",
             "--strict-mcp-config",
             "--mcp-config", os.path.expanduser("~/.claude/browser-agent-configs/no-agents-mcp.json"),
             "-p", "--output-format", "json", prompt],
            capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        log("Claude timed out after 900s")
        return 1

    if proc.returncode != 0:
        log(f"Claude exited rc={proc.returncode}: {proc.stderr[-500:]}")
        return 1

    decisions = parse_claude_json(proc.stdout)
    if not decisions:
        log("Could not parse Claude JSON output.")
        log(f"Last 500 chars of output: {proc.stdout[-500:]}")
        return 1

    log(f"Claude picked {len(decisions.get('posts', []))} posts, "
        f"skipped {len(decisions.get('skipped', []))}.")

    posted, failed = post_and_log(decisions, claude_session_id)

    elapsed = int(time.time() - run_start)
    log(f"=== Cycle complete: posted={posted}, failed={failed}, elapsed={elapsed}s ===")

    # Fetch real Claude cost from the session we ran (orchestrator/SDK billing).
    cycle_cost = 0.0
    try:
        _resp = api_get(
            "/api/v1/claude-sessions/cost",
            query={"session_id": claude_session_id},
        )
        cycle_cost = float(((_resp or {}).get("data") or {}).get("parent_cost") or 0.0)
    except Exception as _e:
        log(f"WARNING: could not fetch session cost: {_e}")

    # Log cycle summary to the run tracking table
    try:
        subprocess.run(
            ["python3", os.path.join(SCRIPTS, "log_run.py"),
             "--script", "run-moltbook-cycle",
             "--posted", str(posted), "--skipped", str(len(decisions.get("skipped", []))),
             "--failed", str(failed), "--cost", f"{cycle_cost:.4f}", "--elapsed", str(elapsed)],
            timeout=15,
        )
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
