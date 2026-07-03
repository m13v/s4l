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

from http_api import api_get, api_patch  # noqa: E402
import learned_preferences as lp  # noqa: E402

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_CLAUDE_SH = os.path.join(REPO_DIR, "scripts", "run_claude.sh")
LOCK_PATH = os.path.expanduser("~/.social-autoposter-mcp/feedback-digest.lock")
MAX_EVENTS_PER_RUN = 200
CLAUDE_TIMEOUT_SEC = 180

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
    parts = [f"[{e.get('decision')}]"]
    if e.get("reject_category"):
        parts.append(f"category={e['reject_category']}")
    if e.get("thread_author"):
        parts.append(f"author=@{e['thread_author']}")
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
    draft = (e.get("draft_text") or "").strip()
    if draft:
        line += f"\n  draft was: {draft[:200]}"
    url = (e.get("thread_url") or "").strip()
    if url:
        line += f"\n  thread: {url}"
    return line


def build_prompt(project: dict, events: list[dict]) -> str:
    block = lp.get_block(project)
    rejected = [e for e in events if e.get("decision") == "rejected"]
    approved = [e for e in events if e.get("decision") == "approved"]
    voice_never = ((project.get("voice") or {}).get("never")) or []
    guard_do_not = ((project.get("content_guardrails") or {}).get("do_not")) or []

    ev_lines = "\n".join(f"{i + 1}. {_event_line(e)}" for i, e in enumerate(events))

    return f"""You maintain the learned_preferences block for the project "{project.get('name')}" in a social-posting pipeline. The block distills the user's own approve/reject decisions on draft cards into short standing preferences that steer future thread selection and drafting. It is SOFT guidance read by the drafting model, not a filter.

CURRENT learned_preferences:
{json.dumps({k: block[k] for k in ("audience_avoid", "audience_prefer", "thread_avoid", "draft_style_notes")}, indent=2)}

CURRENT voice.never: {json.dumps(voice_never)}
CURRENT content_guardrails.do_not: {json.dumps(guard_do_not)}

NEW REVIEW EVENTS since the last digest ({len(rejected)} rejected, {len(approved)} approved):
{ev_lines}

Categories: wrong_author = the thread's author/audience was a bad fit; off_topic = the thread itself was a bad fit; bad_draft = thread was fine but the written reply was off; other = see the note. "user_checked=profile_click" means the user opened the author's profile before deciding (a strong author-quality signal even without a note).

Propose changes to the block. RULES, in priority order:
1. Be conservative. Prefer NO changes over speculative ones. An empty plan is a good plan when the evidence is thin.
2. Generalize only what the evidence supports: 2+ events agreeing justify a general entry; a single reject justifies at most one narrowly-scoped entry, and only when its note or interactions make the reason explicit.
3. Describe author/audience TYPES, never individual handles. "crypto/web3-native accounts shilling tokens" is right; "@someguy" is wrong. Preferences must generalize.
4. Approvals are counter-evidence. If approvals contradict an existing entry, propose removing or narrowing it. Also propose removing entries that events show are stale.
5. bad_draft events feed draft_style_notes (or, ONLY for a clearly recurring phrasing complaint, voice_never_add / guardrails_do_not_add; use those sparingly, they touch curated fields).
6. Each entry: one sentence, under 200 characters, plain language, no em dashes, no hashtags, understandable a month from now without these events.
7. Respect the cap: at most {lp.MAX_ENTRIES_PER_LIST} entries per list. If a list is full, fold the new signal into an existing entry via remove+add.

OUTPUT: a single JSON object, nothing else. Schema:
{{"changes": {{"audience_avoid": {{"add": [], "remove": []}}, "audience_prefer": {{"add": [], "remove": []}}, "thread_avoid": {{"add": [], "remove": []}}, "draft_style_notes": {{"add": [], "remove": []}}}}, "voice_never_add": [], "guardrails_do_not_add": [], "rationale": "one short sentence"}}
"remove" values must match existing entries EXACTLY. Omit empty keys if you like; an all-empty plan means "no changes"."""


def _provider_env() -> dict:
    """Route the Claude turn through the local job queue (drained by the
    saps-worker Claude Desktop scheduled task) whenever that worker is actually
    firing; otherwise leave the provider unset so run_claude.sh execs the
    claude CLI directly (operator Macs). An explicit SAPS_CLAUDE_PROVIDER in
    the environment always wins. This is the same queue lane the drafting
    pipeline uses — the digest is just one more job type on it."""
    env = dict(os.environ)
    if env.get("SAPS_CLAUDE_PROVIDER"):
        return env
    try:
        import schedule_state

        if schedule_state.compute() == "ok":
            env["SAPS_CLAUDE_PROVIDER"] = "queue"
    except Exception:
        pass
    return env


def call_claude(prompt: str) -> tuple[bool, str, str]:
    """Headless Claude turn, cost-tracked via run_claude.sh (script_tag
    feedback-digest). Queue-routed when a worker is firing (see _provider_env);
    otherwise mirrors scripts/link_tail.py call_claude()."""
    env = _provider_env()
    queued = env.get("SAPS_CLAUDE_PROVIDER") == "queue"
    # Queue lane waits for the every-minute worker to claim + draft; give it
    # the same generous budget the pipeline's queued calls get.
    timeout_sec = 900 if queued else CLAUDE_TIMEOUT_SEC
    if os.path.exists(RUN_CLAUDE_SH):
        cmd = ["bash", RUN_CLAUDE_SH, "feedback-digest", "-p", prompt,
               "--max-turns", "1", "--disallowed-tools", DISALLOWED_TOOLS]
    else:
        cmd = ["claude", "-p", prompt, "--max-turns", "1",
               "--disallowed-tools", DISALLOWED_TOOLS]
    empty_mcp = "/tmp/.feedback_digest_empty_mcp.json"
    try:
        if not os.path.exists(empty_mcp):
            Path(empty_mcp).write_text('{"mcpServers": {}}')
        cmd += ["--strict-mcp-config", "--mcp-config", empty_mcp]
    except Exception:
        pass
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_sec, cwd=REPO_DIR, env=env)
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


def digest_project(project: dict, platform: str, dry_run: bool) -> None:
    name = project.get("name")
    resp = api_get("/api/v1/review-events",
                   {"project": name, "platform": platform, "unprocessed": "true",
                    "limit": str(MAX_EVENTS_PER_RUN)})
    events = ((resp or {}).get("data") or {}).get("events") or []
    if not events:
        return
    prompt = build_prompt(project, events)
    if dry_run:
        log(f"project={name} platform={platform} events={len(events)} DRY RUN prompt below")
        print(prompt)
    ok, out, err = call_claude(prompt)
    if not ok:
        log(f"project={name} platform={platform} events={len(events)} claude_failed={err} (events left unprocessed)")
        return
    plan = parse_plan(out)
    if plan is None:
        log(f"project={name} platform={platform} events={len(events)} plan_unparseable (events left unprocessed): {out[:200]}")
        return
    if dry_run:
        print(json.dumps(plan, indent=2))
        log(f"project={name} platform={platform} events={len(events)} DRY RUN (nothing applied/marked)")
        return

    event_ids = [int(e["id"]) for e in events if str(e.get("id", "")).isdigit() or isinstance(e.get("id"), int)]
    result = lp.apply_mutations(name, plan, source_event_ids=event_ids)
    if not result.get("ok"):
        log(f"project={name} platform={platform} events={len(events)} apply_failed={result.get('error')} (events left unprocessed)")
        return
    marked = 0
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", help="digest only this project")
    ap.add_argument("--dry-run", action="store_true", help="print prompt+plan, change nothing")
    ap.add_argument("--min-events", type=int,
                    default=int(os.environ.get("SAPS_FEEDBACK_MIN_EVENTS", "1")),
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
    if not counts:
        log("no unprocessed review events")
        return 0

    for row in counts:
        name = row.get("project")
        platform = row.get("platform") or "twitter"
        n = int(row.get("unprocessed") or 0)
        if args.project and name != args.project:
            continue
        if n < args.min_events:
            log(f"project={name} platform={platform} events={n} below_min={args.min_events}, waiting")
            continue
        proj = by_name.get(name)
        if proj is None:
            log(f"project={name} not in local config, skipping (events left for the owning install)")
            continue
        try:
            digest_project(proj, platform, args.dry_run)
        except Exception as e:
            log(f"project={name} digest_error={e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
