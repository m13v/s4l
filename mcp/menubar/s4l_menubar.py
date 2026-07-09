"""S4L menu bar app — a tiny live mini-dashboard for social-autoposter.

A status-bar companion that mirrors the in-chat dashboard's three states, but
much smaller: the menu bar title carries the at-a-glance state and the dropdown
is a flat native list. It NEVER duplicates pipeline logic — it reads state via
s4l_state (loopback tools when Claude Desktop is up, raw state files when it's
down).

The one capability it cannot have is injecting a prompt into the Claude Desktop
chat (that bridge only exists for the inline panel iframe). So the model-driven
actions (Set up, Re-arm schedule) degrade to copying the prompt to the clipboard
+ focusing Claude Desktop; the no-model actions (open dashboard) work standalone.

Runs as a LaunchAgent off the owned venv (rumps is installed there by the
runtime install step). No .app bundle, so notifications go through osascript
rather than rumps.notification (which needs a bundle id).
"""

import functools
import glob
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

# --- stderr timestamp wrapper -------------------------------------------------
# menubar.err.log (this process's stderr, redirected by the launchd plist) has
# never carried timestamps, so correlating a line like "[s4l-card] approved at
# level 1" with the UTC-stamped logs elsewhere (post-*.log, post-preempt-
# events.log, panel-events.log) meant guessing from file position alone
# (2026-07-08 forensics). Wrap sys.stderr once, here, before any other module
# gets a chance to print, so every line written to stderr for the rest of the
# process gets a leading UTC timestamp in the same format those other logs
# use — zero changes needed at any individual print()/write() call site
# (s4l_card._log, the many sys.stderr.write(...) calls in this file, rumps'/
# PyObjC's own warnings, …).
class _TimestampedStderr:
    def __init__(self, stream):
        self._stream = stream
        self._newline_pending = True  # True at the start of a fresh line

    def write(self, data):
        if not data:
            return 0
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lines = data.split("\n")
        out = []
        for i, line in enumerate(lines):
            if i > 0:
                out.append("\n")
            starts_new_line = i > 0 or self._newline_pending
            if starts_new_line and line:
                out.append(f"[{ts}] ")
            out.append(line)
        self._newline_pending = data.endswith("\n")
        return self._stream.write("".join(out))

    def flush(self):
        self._stream.flush()

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False


sys.stderr = _TimestampedStderr(sys.stderr)

# --- Sentry bootstrap --------------------------------------------------------
# The menu bar runs as a standalone KeepAlive LaunchAgent off the owned venv,
# a separate process from the MCP server, so it was a Sentry blind spot: a crash
# (most often rumps missing/broken in the venv -> "menu bar didn't start") only
# ever landed in the local menubar.err.log. Wire it in BEFORE importing rumps so
# even an import-time failure of the menu bar's heaviest dependency is reported.
# sentry_init lives in the pipeline's scripts/ dir (S4L_REPO_DIR is exported by
# the launchd plist) and sentry-sdk is in the owned venv (requirements.txt). All
# best-effort: a missing repo path or SDK degrades to a silent no-op.
_sentry = None
try:
    # Read the repo dir from the plist env; this read runs
    # BEFORE scripts/ is on sys.path, so the s4l_env mirror can't help yet.
    _repo = os.environ.get("S4L_REPO_DIR")
    if _repo:
        _scripts = os.path.join(_repo, "scripts")
        if _scripts not in sys.path:
            sys.path.insert(0, _scripts)
    # SAPS_->S4L_ env mirror (brand rename 2026-07-03): old launchd plists still
    # export SAPS_*; everything below (and every subprocess) reads S4L_*.
    try:
        import s4l_env  # noqa: E402

        s4l_env.mirror()
    except Exception:
        pass
    import sentry_init as _sentry  # noqa: E402

    _sentry.init()
except Exception:
    _sentry = None

# Ship this process's stderr to the Cloud Run log relay (same endpoint the
# .mcpb server uses for pipeline subprocess output). Without this, every
# [s4l-card] / [s4l-menubar] line only ever existed in the local
# menubar.err.log and the review-surface incidents were invisible centrally.
# Installed after the S4L_REPO_DIR sys.path insertion above (the relay needs
# scripts/http_api.py for the X-Installation identity). Best-effort.
try:
    import s4l_log_relay  # noqa: E402

    s4l_log_relay.install()
except Exception:
    pass


def _capture(err, **tags):
    """Report a handled menu-bar error to Sentry (component=menubar) without ever
    raising into the caller. No-op if the Sentry bootstrap above failed."""
    try:
        if _sentry is not None:
            tags.setdefault("component", "menubar")
            _sentry.capture_exception(err, tags=tags)
    except Exception:
        pass


def _capture_msg(message, level="warning", _extra=None, **tags):
    """Report a handled menu-bar CONDITION (not an exception) to Sentry so we get
    fleet-wide signal on operational states like an orphaned/disabled/rate-limited
    draft schedule. capture_exception only covers thrown errors; this covers the
    "nothing crashed but the autopilot isn't running" case. No-op if the Sentry
    bootstrap failed. `_extra` (dict) rides as event extras for structured
    payloads too big for tags (e.g. the registry summary)."""
    try:
        if _sentry is not None:
            tags.setdefault("component", "menubar")
            _sentry.capture_message(message, level=level, tags=tags, extra=_extra)
    except Exception:
        pass


def _registry_summary_for_capture():
    """The scheduled-task registry breakdown (scripts/scheduled_tasks_snapshot
    build_summary) as a Sentry extra, so every needs-attention event carries the
    WHY (which account registry has the task, enabled, last_run_at age) instead
    of requiring on-box forensics. Best-effort: None on any failure."""
    try:
        import scheduled_tasks_snapshot
        return scheduled_tasks_snapshot.build_summary()
    except Exception:
        return None


def _flush():
    try:
        if _sentry is not None:
            _sentry.flush()
    except Exception:
        pass


try:
    import rumps  # noqa: E402
except Exception as _import_err:
    # rumps missing/broken in the owned venv is THE "menu bar didn't start" case.
    # Report it explicitly, flush, then re-raise so launchd records the crash too.
    _capture(_import_err, phase="import_rumps")
    _flush()
    raise

import s4l_state as st  # noqa: E402

# AppKit is available in the owned venv (PyObjC is a rumps dependency). We use it
# to pull the accessory (LSUIElement) app to the front AND to force the alert
# window itself above whatever else is on screen. Guarded so a missing AppKit
# never breaks the menu bar — the alert still shows, just possibly not
# front-most.
try:
    from AppKit import NSApplication, NSAlert, NSFloatingWindowLevel  # noqa: E402
except Exception:
    NSApplication = None
    NSAlert = None
    NSFloatingWindowLevel = None


def _activate_front():
    """Bring this accessory app to the front so the next alert opens on top of
    whatever was frontmost, instead of behind it. Best-effort; kept as a
    belt-and-suspenders call alongside _show_alert's own window-level fix
    below, since activation also affects which app is "key" for keyboard
    input on the alert's buttons."""
    try:
        if NSApplication is not None:
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def _show_alert(title=None, message='', ok=None, cancel=None, other=None):
    """Like rumps.alert(), but actually shows up on top.

    rumps.alert() builds an NSAlert and immediately calls runModal() with no
    window-level or ordering call at all — it just assumes the app is already
    active. activateIgnoringOtherApps_ (_activate_front, above) is async, so
    it can lose the race against runModal()'s own (assumed-active) window
    presentation: the alert window is created at the default level and never
    explicitly raised, so when this accessory app ISN'T active (e.g. Claude
    Desktop is frontmost), it renders BEHIND Claude's window instead of on
    top of it. Confirmed recurring 2026-07-07 (uninstall modal hidden behind
    Claude Desktop) despite the activation call landing 2026-06-30.

    Fix: build the NSAlert ourselves (same call rumps.alert uses) and force
    its window to NSFloatingWindowLevel + orderFrontRegardless() BEFORE
    runModal(), mirroring the pattern s4l_card.py's review panel already uses
    successfully (see _ReviewPanel._build: setLevel_(NSFloatingWindowLevel) +
    orderFrontRegardless()). Floating level sits above normal app windows in
    the window server's global z-order regardless of app-activation state,
    so it doesn't depend on the activation race resolving in time."""
    _activate_front()
    if NSAlert is None:
        # AppKit unavailable in this venv: fall back to rumps' plain alert.
        return rumps.alert(title=title, message=message, ok=ok, cancel=cancel, other=other)
    msg = str(message).replace('%', '%%')
    if not isinstance(cancel, str):
        cancel = 'Cancel' if cancel else None
    alert = NSAlert.alertWithMessageText_defaultButton_alternateButton_otherButton_informativeTextWithFormat_(
        title, ok, cancel, other, msg)
    alert.setAlertStyle_(0)  # same style rumps.alert() uses
    try:
        alert.window().setLevel_(NSFloatingWindowLevel)
        alert.window().orderFrontRegardless()
    except Exception:
        pass
    return alert.runModal()


CLAUDE_APP = "Claude"


def _claude_launch_env():
    """Environment for launching Claude via `open`, with every S4L_*/SAPS_* key
    stripped. This menubar runs under a launchd plist that exports S4L_REPO_DIR
    (and friends) pointing at the MATERIALIZED pipeline repo, and macOS `open`
    propagates the caller's environment into the launched app. A relaunch from
    here therefore poisoned Claude Desktop (and every MCP server child) with
    S4L_REPO_DIR=<materialized repo>, which ensurePipelineCurrent() read as
    "user's own clone, never touch" — so pipeline updates silently never
    applied and the update banner re-fired forever (2026-07-03). Claude needs
    none of these vars; anything that does gets them from its own plist."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("S4L_", "SAPS_"))}
POLL_SECONDS = 5

# Our own LaunchAgent. Quit boots it out and deletes the plist so the tray is
# genuinely gone (KeepAlive can't respawn it, RunAtLoad can't resurrect it at
# next login). Keep the label in sync with MENUBAR_LABEL in mcp/src/runtime.ts.
MENUBAR_LABEL = "com.m13v.social-autoposter.menubar"
MENUBAR_PLIST = os.path.join(
    os.path.expanduser("~"), "Library", "LaunchAgents", f"{MENUBAR_LABEL}.plist"
)
# Stop sentinel read by the MCP server's ensureMenubar()/provision paths: while
# present, no auto-start path may reinstall the tray. Cleared only by explicit
# start actions (restart_menubar tool, queue_setup re-arm). Keep the filename in
# sync with MENUBAR_STOP_FLAG in mcp/src/runtime.ts.
STOP_FLAG = os.path.join(st.state_dir(), "stopped.flag")
# Pause sentinel (lighter than STOP_FLAG): while present, the pipeline daemons
# (kicker/reaper/stall-watch/memory-snapshot) stay unloaded and claude_job.py's
# `next` reports no work — but Claude Desktop, this tray, and the draft
# schedule registration are untouched. Written/cleared ONLY by the MCP's
# pause_s4l tool (see _pause_work below); this constant is read-only
# here, purely to render the menu label. Keep the filename in sync with
# pauseFlagPath() in mcp/src/index.ts.
PAUSE_FLAG = os.path.join(st.state_dir(), "paused.flag")

# Autopilot scheduled tasks. Queue workers must RUN in a dedicated folder
# (~/.s4l-worker) so their once-a-minute sessions don't flood the user's
# interactive Claude Code history (Claude buckets sessions by cwd). s4l-worker
# is the universal type-blind worker (2026-07-02, one task drains every job
# type); task ids are USER-VISIBLE in the Routines UI, so the canonical id
# carries the S4L brand, never the internal "saps" prefix. The phase1/phase2b
# pair (and the short-lived staging "saps-worker" from rc.2/rc.3) are legacy.
# The single pre-queue autopilot task is deprecated and removed outright. Keep
# this in sync with queueWorkerCwd()/QUEUE_WORKERS/LEGACY_QUEUE_WORKER_TASK_IDS
# in mcp/src/index.ts and scripts/s4l_box_update.sh.
WORKER_TASK_IDS = ("s4l-worker", "saps-worker", "saps-phase1-query", "saps-phase2b-draft")
# The universal worker every install converges on. Every legacy id is
# CONSOLIDATED into it by the same one-restart self-heal that fixes task
# folders: the registry rewrite (while Claude is down) replaces all legacy
# entries with one s4l-worker entry. Until a box walks that path, the legacy
# tasks still work (their SKILL.md is refreshed to the universal body on boot).
WORKER_TASK_ID = "s4l-worker"
LEGACY_WORKER_TASK_IDS = ("saps-worker", "saps-phase1-query", "saps-phase2b-draft")
DEPRECATED_TASK_IDS = ("social-autoposter-autopilot",)
WORKER_CWD = os.path.join(os.path.expanduser("~"), ".s4l-worker")
# "Claude*": the host app can run with a custom --user-data-dir (per-account
# dirs like "Claude-mediar"), putting the live registry outside plain "Claude/".
# Keep in sync with scripts/schedule_state.py::SCHED_REGISTRY_GLOB.
SCHED_REGISTRY_GLOB = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Claude*",
    "claude-code-sessions", "*", "*", "scheduled-tasks.json",
)

GLYPH = {"complete": "✓", "in_progress": "…", "blocked": "✗"}

# Menu-bar title spinner shown while a post is in flight.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Prompts the model-driven menu items type into the Claude Desktop composer.
# SETUP_PROMPT mirrors the in-chat panel's Setup button (panel.ts) verbatim so
# both entry points kick off the same end-to-end flow.
SETUP_PROMPT = (
    "Set up the S4L plugin end to end now. Inspect and repair the runtime, "
    "auto-detect and connect my X session, scan my profile, discover and research "
    "my product, then infer and save a complete project with seeded search topics. "
    "Keep going without asking me to approve each safe setup step. Ask only if I "
    "must interactively sign in or no product can be identified."
)
UPDATE_PROMPT = "Update the S4L plugin to the latest version."
# Re-arm goes through the HOST create_scheduled_task path (the same one onboarding
# uses) — it registers the routines under whatever account is logged in and shows
# up in Routines. The host tool only runs inside an agent chat, so the menu bar
# hands Claude this prompt (auto-typed, clipboard+paste fallback). We do NOT write
# scheduled-tasks.json directly — that can't reliably target a just-switched-into
# account, which is exactly the bug it caused.
REARM_PROMPT = (
    "Set up the S4L draft autopilot schedule for this Claude account. "
    "If queue_setup is available, call it; then for s4l-worker call the host tool "
    "create_scheduled_task with taskId, cronExpression \"* * * * *\", notifyOnCompletion "
    "false (REQUIRED — the default true pops a notification every minute), and the prompt "
    "— read it from ~/.claude/scheduled-tasks/s4l-worker/SKILL.md (already on disk). "
    "If the task already exists, call update_scheduled_task with taskId s4l-worker and "
    "notifyOnCompletion false instead. "
    "Do not redo my X connection or project setup — only register the scheduled task. "
    "Before calling create_scheduled_task/list_scheduled_tasks/update_scheduled_task, run "
    "(via Bash, best-effort, ignore any error and continue if the path doesn't exist): "
    "python3 ~/.social-autoposter-mcp/repo/package/scripts/mark_event.py start <that tool's name>; "
    "immediately after it returns, run the same with 'end' instead of 'start'. This makes a hang "
    "in that host tool directly observable instead of invisible. "
    "Keep replies short."
)
# Universal diagnose-and-heal prompt behind the ⚠ "Diagnose & fix" menu item. One
# prompt for EVERY persistent attention state (draft stuck, rate-limited, schedule
# missing/disabled): the menu bar can only see the symptom, Claude on the box can
# see the cause. The prompt encodes the known heal patterns (the 2026-07-06
# dead-claim incident chief among them), forbids code hot patches, and ends by
# shipping a report back to us via scripts/send_diagnostic_report.py, so every
# click doubles as fleet telemetry about what actually breaks in the field.
DIAGNOSE_PROMPT_TEMPLATE = (
    "The S4L plugin's menu bar on this machine is showing a persistent warning. "
    "Reason code: {reason}. Detail: {detail}. Diagnose and heal it now.\n"
    "Evidence lives under ~/.social-autoposter-mcp: activity.json (the producer's "
    "live drafting label), claude-queue/ (pending/, running/ — each running job "
    "stamps claim_pid, result/, provider.log, reaper-status.json, drain-status.json) "
    "and the reaper log at repo/package/skill/logs/launchd-claude-reaper-stderr.log. "
    "Worker transcripts: the *.jsonl files in the ~/.claude/projects/ entry for the "
    "~/.s4l-worker directory.\n"
    "Known heal patterns, in order of likelihood: "
    "(1) dead claim — a job in claude-queue/running/ whose claim_pid no longer "
    "exists in ps was orphaned by a killed worker (app quit mid-draft); move its "
    "json back to claude-queue/pending/<its type>/ under the SAME filename with the "
    "claim_pid and claimed_at keys deleted, and the every-minute worker will "
    "re-claim it. "
    "(2) rate-limited — worker transcripts end in 429/limit errors; nothing local "
    "to fix, say so in the report. "
    "(3) schedule missing/disabled — re-register via queue_setup or the host "
    "create_scheduled_task with notifyOnCompletion false and the prompt from "
    "~/.claude/scheduled-tasks/s4l-worker/SKILL.md. Before calling create_scheduled_task/"
    "list_scheduled_tasks/update_scheduled_task, bracket it (Bash, best-effort, ignore if "
    "missing): python3 ~/.social-autoposter-mcp/repo/package/scripts/mark_event.py start "
    "<tool name>, then ...end <tool name> right after it returns — makes a hang in that "
    "host tool directly observable.\n"
    "HARD RULE: fix state files only. Never edit, copy, or patch code in "
    "~/.social-autoposter-mcp or any extension bundle (hot patches are auto-reverted "
    "and mask the real bug).\n"
    "Finally — ALWAYS, healed or not — write a short markdown report (symptom, root "
    "cause, actions taken, current state) to "
    "~/.social-autoposter-mcp/diagnostics/report-<UTC-timestamp>.md and run "
    "`~/.social-autoposter-mcp/runtime/.venv/bin/python3 "
    "~/.social-autoposter-mcp/repo/package/scripts/send_diagnostic_report.py "
    "<that file>` so the report reaches the S4L developers. Keep replies short."
)

# A pending draft job older than this (seconds) with nothing claiming it means no
# routine is draining the queue — the worker would claim within a minute if it
# were firing. False-positive-free: an idle queue has no pending job at all, so a
# quiet pipeline (no candidates) never trips this. Comfortably above the host
# scheduler's per-minute cadence + a slow claim.
AUTOPILOT_STALL_SECONDS = 180

# A job CLAIMED but never finished (sits in running/ this long) means a worker
# picked it up and then wedged mid-run (the claude -p drafting child died / never
# spawned). Generous enough that the longest real drafting turn never trips it.
# Keep in sync with RUNNING_STALL_SECONDS (scripts/autopilot_stall_watch.py).
AUTOPILOT_RUNNING_STALL_SECONDS = 900

# The "firing" window (how fresh lastRunAt must be) lives in the single source of
# truth, scripts/schedule_state.py (FIRING_WINDOW there). _schedule_state delegates
# to it, so it is intentionally NOT redefined here.

# How long the producer can sit narrating "draft Nm" before we treat
# the draft as STUCK rather than healthy. The producer writes that label the whole
# time it blocks waiting for a worker to return a result (up to its 30-min queue
# timeout). A healthy drain clears in ~1-2 min; if the label has been "drafting"
# this long, the worker keeps dying mid-run (host inactivity-kill) or never claims,
# and nothing is draining — so we flip the menu bar from a reassuring spinner to
# ⚠ instead of letting the stale "drafting (8m)" lie persist. Well above any
# healthy single drain (the worker itself dies at ~2 min today).
DRAFT_STUCK_SECONDS = 300

# A poll can read a transient "not stuck" moment (e.g. the activity file
# between one dying worker attempt and the next claim on the same aged job)
# even though the underlying episode never really cleared. Without this, each
# such blip resets _stall_notified and the very next poll re-fires the Sentry
# alert, producing a burst of duplicate events for one continuous episode
# (observed: 5 events in 40s, 2026-07-07). Require attention to read false for
# this long, continuously, before treating the episode as over.
ATTENTION_CLEAR_COOLDOWN_SECONDS = 60

# Unattended-review watchdog. A card stack is open with pending drafts and the
# user has not decided or clicked anything on it for REVIEW_UNATTENDED_SECONDS:
# treat that as "the user is not seeing this window" regardless of what AppKit
# reports (a card can be fully drawn yet parked on a display corner nobody
# looks at, which hid 12 drafts for 3 hours on 2026-07-02). The response is
# SELF-HEALING, not a prompt: move the card to the pointer's screen and raise
# it, then keep re-healing every REVIEW_HEAL_EVERY_SECONDS while the drought
# lasts. One notification per episode; one Sentry event after
# REVIEW_UNATTENDED_SENTRY_SECONDS so silently-ignored review surfaces are
# visible fleet-wide.
REVIEW_UNATTENDED_SECONDS = float(
    os.environ.get("S4L_REVIEW_UNATTENDED_S", "1200")
)
REVIEW_HEAL_EVERY_SECONDS = float(
    os.environ.get("S4L_REVIEW_HEAL_EVERY_S", "600")
)
REVIEW_UNATTENDED_SENTRY_SECONDS = 3600.0


def _label_elapsed_secs(label):
    """Parse the trailing duration the producer encodes in a drafting activity
    label — 'draft 8m', 'draft ⧖18m' (⧖ = still queued, unclaimed), '... 45s' —
    into seconds. Returns 0 when there's no parseable duration. _fmt_dur
    (claude_job.py) only ever emits '<n>s' (<60s) or '<n>m', so this mirror
    stays trivial."""
    if not label:
        return 0
    import re
    matches = re.findall(r"(\d+)\s*([sm])\b", str(label))
    if not matches:
        return 0
    n, unit = matches[-1]
    return int(n) * (60 if unit == "m" else 1)


def _glyph(status):
    return GLYPH.get(status, "·")


def _osa_quote(s):
    """Escape a Python string for an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _claude_send_script(prompt):
    """AppleScript that focuses Claude, pastes `prompt` into the focused composer,
    and presses Return. Uses the clipboard (saved + restored) rather than slow
    per-character keystrokes, and waits longer on a cold launch so the window is
    ready before pasting."""
    p = _osa_quote(prompt)
    return "\n".join(
        [
            'set prevClip to ""',
            "try",
            "    set prevClip to (the clipboard as text)",
            "end try",
            f'set the clipboard to "{p}"',
            'tell application "System Events" to set wasRunning to (exists process "Claude")',
            'tell application "Claude" to activate',
            "if wasRunning then",
            "    delay 0.5",
            "else",
            "    delay 2.5",
            "end if",
            'tell application "System Events"',
            '    keystroke "v" using {command down}',
            "    delay 0.15",
            "    key code 36",
            "end tell",
            "delay 0.3",
            'if prevClip is not "" then set the clipboard to prevClip',
        ]
    )


class S4LMenuBar(rumps.App):
    def __init__(self):
        super().__init__("S4L", quit_button=None)
        self._last_blocker_code = None
        self._sig = None  # last rendered state signature; skip rebuild if unchanged
        self._review_active = False  # a review-card sequence is on screen
        # Signature of the pending drafts last presented. We de-dup on the CONTENT
        # of the pending set, NOT on the batch_id: the server intentionally reuses
        # a constant batch_id ("review-queue") so a continuous autopilot's drafts
        # accumulate into one queue. Keying de-dup on that constant suppressed every
        # later batch for the life of this process (only a restart cleared it),
        # which is exactly the "drafts queued but no cards" bug.
        self._last_review_sig = None
        # Per-card posting. Each approved card posts the INSTANT it's approved,
        # serialized through one persistent worker so two posts never drive the
        # shared harness Chrome at once (the poster lock fails a concurrent peer
        # after 45s rather than queuing it, which would land the 2nd card 0/N).
        # `_review_active` stays true while the panel is open OR posts are still
        # draining, so a half-posted set is never re-presented as fresh cards.
        self._post_q = queue.Queue()
        self._post_worker = None
        self._review_lock = threading.Lock()
        self._panel_open = False
        # Unattended-review watchdog state (_maybe_heal_review).
        self._review_heal_at = 0.0
        self._review_unattended_notified = False
        self._review_unattended_captured = False
        # This session's card decisions, for store reconciliation: the MCP
        # server's post_drafts rewrites the whole review store from a copy read
        # before a minutes-long posting run, so a decision stamped mid-drain can
        # be clobbered; the tick re-stamps anything missing (_reconcile_store).
        self._session_decisions = []
        self._reconciled_at = 0.0
        # The /tmp plan path is a compatibility symlink into the durable store;
        # recreate it if a reboot swept it (merge_review_queue also does this).
        try:
            st.ensure_store_symlink()
        except Exception:
            pass
        self._posts_outstanding = 0
        self._posting_batch_total = 0
        self._posting_batch_done = 0
        self._spin_i = 0
        self._spinner = None  # fast rumps.Timer animating the title while busy
        # Durable posting progress, derived from the review-queue PLAN rather than
        # this process's in-memory burst queue. The in-memory counter dies on a
        # menu bar restart and is blind to posts driven by the autopilot/agent, so
        # the title used to fall back to plain "S4L" mid-drain. These track a drain
        # by the plan's posted count climbing, with hysteresis across the multi-
        # second gaps between individual posts so the indicator never blinks off.
        self._posting_label = None
        self._drain_baseline = None  # posted count just before this drain started
        self._drain_last_posted = None
        self._drain_last_change = 0.0
        # Edge-triggered (not one-shot): re-enqueue any approvals the durable queue
        # recorded but never confirmed posted (a restart, or a stranded approval
        # from a loopback outage, wiped/never populated the in-memory _post_q).
        # panel-endpoint.json is last-writer-wins across many short-lived MCP
        # instances (Claude Code side-panel sessions, the s4l-worker queue runner,
        # etc.), so "reachable" can flap: down for a while, then some session
        # boots and it's briefly up again. We used to resume only on the FIRST
        # reachable tick ever, which meant a card that got stranded after that
        # first success stayed stranded until the whole menubar process restarted.
        # Now we re-arm on every reachable->unreachable transition so the NEXT
        # reachable tick resumes again, and throttle the (blocking, ~1.5s worst
        # case) reachability probe itself so a steady-state outage doesn't cost
        # a hitch every 5s tick.
        self._resumed = False
        self._loopback_check_due_at = 0.0
        # Reliable self-check of our own Accessibility (TCC) grant — this is the
        # faithful reading (our launchd process identity, not a parent's). Logged
        # so menubar.err.log records whether keystroke posting will work.
        sys.stderr.write(
            f"[s4l-menubar] accessibility_trusted={st.accessibility_trusted()}\n"
        )
        sys.stderr.flush()
        # Status-item visibility watchdog (telemetry only). A live process says
        # nothing about whether macOS is actually DRAWING our icon: on 2026-07-07
        # a customer's menu bar process ran for ~25 min with no visible icon
        # (post-reboot login storm) and support had no signal to tell "process
        # dead" from "icon hidden". _tick probes the real NSStatusItem every 60s,
        # persists menubar-state.json (memory_snapshot ships it on the heartbeat)
        # and logs transitions to stderr. Deliberately NO auto-restart: the
        # occlusion probe also reads not-visible during fullscreen apps and the
        # locked screen, so acting on it would false-fire; the remote fix is the
        # restart_menubar MCP tool.
        # First probe at +60s, not immediately: the window server takes a few
        # seconds to place a fresh status item (occlusion reads not-visible and
        # the frame sits at x=0 until then — measured 2026-07-07), so an eager
        # probe would log a false "hidden" on every single process start.
        self._icon_check_due_at = time.monotonic() + 60.0
        self._icon_hidden_since = None
        self._icon_last_on_screen = "unprobed"
        self._icon_watch_started = time.monotonic()
        self._timer = rumps.Timer(self._tick, POLL_SECONDS)
        self._timer.start()
        # Light 1s poll for server activity (scanning/drafting/posting/…); it
        # spins up the fast title-spinner on demand. Idle cost is one tiny file
        # read per second.
        self._act_poll = rumps.Timer(self._poll_activity, 1.0)
        self._act_poll.start()
        # Update availability comes from ONE source: scripts/snapshot.py's
        # _latest_published (GitHub releases/latest first, npm fallback — boxes
        # have no npm; same probe as mcp/src/version.ts), surfaced in the
        # snapshot as update_available/latest_version. _tick copies those
        # snapshot fields onto these attrs every poll. No second GitHub/manifest
        # check here anymore (it diverged from the header and once showed an
        # "update to an OLDER version" because it polled a different registry).
        self._update_available = False
        self._latest_version = None
        # True from the instant "Update now" is clicked until the download +
        # unzip either fails or hands off to the update-verify.json marker
        # (see _update_in_flight). Drives the immediate title spinner + menu
        # disable so the click never reads as dead while curl/unzip run.
        self._updating_mcpb = False
        # Release channel + resolved release tag for this box (from the snapshot;
        # stable = releases/latest, staging = newest release overall). The tag is
        # what a staging update downloads from. See scripts/s4l_channel.py.
        self._channel = "stable"
        self._latest_tag = None
        # Self-heal (modal-first): if the autopilot scheduled tasks are running in
        # the wrong folder (or the deprecated single autopilot task still exists),
        # OFFER to relocate them to ~/.s4l-worker so their once-a-minute runs stop
        # polluting the user's interactive Claude Code history. The fix needs a
        # single Claude restart (the app caches the registry in memory), so we ASK
        # first with a modal — same consent pattern as Quit/re-arm — and never
        # restart Claude out from under the user silently. Prompt at most once per
        # process; a 'Later' is re-offered as a menu item.
        self._relocating = False
        self._cwd_healed = False
        self._reloc_prompted = False
        self._reloc_needed = False
        # One-shot guard so the "autopilot not running" notification fires once per
        # stall episode, not every poll. Reset when the stall clears.
        self._stall_notified = False
        # Wall-clock time (time.time()) attention first read false since the last
        # true reading, or None while attention is true / already cleared. Debounces
        # _stall_notified's reset against ATTENTION_CLEAR_COOLDOWN_SECONDS so a
        # transient blip doesn't end the episode early and cause a re-fire burst.
        self._attention_clear_since = None
        # Cached stall flag (set each _tick) so the 1s activity poll can suppress a
        # stale "drafting" spinner that would otherwise mask the ⚠ in the title.
        self._stalled = False
        # Cached (kind, detail) explaining why a SCHEDULED autopilot isn't draining
        # ('rate_limited' -> wait/switch, no setup button; 'failing' -> generic).
        self._stall_reason_info = ("", "")
        # Cached schedule state for the current account: 'missing'/'disabled'/'ok'/
        # 'unknown'. PRIMARY driver of the menu's attention section.
        self._schedule_state_cache = "ok"
        self._reloc_timer = rumps.Timer(self._maybe_relocate_tasks, 90)
        self._reloc_timer.start()
        self._tick(None)

    # ---- side effects -----------------------------------------------------
    def _open_claude(self, _=None):
        subprocess.run(["open", "-a", CLAUDE_APP], capture_output=True,
                       env=_claude_launch_env())

    def _copy_to_clipboard(self, text):
        """Put text on the clipboard via pbcopy. Unlike the AppleScript keystroke
        paste, this needs NO Accessibility grant, so it's the always-works fallback
        when automation can't run. Returns True on success."""
        try:
            p = subprocess.run(["pbcopy"], input=text, text=True, timeout=10)
            return p.returncode == 0
        except Exception:
            return False

    def _manual_paste_fallback(self, prompt, reason):
        """Automation couldn't paste (no Accessibility, or osascript failed). Don't
        dead-end: drop the prompt on the clipboard and open Claude so the user can
        paste it themselves (Cmd+V, Enter). This is what makes re-arm/setup usable
        even when the TCC grant is stale (granted but the running process still
        reads untrusted until restart)."""
        copied = self._copy_to_clipboard(prompt)
        self._open_claude()
        if copied:
            self._notify(
                "S4L · prompt copied to clipboard",
                f"{reason} Paste it into Claude (⌘V) and press Enter to continue.",
            )
        else:
            self._notify("S4L", f"{reason} Open Claude and type your request there.")
        return False

    def _clipboard_prompt(self, prompt, title, action_desc):
        """The menu bar's UNIVERSAL way to hand an agent-driven action to Claude
        without depending on the MCP/loopback being up (the menu bar can't inject
        into the chat like the panel's sendMessage): copy the prompt to the
        clipboard, show a modal, and open Claude so the user pastes it (Cmd+V,
        Enter). This is the same reliable pattern re-arm has always used,
        generalized to every agent action. NO auto-type (focus/timing/Accessibility
        flaky — that path was the source of frozen-looking menus), NO MCP call."""
        copied = self._copy_to_clipboard(prompt)
        if copied:
            msg = ("The prompt is copied to your clipboard.\n\nClick OK, then click "
                   "into the Claude chat, paste it (Cmd+V), and press Enter — "
                   + action_desc + ".")
        else:
            msg = "Click OK, then open Claude and ask it to " + action_desc + "."
        try:
            _activate_front()
            _show_alert(title=title, message=msg, ok="OK")
        except Exception:
            self._notify("S4L · prompt copied" if copied else "S4L",
                         "Paste the prompt into Claude (Cmd+V) and press Enter.")
        self._open_claude()
        return True

    def _send_to_claude(self, prompt):
        """Back-compat shim: every agent-driven menu action now uses the reliable
        clipboard-prompt model (no flaky auto-type). Delegates to _clipboard_prompt."""
        return self._clipboard_prompt(
            prompt, "Send to Claude", "Claude will take it from there"
        )

    # Agent-driven action: hand the full setup prompt to Claude via the clipboard.
    def _setup(self, _=None):
        self._clipboard_prompt(
            SETUP_PROMPT,
            "Set up S4L in Claude",
            "Claude will set up your runtime, connect X, configure your project, and "
            "schedule the autopilot",
        )


    def _diagnose_fix(self, _=None):
        """Universal ⚠ escape hatch: hand Claude a diagnose-and-heal prompt for
        whatever persistent attention state the menu bar is showing, via the same
        clipboard-prompt flow as Set up / Re-arm. The prompt makes Claude ship a
        report back through send_diagnostic_report.py, so we hear about every
        field failure this button gets used on — the click itself is also
        captured, so \"clicked but no report arrived\" is a visible signal."""
        reason, detail = getattr(self, "_stall_reason_info", ("", "")) or ("", "")
        if not reason:
            sched = getattr(self, "_schedule_state_cache", "") or ""
            reason = f"schedule_{sched}" if sched in ("missing", "disabled") else "unknown"
        _capture_msg(
            "S4L diagnose&fix clicked",
            phase="diagnose_fix",
            reason=reason,
        )
        self._clipboard_prompt(
            DIAGNOSE_PROMPT_TEMPLATE.format(reason=reason, detail=detail or "n/a"),
            "Diagnose & fix S4L in Claude",
            "Claude will diagnose the warning, heal what it safely can, and send "
            "a report to the S4L developers",
        )

    def _rearm(self, _=None):
        """Register the draft schedule for the CURRENT account via the host
        create_scheduled_task flow (same as onboarding) — it registers under
        whatever account is logged in and shows in Routines. The host tool only
        runs inside a chat turn, and the menu bar CANNOT inject into the chat (that
        bridge is panel-only), so the reliable path here is: copy the prompt to the
        clipboard, open Claude, and tell the user to paste it. (The dashboard
        widget's button does this in one click via app.sendMessage — no paste.) We
        do NOT auto-type (focus/timing flaky) and do NOT write the registry directly
        (can't reliably target a just-switched-into account).

        The click itself is captured (mirroring _diagnose_fix) — previously this
        button had NO telemetry at all, so a past incident (Karol, 2026-07-07)
        could only establish "a human must have clicked SOMETHING" from the code
        path, never which button or exactly when. The registry snapshot rides as
        an extra so the pre-rearm state is on record without on-box forensics."""
        sched = getattr(self, "_schedule_state_cache", "") or ""
        _capture_msg(
            "S4L rearm clicked",
            phase="rearm",
            surface="menubar",
            schedule_state=sched or "unknown",
            _extra={"registry": _registry_summary_for_capture()},
        )
        self._clipboard_prompt(
            REARM_PROMPT,
            "Set up the draft schedule",
            "that schedules the draft tasks for this account",
        )

    def _finish_schedule_setup(self, _=None):
        """One-click fix for schedule_state == 'missing' when
        scheduled_task_selfheal.can_create_for_active_account() confirms a
        session directory already exists for the active account (2026-07-08):
        quit Claude, create the registration via a direct file write (the
        same heal() the update flow uses — see its module docstring for why
        this is equivalent to what create_scheduled_task would produce), then
        relaunch. Primary action for this case now instead of re-arm: no
        clipboard paste, no chat turn required. Re-arm remains the fallback
        for the rarer case where no session directory exists yet for the
        active account (see _build_menu) — fix 5 never fabricates one.

        No confirm dialog: the restart is already disclosed in the menu item
        label itself ("...(restarts Claude)", _build_menu) — a modal repeating
        that would just be a second click for information the user already
        has (2026-07-09). The notify toast below still tells them it started."""
        _capture_msg(
            "S4L finish-schedule-setup clicked",
            phase="draft_schedule",
            reason="missing",
            _extra={"scheduled_tasks": _registry_summary_for_capture()},
        )
        self._notify("S4L", "Restarting Claude Desktop… setting up your draft schedule.")
        threading.Thread(target=self._finish_schedule_setup_work, daemon=True).start()

    def _finish_schedule_setup_work(self):
        try:
            user_data_dirs = self._claude_user_data_dirs()
            self._quit_claude_and_wait()
            # Claude is down: the same safe window _rewrite_scheduled_task_cwd
            # always required. In-process is fine here (unlike the update
            # flow's _fresh variant) — no new bundle was just downloaded, so
            # the currently-running code IS the current code; nothing to go
            # stale against.
            self._rewrite_scheduled_task_cwd()
            self._relaunch_claude(user_data_dirs)
            self._sig = None
            # Verify rather than claim success unconditionally — the earlier,
            # now-deleted silent restart-fix drew a direct complaint for
            # giving zero feedback either way (2026-07-08). CREATED_GRACE in
            # schedule_state.py means a freshly-created, never-yet-fired task
            # already reads "ok", so this doesn't need to wait for an actual
            # first fire — just for Claude to be back up and the file to have
            # landed.
            time.sleep(12)
            state = self._schedule_state()
            if state == "ok":
                self._notify(
                    "S4L drafts set up",
                    "Your draft schedule is registered — drafting starts within a few minutes.",
                )
            else:
                self._notify(
                    "S4L couldn't finish setup",
                    "Restarting didn't register the schedule. Open the S4L menu → "
                    "“Set up draft schedule” to finish it manually.",
                )
        except Exception as e:
            self._notify("S4L setup restart failed", str(e)[:140])
            _capture(e, phase="finish_schedule_setup")

    # ---- schedule-state detection ----------------------------------------
    def _schedule_state(self):
        """Is the draft schedule registered AND running for the live account?
        Returns 'ok' | 'disabled' | 'missing'. Delegates to the SINGLE source of
        truth, scripts/schedule_state.py (shared with the Node MCP server, which
        shells out to the same script), so the firing-detection algorithm lives in
        exactly one place and the menu bar + dashboard can never drift. The script
        is on sys.path via the S4L_REPO_DIR/scripts insertion near the top of this
        file. Any failure -> 'missing' (safe: never a false 'ok')."""
        try:
            import schedule_state
            return schedule_state.compute()
        except Exception as e:
            _capture(e, phase="schedule_state")
            return "missing"

    # ---- autopilot liveness (the false-green fix) -------------------------
    def _autopilot_stalled(self):
        """True when setup is done but no scheduled-task routine is draining the
        draft queue — the signature of a Claude account switch orphaning the
        routines while their global SKILL.md files (the old "autopilot_on" proxy)
        stay put. Two complementary signals, OR'd; best-effort, pure file reads:

          (1) LATCHED: the producer's drain-status shows >=1 consecutive timeout
              with no successful drain since. Persists across the gap between cycles
              (the producer removes the job on timeout, so there's no pending file
              to see between cycles) -> the ⚠ stays on continuously instead of
              flickering off. This is the durable signal.
          (2) FAST: a draft job has sat unclaimed in pending/ past
              AUTOPILOT_STALL_SECONDS -> catches a fresh stall ~3 min in, before
              the first full producer timeout has even latched (1).
          (3) IN-FLIGHT: a draft job was claimed (moved to running/) but never
              finished within AUTOPILOT_RUNNING_STALL_SECONDS -> the worker picked
              it up and then wedged mid-run. Self-clearing (the file is removed on
              result or swept next cycle), so unlike the abandoned drain latch it
              does NOT stay stale after recovery.

        NOTE: kept in sync with scripts/autopilot_stall_watch.py (the fleet Sentry
        backstop). The menu-bar ⚠ itself is driven by _schedule_state, NOT this
        method — the attention/⚠ path keys off schedule_state so a firing-but-
        momentarily-empty queue stays green (an earlier drain-latch ⚠ stayed stale
        after recovery and was deliberately removed). This method exists for the
        watcher-parity contract and _stall_reason.
        """
        qroot = os.path.join(st.state_dir(), "claude-queue")
        # (1) latched producer drain-status
        try:
            with open(os.path.join(qroot, "drain-status.json")) as f:
                if int((json.load(f) or {}).get("consecutive_timeouts", 0) or 0) >= 1:
                    return True
        except Exception:
            pass
        # (2) fast pending-age
        try:
            oldest = None
            for sub in glob.glob(os.path.join(qroot, "pending", "*")):
                # feedback-digest jobs are latency-insensitive (hourly kicker,
                # retried forever) and may wait behind a long draft job; their
                # age is not an autopilot stall. Mirrors autopilotStalled() in
                # mcp/src/index.ts.
                if os.path.basename(sub) == "feedback-digest":
                    continue
                for jf in glob.glob(os.path.join(sub, "*.json")):
                    if jf.endswith(".tmp"):
                        continue
                    try:
                        m = os.path.getmtime(jf)
                    except OSError:
                        continue
                    if oldest is None or m < oldest:
                        oldest = m
            if oldest is not None and (time.time() - oldest) > AUTOPILOT_STALL_SECONDS:
                return True
        except Exception:
            pass
        # (3) in-flight running-age (claimed then wedged). running/ is flat.
        try:
            oldest = None
            for jf in glob.glob(os.path.join(qroot, "running", "*.json")):
                if jf.endswith(".tmp"):
                    continue
                try:
                    m = os.path.getmtime(jf)
                except OSError:
                    continue
                if oldest is None or m < oldest:
                    oldest = m
            if oldest is not None and (time.time() - oldest) > AUTOPILOT_RUNNING_STALL_SECONDS:
                return True
        except Exception:
            pass
        return False

    def _recent_worker_outcome(self, window=600):
        """Inspect worker transcripts written in the last `window` seconds (the
        ~/.s4l-worker bucket). Returns (ran, rate_limit_msg):
          ran           — a routine actually EXECUTED recently (a worker that runs
                          leaves a transcript; an orphaned/not-firing account leaves
                          none). This is what tells "routines fire but fail" apart
                          from "routines gone".
          rate_limit_msg— set when a recent run hit the Claude weekly/usage limit
                          (re-arm cannot fix that); carries a short 'resets …' string.
        Account-agnostic on purpose: it keys off actual execution, not a per-account
        lastRunAt that freezes (and lies) after an account switch."""
        ran = False
        limit_msg = None
        try:
            now = time.time()
            files = glob.glob(
                os.path.expanduser("~/.claude/projects/*s4l-worker*/*.jsonl")
            )
            recent = [f for f in files if (now - os.path.getmtime(f)) <= window]
            recent.sort(key=os.path.getmtime, reverse=True)
            if recent:
                ran = True
            cutoff = now - window
            for f in recent[:5]:
                try:
                    fh = open(f)
                except Exception:
                    continue
                # CRITICAL: only treat this as a limit when it is an actual API
                # ERROR, and a FRESH one — never loose prose, and never an old
                # error line inside a still-hot file.
                #
                # Two false-positive classes this loop must reject:
                #   (1) Markers as CONTENT. The drafting prompt embeds candidate
                #       threads + the feedback report, which frequently contain
                #       phrases like "weekly limit" / "rate limit" (an AI-product
                #       timeline is full of them — a 'claude-meter' example post
                #       false-tripped the old prose match on 2026-06-29). A session
                #       that READS a transcript or this source file re-embeds the
                #       literal marker strings too. So a substring hit is only a
                #       cheap prefilter; the verdict comes from the parsed line's
                #       TOP-LEVEL fields, which only the SDK writes on real errors.
                #   (2) Stale errors in long-lived sessions. A session that 429'd
                #       once (e.g. mid account-rotation) but recovered keeps its
                #       file mtime fresh as it works, so a whole-file scan re-trips
                #       the banner for as long as the session lives (2026-07-06:
                #       banner stuck long after the limit lifted). Gate each error
                #       line on its OWN timestamp being inside the window.
                with fh:
                    for line in fh:
                        low = line.lower()
                        if (
                            '"apierrorstatus":429' not in low
                            and '"error":"rate_limit"' not in low
                            and '"isapierrormessage":true' not in low
                        ):
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if not (d.get("isApiErrorMessage") or d.get("error")):
                            continue  # marker was content, not a real error line
                        ts = d.get("timestamp")
                        if ts:
                            try:
                                import datetime as _dt
                                t = _dt.datetime.fromisoformat(
                                    ts.replace("Z", "+00:00")
                                ).timestamp()
                                if t < cutoff:
                                    continue  # old error; the session recovered
                            except Exception:
                                pass  # unparseable ts -> keep (fail loud, not silent)
                        # 'resets 4:30pm (America/Los_Angeles)' style prose rides
                        # inside the error message on both shapes; surface it so
                        # the menu can say WHEN instead of just "rate limited".
                        m = re.search(r"resets [^\"\\]{0,40}", line)
                        resets = " — " + m.group(0).strip().rstrip(".") if m else ""
                        if d.get("apiErrorStatus") == 429 or d.get("error") == "rate_limit":
                            limit_msg = "Claude rate limit reached (429)" + resets
                            break
                        # Non-429 API errors only count with weekly/usage-limit
                        # prose in them; a plain 401/500 is NOT a usage limit.
                        if (
                            "weekly limit" in low
                            or "usage limit" in low
                            or "hit your limit" in low
                        ):
                            limit_msg = (
                                m.group(0).strip().rstrip(".")
                                if m
                                else "Claude usage limit reached"
                            )
                            break
                if limit_msg:
                    break
        except Exception:
            pass
        return ran, limit_msg

    def _stall_reason(self):
        """Why drafts aren't draining, so the menu offers the RIGHT action:
          ('orphaned', '')        routines aren't firing -> Re-arm fixes it.
          ('rate_limited', msg)   routines fire but the account hit its Claude
                                  limit -> Re-arm is useless; wait/switch account.
          ('failing', '')         routines fire but drafts fail for another reason.
        Only meaningful when _autopilot_stalled() is True."""
        ran, limit_msg = self._recent_worker_outcome()
        if limit_msg:
            return ("rate_limited", limit_msg)
        if not ran:
            return ("orphaned", "")
        return ("failing", "")

    def _toggle_lane(self, lane):
        """Flip ONE engagement lane (personal_brand|promotion). Pure local state
        write (no model, no network): the cycle reads mode.json on its next run.
        Rebuild the menu right away so the checkmarks reflect the change instantly."""
        flags = st.toggle_lane(lane)
        pb, pr = flags.get("personal_brand"), flags.get("promotion")
        if pb and pr:
            msg = f"Personal brand + promotion both on (cycles split {self._split_pct()})"
        elif pb:
            msg = "Personal brand only: organic, link-free"
        elif pr:
            msg = "Promotion only: marketing your products"
        else:
            msg = "Both lanes off (cycle falls back to personal brand)"
        self._notify("S4L engagement lanes", msg)
        # Force the next tick to rebuild (flags are in the signature, but null it
        # so the rebuild can't be skipped) and rebuild now for snappy feedback.
        self._sig = None
        try:
            self._tick(None)
        except Exception as e:
            sys.stderr.write(f"[s4l-menubar] lane toggle rebuild failed: {e}\n")
            sys.stderr.flush()

    def _toggle_personal(self, _=None):
        self._toggle_lane(st.MODE_PERSONAL_BRAND)

    def _toggle_promotion(self, _=None):
        self._toggle_lane(st.MODE_PROMOTION)

    # Personal-brand share presets for the both-lanes-on state. rumps has no
    # slider, so the "Lane split" submenu offers these fixed points; the
    # dashboard's slider can set anything in between and the checkmark simply
    # lands on the nearest exact match (or none).
    SPLIT_PRESETS = (0.9, 0.75, 0.5, 0.25, 0.1)

    def _split_pct(self, share=None):
        """'70/30' style personal/promotion percent string for menu copy."""
        if share is None:
            share = st.read_split()
        return f"{round(share * 100)}/{round((1 - share) * 100)}"

    def _on_split_preset(self, share, _sender=None):
        """Menu callback shim: rumps passes the clicked MenuItem last."""
        self._set_split(share)

    def _set_split(self, share):
        """Set the personal-brand share (pure local mode.json write, same
        contract as _toggle_lane) and rebuild so the checkmark moves instantly."""
        written = st.write_split(share)
        self._notify(
            "S4L lane split",
            f"Cycles now split {self._split_pct(written)} personal brand/promotion",
        )
        self._sig = None
        try:
            self._tick(None)
        except Exception as e:
            sys.stderr.write(f"[s4l-menubar] lane split rebuild failed: {e}\n")
            sys.stderr.flush()

    # ---- factory reset (menu-bar driven) ----------------------------------
    def _reset_machine(self, _=None):
        """One-click 'reset this test machine to factory-fresh'. Runs the repo's
        scripts/reset-test-machine.sh, whose one standard path quits Claude
        Desktop, removes the Desktop extension + scheduled tasks, wipes the
        state dir, then restarts Claude Desktop fresh.

        CRITICAL self-kill avoidance: that script does `pkill -f s4l_menubar.py`
        and boots out the menubar LaunchAgent (steps near line 124/141). If we ran
        it as a direct child it would kill itself mid-wipe. So we detach it into its
        own session (start_new_session=True) with a distinct command line — `pkill`
        can't match it and the menubar dying doesn't take it down. The menubar is a
        launchd process (NOT a Claude child), so it's the right place to drive a
        reset that has to outlive Claude Desktop. Output streams to a log the user
        can inspect after the menubar disappears."""
        repo = os.environ.get("S4L_REPO_DIR") or ""
        script = os.path.join(repo, "scripts", "reset-test-machine.sh")
        if not repo or not os.path.exists(script):
            self._alert("Uninstall unavailable",
                        "Couldn't find reset-test-machine.sh. S4L_REPO_DIR isn't "
                        "pointing at a pipeline source on this machine.")
            return
        # ok=1 (plugin reset, keeps X login + browser layer), other=-1 (deep wipe),
        # cancel=0. See rumps.alert: default=1, alternate=0, other=-1.
        _activate_front()
        choice = _show_alert(
            title="Uninstall S4L?",
            message=(
                "This quits Claude Desktop, removes the S4L extension + its "
                "scheduled tasks, and wipes the state dir, then restarts Claude "
                "Desktop fresh (without S4L). This does NOT delete Claude Desktop "
                "itself. The menu bar will disappear during the uninstall.\n\n"
                "Uninstall: keep your X login + browser layer (quick uninstall).\n"
                "Deep wipe: also remove the shared browser profiles + toolchain."
            ),
            ok="Uninstall & Restart Claude", cancel="Cancel", other="Deep wipe",
        )
        if choice == 0:  # cancel
            return
        deep = (choice == -1)
        args = ["bash", script, "--yes"] + (["--deep"] if deep else [])
        log_path = "/tmp/s4l-reset.log"
        try:
            log = open(log_path, "ab", buffering=0)
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,   # detach: survive pkill + menubar bootout
                close_fds=True,
                cwd=repo,
            )
        except Exception as e:
            _capture(e, action="reset_machine")
            self._alert("Uninstall failed to start", str(e)[:200])
            return
        # Best-effort heads-up before the menubar gets pkilled by the script.
        self._notify(
            "S4L uninstall started",
            "Uninstalling" + (" (deep)" if deep else "") +
            "… the menu bar will vanish and Claude Desktop will restart when "
            "done; log at " + log_path,
        )

    def _alert(self, title, message):
        try:
            _activate_front()
            _show_alert(title=title, message=message, ok="OK")
        except Exception:
            pass

    # ---- disable scheduled tasks (menu-bar driven) ------------------------
    def _has_scheduled_tasks(self):
        """Read-only: True if any S4L worker/autopilot task is registered in any
        scheduled-tasks.json. Gates whether the 'Disable scheduled tasks' item is
        worth showing."""
        try:
            wanted = set(WORKER_TASK_IDS) | set(DEPRECATED_TASK_IDS)
            for f in glob.glob(SCHED_REGISTRY_GLOB):
                try:
                    with open(f) as fh:
                        d = json.load(fh)
                except Exception:
                    continue
                for t in d.get("scheduledTasks", []):
                    if t.get("id") in wanted:
                        return True
        except Exception:
            pass
        return False

    def _pause_toggle(self, _=None):
        """Pause/Resume: the lighter, fully reversible alternative to Quit. Stops
        S4L's own scan/draft/post pipeline (the launchd kicker + its support
        daemons) while leaving Claude Desktop, this tray, and the draft
        schedule registration alone — nothing is deleted, so it survives a
        Claude restart and Resume brings it right back. Runs the real
        launchctl work directly via st.pause_s4l()/resume_s4l() (this process
        already does its own launchctl calls elsewhere, e.g. _quit_work) so it
        works even when Claude Desktop is closed — no MCP/loopback round trip."""
        action = "resume" if os.path.exists(PAUSE_FLAG) else "pause"
        self._notify("S4L", "Resuming…" if action == "resume" else "Pausing…")
        threading.Thread(target=self._pause_work, args=(action,), daemon=True).start()

    def _pause_work(self, action):
        res = st.pause_s4l() if action == "pause" else st.resume_s4l()
        ok = bool(res.get("ok"))
        if action == "pause":
            self._notify(
                "S4L",
                "S4L paused. Drafting and posting are stopped; Claude and the tray "
                "stay up. Click Resume S4L to start again."
                if ok else "Couldn't fully pause S4L — see logs.",
            )
        else:
            self._notify(
                "S4L", "S4L resumed." if ok else "Couldn't fully resume S4L — see logs."
            )
        self._sig = None
        try:
            self._tick(None)
        except Exception as e:
            sys.stderr.write(f"[s4l-menubar] pause toggle rebuild failed: {e}\n")
            sys.stderr.flush()

    def _quit_app(self, _=None):
        """The single Quit path. Quitting stops the autopilot completely: the
        draft/query scheduled tasks are removed so they no longer fire, AND the
        tray itself goes away for good (stop flag + plist removal + self
        bootout — see _quit_work). Claude Desktop OWNS the live schedule and
        caches the registry in memory, clobbering any live edit on the next
        fire — so the only reliable way to disable them is to quit Claude,
        strip the tasks while it's down, then relaunch. We warn the user with a
        modal FIRST that Claude Desktop will restart, since the app window will
        close and reopen under them."""
        _activate_front()
        choice = _show_alert(
            title="Quit the S4L autoposter?",
            message=(
                "Quitting stops the autoposter completely: the draft + query "
                "scheduled tasks are removed so nothing fires anymore, and this "
                "menu bar icon goes away and stays away.\n\n"
                "Claude Desktop will quit and restart to apply this — its window "
                "will close and reopen in a moment. Your X login, browser layer, "
                "and config all stay.\n\n"
                "To start S4L again later, open Claude and say \"start S4L\" "
                "(or re-run setup)."
            ),
            ok="Quit & restart Claude", cancel="Cancel",
        )
        if choice != 1:  # only default button (OK) proceeds
            return
        self._notify("S4L", "Quitting… Claude will restart in a moment.")
        threading.Thread(target=self._quit_work, daemon=True).start()

    def _remove_scheduled_tasks(self):
        """Strip ALL S4L worker + deprecated tasks from every scheduled-tasks.json
        registry, and remove their on-disk task dirs. Caller MUST invoke this only
        while Claude is DOWN (the running app caches the registry and clobbers a
        live edit on the next fire). Best-effort; never raises. Sibling of
        _rewrite_scheduled_task_cwd, but it DELETES the worker tasks instead of
        relocating them."""
        wanted = set(WORKER_TASK_IDS) | set(DEPRECATED_TASK_IDS)
        try:
            for f in glob.glob(SCHED_REGISTRY_GLOB):
                try:
                    with open(f) as fh:
                        d = json.load(fh)
                except Exception:
                    continue
                tasks = d.get("scheduledTasks") or []
                kept = [t for t in tasks if t.get("id") not in wanted]
                if len(kept) == len(tasks):
                    continue
                d["scheduledTasks"] = kept
                try:
                    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(f))
                    with os.fdopen(fd, "w") as fh:
                        json.dump(d, fh, indent=2)
                    os.replace(tmp, f)
                except Exception:
                    pass
        except Exception:
            pass
        # Remove the on-disk task dirs (prompt/SKILL.md) so a stale file can't
        # re-register them.
        try:
            import shutil
            base = os.path.join(os.path.expanduser("~"), ".claude", "scheduled-tasks")
            for tid in wanted:
                shutil.rmtree(os.path.join(base, tid), ignore_errors=True)
        except Exception:
            pass

    def _quit_work(self):
        """Quit/kill Claude, strip the scheduled tasks while it's down, relaunch
        Claude, then take THIS tray down for good. Mirror of
        _relocate_restart_work's restart block. The menu bar is a separate
        launchd process, so killing Claude does not kill us.

        The stop flag is written FIRST: the relaunched Claude boots the MCP
        server, whose ensureMenubar() would otherwise reinstall the tray
        unconditionally (the reappearing-icon bug). The plist is deleted so
        RunAtLoad can't resurrect us at next login, and the final bootout
        removes the KeepAlive job — which kills this process, so it must be
        the last thing we do."""
        try:
            # Capture while Claude is still alive (see _claude_user_data_dirs):
            # the post-quit relaunch must preserve custom --user-data-dirs.
            user_data_dirs = self._claude_user_data_dirs()
            try:
                with open(STOP_FLAG, "w") as fh:
                    fh.write(f"user quit via menu bar at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
            except Exception as e:
                _capture(e, action="quit_stop_flag")
            self._quit_claude_and_wait()
            self._remove_scheduled_tasks()
            try:
                os.remove(MENUBAR_PLIST)
            except FileNotFoundError:
                pass
            except Exception as e:
                _capture(e, action="quit_remove_plist")
            self._relaunch_claude(user_data_dirs)
            self._notify("S4L", "S4L stopped. Say \"start S4L\" in Claude to bring it back.")
        except Exception as e:
            _capture(e, action="quit_app")
            self._notify("S4L", "Couldn't fully stop S4L — see logs.")
        finally:
            # Boot out our own KeepAlive agent. launchd kills this process as
            # part of the bootout, so nothing after this line is guaranteed to
            # run. Runs even if the Claude restart above failed: the user asked
            # for the tray to be gone.
            subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{MENUBAR_LABEL}"],
                capture_output=True, timeout=15,
            )
            # Only reached if bootout didn't kill us (e.g. dev run outside
            # launchd). Exit 0: KeepAlive {SuccessfulExit: false} treats a clean
            # exit as final. os._exit because we're on a background thread.
            os._exit(0)

    def _update(self, _=None):
        self._send_to_claude(UPDATE_PROMPT)

    # ---- .mcpb self-update (menu-bar driven) ------------------------------
    @staticmethod
    def _ext_dir():
        """Resolve this plugin's Claude Desktop extension dir.

        Claude derives the extension id from the manifest author, so it changed
        `local.mcpb.m13v.social-autoposter` ->
        `local.mcpb.s4l.ai.social-autoposter` when the author became "S4L.ai". A
        hardcoded id silently breaks the self-update button on every fresh
        install (the update unzips into a dir that doesn't exist, so the version
        never advances and fixes never land). Pick the newest `*social-autoposter`
        extension dir that actually has a manifest.json; fall back to the
        historical id so old boxes are unaffected.

        Scans "Claude*" app-support roots, not just "Claude": the host app can
        run with a custom --user-data-dir (per-account dirs like
        "Claude-mediar"), and the live extension lives under THAT dir while
        plain "Claude/" may have no Claude Extensions at all. Same blind spot
        family as scripts/schedule_state.py::SCHED_REGISTRY_GLOB (fixed
        2026-07-02); here it made the update button unzip into a void and fail
        verification on such machines.
        """
        app_support = os.path.expanduser("~/Library/Application Support")
        best, best_mtime = None, -1.0
        for root in glob.glob(os.path.join(app_support, "Claude*", "Claude Extensions")):
            try:
                for name in os.listdir(root):
                    if not name.endswith("social-autoposter"):
                        continue
                    d = os.path.join(root, name)
                    if not os.path.exists(os.path.join(d, "manifest.json")):
                        continue
                    m = os.path.getmtime(d)
                    if m > best_mtime:
                        best, best_mtime = d, m
            except OSError:
                continue
        return best or os.path.join(
            app_support, "Claude", "Claude Extensions",
            "local.mcpb.m13v.social-autoposter",
        )

    MCPB_URL = (
        "https://github.com/m13v/s4l/releases/latest/download/"
        "social-autoposter.mcpb"
    )
    RELEASE_API = (
        "https://api.github.com/repos/m13v/s4l/releases/latest"
    )

    def _mcpb_url(self):
        """Download URL for THIS box's channel. Stable uses releases/latest
        (server-resolved); staging pulls the specific resolved tag, since
        releases/latest excludes the prerelease a staging box wants. Falls back to
        releases/latest whenever the tag is unknown."""
        if self._channel == "staging" and self._latest_tag:
            return (
                "https://github.com/m13v/s4l/releases/download/"
                "%s/social-autoposter.mcpb" % self._latest_tag
            )
        return self.MCPB_URL


    def _do_mcpb_update(self, _=None):
        """User clicked 'Update now & restart Claude Desktop'. Pull the latest .mcpb, unpack it over
        the Desktop extension dir in place, and restart Claude so the new server
        loads. The menu bar is a launchd process (not a Claude child), so the
        restart is clean. Heavy work runs on a background thread.

        Give instant feedback before any of that: the click used to be silent
        until the marker file appeared (up to 300s later), reading as a dead
        button. Flip _updating_mcpb, kick the spinner, and force a menu
        rebuild synchronously so the title + disabled items change this tick,
        not on the next 5s poll."""
        if self._update_in_flight():
            return  # already running; ignore a double-click
        self._updating_mcpb = True
        self._sig = None
        if self._spinner is None:
            self._spin_i = 0
            self._spinner = rumps.Timer(self._spin, 0.12)
            self._spinner.start()
        self._notify("S4L", "Updating… Claude will restart in a moment.")
        threading.Thread(target=self._mcpb_update_work, daemon=True).start()
        self._tick(None)

    @staticmethod
    def _claude_user_data_dirs():
        """The --user-data-dir of every RUNNING Claude instance, in ps order;
        a default-profile instance (no flag) is recorded as None. Empty when
        no Claude is running.

        Must be captured BEFORE quitting Claude: a bare `open -a Claude`
        relaunch drops the flag and boots the DEFAULT-profile instance,
        stranding users who run Claude with a per-account data dir (found
        2026-07-02: the update restart landed in the wrong profile). killall
        takes down EVERY profile's instance, so all of them must be captured
        and relaunched, not just the first ps match. The value can contain
        spaces (…/Application Support/…), so parse the ps line with a regex
        up to the next ` --` flag, not by token split.
        """
        dirs = []
        try:
            out = subprocess.run(["ps", "-axo", "command"], capture_output=True,
                                 text=True, timeout=10).stdout
            for line in out.splitlines():
                if "/Claude.app/Contents/MacOS/Claude" not in line:
                    continue
                m = re.search(r"--user-data-dir=(.+?)(?= --|$)", line)
                d = m.group(1).strip() if m else None
                if d not in dirs:
                    dirs.append(d)
        except Exception:
            pass
        return dirs

    @classmethod
    def _claude_user_data_dir(cls):
        """First custom --user-data-dir among running instances, or None.
        Prefer _claude_user_data_dirs when relaunching after a kill — the
        kill takes every profile down, not just this one."""
        return next((d for d in cls._claude_user_data_dirs() if d), None)

    @staticmethod
    def _claude_running():
        """True while a Claude Desktop main process is alive. pgrep -x matches
        the binary name exactly, so 'Claude Helper …' renderers and claude-code
        CLI children don't count."""
        try:
            return subprocess.run(["pgrep", "-x", "Claude"],
                                  capture_output=True, timeout=10).returncode == 0
        except Exception:
            return False

    def _quit_claude_and_wait(self, grace_sec=300):
        """Ask Claude to quit and return only once every instance is gone,
        escalating to killall if the graceful quit stalls.

        The quit Apple event doesn't get its reply until the app finishes
        tearing down, which can take minutes with claude-code sessions open.
        2026-07-02: teardown outlived the old inline block's 20s subprocess
        timeout, the TimeoutExpired flew past the caller's relaunch step, and
        Claude finished quitting on its own with nothing left to restart it.
        A timeout on the osascript call is expected and harmless; process
        polling is the real completion signal."""
        try:
            subprocess.run(["osascript", "-e", 'tell application "Claude" to quit'],
                           capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.time() + grace_sec
        while self._claude_running() and time.time() < deadline:
            time.sleep(3)
        if self._claude_running():
            subprocess.run(["killall", "Claude"], capture_output=True)  # quit stalled
            time.sleep(2)
        if self._claude_running():
            subprocess.run(["killall", "-9", "Claude"], capture_output=True)
            time.sleep(1)

    def _relaunch_claude(self, user_data_dirs=None):
        """Reopen Claude, preserving each custom --user-data-dir captured
        before the kill (accepts a single dir or a list). `open -n` forces a
        fresh instance per profile — without it LaunchServices focuses the
        first instance and drops the args, so only one profile would return.
        Retries once if no process appears: right after a kill, `open` can
        no-op while LaunchServices still thinks the app is running. Keep in
        sync with the other relaunch sites."""
        if isinstance(user_data_dirs, str):
            user_data_dirs = [user_data_dirs]

        def _open_all():
            env = _claude_launch_env()
            for d in (user_data_dirs or [None]):
                if d:
                    subprocess.run(
                        ["open", "-n", "-a", CLAUDE_APP, "--args",
                         f"--user-data-dir={d}"],
                        capture_output=True, timeout=20, env=env)
                else:
                    subprocess.run(["open", "-a", CLAUDE_APP],
                                   capture_output=True, timeout=20, env=env)

        _open_all()
        time.sleep(5)
        if not self._claude_running():
            time.sleep(5)
            _open_all()

    def _mcpb_update_work(self):
        tmpd = tempfile.mkdtemp(prefix="s4l-update-")
        mcpb = os.path.join(tmpd, "social-autoposter.mcpb")
        try:
            # Capture while Claude is still alive; unreadable after the kill.
            user_data_dirs = self._claude_user_data_dirs()
            r = subprocess.run(["curl", "-fLs", "-m", "300", self._mcpb_url(), "-o", mcpb],
                               capture_output=True, timeout=320)
            if r.returncode != 0 or not os.path.exists(mcpb) or os.path.getsize(mcpb) < 100000:
                self._updating_mcpb = False
                self._sig = None
                self._notify("S4L update failed", "Couldn't download the update — check your connection.")
                return
            r = subprocess.run(["unzip", "-oq", mcpb, "-d", self._ext_dir()],
                               capture_output=True, timeout=180)
            if r.returncode != 0:
                self._updating_mcpb = False
                self._sig = None
                self._notify("S4L update failed", "Couldn't unpack the update.")
                return
            # Record what we just installed so the tick loop can verify the
            # EFFECTIVE version actually advanced after the restart. The old
            # flow claimed success unconditionally, which lied on boxes whose
            # pipeline repo was pinned (e.g. by a stray git checkout): the
            # extension dir updated but the running install stayed old.
            target = ""
            try:
                with open(os.path.join(self._ext_dir(), "manifest.json")) as f:
                    target = str((json.load(f) or {}).get("version") or "")
            except Exception:
                target = ""
            if target:
                try:
                    with open(self._update_verify_path(), "w") as f:
                        json.dump({"target": target, "started_at": time.time()}, f)
                except Exception:
                    pass
            # Restart Claude so the refreshed server loads (we're decoupled from it).
            self._quit_claude_and_wait()
            # Claude is fully down now — relocate the autopilot scheduled tasks'
            # cwd so their once-a-minute runs stop flooding the user's interactive
            # `claude --resume` history. MUST happen while Claude is down (it caches
            # the registry in memory and clobbers live edits). See queueWorkerCwd()
            # in mcp/src/index.ts and the same routine in scripts/s4l_box_update.sh.
            # _fresh (not the in-process call): this is a mid-update self-heal,
            # so it must run the code that was JUST downloaded, not whatever
            # this already-running (about to be replaced) process imported at
            # ITS OWN startup — see _rewrite_scheduled_task_cwd_fresh's docstring.
            self._rewrite_scheduled_task_cwd_fresh()
            if target:
                # The graceful quit can eat minutes; restart the verify clock
                # now that Claude is actually down so UPDATE_VERIFY_GRACE_SEC
                # measures server boot, not app teardown.
                try:
                    with open(self._update_verify_path(), "w") as f:
                        json.dump({"target": target, "started_at": time.time()}, f)
                except Exception:
                    pass
            self._relaunch_claude(user_data_dirs)
            self._update_available = False
            self._sig = None
            if target:
                # Honest phrasing: the verdict (success OR the real blocker)
                # comes from _check_update_verdict once the new server settles.
                self._notify("S4L update", f"v{target} installed; Claude is restarting. I'll confirm once it's live.")
            else:
                self._notify("S4L updated", "Claude restarted on the latest version.")
        except Exception as e:
            self._updating_mcpb = False
            self._sig = None
            self._notify("S4L update failed", str(e)[:140])
        finally:
            try:
                import shutil
                shutil.rmtree(tmpd, ignore_errors=True)
            except Exception:
                pass

    # ---- post-update verification (marker + tick-driven verdict) ----------
    # _mcpb_update_work writes a marker with the version it unpacked; the tick
    # loop (which survives the Claude restart, and also runs in the REPLACEMENT
    # menu bar process if the server reloads this agent) compares it against the
    # version the pipeline actually resolves to. Success notifies honestly;
    # failure names the real blocker (a stray git checkout pinning the repo)
    # instead of the old unconditional "restarted on the latest version" toast.
    UPDATE_VERIFY_GRACE_SEC = 240

    @staticmethod
    def _update_verify_path():
        return os.path.join(st.state_dir(), "update-verify.json")

    def _update_in_flight(self):
        """True from the instant 'Update now' is clicked (_updating_mcpb, set
        before curl/unzip even start) through Claude's restart + verification
        (update-verify.json). Single predicate the title spinner, the menu's
        update section, and the disable-other-items pass all read, so there's
        no gap between click and marker where the UI looks idle."""
        return self._updating_mcpb or os.path.exists(self._update_verify_path())

    @staticmethod
    def _effective_version():
        """Return (version, repo_dir) the install actually runs, reading the
        same sources snapshot.py uses. runtime.json's repo_dir is authoritative
        (it is what the server re-points after healing a stray checkout); the
        env / ~/social-autoposter fallbacks mirror the legacy resolution."""
        repo = None
        try:
            with open(os.path.join(st.state_dir(), "runtime.json")) as f:
                rd = (json.load(f) or {}).get("repo_dir")
            if rd and os.path.isdir(os.path.join(rd, "scripts")):
                repo = rd
        except Exception:
            pass
        if not repo:
            repo = os.environ.get("S4L_REPO_DIR") or os.path.expanduser(
                "~/social-autoposter"
            )
        for rel in (("mcp", "dist", "version.json"), ("package.json",)):
            try:
                with open(os.path.join(repo, *rel)) as f:
                    v = (json.load(f) or {}).get("version")
                if v:
                    return str(v), repo
            except Exception:
                continue
        return None, repo

    def _check_update_verdict(self):
        p = self._update_verify_path()
        if not os.path.exists(p):
            return
        try:
            with open(p) as f:
                marker = json.load(f) or {}
        except Exception:
            marker = {}
        target = str(marker.get("target") or "")
        try:
            started = float(marker.get("started_at") or 0)
        except (TypeError, ValueError):
            started = 0.0
        if not target:
            self._drop_update_marker(p)
            return
        effective, repo = self._effective_version()
        # ONE comparator: st.ver_key delegates to scripts/snapshot.py::_ver_key
        # (rc-aware). If the snapshot module can't load this tick, skip the
        # verdict; the grace window below still resolves the marker either way.
        try:
            settled = bool(effective) and st.ver_key(effective) >= st.ver_key(target)
        except Exception:
            settled = False
        if settled:
            self._drop_update_marker(p)
            self._notify("S4L updated", f"Now on v{effective}.")
            return
        if time.time() - started < self.UPDATE_VERIFY_GRACE_SEC:
            return  # Claude restart + server boot + pipeline refresh still settling
        self._drop_update_marker(p)
        if repo and os.path.isdir(os.path.join(repo, ".git")):
            self._notify(
                "S4L update did not take effect",
                f"Still v{effective or 'unknown'}: the install is pinned by a git "
                f"checkout at {repo}. Remove or rename that folder, then update again.",
            )
        else:
            self._notify(
                "S4L update did not take effect",
                f"Still v{effective or 'unknown'} (target v{target}). "
                "Try updating again from the menu.",
            )

    def _drop_update_marker(self, p):
        try:
            os.remove(p)
        except OSError:
            pass
        # Single resolve point for _update_in_flight: whatever path got us
        # here (settled, timed out, or a stale no-target marker), the in-flight
        # window is over, so re-enable the rest of the menu on the next paint.
        self._updating_mcpb = False
        self._sig = None

    @staticmethod
    def _scheduled_task_cwd_needs_fix():
        """Read-only: True if any worker task runs in the wrong folder, the
        deprecated autopilot task still exists, OR a legacy per-type worker
        registration remains (to be consolidated into the single s4l-worker).
        Drives the one-shot self-heal."""
        try:
            for f in glob.glob(SCHED_REGISTRY_GLOB):
                try:
                    with open(f) as fh:
                        d = json.load(fh)
                except Exception:
                    continue
                for t in d.get("scheduledTasks", []):
                    tid = t.get("id")
                    if tid in DEPRECATED_TASK_IDS:
                        return True
                    if tid in LEGACY_WORKER_TASK_IDS:
                        return True
                    if tid in WORKER_TASK_IDS and t.get("cwd") != WORKER_CWD:
                        return True
        except Exception:
            pass
        return False

    @staticmethod
    def _ensure_worker_skill_md():
        """Make sure ~/.claude/scheduled-tasks/s4l-worker/SKILL.md exists before
        we register a task that points at it. The MCP writes it on every boot
        (create-if-missing), so normally this is a no-op; as a belt-and-suspenders
        fallback we clone a legacy worker's file (same universal body since
        prompt v7) and fix the frontmatter name."""
        base = os.path.join(os.path.expanduser("~"), ".claude", "scheduled-tasks")
        dst = os.path.join(base, WORKER_TASK_ID, "SKILL.md")
        if os.path.exists(dst):
            return True
        for tid in LEGACY_WORKER_TASK_IDS:
            src = os.path.join(base, tid, "SKILL.md")
            try:
                with open(src) as fh:
                    body = fh.read()
            except Exception:
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "w") as fh:
                    fh.write(body.replace(f"name: {tid}", f"name: {WORKER_TASK_ID}", 1))
                return True
            except Exception:
                continue
        return False

    def _rewrite_scheduled_task_cwd(self):
        """Registry self-heal, run ONLY while Claude is DOWN (the running app
        caches the registry in memory and clobbers a live edit on the next
        fire). Delegates to scripts/scheduled_task_selfheal.py::heal() — the
        single implementation, importable in-process (here) or runnable as a
        standalone subprocess (see _rewrite_scheduled_task_cwd_fresh below,
        used by the update/relocate flows specifically because THIS in-process
        call can only ever execute whatever code this already-running process
        imported at its own startup, which is stale mid-update; see that
        script's module docstring for the full story, 2026-07-08).
        Best-effort: never raises. Kept in sync with scripts/s4l_box_update.sh
        and queueWorkerCwd()/QUEUE_WORKERS in mcp/src/index.ts."""
        try:
            import scheduled_task_selfheal
            scheduled_task_selfheal.heal()
        except Exception:
            pass

    def _fresh_pipeline_tmpdir(self):
        """Extract THIS UPDATE's embedded pipeline.tgz (already unpacked to
        self._ext_dir() by the caller) into a throwaway temp dir and return
        its root, or None on any failure. See _rewrite_scheduled_task_cwd_fresh
        for why this matters: a subprocess run from here always imports
        whatever is on disk at invocation time, so — unlike an in-process
        call on this already-running (and about to be replaced) process — it
        can never be stale."""
        try:
            pipeline_tgz = os.path.join(self._ext_dir(), "dist", "pipeline.tgz")
            if not os.path.exists(pipeline_tgz):
                return None
            tmpd = tempfile.mkdtemp(prefix="s4l-selfheal-")
            r = subprocess.run(["tar", "-xzf", pipeline_tgz, "-C", tmpd],
                                capture_output=True, timeout=60)
            return tmpd if r.returncode == 0 else None
        except Exception:
            return None

    def _rewrite_scheduled_task_cwd_fresh(self):
        """Run the registry self-heal via a FRESH SUBPROCESS unpacked from the
        just-downloaded bundle, instead of self._rewrite_scheduled_task_cwd
        (in-process — can only ever run the code THIS process imported at ITS
        OWN startup). Found on a real test box (2026-07-08): a self-heal fix
        shipped in a release silently did nothing during the very update that
        shipped it, because _mcpb_update_work calls the self-heal BEFORE this
        process quits and relaunches with the new code — Python does not
        hot-reload an already-imported module just because newer .py files
        landed on disk mid-run. Every future fix to the self-heal logic would
        have hit this same "one version behind" bug via this call site.
        Falls back to the in-process call if extraction fails for any reason
        (best-effort; a same-version restart isn't stale anyway)."""
        tmpd = self._fresh_pipeline_tmpdir()
        if not tmpd:
            self._rewrite_scheduled_task_cwd()
            return
        try:
            script = os.path.join(tmpd, "package", "scripts", "scheduled_task_selfheal.py")
            if not os.path.exists(script):
                self._rewrite_scheduled_task_cwd()
                return
            py = os.environ.get("S4L_PYTHON") or sys.executable or "python3"
            subprocess.run([py, script], capture_output=True, timeout=60)
        except Exception:
            self._rewrite_scheduled_task_cwd()
        finally:
            try:
                import shutil
                shutil.rmtree(tmpd, ignore_errors=True)
            except Exception:
                pass

    def _maybe_relocate_tasks(self, _=None):
        """Timer callback: detect, then ASK. If the autopilot tasks are in the wrong
        folder (or the deprecated task lingers), prompt the user before relocating —
        the fix needs a single Claude restart (the app caches the registry in
        memory), so we never restart silently. Prompts at most once per process;
        'Later' stops the auto-prompt but the fix stays available from the menu
        ('Tidy autopilot history'). Runs on the main thread (rumps timer), so the
        modal is safe to raise here."""
        if self._relocating or self._cwd_healed or self._reloc_prompted:
            return
        try:
            if not self._scheduled_task_cwd_needs_fix():
                self._reloc_needed = False
                return
            self._reloc_needed = True
            self._reloc_prompted = True
            try:
                self._reloc_timer.stop()   # one auto-prompt per process
            except Exception:
                pass
            self._prompt_relocate_tasks()
        except Exception:
            pass

    def _prompt_relocate_tasks(self, _=None):
        """Modal-first relocate. Warns (like Quit does) that Claude restarts once,
        then runs the kill -> rewrite cwd -> relaunch on a background thread. Wired
        to both the auto-detect timer and the 'Tidy autopilot history' menu item
        (the `_` arg), so 'Later' is never a dead end."""
        if self._relocating:
            return
        if not self._scheduled_task_cwd_needs_fix():
            self._reloc_needed = False
            self._cwd_healed = True
            return
        _activate_front()
        choice = _show_alert(
            title="Tidy S4L background tasks?",
            message=(
                "Move the background worker to a dedicated folder so its "
                "once-a-minute runs stop cluttering your `claude --resume` "
                "history.\n\n"
                "Claude Desktop will restart once to apply. Your X login, "
                "drafts, and config all stay."
            ),
            ok="Tidy & restart Claude", cancel="Later",
        )
        if choice != 1:  # only default button (OK) proceeds
            return
        self._relocating = True
        self._notify("S4L", "Tidying autopilot… Claude will restart once.")
        threading.Thread(target=self._relocate_restart_work, daemon=True).start()

    def _relocate_restart_work(self):
        """Restart Claude with the tasks relocated. Mirror of _mcpb_update_work's
        restart block: quit/kill Claude, rewrite the registry while it's down, then
        relaunch. The menu bar is a separate launchd process, so killing Claude does
        not kill us."""
        try:
            # Capture while Claude is still alive (see _claude_user_data_dirs):
            # a bare relaunch drops a custom --user-data-dir and boots the
            # wrong profile, which orphans these very tasks from the registry.
            user_data_dirs = self._claude_user_data_dirs()
            self._quit_claude_and_wait()
            # _fresh here too (2026-07-08): no code update happens in this flow
            # today, so it behaves identically to the in-process call (safe
            # fallback either way) — used unconditionally at every quit-heal-
            # relaunch call site so this can never regress silently if that
            # ever changes, rather than reasoning per call site about staleness.
            self._rewrite_scheduled_task_cwd_fresh()
            self._relaunch_claude(user_data_dirs)
            time.sleep(8)  # let Claude reload the registry before we re-check
            if not self._scheduled_task_cwd_needs_fix():
                self._cwd_healed = True
                self._reloc_needed = False
            # Push a fresh heartbeat now so the server/dashboard reflects the
            # corrected scheduled-task folder state within seconds instead of
            # waiting up to ~15 min for the next MCP heartbeat. Best-effort.
            self._fire_heartbeat()
        except Exception:
            pass
        finally:
            self._relocating = False

    def _fire_heartbeat(self):
        """Best-effort: run the npx-lane heartbeat.sh once so the install's
        scheduled_tasks sample updates centrally right after a relocation. Never
        raises; a missing repo/script or network hiccup is silently ignored (the
        MCP's own ~15-min heartbeat is the durable channel)."""
        try:
            repo = os.environ.get("S4L_REPO_DIR") or ""
            hb = os.path.join(repo, "scripts", "heartbeat.sh")
            if not (repo and os.path.exists(hb)):
                return
            env = dict(os.environ, REPO_DIR=repo)
            subprocess.run(["bash", hb], capture_output=True, timeout=30, env=env)
        except Exception:
            pass

    def _open_dashboard(self, _=None):
        # ONE serving path (2026-07-03, per user): always the menu-bar dashboard
        # server. It serves panel.html and answers reads from scripts/snapshot.py,
        # so it works identically whether Claude is up or not. The old "prefer the
        # live MCP loopback" heuristic is gone on purpose: panel-endpoint.json is
        # last-writer-wins across MANY short-lived MCP instances (Claude Code
        # sessions, queue workers), so it usually pointed at a dead server and the
        # dashboard flapped between two paths. Agent actions on this page hand off
        # to Claude (isError -> "open Claude"); do NOT reintroduce the loopback
        # preference or any second dashboard path.
        try:
            import dashboard_server  # mcp/menubar/dashboard_server.py
            url = dashboard_server.url() or dashboard_server.start()
        except Exception:
            url = None
        if url:
            subprocess.run(["open", url], capture_output=True)
        else:
            self._open_claude()

    def _notify(self, title, message):
        # rumps.notification needs an .app bundle id; a bare launchd script has
        # none, so drive Notification Center through osascript instead.
        script = (
            f"display notification {json.dumps(message)} "
            f"with title {json.dumps(title)}"
        )
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        except Exception:
            pass

    # _toggle_ap removed: autopilot is the Claude Desktop scheduled task now, managed
    # in the Scheduled tab. The menu bar mirrors the dashboard (no launchd toggle).

    # ---- activity spinner -------------------------------------------------
    # The server writes activity.json while a tool runs (scanning/drafting/
    # posting/…). _poll_activity (1s) starts the fast spinner; _spin (0.12s)
    # animates the title with the label and stops itself when activity clears.
    # Both run on the main thread (rumps timers).
    def _poll_activity(self, _):
        # Refresh the durable plan-based posting label (cheap: one small file read)
        # so the title can show steady posting progress even when the server's
        # per-post activity.json is momentarily empty between posts.
        self._posting_label = self._compute_posting_label()
        # An update in flight always owns the title (Claude is about to
        # restart regardless of stall/posting state) — make sure the spinner
        # is running and skip the stall short-circuit below, which would
        # otherwise stomp "S4L updating" with "S4L ⚠" within a second.
        if self._update_in_flight():
            if self._spinner is None:
                self._spin_i = 0
                self._spinner = rumps.Timer(self._spin, 0.12)
                self._spinner.start()
            return
        # While the autopilot is stalled, the server's "drafting"/"scanning" label is
        # stale (the producer re-asserts it for the whole time it blocks on a job no
        # routine will claim). Don't let the spinner own the title with that lie —
        # drop it and paint the ⚠ stall state. A genuine posting drain (durable
        # posting label) is real work, so it still shows.
        if self._stalled and not self._posting_label:
            if self._spinner is not None:
                self._spinner.stop()
                self._spinner = None
            self.title = "S4L ⚠"
            return
        act = st.read_activity()
        has_label = bool((act and act.get("label")) or self._posting_label)
        if has_label and self._spinner is None:
            self._spin_i = 0
            self._spinner = rumps.Timer(self._spin, 0.12)
            self._spinner.start()

    def _compute_posting_label(self):
        """Posting progress from the durable review-queue plan, with hysteresis.

        A drain is detected by the plan's posted count INCREASING (or the server's
        activity.json reporting a post in flight) — never by the raw unposted
        backlog, which sits non-zero for drafts merely awaiting review. The label
        is held for a grace window after the last post so the indicator doesn't
        blink back to "S4L" during the multi-second gaps between posts. Survives a
        menu bar restart and reflects posts driven by the autopilot/agent, not just
        this process's own approval queue."""
        now = time.time()
        try:
            posted = st.review_queue_posted_count()
        except Exception:
            posted = None
        act = st.read_activity()
        act_posting = bool(act and "posting" in str(act.get("label") or ""))
        if posted is None:
            posted = self._drain_last_posted  # ride through a transient read miss
        if posted is None:
            return None
        if self._drain_last_posted is None:
            self._drain_last_posted = posted
        advanced = posted > self._drain_last_posted
        if advanced or act_posting:
            if self._drain_baseline is None:
                # New drain: baseline is the count just BEFORE its first post.
                self._drain_baseline = self._drain_last_posted
            self._drain_last_change = now
        if advanced:
            self._drain_last_posted = posted
        if self._drain_baseline is None:
            return None
        # Drain is over once the server is idle AND no new post landed for a grace
        # window (covers the long gaps between the last few slow posts).
        if not act_posting and now - self._drain_last_change > 45.0:
            self._drain_baseline = None
            return None
        sent = max(0, posted - self._drain_baseline)
        return f"posting +{sent}"

    def _spin(self, _):
        # An update in flight beats everything else: the click-to-restart
        # window needs its own unmistakable, unmasked spinner state.
        if self._update_in_flight():
            self._spin_i = (self._spin_i + 1) % len(SPINNER)
            self.title = f"S4L updating {SPINNER[self._spin_i]}"
            return
        # Stall beats a stale activity label: bail (and self-stop) so the title
        # falls back to "S4L ⚠" rather than a "drafting" lie. _poll_activity also
        # stops us within 1s; this makes the switch immediate.
        if self._stalled and not self._posting_label:
            if self._spinner is not None:
                self._spinner.stop()
                self._spinner = None
            self.title = "S4L ⚠"
            return
        act = st.read_activity()
        label = act.get("label") if act else None
        act_state = act.get("state") if act else None
        # For POSTING, prefer the menu bar's durable cumulative label over the
        # server's per-call "1/1" so the count climbs smoothly and the indicator
        # holds through the gaps between posts. Non-posting activity (scanning /
        # drafting) keeps its own server label.
        if self._posting_label and (
            not label or act_state == "posting" or "posting" in str(label)
        ):
            label = self._posting_label
        if label:
            # The update arrow must stay visible even while a tool runs, so the
            # "update available" signal is never masked by activity. _tick skips the
            # title repaint while the spinner owns it, so the arrow is injected here.
            head = "S4L ⬆" if self._update_available else "S4L"
            # A "✓" label (e.g. "posted 3/10 ✓") is a momentary confirmation, not
            # ongoing work — show it without the spinner glyph so it reads as done.
            if "✓" in label:
                self.title = f"{head} {label}"
            else:
                self._spin_i = (self._spin_i + 1) % len(SPINNER)
                self.title = f"{head} {label} {SPINNER[self._spin_i]}"
            return
        try:
            if self._spinner is not None:
                self._spinner.stop()
        except Exception:
            pass
        self._spinner = None
        self.title = "S4L"
        self._sig = None  # force the next tick to repaint title + menu

    def _resume_approved_queue(self):
        """Restart recovery: re-enqueue approvals that were recorded durably but
        never confirmed posted (the in-memory _post_q died with the old process).
        Skip any the plan already shows as posted, so a card that landed on X just
        before the kill — but whose status update was lost — isn't posted twice."""
        # Store lane (canonical): approved-but-unposted rows in the review store.
        # Legacy lane: pre-store approved-queue.json entries (read-only now; the
        # ledger is no longer written). Union, store first, dedup by (batch, n).
        pending = list(st.store_pending_posts())
        seen = {(it.get("batch"), it.get("n")) for it in pending}
        legacy = [
            it for it in st.approved_queue_pending()
            if (it.get("batch"), it.get("n")) not in seen
        ]
        pending.extend(legacy)
        if not pending:
            return
        posted_ns = set()
        try:
            req = st.read_review_request()
            plan_path = (req or {}).get("plan_path") or st.store_path()
            plan = st.read_plan(plan_path)
            for i, c in enumerate(((plan or {}).get("candidates") or [])):
                if c.get("posted") is True:
                    posted_ns.add(i + 1)
        except Exception:
            pass
        resumed = 0
        for it in pending:
            batch, n = it.get("batch"), it.get("n")
            if n in posted_ns:
                st.approved_queue_set_status(batch, n, "posted")  # reconcile lost update
                continue
            decision = {
                "n": n,
                "approved": True,
                "candidate_id": it.get("candidate_id"),
                "text": it.get("text") or "",
                "edited": bool(it.get("edited")),
                "drop_link": bool(it.get("drop_link")),
            }
            with self._review_lock:
                self._posts_outstanding += 1
                self._posting_batch_total += 1
                self._review_active = True
                self._write_posting_activity_locked()
            self._post_q.put((batch, decision))
            resumed += 1
        if resumed:
            self._ensure_post_worker()
            sys.stderr.write(
                f"[s4l-menubar] resumed {resumed} approved-but-unposted draft(s) after restart\n"
            )
            sys.stderr.flush()
            self._notify("S4L", f"Resuming {resumed} approved draft(s) after restart…")

    # ---- tick: read state, set title, (re)build menu ----------------------
    def _probe_statusitem(self):
        """Actual on-screen state of our NSStatusItem: (visible_pref, on_screen).

        visible_pref False = the item was removed from the bar (e.g. the user
        Cmd-dragged it off). on_screen False with visible_pref True = the item
        exists but macOS is not drawing it: menu bar full / notch clipping, a
        status-item render glitch after a login storm, OR a benign fullscreen
        app / locked screen (why this feeds telemetry, never an automatic
        action). Two independent signals are recorded because we don't yet know
        which one is reliable in the field: the window's occlusion state and
        whether its frame intersects any screen (clipped items get parked
        off-screen). Either value may be None when the probe can't answer (old
        macOS, rumps internals moved). Never raises."""
        try:
            item = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
            if item is None:
                return None, None
            visible_pref = None
            try:
                visible_pref = bool(item.isVisible())
            except Exception:
                pass
            on_screen = None
            try:
                win = item.button().window()
                if win is not None:
                    from AppKit import NSScreen, NSWindowOcclusionStateVisible
                    occluded_visible = bool(
                        win.occlusionState() & NSWindowOcclusionStateVisible
                    )
                    f = win.frame()
                    intersects = False
                    for s in NSScreen.screens():
                        sf = s.frame()
                        if (
                            f.origin.x < sf.origin.x + sf.size.width
                            and f.origin.x + f.size.width > sf.origin.x
                            and f.origin.y < sf.origin.y + sf.size.height
                            and f.origin.y + f.size.height > sf.origin.y
                        ):
                            intersects = True
                            break
                    on_screen = occluded_visible and intersects
            except Exception:
                pass
            return visible_pref, on_screen
        except Exception:
            return None, None

    def _icon_watch_tick(self, now):
        """60s-throttled visibility probe + menubar-state.json write. See the
        __init__ comment for why this exists and why it never self-heals."""
        if now < self._icon_check_due_at:
            return
        self._icon_check_due_at = now + 60.0
        try:
            visible_pref, on_screen = self._probe_statusitem()
            if on_screen is False:
                if self._icon_hidden_since is None:
                    self._icon_hidden_since = now
            else:
                self._icon_hidden_since = None
            hidden_for = (
                int(now - self._icon_hidden_since)
                if self._icon_hidden_since is not None
                else 0
            )
            if on_screen != self._icon_last_on_screen:
                sys.stderr.write(
                    f"[s4l-menubar] statusitem on_screen={on_screen} "
                    f"visible_pref={visible_pref} hidden_for_sec={hidden_for}\n"
                )
                sys.stderr.flush()
                self._icon_last_on_screen = on_screen
            dest = os.path.join(st.state_dir(), "menubar-state.json")
            tmp = dest + ".tmp"
            with open(tmp, "w") as f:
                json.dump(
                    {
                        "ts": time.time(),
                        "pid": os.getpid(),
                        "uptime_sec": int(now - self._icon_watch_started),
                        "statusitem_on_screen": on_screen,
                        "statusitem_visible_pref": visible_pref,
                        "hidden_for_sec": hidden_for,
                    },
                    f,
                )
            os.replace(tmp, dest)
        except Exception:
            pass

    def _tick(self, _):
        # Post-update verdict: cheap (a single stat when no update is pending).
        try:
            self._check_update_verdict()
        except Exception:
            pass
        # Restart/outage recovery: resume any approved-but-unposted drafts the
        # durable queue recorded, instead of stranding them and re-presenting the
        # cards. Throttled (at most once/60s) because the reachability probe is a
        # blocking HTTP call on the main thread; edge-triggered on
        # unreachable->reachable so a card stranded by a loopback outage that
        # started AFTER the very first successful tick still gets retried once
        # the loopback comes back, not just on process start.
        now = time.monotonic()
        if now >= self._loopback_check_due_at:
            self._loopback_check_due_at = now + 60.0
            if st.loopback_reachable():
                if not self._resumed:
                    self._resumed = True
                    try:
                        self._resume_approved_queue()
                    except Exception as e:
                        sys.stderr.write(f"[s4l-menubar] resume approved queue failed: {e}\n")
                        sys.stderr.flush()
                    # Drain any review-events the outbox buffered while offline /
                    # before the last restart. Async + idempotent (server dedups
                    # event_uuid).
                    try:
                        st.flush_review_events_async()
                    except Exception:
                        pass
            else:
                self._resumed = False
        # Status-item visibility telemetry (60s-throttled inside; see __init__).
        self._icon_watch_tick(now)
        # The activity spinner owns the TITLE while a tool runs (we don't fight it at
        # 0.12s), but the menu + update indicator must still refresh mid-run —
        # otherwise the "Update now & restart Claude Desktop" item never appears on a box that's always
        # busy (continuous autopilot). So we no longer bail out wholesale when busy;
        # we only skip the title repaint and the review pop-up.
        busy = self._spinner is not None
        snap = st.snapshot()
        ob = snap.get("onboarding") or st.read_onboarding()
        runtime_ready = bool(snap.get("runtime_ready"))
        if "setup_complete" in snap:
            # Single source of truth: the server computes setup_complete (runtime +
            # a ready project + X connected) and we read it the SAME way whether it
            # came live from the loopback or from the persisted status-summary.json.
            # This is what stops the old 7/8-vs-"set up" flip-flop between the live
            # and offline paths — they no longer use different rules.
            setup_complete = bool(snap.get("setup_complete"))
        elif snap.get("_live"):
            # Legacy live server (pre-setup_complete) during a version skew.
            setup_complete = (
                runtime_ready
                and snap.get("projects_ready", 0) > 0
                and bool(snap.get("x_connected"))
            )
        else:
            # Truly fresh install, no summary yet: the ledger's "complete" is the proxy.
            setup_complete = bool(ob and ob.get("complete"))
        blocker = (ob or {}).get("current_blocker")
        blocker_code = (blocker or {}).get("code")
        # --- Autopilot health (only meaningful once setup is complete) --------
        # SINGLE signal: is the draft schedule registered AND firing for the live
        # account (schedule_state)? 'ok' = the host is running the tasks -> healthy,
        # NO warning (even if no draft has drained yet — that's just an empty queue
        # between cycles, not a setup problem). 'missing'/'disabled' = not running
        # for this account -> show re-arm. We deliberately do NOT drive the menu off
        # the drain-status latch anymore: it stayed stale after recovery and made a
        # firing, healthy autopilot look "not set up".
        # Always read the REAL schedule state (no setup-gated "ok" fallback that
        # lied). The re-arm WARNING still only fires once setup is complete, so we
        # never nag the user mid-onboarding — only the value is now always honest.
        schedule_state = self._schedule_state()
        self._schedule_state_cache = schedule_state
        # 'stalled' (task present + enabled FOR THE ACTIVE ACCOUNT, host
        # scheduler stopped launching it: the Desktop warm-session wedge,
        # Karol 2026-07-06) needs attention just like missing/disabled.
        # schedule_state.py is account-scoped (2026-07-08): an account switch
        # now correctly resolves to 'missing' (the active account genuinely
        # has no registration), not 'stalled' — a stale OTHER account's
        # registry is never consulted for the active account's health, so
        # 'stalled' can no longer be misdiagnosed here.
        attention = setup_complete and schedule_state in ("missing", "disabled", "stalled")
        # Routines-lane rate limit (429): the draft tasks ARE registered and firing
        # for this account, but every run dies on a Claude rate limit, so nothing
        # drafts. Re-arm can't fix that — surface it as its own ⚠ attention state
        # with a "rate-limited" reason. Only meaningful when the schedule is firing
        # ('ok'); the missing/disabled case already owns the ⚠. Throttled (~30s):
        # scanning the worker-transcript bucket is glob-heavy and changes slowly.
        if setup_complete and schedule_state == "ok":
            now_rl = time.time()
            if now_rl - getattr(self, "_rl_checked_at", 0.0) >= 30:
                self._rl_checked_at = now_rl
                reason, msg = self._stall_reason()
                self._stall_reason_info = (reason, msg) if reason == "rate_limited" else ("", "")
            if self._stall_reason_info[0] == "rate_limited":
                attention = True
        else:
            self._stall_reason_info = ("", "")
        # Draft worker stuck/killed: the producer narrates "drafting replies (Nm)"
        # the whole time it blocks waiting for a worker to return a result, with NO
        # idea the worker died. A healthy drain clears in ~1-2 min; once that label
        # has been "drafting" past DRAFT_STUCK_SECONDS the worker keeps getting
        # killed mid-run (or never claims) and nothing is draining — flip to ⚠
        # instead of leaving the reassuring "drafting (8m)" spinner up. Skip when a
        # more specific cause (rate limit) already owns the reason.
        #
        # Gated on schedule_state in ("ok", "stalled"), NOT missing/disabled: when
        # the schedule is missing/disabled (e.g. orphaned by an account switch),
        # the producer ALSO sits "drafting" forever, and without excluding those two
        # draft_stuck shadowed the missing/disabled branch in _build_menu — the user
        # saw "worker keeps getting killed" with NO Re-arm button instead of "Draft
        # tasks aren't scheduled on this account" + the one-click fix (Karol,
        # 2026-07-06). "stalled" is deliberately included (2026-07-08): that
        # branch's fix (Set up draft schedule / re-arm) works for it too, but a
        # job that has sat this long — claimed-and-hung, OR never claimed at all
        # (see the ⧖ prefix check below) — is more specific, direct evidence
        # from the queue itself than the host's lastRunAt staleness, so it's
        # worth surfacing as its own reason even under "stalled".
        if (
            setup_complete
            and schedule_state in ("ok", "stalled")
            and self._stall_reason_info[0] != "rate_limited"
        ):
            _act = st.read_activity()
            if (
                _act
                and _act.get("state") == "drafting"
                and _label_elapsed_secs(_act.get("label")) >= DRAFT_STUCK_SECONDS
            ):
                attention = True
                self._stall_reason_info = ("draft_stuck", _act.get("label") or "")
        # Drop the stale "drafting" spinner while we need attention so the ⚠ shows.
        self._stalled = attention

        # Spinner owns the title while busy; _spin already keeps the ⬆ visible there.
        if not busy:
            self._render_title(setup_complete, ob, blocker, attention)

        # Blocker notification only on transition into a new blocker.
        if blocker and blocker_code != self._last_blocker_code:
            self._notify(
                "S4L setup needs you",
                blocker.get("message", "Setup is blocked"),
            )
        self._last_blocker_code = blocker_code
        # Notify once per episode (the draft schedule isn't running for this account).
        if attention:
            self._attention_clear_since = None
        if attention and not self._stall_notified:
            # Fleet-wide telemetry: the draft autopilot needs attention on THIS
            # install (orphaned by an account switch, disabled, rate-limited, or a
            # stuck worker). Only channel that surfaces "customer's autopilot silently
            # stopped drafting" to us; the cycle log lives only on their machine.
            # Once per episode (gated by _stall_notified), so it never spams.
            _reason = (
                self._stall_reason_info[0]
                or (schedule_state if schedule_state in ("disabled", "stalled") else "missing")
            )
            _capture_msg(
                f"S4L draft autopilot needs attention: {_reason}",
                level="warning",
                _extra={
                    "scheduled_tasks": _registry_summary_for_capture(),
                    "stall_label": self._stall_reason_info[1] or None,
                },
                phase="draft_schedule",
                reason=_reason,
                schedule_state=str(schedule_state),
            )
            if self._stall_reason_info[0] == "rate_limited":
                self._notify(
                    "S4L Claude rate-limited",
                    "Drafts can’t run — this Claude account hit its rate limit. "
                    + (self._stall_reason_info[1] or "Wait for the limit to reset or switch account."),
                )
            elif self._stall_reason_info[0] == "draft_stuck":
                # Previously unhandled here: fell through to the final `else`
                # below and told the user to click "Set up draft schedule" — a
                # button that doesn't exist in the draft_stuck menu (only
                # Diagnose does). Fixed 2026-07-08 so the notification always
                # matches the one button the menu actually shows.
                _unclaimed = "⧖" in (self._stall_reason_info[1] or "")
                self._notify(
                    "S4L drafts not completing",
                    ("No worker is claiming draft jobs. " if _unclaimed else
                     "A worker claimed a draft job and never finished it. ")
                    + "Open the S4L menu → “Diagnose & fix in Claude…”.",
                )
            elif schedule_state == "disabled":
                self._notify(
                    "S4L draft tasks disabled",
                    "The draft tasks are scheduled but disabled. Open the S4L menu → "
                    "“Set up draft schedule” to re-enable.",
                )
            elif schedule_state == "stalled":
                self._notify(
                    "S4L drafts stopped",
                    "Claude’s scheduler stopped running the draft tasks (a known "
                    "Claude Desktop glitch). Open the S4L menu → “Set up draft "
                    "schedule” to re-register it.",
                )
            else:
                can_selfheal = False
                try:
                    import scheduled_task_selfheal
                    can_selfheal = scheduled_task_selfheal.can_create_for_active_account()
                except Exception:
                    pass
                if can_selfheal:
                    self._notify(
                        "S4L draft autopilot not scheduled",
                        "No draft tasks are running on this Claude account (switching "
                        "accounts clears them). Open the S4L menu → “Finish setting up drafts”.",
                    )
                else:
                    self._notify(
                        "S4L draft autopilot not scheduled",
                        "No draft tasks are running on this Claude account (switching "
                        "accounts clears them). Open the S4L menu → “Set up draft schedule”.",
                    )
            self._stall_notified = True
        elif not attention and self._stall_notified:
            if self._attention_clear_since is None:
                self._attention_clear_since = time.time()
            elif time.time() - self._attention_clear_since >= ATTENTION_CLEAR_COOLDOWN_SECONDS:
                self._stall_notified = False
                self._attention_clear_since = None

        # Single-source update signal: copy the snapshot's result (snapshot.py
        # _latest_published: GitHub releases/latest first, npm fallback; semver >,
        # surfaced as update_available/latest_version). No separate poll here.
        self._update_available = bool(snap.get("update_available"))
        self._latest_version = snap.get("latest_version")
        self._channel = snap.get("channel") or "stable"
        self._latest_tag = snap.get("latest_tag")

        # Only rebuild the menu when something user-visible changed, so an open
        # menu isn't torn down under the user's cursor every poll.
        done = (
            sum(1 for m in ob["milestones"] if m.get("status") == "complete")
            if ob
            else 0
        )
        # _update_available / _latest_version are in the signature so a freshly
        # detected update rebuilds the menu (adding "Update now & restart Claude Desktop") even mid-run.
        sig = (
            runtime_ready,
            setup_complete,
            blocker_code,
            done,
            bool(snap.get("autopilot_on")),
            snap.get("version"),
            snap.get("update_available"),
            self._update_available,
            self._latest_version,
            snap.get("x_handle"),
            snap.get("projects_ready"),
            snap.get("projects_total"),
            tuple(sorted((st.read_flags() or {}).items())),
            st.read_split(),
            attention,
            schedule_state,
            self._stall_reason_info,
            os.path.exists(PAUSE_FLAG),
        )
        if sig != self._sig:
            self._sig = sig
            self._build_menu(runtime_ready, setup_complete, ob, blocker, snap, attention, schedule_state)

        # Draft-review pop-ups: if a draft cycle left a review request, present the
        # cards. Don't start a review mid-run (the spinner means a tool is active).
        if not busy:
            self._maybe_start_review()
        # Self-heal an open-but-ignored card (runs even while busy: it only
        # touches an existing window, never starts a review).
        self._maybe_heal_review()
        # Store reconciliation: re-stamp any of this session's decisions that
        # the server's whole-file plan rewrite clobbered mid-drain. Throttled;
        # cheap when the session has no decisions.
        now_rc = time.time()
        if self._session_decisions and now_rc - self._reconciled_at >= 15:
            self._reconciled_at = now_rc
            try:
                fixed = st.store_reconcile_decisions("review-queue", self._session_decisions)
                if fixed:
                    sys.stderr.write(
                        f"[s4l-menubar] re-stamped {fixed} decision(s) clobbered by a plan rewrite\n"
                    )
                    sys.stderr.flush()
            except Exception:
                pass

    # ---- draft review pop-ups ---------------------------------------------
    def _posting_activity_label_locked(self):
        """Progress for the current menu-bar approval burst.

        The server receives one post_drafts call per approved card, so its native
        view is always 1/1. The menu bar owns the burst queue and can show the
        useful progress: current approved post / total approved so far.
        """
        if self._posts_outstanding <= 0:
            return None
        total = max(
            self._posting_batch_total,
            self._posting_batch_done + self._posts_outstanding,
        )
        current = min(total, self._posting_batch_done + 1)
        return f"posting {current}/{total}"

    def _write_posting_activity_locked(self):
        label = self._posting_activity_label_locked()
        if label:
            st.write_activity("posting", label)
        return label

    def _reset_posting_progress_locked(self):
        self._posting_batch_total = 0
        self._posting_batch_done = 0

    def _maybe_start_review(self):
        req = st.read_review_request()
        if not req:
            return
        batch = req.get("batch_id")
        if not batch:
            return
        plan = st.read_plan(req.get("plan_path") or "")
        drafts = st.review_drafts(plan)
        # Nothing left to review (empty, missing plan, or all already posted via
        # the chat surface) — clear the signal and reset the signature so a future
        # batch is presented fresh.
        if not drafts:
            self._last_review_sig = None
            st.clear_review_request()
            return
        # De-dup on the CONTENT of the pending set (each draft's plan index + reply
        # text), not the constant batch_id. This means: re-present whenever NEW
        # drafts arrive (the signature changes), but don't re-pop the identical
        # cards we already showed for this same pending set. No restart is ever
        # needed for new pending drafts to surface.
        sig = tuple((d.get("n"), d.get("reply_text") or "") for d in drafts)
        if sig == self._last_review_sig:
            return
        # A review is already in flight. Two cases:
        #  - A card is ON SCREEN (_panel_open): push the newly-queued drafts into
        #    the open card so the "X of N" counter and the reviewable stack grow
        #    live. This is the fix for the "card froze at 1 of 4 while 137 piled
        #    up" bug — drafts that arrived after the card opened used to be
        #    stranded because this method returned early on _review_active.
        #  - Posting is DRAINING with no panel up (_review_active but not
        #    _panel_open): leave the signature untouched so the full pending set
        #    is presented fresh once the drain completes (don't pop a card mid-post).
        if self._review_active:
            if self._panel_open:
                try:
                    import s4l_card

                    s4l_card.extend_active(drafts)
                except Exception as e:
                    sys.stderr.write(f"[s4l-menubar] extend cards failed: {e}\n")
                    sys.stderr.flush()
                self._last_review_sig = sig
            return
        with self._review_lock:
            self._reset_posting_progress_locked()
            self._review_active = True
            self._panel_open = True
        try:
            import s4l_card

            # present_feedback (the menu bar's feedback item) falls back to
            # the module-level default handler; register ours before any
            # card shows.
            s4l_card.set_feedback_handler(self._on_feedback_text)
            s4l_card.present_review(
                drafts,
                on_decision=lambda d: self._on_card_decision(batch, d),
                on_complete=lambda decisions: self._on_review_closed(batch, decisions),
            )
            # Record as shown only AFTER the cards are actually up, so a transient
            # card-UI failure never permanently suppresses this pending set.
            self._last_review_sig = sig
            # No macOS notification for fresh drafts, per explicit user
            # request (2026-07-03): the card itself is the surface. A missed
            # card is the unattended watchdog's job (it heals the window and
            # notifies once per episode); a stderr line keeps fresh stacks
            # greppable.
            n = len(drafts)
            sys.stderr.write(f"[s4l-menubar] presented {n} draft card(s)\n")
        except Exception as e:
            # Card UI unavailable — don't strand the batch; chat review still works.
            self._review_active = False
            self._panel_open = False
            sys.stderr.write(f"[s4l-menubar] review cards failed: {e}\n")
            sys.stderr.flush()
            _capture(e, phase="review_cards")

    def _maybe_heal_review(self):
        """Self-heal an unattended review card. A card can be fully drawn yet
        outside the user's attention (wrong display, buried corner) and AppKit
        cannot see attention, so measure the outcome instead: drafts pending
        with no decision or interaction for REVIEW_UNATTENDED_SECONDS. Heal
        automatically (move to the pointer's screen, raise, no user action
        required), re-healing on a throttle while the drought lasts. Notify
        once per episode; after REVIEW_UNATTENDED_SENTRY_SECONDS emit one
        Sentry event so ignored review surfaces are visible fleet-wide."""
        try:
            import s4l_card

            status = s4l_card.active_status()
        except Exception:
            return
        if not status or not status.get("pending"):
            self._review_unattended_notified = False
            self._review_unattended_captured = False
            return
        anchor = max(
            status.get("presented_at") or 0,
            status.get("last_decision_at") or 0,
            status.get("last_interaction_at") or 0,
        )
        if not anchor:
            return
        now = time.time()
        idle = now - anchor
        if idle < REVIEW_UNATTENDED_SECONDS:
            self._review_unattended_notified = False
            self._review_unattended_captured = False
            return
        if now - self._review_heal_at >= REVIEW_HEAL_EVERY_SECONDS:
            self._review_heal_at = now
            healed = False
            try:
                healed = s4l_card.heal_active()
            except Exception as e:
                sys.stderr.write(f"[s4l-menubar] review heal failed: {e}\n")
                sys.stderr.flush()
            if healed and not self._review_unattended_notified:
                self._review_unattended_notified = True
                self._notify(
                    "S4L drafts waiting",
                    f"{status.get('pending')} drafts have been waiting "
                    f"{int(idle // 60)} min. Moved the review card to your "
                    "screen.",
                )
        if (
            idle >= REVIEW_UNATTENDED_SENTRY_SECONDS
            and not self._review_unattended_captured
        ):
            self._review_unattended_captured = True
            _capture_msg(
                "S4L review card unattended",
                level="warning",
                phase="review_unattended",
                pending=str(status.get("pending")),
                idle_min=str(int(idle // 60)),
                visible=str(status.get("occlusion_visible")),
                screen=str(status.get("screen")),
            )

    def _ship_review_event(self, batch, decision):
        """Queue the decision (with reason, link clicks, dwell) for the
        review-events feedback rail. Outbox append + async flush; never raises
        and never blocks the card UI."""
        try:
            cid = decision.get("candidate_id")
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                cid = None
            st.review_event_add(
                {
                    "platform": "twitter",
                    "project": decision.get("project"),
                    "candidate_id": cid,
                    "batch_id": batch,
                    "card_n": decision.get("n"),
                    "decision": "approved" if decision.get("approved") else "rejected",
                    "edited": bool(decision.get("edited")),
                    "drop_link": bool(decision.get("drop_link")),
                    "loved": bool(decision.get("loved")),
                    "reject_category": decision.get("reject_category"),
                    "reject_note": decision.get("reject_note"),
                    "interactions": decision.get("interactions") or [],
                    "dwell_ms": decision.get("dwell_ms"),
                    "thread_url": decision.get("thread_url"),
                    "thread_author": decision.get("thread_author"),
                    "draft_text": decision.get("text"),
                    # Pre-edit draft (None unless edited=true): lets the
                    # feedback digest diff what the user changed.
                    "original_text": decision.get("original_text"),
                    # Draft language (ISO 639-1, None on older plans). draft_text
                    # and original_text are ALWAYS original-language (what
                    # posts); English translations on the card are display-only
                    # and never shipped here.
                    "language": decision.get("language"),
                }
            )
        except Exception:
            pass

    def _on_feedback_text(self, text):
        """Ship overall feedback (the menu bar's "Give overall feedback to AI…" item; the
        card had its own button once but it moved out, 2026-07-03 feedback)
        as a decision='feedback' review event on the same outbox rail as
        card decisions. project is intentionally omitted
        (NULL server-side): the feedback digest folds project-less feedback
        into EVERY configured project's prompt."""
        body = (text or "").strip()[:2000]
        if not body:
            return
        try:
            st.review_event_add(
                {
                    "platform": "twitter",
                    "decision": "feedback",
                    "batch_id": "overall-feedback",
                    "reject_note": body,
                }
            )
            self._notify("S4L", "Feedback sent. It will steer future drafts.")
        except Exception:
            pass

    def _menu_feedback(self, _):
        # Dropdown entry point for the overall-feedback composer. Rumps menu
        # callbacks run on the main run loop, which present_feedback requires.
        try:
            import s4l_card

            s4l_card.present_feedback(self._on_feedback_text)
        except Exception as e:
            sys.stderr.write(f"[s4l-menubar] feedback composer failed: {e}\n")
            _capture(e, phase="feedback_composer")

    def _on_card_decision(self, batch, decision):
        # Runs on the main thread the INSTANT a card is approved/rejected. An
        # approved card is enqueued for immediate posting; a REJECTED card is
        # persisted (marked done so it's never re-shown for review) on a quick
        # background thread. We never block inline here — posting can take minutes
        # and would freeze the card UI while the user reviews the rest of the stack.
        self._ship_review_event(batch, decision)
        if not decision.get("approved"):
            n = decision.get("n")
            # Durable local record FIRST, mirroring approved_queue_add for approvals.
            # review_drafts() consults this, so the rejected card is suppressed from
            # re-review IMMEDIATELY and even if the loopback is down when the
            # background plan-flag write below runs. Without this, a reject was a
            # fire-and-forget loopback call with a swallowed exception, so rejects
            # silently vanished and the card "came back" — unlike durable approvals.
            try:
                st.store_stamp_decision(batch, decision)
                self._session_decisions.append(dict(decision))
            except Exception:
                pass

            def _persist_reject():
                try:
                    st.post_drafts(batch, reject=[n], timeout=30)
                except Exception:
                    pass

            threading.Thread(target=_persist_reject, daemon=True).start()
            return
        n = decision.get("n")
        # Persist the approval DURABLY before posting, so a menu bar / Claude
        # restart resumes the drain instead of stranding it and re-presenting the
        # card. The in-memory _post_q below is just the fast path; this file is the
        # source of truth review_drafts() consults to avoid re-showing it.
        st.store_stamp_decision(batch, decision)
        self._session_decisions.append(dict(decision))
        with self._review_lock:
            self._posts_outstanding += 1
            self._posting_batch_total += 1
            self._review_active = True
            self._write_posting_activity_locked()
        self._post_q.put((batch, decision))
        self._ensure_post_worker()

    def _on_review_closed(self, batch, decisions):
        # Fires when the card sequence ends (last card decided or window closed).
        # The panel is gone, but approved cards may still be draining — keep the
        # review "active" until the queue empties so the not-yet-posted remainder
        # isn't re-presented as a fresh batch.
        with self._review_lock:
            self._panel_open = False
            if self._posts_outstanding <= 0:
                self._review_active = False
                self._reset_posting_progress_locked()
        # Only clear the review marker when the queue is actually drained. The old
        # code cleared it unconditionally, so if the user closed the card with
        # drafts still undecided (or more had piled up than they reviewed), the
        # backlog was stranded — presentation is gated on this marker. Keep it when
        # anything remains so the leftover re-presents fresh on the next tick.
        remaining = 0
        try:
            req = st.read_review_request()
            if req:
                remaining = len(st.review_drafts(st.read_plan(req.get("plan_path") or "")))
        except Exception:
            remaining = 0
        if remaining <= 0:
            st.clear_review_request()
        # Drop the dedup signature so whatever is left is presented fresh (not
        # suppressed as "already shown") once posting finishes draining.
        self._last_review_sig = None
        # Retry any review-events a per-decision flush left behind (e.g. the
        # API was briefly unreachable mid-review).
        try:
            st.flush_review_events_async()
        except Exception:
            pass
        if not any(d.get("approved") for d in decisions):
            self._notify("S4L", "No drafts approved — nothing posted.")

    def _ensure_post_worker(self):
        # One persistent daemon worker drains the approved-card queue. It never
        # exits (avoids an enqueue-vs-exit race) — an idle parked thread is cheap.
        if self._post_worker is not None and self._post_worker.is_alive():
            return
        self._post_worker = threading.Thread(target=self._post_worker_loop, daemon=True)
        self._post_worker.start()

    def _post_worker_loop(self):
        # Serialized poster: one approved card at a time so two posts never drive
        # the shared harness Chrome simultaneously. The menu bar passes a burst
        # progress label into post_drafts, so the spinner shows e.g. "posting 17/95"
        # even though each server call is still one approved draft.
        while True:
            batch, decision = self._post_q.get()  # blocks until a card is approved
            n = decision.get("n")
            try:
                # No "Posting draft N…" banner: the menu-bar spinner already shows
                # live posting progress, so a Notification Center toast per approved
                # card is pure noise. Only failures (below) raise a notification.
                st.approved_queue_set_status(batch, n, "posting")
                with self._review_lock:
                    activity_label = self._posting_activity_label_locked()
                cl = [n] if decision.get("drop_link") else None
                # Two-draft cards: a draft_variant means the human picked
                # (or kept) a specific slot on a dual-draft card, route
                # through edits=[...] even with no hand-typed change, so the
                # candidate's style/assigned_style/assigned_mode switch to
                # match whichever draft is actually posting (mcp/src/index.ts
                # post_drafts applies both from the same entry). Without
                # this, a card where the human accepted the recommended
                # draft as-is (edited=False) would still need its variant
                # tag carried through for consistency with a later switch.
                draft_variant = decision.get("draft_variant")
                if decision.get("edited") or draft_variant:
                    edit = {"n": n, "text": decision.get("text") or ""}
                    if draft_variant:
                        edit["variant"] = draft_variant
                    res = st.post_drafts(
                        batch,
                        edits=[edit],
                        clear_link=cl,
                        activity_label=activity_label,
                    )
                else:
                    res = st.post_drafts(batch, post=[n], clear_link=cl, activity_label=activity_label)
                if res is None:
                    # Loopback unreachable (Claude closed, mid-restart, or an
                    # account switch tore down the old MCP server before a new
                    # one registered). Unlike a real posting failure, nothing
                    # was ever attempted here — post_drafts never reached a
                    # server — so there's no double-post risk in retrying.
                    # Deliberately do NOT call store_mark_post_failed / set
                    # status "failed": both would exclude this card from
                    # store_pending_posts()/approved_queue_pending(), which is
                    # exactly what _resume_approved_queue() reads on the next
                    # unreachable->reachable edge. Leaving it "queued" lets
                    # that existing restart-recovery path retry it
                    # automatically once a server re-registers, instead of
                    # stranding it for manual dashboard retry.
                    st.approved_queue_set_status(batch, n, "queued", error="loopback_unreachable")
                    self._notify(
                        "S4L", "Server unreachable — will retry automatically once Claude Desktop reconnects."
                    )
                else:
                    posted = res.get("posted") if isinstance(res, dict) else None
                    if posted == 0:
                        st.approved_queue_set_status(batch, n, "failed", error="posted_0")
                        st.store_mark_post_failed(batch, n, decision.get("candidate_id"), "posted_0")
                        self._notify("S4L", f"Draft {n} didn't post — see the dashboard for why.")
                    else:
                        # Success is silent: the spinner + dashboard already reflect
                        # it. No per-card "Posted draft N." banner. The server
                        # stamps posted/our_url into the store itself; the legacy
                        # set_status only settles pre-store ledger entries.
                        st.approved_queue_set_status(batch, n, "posted")
            except Exception as e:
                st.approved_queue_set_status(batch, n, "failed", error=str(e)[:200])
                st.store_mark_post_failed(batch, n, decision.get("candidate_id"), str(e)[:200])
                sys.stderr.write(f"[s4l-menubar] post draft {n} failed: {e}\n")
                sys.stderr.flush()
                _capture(e, phase="post_card")
            finally:
                with self._review_lock:
                    self._posting_batch_done += 1
                    self._posts_outstanding -= 1
                    if self._posts_outstanding > 0:
                        self._write_posting_activity_locked()
                    elif not self._panel_open:
                        self._review_active = False
                        self._reset_posting_progress_locked()
                self._post_q.task_done()

    def _render_title(self, setup_complete, ob, blocker, attention=False):
        if blocker or attention:
            self.title = "S4L ⚠"  # warning (setup blocked OR autopilot needs attention)
        elif not setup_complete and ob and not ob.get("complete"):
            done = sum(1 for m in ob["milestones"] if m.get("status") == "complete")
            self.title = f"S4L {done}/{len(ob['milestones'])}"
        elif self._update_available:
            self.title = "S4L ⬆"  # update available — open the menu to update
        else:
            self.title = "S4L"

    # ---- menu construction ------------------------------------------------
    def _build_menu(self, runtime_ready, setup_complete, ob, blocker, snap, attention=False, schedule_state="ok"):
        self.menu.clear()
        items = []

        # Version comes from the snapshot ONLY (snapshot.py reads the installed
        # manifest). The old st.version() fallback read panel-endpoint.json — a
        # second, often-stale source written by whichever MCP instance booted last.
        ver = snap.get("version")
        header = rumps.MenuItem(f"S4L · v{ver}" if ver else "S4L")
        header.set_callback(None)  # non-clickable label
        items.append(header)
        items.append(rumps.separator)

        # Pause: the lighter, fully reversible alternative to Quit — stops
        # drafting/posting but leaves Claude Desktop, this tray, and the draft
        # schedule alone. Top of the menu: it's the primary control. See
        # _pause_toggle.
        if os.path.exists(PAUSE_FLAG):
            items.append(self._label("⏸ S4L is paused — drafting & posting stopped"))
            items.append(rumps.MenuItem("Resume S4L", callback=self._pause_toggle))
        else:
            items.append(rumps.MenuItem("Pause S4L", callback=self._pause_toggle))
        items.append(rumps.separator)

        # Attention = the draft schedule isn't running for THIS account (missing or
        # disabled). "Set up draft schedule" fixes it via host create_scheduled_task.
        # When the schedule IS firing (ok), attention is False and nothing shows here
        # — a firing autopilot reads as healthy even if no draft has drained yet.
        if attention:
            # Exactly ONE clickable action per ⚠ scenario, always — never stack a
            # specific fix next to the universal "Diagnose & fix" escape hatch.
            # Each branch below picks whichever single action is most relevant:
            # a known mechanical fix when one exists, or Diagnose when it doesn't
            # (2026-07-08, replacing the old "specific fix + Diagnose" pattern that
            # showed 2-3 buttons at once and confused which one to click).
            if self._stall_reason_info[0] == "rate_limited":
                # Routines fire but every run dies on a Claude rate limit (429).
                # Re-arm/restart can't fix this — Diagnose is the one relevant action.
                items.append(self._label("⚠ Claude rate-limited — drafts can’t run"))
                items.append(self._label(
                    "   " + (self._stall_reason_info[1] or "wait for reset or switch account")
                ))
                items.append(rumps.MenuItem("Diagnose & fix in Claude…", callback=self._diagnose_fix))
            elif self._stall_reason_info[0] == "draft_stuck":
                # Routines fire and the producer keeps narrating "drafting" but
                # nothing is finishing. Two different root causes share this one
                # reason code, and the label already tells us which: a '⧖' prefix
                # (see _label_elapsed_secs) means the job has sat in the pending
                # queue this whole time with no worker ever claiming it; no '⧖'
                # means something DID claim it and then hung or died mid-run.
                # Re-arm/restart can't fix either case — Diagnose is the one action.
                _unclaimed = "⧖" in (self._stall_reason_info[1] or "")
                items.append(self._label(
                    "⚠ No worker is claiming draft jobs" if _unclaimed
                    else "⚠ Draft not completing — worker keeps getting killed"
                ))
                items.append(self._label(
                    "   " + (self._stall_reason_info[1] or "drafting") + " — no result yet"
                ))
                items.append(rumps.MenuItem("Diagnose & fix in Claude…", callback=self._diagnose_fix))
            elif schedule_state == "disabled":
                items.append(self._label("⚠ Draft tasks are scheduled but disabled"))
                items.append(rumps.MenuItem("Set up draft schedule for this account", callback=self._rearm))
            elif schedule_state == "stalled":
                # Task registered + enabled FOR THE ACTIVE ACCOUNT (schedule_state.py
                # is account-scoped, 2026-07-08 — see its module docstring) but the
                # host stopped launching it: the Claude Desktop warm-session wedge
                # (finished worker sessions never exit; the overlap guard skips
                # every fire). Re-arm goes through the same real create_scheduled_task
                # path as onboarding — a verified fix the user has seen work —
                # rather than a silent Claude Desktop quit/relaunch with no visible
                # feedback, which field evidence (2026-07-08) showed doesn't
                # reliably clear the underlying stall (that field incident turned
                # out to be a DIFFERENT bug: schedule_state.py was reading a stale,
                # no-longer-active account's registry — now fixed, so "stalled"
                # here means what it says: same account, task present, just not
                # firing). Distinct label from the "aren't scheduled" case below
                # since the task DOES exist here; same one-click remedy either way.
                items.append(self._label("⚠ Drafts stopped — Claude’s scheduler is stuck"))
                items.append(rumps.MenuItem("Set up draft schedule for this account", callback=self._rearm))
            else:
                items.append(self._label("⚠ Draft tasks aren’t scheduled on this account"))
                # Prefer the automatic fix (2026-07-08): if the active account
                # already has a session directory, _finish_schedule_setup can
                # create the registration directly (heal() fix 5) with no
                # clipboard paste needed. Fall back to re-arm only when fix 5
                # has nowhere to write (no session dir yet for this account —
                # it never fabricates one), since that's the one case where
                # only the live create_scheduled_task host tool can help.
                can_selfheal = False
                try:
                    import scheduled_task_selfheal
                    can_selfheal = scheduled_task_selfheal.can_create_for_active_account()
                except Exception:
                    pass
                if can_selfheal:
                    # Say "restarts Claude" in the label itself, not just the
                    # confirm modal — a menu item should be honest about a
                    # disruptive action (closing/reopening the whole app)
                    # before the user has committed to clicking it, matching
                    # what the old "Restart Claude Desktop to fix" button made
                    # obvious upfront (2026-07-08).
                    items.append(rumps.MenuItem(
                        "Finish setting up drafts (restarts Claude)",
                        callback=self._finish_schedule_setup,
                    ))
                else:
                    items.append(rumps.MenuItem("Set up draft schedule for this account", callback=self._rearm))
            items.append(rumps.separator)

        if not runtime_ready:
            items += self._state_a()
        elif not setup_complete:
            items += self._state_b(ob, blocker)
        else:
            items += self._state_c(snap)

        # Engagement lanes — ALWAYS visible (every state), not just post-setup, so
        # the user can see + flip either lane any time. Two INDEPENDENT checkmarks
        # (both can be on -> the cycle splits per personal_brand_share). Single
        # source: snap['flags'] (mode.json), same value the dashboard shows.
        flags = snap.get("flags") or st.read_flags()
        personal_on = bool(flags.get("personal_brand"))
        promo_on = bool(flags.get("promotion"))
        items.append(rumps.separator)
        pb_item = rumps.MenuItem("Personal brand", callback=self._toggle_personal)
        pb_item.state = 1 if personal_on else 0
        items.append(pb_item)
        pr_item = rumps.MenuItem("Product promotion", callback=self._toggle_promotion)
        pr_item.state = 1 if promo_on else 0
        items.append(pr_item)
        if personal_on and promo_on:
            # Both lanes on: the split becomes meaningful, so offer the presets.
            share = st.read_split()
            split_menu = rumps.MenuItem(f"Lane split: {self._split_pct(share)}")
            for preset in self.SPLIT_PRESETS:
                it = rumps.MenuItem(
                    f"{self._split_pct(preset)} personal/promotion",
                    callback=functools.partial(self._on_split_preset, preset),
                )
                it.state = 1 if round(share * 100) == round(preset * 100) else 0
                split_menu.add(it)
            items.append(split_menu)

        items.append(rumps.separator)
        items.append(rumps.MenuItem("Open dashboard", callback=self._open_dashboard))
        # The one entry point for overall feedback (the review card no longer
        # carries a Feedback button); named for what it does to the pipeline,
        # not the mechanism.
        items.append(rumps.MenuItem("Give overall feedback to AI…", callback=self._menu_feedback))
        # While the update-verify marker is pending, the pipeline copy still
        # resolves the OLD version (it only advances once the restarted server
        # re-provisions repo/package, ~2 min), so the snapshot honestly reports
        # update_available and the menu re-showed "update to vN" right after
        # the user clicked update — reading as a failed install (2026-07-03).
        # Show the in-progress state instead; _check_update_verdict drops the
        # marker on success or after UPDATE_VERIFY_GRACE_SEC either way.
        if self._updating_mcpb:
            items.append(rumps.separator)
            items.append(self._label("⇩ Updating… downloading & installing"))
        elif os.path.exists(self._update_verify_path()):
            items.append(rumps.separator)
            items.append(self._label("⏳ Finishing update… verifying install"))
        elif self._update_available and self._latest_version:
            items.append(rumps.separator)
            items.append(self._label(f"⬆ Update available · v{self._latest_version}"))
            items.append(
                rumps.MenuItem(
                    "Update now & restart Claude Desktop",
                    callback=self._do_mcpb_update,
                )
            )
        if self._reloc_needed and not self._relocating:
            items.append(rumps.separator)
            items.append(rumps.MenuItem("Tidy autopilot history…", callback=self._prompt_relocate_tasks))
        items.append(rumps.separator)
        quit_menu = rumps.MenuItem("Quit or Uninstall")
        quit_menu.add(rumps.MenuItem("Uninstall S4L…", callback=self._reset_machine))
        quit_menu.add(rumps.MenuItem("Quit", callback=self._quit_app))
        items.append(quit_menu)

        # Collapse consecutive/edge separators so an empty section (e.g. State C
        # now renders no status labels) can't leave a doubled or dangling divider.
        cleaned = []
        for it in items:
            is_sep = it is rumps.separator
            if is_sep and (not cleaned or cleaned[-1] is rumps.separator):
                continue
            cleaned.append(it)
        while cleaned and cleaned[-1] is rumps.separator:
            cleaned.pop()
        # An update is in flight: grey out every other action so nothing races
        # the download/unzip/restart (a second update click, a post, a lane
        # toggle, Quit killing the background thread mid-curl).
        if self._update_in_flight():
            for it in cleaned:
                if it is not rumps.separator and it.callback is not None:
                    it.set_callback(None)
        for it in cleaned:
            self.menu.add(it)

    def _label(self, text):
        item = rumps.MenuItem(text)
        item.set_callback(None)
        return item

    # State A — runtime not installed yet.
    def _state_a(self):
        return [
            self._label("Runtime not installed"),
            rumps.MenuItem("Set up in Claude", callback=self._setup),
        ]

    # State B — runtime ready, setup running/incomplete (the ramp state).
    def _state_b(self, ob, blocker):
        out = []
        if ob:
            milestones = ob["milestones"]
            done = sum(1 for m in milestones if m.get("status") == "complete")
            total = len(milestones)
            cur = next(
                (m for m in milestones if m.get("status") == "in_progress"), None
            ) or next(
                (m for m in milestones if m.get("status") != "complete"), None
            )
            cur_label = (
                st.MILESTONE_LABELS.get(cur["id"], cur["id"]).lower() if cur else ""
            )
            line = f"Setting up…  {done}/{total}"
            if cur_label:
                line += f"  ·  {cur_label}"
            out.append(self._label(line))

            sub = rumps.MenuItem("Setup steps")
            for m in milestones:
                sub.add(
                    self._label(
                        f"{_glyph(m.get('status'))} "
                        f"{st.MILESTONE_LABELS.get(m['id'], m['id'])}"
                    )
                )
            out.append(sub)
        else:
            out.append(self._label("Setting up…"))

        if blocker:
            out.append(rumps.separator)
            out.append(
                rumps.MenuItem(
                    f"⚠ Needs you: {blocker.get('message', '')}",
                    callback=self._setup,
                )
            )
        out.append(rumps.MenuItem("Set up in Claude", callback=self._setup))
        return out

    # State C — setup complete. The post-setup status readouts (X handle,
    # projects-ready count, 7-day stats) were removed per user request: that
    # gray informational text belongs on the dashboard, not the dropdown. The
    # menu goes straight from the header to the engagement lanes + Open dashboard.
    # The engagement-mode toggles live in _build_menu (shown in EVERY state), and
    # there is deliberately no "Run draft cycle" / "Post approved drafts" item
    # (the autopilot drafts on its own; approving a review card already posts it).
    def _state_c(self, snap):
        return []


if __name__ == "__main__":
    try:
        S4LMenuBar().run()
    except Exception as _run_err:
        # The run loop dying is the other "menu bar didn't start / vanished" case.
        # Report + flush before the KeepAlive relaunch so it isn't lost on teardown.
        _capture(_run_err, phase="run")
        _flush()
        raise
