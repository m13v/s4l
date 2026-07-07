#!/usr/bin/env python3
"""
claude_job.py — queue-backed substitute for `claude -p` for the pipeline's
pure text->JSON turns, on every machine (operator Macs and customer .mcpb
boxes alike).

The deterministic pipeline never calls `claude` directly; every invocation goes
through scripts/run_claude.sh. For script tags mapped in TAG_TO_TYPE below,
run_claude.sh delegates here instead of exec'ing the `claude` binary. The
pipeline is otherwise untouched: it enqueues the same prompt + json-schema it
would have passed to claude, blocks until a result appears, and gets back bytes
shaped exactly like claude's `--output-format json` envelope, so the existing
parsers don't change.

Four roles:
  provider  — (producer side, called by run_claude.sh) extract the prompt (stdin
              or trailing arg) + --json-schema, enqueue a typed job, BLOCK until a
              result lands, then print a claude-json-shaped envelope to stdout.
  next      — (consumer side, called by a Claude Desktop scheduled task) atomically
              claim the oldest pending job of a given type and print it as JSON.
  result    — (consumer side) store the JSON the task produced (validated) and
              unblock the waiting provider.

Queue = plain files under <state_dir>/claude-queue/. No DB, no network.
  state_dir = $S4L_STATE_DIR or ~/.social-autoposter-mcp

Job-type mapping is by run_claude.sh script_tag, and TAG_TO_TYPE is the ONLY
router (the S4L_CLAUDE_PROVIDER env var was removed 2026-07-06): run_claude.sh
asks `eligible --tag` and routes mapped tags through the queue unconditionally,
on every machine. Only PURE text->JSON calls belong in the map; unmapped tags
always run the real `claude -p`. Migrating a lane onto the queue = adding its
tag to TAG_TO_TYPE, nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid

# SAPS_->S4L_ env mirror (brand rename 2026-07-03): old launchd plists and
# scheduled-task prompts still export SAPS_*; this process reads S4L_*.
import s4l_env  # noqa: E402  (lives next to this file in scripts/)

s4l_env.mirror()

# Best-effort menu-bar activity narration. Importable because this script's own
# directory (scripts/) is on sys.path[0] when run as `python3 .../claude_job.py`.
# A failure to import (or to write) must NEVER affect the queue's real work.
try:
    import s4l_activity as _activity  # type: ignore
except Exception:  # pragma: no cover - cosmetic only
    _activity = None

# script_tag -> queue type. ONLY pure text->JSON claude calls belong here.
TAG_TO_TYPE = {
    "run-twitter-cycle-queries": "twitter-query",
    "run-twitter-cycle-prep": "twitter-prep",
    "feedback-digest": "feedback-digest",
    # Topic-invention lane (queue-native since 2026-07-06; invent_topics.py
    # pins the provider to queue itself, no env switch).
    "invent-topic": "invent-topic",
    "invent-queries": "invent-queries",
    # Tail-link bridge (queue-native since 2026-07-06; moved from a post-time
    # call in twitter_post_plan.py to a draft-time call in
    # twitter_gen_links.py's Phase 2b-gen step, which already tolerates the
    # queue worker's cadence — see scripts/link_tail.py).
    "twitter-link-tail": "twitter-link-tail",
}

# queue type -> (activity state, label) the menu bar shows while the job is in
# flight. Phase-1 queries drive the X search ("finding threads"); Phase-2b prep is
# the reply drafting. Both the launchd provider (which blocks for minutes) and the
# scheduled-task worker (which does the LLM turn) narrate from this one map.
TYPE_TO_ACTIVITY = {
    "twitter-query": ("scanning", "search"),
    "twitter-prep": ("drafting", "draft"),
    "feedback-digest": ("learning", "feedback"),
    "invent-topic": ("learning", "new topic"),
    "invent-queries": ("learning", "new queries"),
    "twitter-link-tail": ("drafting", "link bridge"),
}

# queue type -> execution notes PREPENDED to the prompt sidecar at claim time.
# This keeps the scheduled-task worker fully type-blind: its SKILL.md is one
# generic claim -> follow -> submit loop, and anything a specific job type
# needs the executor to know (pacing, persist cadence) travels WITH the job.
# The twitter-prep note exists because the host kills an unattended session
# ~90s after its LAST tool call; drafting a whole batch in one silent turn
# starves that clock (the v6 worker-prompt lesson, moved under the hood).
TYPE_TO_WORKER_NOTES = {
    "twitter-prep": (
        "WORKER EXECUTION NOTES (queue metadata; follow while executing the "
        "prompt below): this unattended session is terminated ~90 seconds after "
        "your LAST tool call. The prompt asks you to draft replies for SEVERAL "
        "candidates. Do NOT draft them all silently in one turn. Work ONE "
        "candidate at a time: draft its reply, then IMMEDIATELY run that "
        "candidate's log_draft.py persist command exactly as the prompt's "
        "persist step specifies (a quick Bash call), THEN move to the next. "
        "Those per-candidate Bash calls keep the session alive. Begin the first "
        "candidate promptly. Only after EVERY candidate is drafted and logged "
        "do you assemble and submit the single result JSON."
    ),
}


def _act_write(qtype: str) -> None:
    if _activity is None:
        return
    sl = TYPE_TO_ACTIVITY.get(qtype)
    if not sl:
        return
    try:
        _activity.write(sl[0], f"{sl[1]}…")
    except Exception:
        pass


def _act_clear() -> None:
    if _activity is None:
        return
    try:
        _activity.clear()
    except Exception:
        pass


def _fmt_dur(secs: float) -> str:
    """Compact human duration for the menu-bar label: '45s', '12m'."""
    s = int(max(0, secs))
    return f"{s}s" if s < 60 else f"{s // 60}m"


def _act_write_progress(
    qtype: str, created: float, claimed_at: float | None, now: float
) -> None:
    """Granular in-flight menu-bar label, so a wedged cycle reads as the TRUTH
    instead of a static 'drafting replies' that lingers for the whole producer
    timeout (the failure mode where the worker never claims the job, or claims it
    and dies mid-run, looked identical to healthy drafting before this).

      - job still in pending/ (no worker has claimed it) -> '<base> ⧖<dur>'
        counting from enqueue. A growing '⧖18m' is the unmistakable tell that
        a scheduled-task worker is orphaned and nothing is draining.
      - job claimed (pending file gone -> moved to running/) -> '<base> <dur>'
        counting from the claim, i.e. real drafting elapsed.

    The menu bar's stall watchdog parses the TRAILING '<n>s'/'<n>m' token out of
    this label (s4l_menubar._label_elapsed_secs), so the duration must stay the
    last number in the string whatever else changes.

    Purely cosmetic and best-effort: a write failure must never affect the queue."""
    if _activity is None:
        return
    sl = TYPE_TO_ACTIVITY.get(qtype)
    if not sl:
        return
    state, base = sl
    if claimed_at is None:
        label = f"{base} ⧖{_fmt_dur(now - created)}"
    else:
        label = f"{base} {_fmt_dur(now - claimed_at)}"
    try:
        _activity.write(state, label)
    except Exception:
        pass

# claude flags that consume the following argv token as their value, so the
# value is never mistaken for the positional prompt. The CLI accepts BOTH
# camelCase and kebab-case spellings for the tool filters; list both. Missing
# kebab spellings bit on 2026-07-03: feedback_digest.py passes
# "--disallowed-tools <list>", the parser treated it as boolean, the tools
# list became the last positional, and _parse_claude_args returned it as the
# prompt; every queue-routed digest job enqueued a tools list instead of the
# real prompt and the worker rejected it (claude_failed=rc=1 hourly).
VALUE_FLAGS = {
    "--mcp-config",
    "--json-schema",
    "--output-format",
    "--input-format",
    "--model",
    "--fallback-model",
    "--system-prompt",
    "--append-system-prompt",
    "--permission-mode",
    "--allowedTools",
    "--disallowedTools",
    "--allowed-tools",
    "--disallowed-tools",
    "--max-turns",
    "--add-dir",
    "--session-id",
    "--settings",
}

POLL_INTERVAL_S = 2.0
# Consumer-side poll cadence for `next --wait-seconds` (see cmd_next). Separate
# constant from POLL_INTERVAL_S (the producer's own wait loop) because the two
# sides have different cost profiles: the producer polls a single result file
# it's blocked on anyway, while the worker's poll re-execs claude_job.py itself
# each pass, so a slightly coarser cadence avoids needless process churn during
# a multi-minute wait.
WORKER_POLL_INTERVAL_S = 5.0
# Per-call budget the producer waits for ONE claude job (a query or a draft-prep
# reasoning turn). Was 600s, which sat right at the edge of the draft call's real
# ~9-10 min need: on the QA box ~41% of twitter-prep jobs breached 600s and got
# dropped (each drop = a lost draft AND an orphaned over-running worker that
# becomes a leaked agent-mode session). The DIRECT launchd `claude -p` lane has no
# such per-call cap — its draft call just runs inside the 180-min cycle watchdog —
# so 600s here made the queue lane diverge and silently fail where the direct lane
# would not. 1800s (30 min) = 3x the real draft need, matching the sibling Twitter
# engagement claude cap (engage-twitter Phase B gtimeout 1800), which removes the
# drops while staying a bounded per-call value (not the whole-cycle budget).
# COUPLING: reap_stale_claude_sessions.py reaps leaked workers at THIS value plus a
# fixed margin (S4L_REAPER_AGE_MARGIN_SEC, default 300s) and MUST stay > it (a lower
# reaper would SIGKILL a draft the producer is still waiting on). Both read
# S4L_CLAUDE_QUEUE_TIMEOUT and both default to 1800; keep them in lockstep if you
# change the base.
DEFAULT_TIMEOUT_S = int(os.environ.get("S4L_CLAUDE_QUEUE_TIMEOUT", "1800"))
# Jobs older than this (pending or running) are swept — a job nobody drained in
# this long is a leftover from a timed-out producer or a dead worker, and keeping
# it would feed a stale prompt to a worker much later. Default 2x the timeout.
STALE_TTL_S = int(os.environ.get("S4L_CLAUDE_QUEUE_STALE_TTL", str(DEFAULT_TIMEOUT_S * 2)))

# ---------------------------------------------------------------------------
# EXPERIMENT (2026-06-29, per user): hide the "Top Posts by Project" few-shot
# block from the Phase-2b drafting prompt.
#
# WHY: that block is ~42% of the prompt — top_performers.py emits up to 5 curated
# example posts for EVERY project (~20 projects = ~74 examples, ~16k tokens), and
# it is NOT scoped to the projects in this cycle's candidates. On a `.mcpb` box the
# drafting runs inside a Claude Desktop scheduled-task session that the app
# SIGTERMs after ~120s; the 38k-token prompt pushed the worker past that window
# before it could submit, so jobs never drained. Dropping this block shrinks the
# prompt to ~22k tokens (one Read, faster turns) while KEEPING the "Best Example
# Per Style" block (which is style-scoped and tiny).
#
# This is a DELIVERY-LAYER trim only: the generator (top_performers.py, locked) is
# untouched — we strip the section from the prompt text right before it is queued.
# A loud marker is left in its place and a provider.log line is emitted, so it is
# obvious the section was intentionally hidden, not lost.
#
# DEFAULT: ON (hidden). Set S4L_HIDE_TOP_BY_PROJECT=0 (or false/no) to restore the
# full per-project example block.
HIDE_TOP_BY_PROJECT = (
    os.environ.get("S4L_HIDE_TOP_BY_PROJECT", "1").strip().lower()
    not in ("0", "false", "no", "off", "")
)


def _strip_top_by_project(prompt: str) -> str:
    """Remove the '### Top Posts by Project' block from a drafting prompt.

    Returns the prompt with that one section replaced by a clearly-labelled
    HIDDEN marker (so anyone reading the prompt sees it was intentionally hidden
    behind S4L_HIDE_TOP_BY_PROJECT, not silently dropped). No-op if the block is
    absent (e.g. query prompts, or a report that produced no per-project posts).
    The section runs from its '### Top Posts by Project' header to the next '##'/
    '###' header (normally '### Bottom N Posts').
    """
    start = "### Top Posts by Project"
    i = prompt.find(start)
    if i < 0:
        return prompt
    m = re.search(r"\n#{2,3} ", prompt[i + len(start):])
    j = (i + len(start) + m.start() + 1) if m else len(prompt)
    marker = (
        "### Top Posts by Project — HIDDEN\n"
        "[This per-project few-shot block (~16k tokens) was hidden at the delivery "
        "layer by claude_job.py via S4L_HIDE_TOP_BY_PROJECT (default ON, added "
        "2026-06-29) so the drafting worker fits Claude Desktop's ~120s "
        "scheduled-session window. Set S4L_HIDE_TOP_BY_PROJECT=0 to restore it. "
        "The 'Best Example Per Style' block above is kept.]\n\n"
    )
    return prompt[:i] + marker + prompt[j:]



# --------------------------------------------------------------------------- #
# Queue layout                                                                #
# --------------------------------------------------------------------------- #
def _apply_state_dir_override(ns) -> None:
    """`--state-dir` wins over $S4L_STATE_DIR. The scheduled-task worker passes
    it explicitly so it always reads the SAME queue the launchd kicker writes to,
    regardless of what env the task session inherits."""
    sd = getattr(ns, "state_dir", None)
    if sd:
        os.environ["S4L_STATE_DIR"] = sd


def state_dir() -> str:
    return os.environ.get("S4L_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def queue_root() -> str:
    return os.path.join(state_dir(), "claude-queue")


def pending_dir(qtype: str) -> str:
    return os.path.join(queue_root(), "pending", qtype)


def running_dir() -> str:
    return os.path.join(queue_root(), "running")


def result_dir() -> str:
    return os.path.join(queue_root(), "result")


def heartbeat_path() -> str:
    """Single file the worker stamps each time it claims or completes a job. Its
    mtime/contents prove the scheduled-task worker is actually draining the queue
    (vs. the SKILL.md merely existing on disk, which survives a Claude account
    switch and gave a false-green). Read by the MCP's autopilot liveness check and
    the stall detector. Empty-queue ("no jobs") fires deliberately do NOT stamp it
    — we want "is a job getting DRAINED", not "did a worker tick"."""
    return os.path.join(queue_root(), "worker-heartbeat.json")


def _stamp_heartbeat(event: str, qtype: str | None = None) -> None:
    """Best-effort: never let a heartbeat write failure break the queue."""
    try:
        os.makedirs(queue_root(), exist_ok=True)
        _atomic_write(
            heartbeat_path(),
            {"at": time.time(), "event": event, "type": qtype or ""},
        )
    except Exception:
        pass


def drain_status_path() -> str:
    """LATCHED autopilot-liveness marker the producer maintains: how many times in
    a row it has enqueued a job and timed out with NO worker draining it. Unlike a
    pending-job age check, this persists across the gaps between cycles (the
    producer removes the job on timeout, so there's no pending file to look at
    between cycles) — so the menu bar / dashboard / Sentry watcher can show a
    CONTINUOUS stall instead of one that flickers off every time a job is removed.
    Cleared (consecutive_timeouts=0) the moment a draft actually drains."""
    return os.path.join(queue_root(), "drain-status.json")


def _read_drain_status() -> dict:
    try:
        with open(drain_status_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _mark_drain_success() -> None:
    """A job drained successfully -> clear the latched stall."""
    try:
        os.makedirs(queue_root(), exist_ok=True)
        _atomic_write(
            drain_status_path(),
            {"consecutive_timeouts": 0, "last_success_at": time.time()},
        )
    except Exception:
        pass


def _bump_drain_timeout() -> None:
    """The producer gave up waiting -> latch/escalate the stall."""
    try:
        os.makedirs(queue_root(), exist_ok=True)
        cur = _read_drain_status()
        prev = int(cur.get("consecutive_timeouts", 0) or 0)
        cur["consecutive_timeouts"] = prev + 1
        cur["last_timeout_at"] = time.time()
        _atomic_write(drain_status_path(), cur)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Opt-in worker self-reap (2026-06-27)                                         #
# --------------------------------------------------------------------------- #
# A scheduled-task worker turn finishes its one queue iteration but Claude
# Desktop keeps the agent-mode `claude` process warm (`--input-format
# stream-json`), so finished workers pile up and leak RAM. The launchd reaper
# (reap_stale_claude_sessions.py) is the GUARANTEE that bounds this. This opt-in
# path is a faster, source-side trim: once THIS worker is provably done (no work
# to do, or its result is already on disk), terminate OUR OWN session so it never
# becomes part of the standing pool. Strictly off unless S4L_WORKER_SELF_REAP is
# set, so it ships dormant and cannot destabilize the default behaviour.
#
# Safety properties:
#   * No-op unless the env flag is set.
#   * Only ever targets a process in OUR OWN ancestry that matches the reaper's
#     worker signature (claude-code agent-mode session). The producer side
#     (run-twitter-cycle.sh -> python) has no such ancestor, so a misplaced call
#     there finds nothing and does nothing.
#   * Detached + delayed: a double-forked grandchild waits a few seconds (so the
#     current turn returns and prints its final line normally) before signalling.
#   * Re-verifies the target's cmdline right before SIGTERM, so a recycled PID is
#     never signalled.
#   * Best-effort throughout: never raises into the caller, never changes the
#     worker's exit code, never touches the result already written to disk.
# LOOSE ancestry probe (2026-07-04). The original tuple duplicated the reaper's
# strict cmdline signature, which Claude Desktop 1.18286.0 broke (Karol's second
# leak: 53 workers piled up while both the reaper AND this self-reap failed the
# same parse). Inside our OWN ancestry the strict fingerprint is unnecessary:
# identification is DETERMINISTIC by construction — claude_job runs as a Bash
# child of the session that invoked it, so the nearest claude-code agent-mode
# ancestor IS that session. What the loose probe cannot tell apart is a
# SCHEDULED WORKER session vs an INTERACTIVE session where someone ran
# `claude_job next` by hand — that discrimination comes from the cwd gate in
# _maybe_self_reap (worker tasks run in ~/.s4l-worker; interactive sessions
# never do), not from cmdline shape.
_SELF_REAP_SIG = (
    "claude-code/",
    "--input-format stream-json",
)
_S4L_WORKER_CWD = os.path.join(os.path.expanduser("~"), ".s4l-worker")


def _self_reap_enabled() -> bool:
    # Default ON since 2026-07-04 (v1.6.202): the dormant flag meant the
    # source-side trim never ran anywhere, leaving the (signature-fragile)
    # launchd reaper as the only defense — and when Desktop changed the cmdline
    # shape, boxes leaked. Opt OUT with S4L_WORKER_SELF_REAP=0.
    return os.environ.get("S4L_WORKER_SELF_REAP", "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _ps_pid_map() -> dict:
    """pid -> (ppid, command) for every process. Empty dict on any failure."""
    out: dict = {}
    try:
        res = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return out
    for line in res.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        out[pid] = (ppid, parts[2])
    return out


def _find_own_session(psmap: dict):
    """Walk OUR ancestry to the nearest claude-code agent-mode ancestor.
    Returns (pid, reverify_token) or None. The token is a stable cmdline prefix
    used to confirm the PID was not recycled before signalling — no UUID/path
    shape assumptions, so a Desktop cmdline change cannot blind this again."""
    pid = os.getpid()
    seen: set = set()
    for _ in range(40):  # bounded climb up the process tree
        if pid in seen:
            break
        seen.add(pid)
        ent = psmap.get(pid)
        if not ent:
            break
        ppid, cmd = ent
        if all(t in cmd for t in _SELF_REAP_SIG) and "Helpers/disclaimer" not in cmd:
            return pid, cmd[:160]
        pid = ppid
        if pid <= 1:
            break
    return None


def _maybe_self_reap(delay: float = 6.0) -> None:
    """Terminate our own finished worker session. See block comment above.

    The cwd gate is the worker-vs-interactive discriminator: scheduled worker
    tasks run with cwd ~/.s4l-worker (enforced at task creation + menubar cwd
    rewrite), while an interactive/debug session that shells `claude_job` runs
    in a project dir. Interactive sessions are therefore never signalled, no
    matter what their cmdline looks like."""
    if not _self_reap_enabled():
        return
    try:
        cwd = os.getcwd()
        if cwd != _S4L_WORKER_CWD and not cwd.startswith(_S4L_WORKER_CWD + os.sep):
            return
        found = _find_own_session(_ps_pid_map())
        if not found:
            return
        target_pid, token = found
        if os.fork() != 0:
            return  # the worker continues + exits normally
    except Exception:
        return
    # child -> detach into its own session, then exit, orphaning the grandchild
    try:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
    except Exception:
        os._exit(0)
    # grandchild (fully detached): wait, re-verify, signal
    try:
        time.sleep(delay)
        cur = _ps_pid_map().get(target_pid)
        if cur and token in cur[1]:  # same session, not a recycled PID
            try:
                os.kill(target_pid, signal.SIGTERM)
            except OSError:
                pass
    except Exception:
        pass
    os._exit(0)


def _ensure_dirs(qtype: str | None = None) -> None:
    for d in (running_dir(), result_dir()):
        os.makedirs(d, exist_ok=True)
    if qtype:
        os.makedirs(pending_dir(qtype), exist_ok=True)


def _atomic_write(path: str, obj) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _atomic_write_text(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _sweep_stale() -> int:
    """Remove pending/running job files older than STALE_TTL_S. Returns count."""
    removed = 0
    now = time.time()
    roots = [running_dir()]
    pend = os.path.join(queue_root(), "pending")
    if os.path.isdir(pend):
        roots += [os.path.join(pend, d) for d in os.listdir(pend)]
    for d in roots:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            fp = os.path.join(d, name)
            try:
                with open(fp) as f:
                    created = json.load(f).get("created_at", 0)
                if now - float(created) > STALE_TTL_S:
                    os.remove(fp)
                    removed += 1
            except Exception:
                continue
    return removed


# --------------------------------------------------------------------------- #
# provider (producer side, run by run_claude.sh)                              #
# --------------------------------------------------------------------------- #
def _parse_claude_args(args: list[str]) -> tuple[str | None, str | None]:
    """Return (trailing_prompt, schema_path) from the verbatim claude argv."""
    schema_path = None
    positionals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json-schema":
            schema_path = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a in VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1  # boolean flag (-p, --strict-mcp-config, --verbose, ...)
            continue
        positionals.append(a)
        i += 1
    prompt = positionals[-1] if positionals else None
    return prompt, schema_path


def _plog(msg: str) -> None:
    """Provider diagnostics go to a log file, NEVER stderr.

    The pipeline captures this wrapper's output with `2>&1` and parses the FIRST
    JSON value (raw_decode). Anything we print to stderr BEFORE the envelope (e.g.
    an "enqueued, waiting" line) lands ahead of the JSON and breaks the parse with
    "Expecting value: line 1 column 2". So stdout carries ONLY the final envelope
    and stderr stays silent; humans read provider.log instead. (fix 2026-06-24)
    """
    try:
        os.makedirs(queue_root(), exist_ok=True)
        with open(os.path.join(queue_root(), "provider.log"), "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def cmd_provider(ns) -> int:
    _apply_state_dir_override(ns)
    qtype = TAG_TO_TYPE.get(ns.tag)
    if not qtype:
        # Not a queue-eligible call. Exit non-zero so run_claude.sh's caller
        # treats it as a claude failure and runs its own fallback path.
        _plog(f"tag '{ns.tag}' is not queue-eligible; no provider")
        return 1

    stdin_text = ""
    if not sys.stdin.isatty():
        try:
            stdin_text = sys.stdin.read()
        except Exception:
            stdin_text = ""

    trailing_prompt, schema_path = _parse_claude_args(ns.claude_args)
    prompt = stdin_text if stdin_text.strip() else (trailing_prompt or "")
    if not prompt.strip():
        _plog("empty prompt; nothing to enqueue")
        return 1

    schema_text = None
    if schema_path and os.path.exists(schema_path):
        try:
            with open(schema_path) as f:
                schema_text = f.read()
        except Exception:
            schema_text = None

    # Delivery-layer trim: hide the "Top Posts by Project" block from drafting
    # prompts so the worker fits the ~120s scheduled-session window. See
    # HIDE_TOP_BY_PROJECT / _strip_top_by_project above. Drafting jobs only.
    if qtype == "twitter-prep" and HIDE_TOP_BY_PROJECT:
        _before_len = len(prompt)
        prompt = _strip_top_by_project(prompt)
        if len(prompt) != _before_len:
            _plog(
                "hid 'Top Posts by Project' block: -%d chars "
                "(S4L_HIDE_TOP_BY_PROJECT on; set =0 to restore)"
                % (_before_len - len(prompt))
            )

    job_id = uuid.uuid4().hex
    created = time.time()
    # Cycle batch id (run-twitter-cycle.sh exports BATCH_ID) so every provider.log
    # line correlates to the cycle's own twitter-cycle-<batch>.log. '-' when off-cycle.
    batch = (os.environ.get("BATCH_ID") or os.environ.get("SA_CYCLE_ID") or "-").strip() or "-"
    _ensure_dirs(qtype)
    _sweep_stale()  # clear leftovers from prior timed-out producers before enqueuing
    job = {
        "job_id": job_id,
        "type": qtype,
        "tag": ns.tag,
        "prompt": prompt,
        "schema": schema_text,
        "created_at": created,
    }
    # Filename is <created_ns>_<job_id>.json so a plain sorted() listing is FIFO.
    fname = f"{int(created * 1e9):020d}_{job_id}.json"
    pending_path = os.path.join(pending_dir(qtype), fname)
    running_path = os.path.join(running_dir(), fname)
    _atomic_write(pending_path, job)
    _plog(f"enqueued {qtype} job {job_id} batch={batch}; waiting for a scheduled task (timeout {ns.timeout}s)")
    # Narrate the (multi-minute) block to the menu bar. The launchd draft lane has
    # no other activity writer, so without this the box looks idle while it works.
    # Cleared by run-draft-and-publish.sh's exit trap at cycle end (and by the
    # worker's cmd_result), so we deliberately do NOT clear on the success path
    # here — that would flicker the indicator off between the cycle's claude calls.
    _act_write(qtype)

    res_path = os.path.join(result_dir(), f"{job_id}.json")
    deadline = created + ns.timeout
    last_hb = created  # last menu-bar heartbeat (see below)
    claimed_at = None  # set the moment a worker moves the job pending/ -> running/
    while time.time() < deadline:
        now = time.time()
        # A worker claims a job by atomically renaming pending/ -> running/, so the
        # pending file vanishing is our signal that drafting actually STARTED (vs.
        # the job still sitting unclaimed). Latch the claim time once so the label
        # can distinguish "waiting for a worker" from "worker is drafting" and show
        # the right elapsed for each.
        if claimed_at is None and not os.path.exists(pending_path):
            claimed_at = now
        # Heartbeat the menu-bar label so its `since` stays fresh for the whole
        # multi-minute block. The consumer (s4l_state.read_activity) ages a label
        # out after a TTL, so without this refresh a long drafting turn would look
        # stale and the spinner would wrongly blink to idle. Refreshing here means
        # the label is fresh EXACTLY while real work is happening, and stops the
        # instant we return or die — so the consumer's TTL can then expire it
        # instead of it freezing forever. Throttled to ~10s; best-effort only. The
        # label now carries claim-state + elapsed so a stuck cycle reads honestly
        # ("queued 18m") instead of a reassuring static "drafting replies".
        if now - last_hb >= 10:
            _act_write_progress(qtype, created, claimed_at, now)
            last_hb = now
        if os.path.exists(res_path):
            try:
                with open(res_path) as f:
                    res = json.load(f)
            except Exception:
                time.sleep(POLL_INTERVAL_S)
                continue
            os.remove(res_path)
            if res.get("status") == "error":
                _plog(f"job {job_id} returned error: {res.get('error', 'unknown')}")
                return 1
            obj = res.get("result")
            # Emit a claude `--output-format json` shaped envelope so the
            # pipeline's existing raw_decode + structured_output/result parser
            # is byte-compatible.
            envelope = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": obj,
                "result": json.dumps(obj) if not isinstance(obj, str) else obj,
            }
            sys.stdout.write(json.dumps(envelope))
            sys.stdout.flush()
            # A worker drained this job -> the autopilot is alive; clear any latched
            # stall so the menu bar / dashboard / Sentry watcher recover.
            _mark_drain_success()
            # Success-consume event: the SILENT gap that made an orphaned result
            # (worker wrote it, producer died before consuming) indistinguishable
            # from a healthy one. A result file only survives in result/ if it was
            # NEVER consumed (we os.remove above), so "consumed" here + a surviving
            # file = the orphan signature the salvage reconciler keys on.
            try:
                _ncand = len(obj.get("candidates")) if isinstance(obj, dict) and isinstance(obj.get("candidates"), list) else "?"
            except Exception:
                _ncand = "?"
            _plog(f"consumed result for job {job_id} batch={batch} ({qtype}); {_ncand} candidates -> producer assembles the plan")
            return 0
        time.sleep(POLL_INTERVAL_S)

    # Don't leak the job: remove it from pending/running so it can't be drafted
    # later with a stale prompt (and so /tmp doesn't accumulate stuck jobs).
    for p in (pending_path, running_path):
        try:
            os.remove(p)
        except OSError:
            pass
    # We gave up waiting for a worker — drop the "drafting" menu-bar label we kept
    # re-asserting while blocked. Otherwise it lingers and the menu bar shows
    # "drafting replies" forever (masking the autopilot-stalled ⚠) even though no
    # routine ever claimed the job.
    _act_clear()
    # Latch the stall so it persists across the gap until the next cycle enqueues
    # (no pending file exists between cycles, so an instantaneous queue check would
    # flicker the ⚠ off). Cleared only when a draft actually drains.
    _bump_drain_timeout()
    _plog(f"timed out after {ns.timeout}s waiting for job {job_id} batch={batch} ({qtype}); removed the job")
    return 79  # mirror run_claude.sh's "blocked, skip cleanly" exit code


# --------------------------------------------------------------------------- #
# next (consumer side, run by a scheduled task)                               #
# --------------------------------------------------------------------------- #
def _agent_session_pid():
    """Best-effort: the Claude agent-mode SESSION pid running THIS worker — the
    exact process the stale-session reaper (reap_stale_claude_sessions.py) would
    target. We climb our own process tree to the ancestor whose cmd carries the
    reaper's worker signature ('claude-code/' + 'local-agent-mode-sessions') and
    return its pid, so the claim can be stamped with it and the reaper can SPARE
    that session for the whole drafting turn (instead of SIGTERMing it at the short
    grace window — the 2026-06-29 draft-kill regression). None if not identifiable;
    the reaper then falls back to its newest-spare heuristic.
    """
    try:
        out = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=10,
        ).stdout
        info = {}
        for line in out.splitlines():
            m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
            if m:
                info[int(m.group(1))] = (int(m.group(2)), m.group(3))
        pid = os.getpid()
        for _ in range(16):  # bounded climb up the tree
            ent = info.get(pid)
            if not ent or ent[0] <= 1:
                break
            ppid = ent[0]
            pcmd = info.get(ppid, (0, ""))[1]
            if ("claude-code/" in pcmd) and ("local-agent-mode-sessions" in pcmd):
                return ppid
            pid = ppid
    except Exception:
        return None
    return None


def _attempt_claim(ns, qtype: str) -> bool:
    """One pass over the pending dirs: try to claim the oldest job. Returns True
    (and prints the claimed job's payload) iff a job was claimed; False if the
    queue was empty this pass. Split out of cmd_next so the poll loop below can
    call it repeatedly without duplicating the claim/stamp/print logic."""
    # "any" (the universal type-blind worker) scans EVERY pending type dir;
    # a comma list scans those types; a single type keeps legacy behavior.
    # Job filenames start with a zero-padded nanosecond timestamp, so one
    # global lexicographic sort is oldest-first across types.
    if qtype == "any":
        _ensure_dirs()
        root = os.path.join(queue_root(), "pending")
        try:
            scan_types = sorted(
                d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))
            )
        except FileNotFoundError:
            scan_types = []
    else:
        scan_types = [t.strip() for t in qtype.split(",") if t.strip()]
        for t in scan_types:
            _ensure_dirs(t)
    entries = []
    for t in scan_types:
        pend = pending_dir(t)
        try:
            for name in os.listdir(pend):
                entries.append((name, pend))
        except FileNotFoundError:
            continue
    entries.sort(key=lambda e: e[0])
    for name, pend in entries:
        if not name.endswith(".json") or name.endswith(".tmp"):
            continue
        src = os.path.join(pend, name)
        dst = os.path.join(running_dir(), name)
        try:
            os.rename(src, dst)  # atomic claim; loser of a race gets FileNotFound
        except FileNotFoundError:
            continue
        try:
            with open(dst) as f:
                job = json.load(f)
        except Exception:
            continue
        # Stamp the agent-session pid that holds THIS claim so the reaper spares it
        # for the whole drafting turn (see _agent_session_pid above).
        agent_pid = _agent_session_pid()
        if agent_pid:
            job["claim_pid"] = agent_pid
        job["claimed_at"] = time.time()
        prompt_file = None
        schema_file = None
        if ns.prompt_file:
            prompt_file = os.path.join(queue_root(), f"prompt-{job['job_id']}.md")
            prompt_body = job.get("prompt") or ""
            # Per-type execution notes ride in the sidecar, not the worker
            # prompt, so the worker stays type-blind (see TYPE_TO_WORKER_NOTES).
            notes = TYPE_TO_WORKER_NOTES.get(job.get("type") or "")
            if notes:
                prompt_body = f"{notes}\n\n---\n\n{prompt_body}"
            _atomic_write_text(prompt_file, prompt_body)
            job["prompt_file"] = prompt_file
            schema = job.get("schema")
            if schema:
                schema_file = os.path.join(queue_root(), f"schema-{job['job_id']}.json")
                _atomic_write_text(schema_file, schema)
                job["schema_file"] = schema_file
        # ALWAYS persist the claim back (claim_pid + any prompt/schema sidecars) so
        # the reaper can read claim_pid; previously this only happened on the
        # --prompt-file lane, leaving claim_pid unstamped for inline callers.
        _atomic_write(dst, job)
        _plog(
            f"claimed {job.get('type') or qtype} job {job['job_id']}; "
            + (f"agent-session pid={agent_pid} stamped (reaper will spare it)"
               if agent_pid else
               "agent-session pid NOT found (reaper falls back to newest-spare)")
        )
        # Narrate the scheduled-task worker's drafting turn to the menu bar. This
        # is the lane that actually runs the LLM; it persists until cmd_result
        # clears it (or the kicker's exit trap does). Covers the box's autopilot.
        _act_write(job.get("type") or qtype)
        # Liveness pulse: a routine actually claimed a job. Proves the worker is
        # firing, not just that its SKILL.md exists (see heartbeat_path()).
        _stamp_heartbeat("claim", job.get("type") or qtype)
        # Hand the consumer exactly what it needs to do the work and report back.
        payload = {"job_id": job["job_id"], "type": job["type"]}
        if ns.prompt_file:
            payload["prompt_file"] = prompt_file
            payload["schema_file"] = schema_file
        else:
            payload["prompt"] = job["prompt"]
            payload["schema"] = job.get("schema")
        print(json.dumps(payload))
        return True
    return False


def cmd_next(ns) -> int:
    _apply_state_dir_override(ns)
    qtype = ns.type
    wait_seconds = max(0, ns.wait_seconds or 0)
    # wait_seconds=0 (default) is the legacy single-shot behavior: one pass,
    # then done. wait_seconds>0 polls in a bounded loop within THIS ONE process
    # (one Bash call from the calling session's perspective) instead of relying
    # on the scheduled task's own cron cadence to re-check. A finished-but-empty
    # pass sleeps WORKER_POLL_INTERVAL_S and tries again until the deadline.
    #
    # COUPLING: the reaper's claim_grace (S4L_REAPER_CLAIM_GRACE_SEC in
    # reap_stale_claude_sessions.py) must stay >= whatever --wait-seconds the
    # worker prompt actually passes, plus margin — a claimless session polling
    # inside this loop is legitimate, not a husk, and a too-tight claim_grace
    # would SIGTERM it mid-poll before it ever gets a chance to claim.
    deadline = time.time() + wait_seconds
    while True:
        if _attempt_claim(ns, qtype):
            return 0
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(WORKER_POLL_INTERVAL_S, remaining))
    print(json.dumps({}))  # no work found within the wait window
    _maybe_self_reap()  # idle turn, no job claimed — safe to retire this session
    return 0


# --------------------------------------------------------------------------- #
# result (consumer side, run by a scheduled task)                             #
# --------------------------------------------------------------------------- #
def _validate_against_schema(obj, schema_text: str | None) -> str | None:
    """Lenient validation. Returns an error string or None if acceptable.

    We deliberately avoid a jsonschema dependency (not guaranteed on the box).
    Enforce only what matters: the result is a JSON object and carries the
    schema's top-level required keys. The prompt itself describes the full shape.
    """
    if schema_text:
        try:
            schema = json.loads(schema_text)
        except Exception:
            schema = None
        if isinstance(schema, dict):
            if schema.get("type") == "object" and not isinstance(obj, dict):
                return "result must be a JSON object"
            required = schema.get("required")
            if isinstance(required, list) and isinstance(obj, dict):
                missing = [k for k in required if k not in obj]
                if missing:
                    return f"result missing required keys: {missing}"
    return None


def cmd_result(ns) -> int:
    _apply_state_dir_override(ns)
    _ensure_dirs()
    # The worker's drafting turn ends here (success or failure); drop the menu-bar
    # label so nothing lingers. The provider's next enqueue re-asserts the right
    # label for the cycle's following claude call, if any.
    _act_clear()
    # Liveness pulse: a routine completed a drain. Keeps the heartbeat fresh
    # across the whole claim->result span the worker was alive.
    _stamp_heartbeat("result")
    job_id = ns.job
    # Read the produced result (JSON object) from a file or stdin.
    if ns.result_file and ns.result_file != "-":
        with open(ns.result_file) as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()
    raw = raw.strip()

    running = None
    # Locate the claimed job to recover its schema (filename carries job_id).
    schema_text = None
    cleanup_files: list[str] = []
    try:
        for name in os.listdir(running_dir()):
            if name.endswith(f"_{job_id}.json"):
                with open(os.path.join(running_dir(), name)) as f:
                    job = json.load(f)
                    schema_text = job.get("schema")
                    cleanup_files = [
                        p for p in (job.get("prompt_file"), job.get("schema_file")) if p
                    ]
                running = os.path.join(running_dir(), name)
                break
    except FileNotFoundError:
        running = None

    if ns.error:
        _atomic_write(
            os.path.join(result_dir(), f"{job_id}.json"),
            {"status": "error", "error": raw or "unspecified"},
        )
        if running and os.path.exists(running):
            os.remove(running)
        for p in cleanup_files:
            try:
                os.remove(p)
            except OSError:
                pass
        print(f"[claude_job] recorded error for job {job_id}", file=sys.stderr)
        _maybe_self_reap()  # error recorded, turn done — safe to retire this session
        return 0

    try:
        obj = json.loads(raw)
    except Exception as e:
        print(
            f"[claude_job] result for job {job_id} is not valid JSON: {e}",
            file=sys.stderr,
        )
        return 2

    err = _validate_against_schema(obj, schema_text)
    if err:
        print(f"[claude_job] result for job {job_id} rejected: {err}", file=sys.stderr)
        return 3

    _atomic_write(
        os.path.join(result_dir(), f"{job_id}.json"),
        {"status": "done", "result": obj},
    )
    if running and os.path.exists(running):
        os.remove(running)
    for p in cleanup_files:
        try:
            os.remove(p)
        except OSError:
            pass
    print(f"[claude_job] stored result for job {job_id}", file=sys.stderr)
    _maybe_self_reap()  # result delivered to disk — safe to retire this session
    return 0


def cmd_eligible(ns: argparse.Namespace) -> int:
    """Routing probe for run_claude.sh: exit 0 when the tag is queue-mapped,
    1 otherwise. TAG_TO_TYPE is the single routing truth — no env var."""
    return 0 if ns.tag in TAG_TO_TYPE else 1


def main() -> int:
    p = argparse.ArgumentParser(description="claude -p queue shim")
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("eligible", help="exit 0 iff --tag is queue-mapped (router probe)")
    pe.add_argument("--tag", required=True)
    pe.set_defaults(func=cmd_eligible)

    pp = sub.add_parser("provider", help="enqueue + block-poll (run by run_claude.sh)")
    pp.add_argument("--tag", required=True)
    pp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    pp.add_argument("--state-dir", default=None, help="override $S4L_STATE_DIR")
    pp.add_argument("claude_args", nargs=argparse.REMAINDER)
    pp.set_defaults(func=cmd_provider)

    pn = sub.add_parser("next", help="claim oldest pending job of a type")
    pn.add_argument("--type", required=True)
    pn.add_argument("--state-dir", default=None, help="override $S4L_STATE_DIR")
    pn.add_argument(
        "--prompt-file",
        action="store_true",
        help="write the prompt/schema to sidecar files and print their paths",
    )
    pn.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="poll for a job up to this many seconds before giving up "
        "(0 = legacy single-shot: check once and return immediately)",
    )
    pn.set_defaults(func=cmd_next)

    pr = sub.add_parser("result", help="store a job's result")
    pr.add_argument("--job", required=True)
    pr.add_argument("--result-file", default="-", help="path to JSON, or - for stdin")
    pr.add_argument("--error", action="store_true", help="record a failure")
    pr.add_argument("--state-dir", default=None, help="override $S4L_STATE_DIR")
    pr.set_defaults(func=cmd_result)

    ns = p.parse_args()
    # argparse.REMAINDER keeps a leading "--"; drop it.
    if getattr(ns, "claude_args", None) and ns.claude_args and ns.claude_args[0] == "--":
        ns.claude_args = ns.claude_args[1:]
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
