#!/usr/bin/env python3
"""Feedback digest: distill human card decisions into learned_preferences.

The scheduled half of the review-events feedback loop (see
scripts/learned_preferences.py for the full loop). Per run:

  1. GET /api/v1/review-events?counts=true — which (project, platform) pairs
     have unprocessed events. The API scopes to this installation, so a
     customer box only ever digests its own user's decisions.
  2. For each project that exists in the local config.json: fetch the
     unprocessed events, build a conservative digest prompt (current block +
     events + approval counter-evidence), run Claude headless via
     run_claude.sh (script_tag feedback-digest, cost-tracked like every other
     pipeline Claude call).
  3. Apply the returned mutation plan through
     learned_preferences.apply_mutations() (whitelist, flock, backup, atomic).
  4. PATCH the events processed (processed_batch=digest-<ts>) so they are
     never digested twice. Events are marked processed even when the plan is
     "no changes" — a considered no-op is a completed digestion, not a retry.

Overall feedback (decision='feedback', project IS NULL; typed into the
composer behind the menu bar's "Give overall feedback to AI…" item) is fetched
once per run,
folded into EVERY configured project's prompt as explicit standing guidance,
and marked processed only after all attempted project digests succeed.
Loved approvals (the stronger emoji in the card's inline approve row:
😄 / ❤️‍🔥) arrive as loved=true on approved events, with the exact
strength as an approve_level_N interaction, and are surfaced to the model
as strong positive evidence.

Failure handling: a Claude failure or unparseable plan leaves the events
unprocessed for the next run. A run-level flock prevents concurrent digests.

Stderr markers (load-bearing, dashboard-parsed; do not reformat):
  [feedback_digest] project=<name> platform=<p> events=<n> applied=<x> dropped=<y> marked=<m>

Usage:
  python3 scripts/feedback_digest.py                 # digest all pending
  python3 scripts/feedback_digest.py --project fazm  # one project
  python3 scripts/feedback_digest.py --dry-run       # print plans, change nothing
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# SAPS_->S4L_ env mirror (brand rename 2026-07-03): old launchd plists and
# scheduled-task prompts still export SAPS_*; this process reads S4L_*.
import s4l_env  # noqa: E402

s4l_env.mirror()

from http_api import api_get, api_patch, api_post  # noqa: E402
import learned_preferences as lp  # noqa: E402

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_CLAUDE_SH = os.path.join(REPO_DIR, "scripts", "run_claude.sh")
LOCK_PATH = os.path.expanduser("~/.social-autoposter-mcp/feedback-digest.lock")
MAX_EVENTS_PER_RUN = 200

DISALLOWED_TOOLS = (
    "ScheduleWakeup,CronCreate,CronDelete,CronList,EnterPlanMode,EnterWorktree,"
    "Bash,Edit,Write,Read,Grep,Glob,WebFetch,WebSearch,Agent,TodoWrite,"
    "NotebookEdit,LSP,Monitor,PushNotification,RemoteTrigger,TaskOutput,"
    "TaskStop,ListMcpResourcesTool,ReadMcpResourceTool"
)


def log(msg: str) -> None:
    print(f"[feedback_digest] {msg}", file=sys.stderr, flush=True)


def _now_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def load_config():
    try:
        return json.loads(Path(lp.config_path()).read_text())
    except Exception:
        return {"projects": []}


def _event_line(e: dict) -> str:
    """One compact evidence line per event for the prompt."""
    parts = [f"[{e.get('decision')}{'+loved' if e.get('loved') else ''}]"]
    if e.get("reject_category"):
        parts.append(f"category={e['reject_category']}")
    if e.get("thread_author"):
        parts.append(f"author=@{e['thread_author']}")
    # Candidate-join context (author_followers, search_topic, tweet_text come
    # from the LEFT JOIN in GET /api/v1/review-events): without it the model
    # sees a bare handle and can never characterize the author TYPE behind a
    # wrong_author reject. Older server deploys just omit the keys.
    if e.get("author_followers") is not None:
        parts.append(f"author_followers={e['author_followers']}")
    if e.get("search_topic"):
        parts.append(f"found_via_topic={e['search_topic']}")
    inter = e.get("interactions") or []
    kinds = sorted({str(i.get("type")) for i in inter if isinstance(i, dict) and i.get("type")})
    if kinds:
        parts.append(f"user_checked={'+'.join(kinds)}")
    if e.get("dwell_ms"):
        parts.append(f"dwell={round(e['dwell_ms'] / 1000, 1)}s")
    if e.get("edited"):
        parts.append("edited_before_approving")
    line = " ".join(parts)
    note = (e.get("reject_note") or "").strip()
    if note:
        line += f"\n  user note: {note[:300]}"
    tweet = (e.get("tweet_text") or "").strip()
    if tweet:
        line += f"\n  their post was: {tweet[:200]}"
    draft = (e.get("draft_text") or "").strip()
    original = (e.get("original_text") or "").strip()
    if original and draft and original != draft:
        # Edited approval: show the diff pair, the strongest style evidence
        # we have (draft_text carries the FINAL text the user actually posted).
        line += f"\n  our ORIGINAL draft was: {original[:300]}"
        line += f"\n  user REWROTE it to: {draft[:300]}"
    elif draft:
        line += f"\n  our draft was: {draft[:200]}"
    url = (e.get("thread_url") or "").strip()
    if url:
        line += f"\n  thread: {url}"
    return line


def build_prompt(project: dict, events: list[dict], overall_events: list[dict] | None = None) -> str:
    block = lp.get_block(project)
    overall_events = overall_events or []
    rejected = [e for e in events if e.get("decision") == "rejected"]
    approved = [e for e in events if e.get("decision") == "approved"]
    loved = [e for e in approved if e.get("loved")]
    voice_never = ((project.get("voice") or {}).get("never")) or []
    guard_do_not = ((project.get("content_guardrails") or {}).get("do_not")) or []

    ev_lines = "\n".join(
        f"{i + 1}. {_event_line(e)}" for i, e in enumerate(events)
    ) or "(none this digest)"
    overall_block = ""
    if overall_events:
        notes = "\n".join(
            f"{i + 1}. {(e.get('reject_note') or '').strip()[:500]}"
            for i, e in enumerate(overall_events)
        )
        overall_block = (
            f"\n\nOVERALL FEEDBACK from the user ({len(overall_events)} "
            f"note{'s' if len(overall_events) != 1 else ''}, typed into the feedback box; "
            "explicit standing guidance about the whole pipeline, NOT about any single thread):\n"
            f"{notes}"
        )

    return f"""You maintain the learned_preferences block for the project "{project.get('name')}" in a social-posting pipeline. The block distills the user's own approve/reject decisions on draft cards into short standing preferences that steer future thread selection and drafting. It is SOFT guidance read by the drafting model, not a filter.

CURRENT learned_preferences:
{json.dumps({k: block[k] for k in ("audience_avoid", "audience_prefer", "thread_avoid", "draft_style_notes")}, indent=2)}

CURRENT voice.never: {json.dumps(voice_never)}
CURRENT content_guardrails.do_not: {json.dumps(guard_do_not)}

NEW REVIEW EVENTS since the last digest ({len(rejected)} rejected, {len(approved)} approved, {len(loved)} of the approvals loved):
{ev_lines}{overall_block}

Categories: wrong_author = the thread's author/audience was a bad fit; off_topic = the thread itself was a bad fit; bad_draft = thread was fine but the written reply was off; other = see the note. "edited_before_approving" with an ORIGINAL/REWROTE pair means the user hand-corrected our draft before posting: the rewrite is a direct statement of the voice they want. Diff the pair; when 2+ edits show the same correction (a phrase type removed, a structure replaced, tone shifted, length cut), distill that recurring pattern into draft_style_notes. Ignore edit content that is lead-specific or cosmetic (typo fixes, one-off facts); learn only what generalizes. "user_checked=profile_click" means the user opened the author's profile before deciding (a strong author-quality signal even without a note). "[approved+loved]" means the user picked a stronger emoji in the approve row ("this was a really good one"; approve_level_N in interactions carries the strength, 3 = best of the best): strong positive evidence for audience_prefer and thread selection, worth roughly two plain approvals.

You can also block SPECIFIC authors via the plan's block_authors list. A block is a permanent hard exclusion of that one handle from all future thread selection, so it is YOUR judgment call, never automatic. Block when the evidence is strong: a wrong_author reject IS a direct human statement about that author (especially with profile_click), and the author context (author_followers, their post, found_via_topic) or the user's note confirms the account itself was the problem rather than the topic. Do NOT block when the reject looks topic-driven (off_topic/bad_draft on a reasonable account) or when you are unsure; the generalizable TYPE entry in audience_avoid is the softer tool for that.

Propose changes to the block. RULES, in priority order:
1. Be conservative. Prefer NO changes over speculative ones. An empty plan is a good plan when the evidence is thin.
2. Generalize only what the evidence supports: 2+ events agreeing justify a general entry; a single reject justifies at most one narrowly-scoped entry, and only when its note, interactions, or author context (follower count, their post, discovery topic) makes the reason explicit. Exceptions: a single loved approval can justify one audience_prefer/thread entry when the pattern it shows is clear, and OVERALL FEEDBACK lines are explicit user instructions that outrank inferred signals; reflect each one in the most fitting list even from a single line, rewritten as a standing preference.
3. Describe author/audience TYPES, never individual handles. "crypto/web3-native accounts shilling tokens" is right; "@someguy" is wrong. Preferences must generalize.
4. Approvals are counter-evidence. If approvals contradict an existing entry, propose removing or narrowing it. Also propose removing entries that events show are stale.
5. bad_draft events feed draft_style_notes (or, ONLY for a clearly recurring phrasing complaint, voice_never_add / guardrails_do_not_add; use those sparingly, they touch curated fields).
6. Each entry: one sentence, under 200 characters, plain language, no em dashes, no hashtags, understandable a month from now without these events.
7. Respect the cap: at most {lp.MAX_ENTRIES_PER_LIST} entries per list. If a list is full, fold the new signal into an existing entry via remove+add.

OUTPUT: a single JSON object, nothing else. Schema:
{{"changes": {{"audience_avoid": {{"add": [], "remove": []}}, "audience_prefer": {{"add": [], "remove": []}}, "thread_avoid": {{"add": [], "remove": []}}, "draft_style_notes": {{"add": [], "remove": []}}}}, "voice_never_add": [], "guardrails_do_not_add": [], "block_authors": [{{"handle": "somehandle", "reason": "one short sentence citing the evidence"}}], "rationale": "one short sentence"}}
"remove" values must match existing entries EXACTLY. Omit empty keys if you like; an all-empty plan means "no changes"."""


def call_claude(prompt: str) -> tuple[bool, str, str]:
    """Headless Claude turn, cost-tracked via run_claude.sh (script_tag
    feedback-digest). The tag is queue-mapped in claude_job.py TAG_TO_TYPE,
    so run_claude.sh routes it through the job queue unconditionally; the
    digest is just one more job type on the same lane the drafting pipeline
    uses. No worker firing means an honest exit 79 from the seam, never a
    silent CLI fallback."""
    # The queue lane waits for the every-minute worker to claim + draft; give
    # it the same generous budget the pipeline's queued calls get.
    timeout_sec = 900
    if not os.path.exists(RUN_CLAUDE_SH):
        # Broken install. Fail loudly; a bare `claude -p` here would silently
        # bypass the queue seam and cost tracking (removed 2026-07-06 with the
        # rest of the CLI fallbacks). Events stay unprocessed for the next run.
        return False, "", f"run_claude_sh_missing: {RUN_CLAUDE_SH}"
    cmd = ["bash", RUN_CLAUDE_SH, "feedback-digest", "-p", prompt,
           "--max-turns", "1", "--disallowed-tools", DISALLOWED_TOOLS]
    empty_mcp = "/tmp/.feedback_digest_empty_mcp.json"
    try:
        if not os.path.exists(empty_mcp):
            Path(empty_mcp).write_text('{"mcpServers": {}}')
        cmd += ["--strict-mcp-config", "--mcp-config", empty_mcp]
    except Exception:
        pass
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_sec, cwd=REPO_DIR)
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            return False, out, f"rc={r.returncode}: {(r.stderr or '')[:300]}"
        if not out:
            return False, "", "empty_stdout"
        return True, out, ""
    except subprocess.TimeoutExpired:
        return False, "", f"timeout_{timeout_sec}s"
    except FileNotFoundError as e:
        return False, "", f"claude_cli_missing: {e}"


def parse_plan(text: str):
    """Extract the JSON plan from model output (tolerates code fences and
    surrounding prose). Returns dict or None."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(t[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _is_actionable(e: dict) -> bool:
    """Whether this event alone justifies burning a Claude turn: rejections,
    loved (strong) approvals, any decision carrying a typed note, and overall
    feedback. A plain approval is not a trigger; it stays unprocessed and rides
    along as evidence when the next actionable event fires a digest."""
    if e.get("decision") != "approved":
        return True
    if e.get("loved"):
        return True
    # An approval where the user rewrote the draft first is a correction,
    # as actionable as a reject (and it feeds edit_examples).
    if e.get("edited"):
        return True
    return bool((e.get("reject_note") or "").strip())


def digest_project(project: dict, platform: str, dry_run: bool,
                   overall_events: list[dict] | None = None) -> bool:
    """Digest one project's pending events (plus any overall-feedback notes,
    which ride along in every project's prompt but are marked processed by
    main(), not here). Returns True when the digest completed (or there was
    nothing to do); False leaves the events unprocessed for the next run."""
    name = project.get("name")
    overall_events = overall_events or []
    resp = api_get("/api/v1/review-events",
                   {"project": name, "platform": platform, "unprocessed": "true",
                    "limit": str(MAX_EVENTS_PER_RUN)})
    events = ((resp or {}).get("data") or {}).get("events") or []
    if not events and not overall_events:
        return True
    if not overall_events and not any(_is_actionable(e) for e in events):
        log(f"project={name} platform={platform} events={len(events)} "
            "plain_approvals_only, no digest (they ride along with the next actionable event)")
        return True
    prompt = build_prompt(project, events, overall_events)
    if dry_run:
        log(f"project={name} platform={platform} events={len(events)} overall={len(overall_events)} DRY RUN prompt below")
        print(prompt)
    ok, out, err = call_claude(prompt)
    if not ok:
        log(f"project={name} platform={platform} events={len(events)} claude_failed={err} (events left unprocessed)")
        return False
    plan = parse_plan(out)
    if plan is None:
        log(f"project={name} platform={platform} events={len(events)} plan_unparseable (events left unprocessed): {out[:200]}")
        return False
    if dry_run:
        print(json.dumps(plan, indent=2))
        log(f"project={name} platform={platform} events={len(events)} DRY RUN (nothing applied/marked)")
        return True

    # block_authors is applied through the blocklist API, not through
    # learned_preferences: pop it before apply_mutations sees the plan.
    block_authors = plan.pop("block_authors", None) or []

    event_ids = [int(e["id"]) for e in events if str(e.get("id", "")).isdigit() or isinstance(e.get("id"), int)]
    result = lp.apply_mutations(name, plan, source_event_ids=event_ids)
    if not result.get("ok"):
        log(f"project={name} platform={platform} events={len(events)} apply_failed={result.get('error')} (events left unprocessed)")
        return False

    # Few-shot edit examples (2026-07-06): every edited approval's
    # (original, final) pair is recorded deterministically, no LLM judgment;
    # the user's rewrite IS the ground truth. record_edit_examples dedups on
    # the final text, so a retried digest never double-records. Best-effort:
    # a failure here never fails the digest (the distilled plan already
    # landed and events must still be marked processed).
    edit_pairs = [
        {"original": e.get("original_text"), "final": e.get("draft_text"),
         "ts": e.get("client_ts") or e.get("created_at")}
        for e in events
        if e.get("decision") == "approved" and e.get("edited")
        and (e.get("original_text") or "").strip()
        and (e.get("draft_text") or "").strip()
        and e.get("original_text") != e.get("draft_text")
    ]
    if edit_pairs:
        try:
            rec = lp.record_edit_examples(name, edit_pairs)
            if rec.get("recorded"):
                log(f"project={name} edit_examples_recorded={rec['recorded']}")
        except Exception as e:
            log(f"project={name} edit_examples_failed={e}")

    # Author blocks the digest agent decided on (its judgment call, never
    # automatic; see the prompt). Applied via the blocklist API so the
    # discovery and reply gates enforce them. Best-effort per handle: a
    # failed POST is logged and skipped, never fails the digest.
    blocked = []
    for entry in block_authors[:10]:  # runaway-plan cap
        if not isinstance(entry, dict):
            continue
        handle = str(entry.get("handle") or "").strip().lstrip("@").lower()
        if not handle:
            continue
        reason = str(entry.get("reason") or "").strip()[:500] or "feedback digest judgment call"
        try:
            api_post("/api/v1/blocklist", {
                "platform": platform,
                "handle": handle,
                "classification": "manual_block",
                "severity": "hard",
                "reason": f"feedback digest: {reason}",
                "added_by": "feedback_digest",
                "project": name,
            }, ok_on_conflict=True)
            blocked.append(handle)
        except Exception as e:
            log(f"project={name} block_author_failed handle={handle}: {e}")
    if blocked:
        log(f"project={name} blocked_authors: {', '.join(blocked)}")

    marked = 0
    if event_ids:
        try:
            presp = api_patch("/api/v1/review-events",
                              {"ids": event_ids, "action": "mark_processed",
                               "processed_batch": f"digest-{_now_stamp()}"})
            marked = ((presp or {}).get("data") or {}).get("updated") or 0
        except Exception as e:
            log(f"project={name} mark_processed_failed={e} (idempotent: next run re-digests, apply dedups)")
    log(
        f"project={name} platform={platform} events={len(events)} "
        f"applied={len(result.get('applied') or [])} dropped={len(result.get('dropped') or [])} marked={marked}"
    )
    for change in result.get("applied") or []:
        log(f"  {change}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", help="digest only this project")
    ap.add_argument("--dry-run", action="store_true", help="print prompt+plan, change nothing")
    ap.add_argument("--min-events", type=int,
                    default=int(os.environ.get("S4L_FEEDBACK_MIN_EVENTS", "1")),
                    help="skip a project until it has this many unprocessed events")
    args = ap.parse_args()

    Path(LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    lock_f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another digest run holds the lock; exiting")
        return 0

    cfg = load_config()
    by_name = {p.get("name"): p for p in (cfg.get("projects") or [])}

    resp = api_get("/api/v1/review-events", {"counts": "true"})
    counts = ((resp or {}).get("data") or {}).get("counts") or []

    # Overall feedback (decision='feedback', project IS NULL; the card's 💬
    # button / menu bar "Send feedback…"): fetched once, folded into EVERY
    # configured project's prompt, and marked processed only after all
    # attempted digests succeed (apply_mutations dedups any re-digest).
    overall_events: list[dict] = []
    try:
        oresp = api_get("/api/v1/review-events",
                        {"unprocessed": "true", "limit": "100"})
        overall_events = [
            e for e in (((oresp or {}).get("data") or {}).get("events") or [])
            if e.get("decision") == "feedback" and not e.get("project")
        ]
    except Exception as e:
        log(f"overall_feedback_fetch_error={e}")
    if overall_events:
        log(f"overall feedback notes pending: {len(overall_events)}")

    if not counts and not overall_events:
        log("no unprocessed review events")
        return 0

    todo: dict[tuple[str, str], int] = {}
    for row in counts:
        name = row.get("project")
        if not name:
            continue  # project-less rows are the overall feedback handled above
        platform = row.get("platform") or "twitter"
        n = int(row.get("unprocessed") or 0)
        if args.project and name != args.project:
            continue
        # Explicit overall feedback shouldn't wait on the card-event threshold.
        if n < args.min_events and not overall_events:
            log(f"project={name} platform={platform} events={n} below_min={args.min_events}, waiting")
            continue
        todo[(name, platform)] = n
    if overall_events:
        for name in by_name:
            if args.project and name != args.project:
                continue
            todo.setdefault((name, "twitter"), 0)

    attempted = 0
    failures = 0
    for (name, platform), _n in todo.items():
        proj = by_name.get(name)
        if proj is None:
            log(f"project={name} not in local config, skipping (events left for the owning install)")
            continue
        attempted += 1
        try:
            if not digest_project(proj, platform, args.dry_run, overall_events):
                failures += 1
        except Exception as e:
            failures += 1
            log(f"project={name} digest_error={e}")

    if overall_events and not args.dry_run:
        if attempted and not failures:
            ids = [int(e["id"]) for e in overall_events
                   if str(e.get("id", "")).isdigit() or isinstance(e.get("id"), int)]
            try:
                presp = api_patch("/api/v1/review-events",
                                  {"ids": ids, "action": "mark_processed",
                                   "processed_batch": f"digest-overall-{_now_stamp()}"})
                marked = ((presp or {}).get("data") or {}).get("updated") or 0
                log(f"overall feedback marked processed: {marked}")
            except Exception as e:
                log(f"overall mark_processed_failed={e} (re-digested next run; apply dedups)")
        elif attempted:
            log("overall feedback left unprocessed (a project digest failed; next run retries)")
        else:
            log("overall feedback pending but no configured project to digest into; left unprocessed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
