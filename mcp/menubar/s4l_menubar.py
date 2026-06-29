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

import glob
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

# --- Sentry bootstrap --------------------------------------------------------
# The menu bar runs as a standalone KeepAlive LaunchAgent off the owned venv,
# a separate process from the MCP server, so it was a Sentry blind spot: a crash
# (most often rumps missing/broken in the venv -> "menu bar didn't start") only
# ever landed in the local menubar.err.log. Wire it in BEFORE importing rumps so
# even an import-time failure of the menu bar's heaviest dependency is reported.
# sentry_init lives in the pipeline's scripts/ dir (SAPS_REPO_DIR is exported by
# the launchd plist) and sentry-sdk is in the owned venv (requirements.txt). All
# best-effort: a missing repo path or SDK degrades to a silent no-op.
_sentry = None
try:
    _repo = os.environ.get("SAPS_REPO_DIR")
    if _repo:
        _scripts = os.path.join(_repo, "scripts")
        if _scripts not in sys.path:
            sys.path.insert(0, _scripts)
    import sentry_init as _sentry  # noqa: E402

    _sentry.init()
except Exception:
    _sentry = None


def _capture(err, **tags):
    """Report a handled menu-bar error to Sentry (component=menubar) without ever
    raising into the caller. No-op if the Sentry bootstrap above failed."""
    try:
        if _sentry is not None:
            tags.setdefault("component", "menubar")
            _sentry.capture_exception(err, tags=tags)
    except Exception:
        pass


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

CLAUDE_APP = "Claude"
POLL_SECONDS = 5

# Autopilot scheduled tasks. The two queue workers must RUN in a dedicated folder
# (~/.s4l-worker) so their once-a-minute sessions don't flood the user's
# interactive Claude Code history (Claude buckets sessions by cwd). The single
# pre-queue autopilot task is deprecated and removed outright. Keep this in sync
# with queueWorkerCwd()/QUEUE_WORKERS in mcp/src/index.ts and scripts/s4l_box_update.sh.
WORKER_TASK_IDS = ("saps-phase1-query", "saps-phase2b-draft")
DEPRECATED_TASK_IDS = ("social-autoposter-autopilot",)
WORKER_CWD = os.path.join(os.path.expanduser("~"), ".s4l-worker")
SCHED_REGISTRY_GLOB = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Claude",
    "claude-code-sessions", "*", "*", "scheduled-tasks.json",
)

GLYPH = {"complete": "✓", "in_progress": "…", "blocked": "✗"}

# Menu-bar title spinner shown while a post is in flight.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Prompts the model-driven menu items type into the Claude Desktop composer.
# SETUP_PROMPT mirrors the in-chat panel's Setup button (panel.ts) verbatim so
# both entry points kick off the same end-to-end flow.
SETUP_PROMPT = (
    "Set up social autoposter end to end now. Inspect and repair the runtime, "
    "auto-detect and connect my X session, scan my profile, discover and research "
    "my product, then infer and save a complete project with seeded search topics. "
    "Keep going without asking me to approve each safe setup step. Ask only if I "
    "must interactively sign in or no product can be identified."
)
UPDATE_PROMPT = "Update social-autoposter to the latest version."
# Re-arm goes through the HOST create_scheduled_task path (the same one onboarding
# uses) — it registers the routines under whatever account is logged in and shows
# up in Routines. The host tool only runs inside an agent chat, so the menu bar
# hands Claude this prompt (auto-typed, clipboard+paste fallback). We do NOT write
# scheduled-tasks.json directly — that can't reliably target a just-switched-into
# account, which is exactly the bug it caused.
REARM_PROMPT = (
    "Set up the social-autoposter draft autopilot schedule for this Claude account. "
    "If queue_setup is available, call it; then for EACH of saps-phase1-query and "
    "saps-phase2b-draft call the host tool create_scheduled_task with taskId, "
    "cronExpression \"* * * * *\", and the prompt — read it from "
    "~/.claude/scheduled-tasks/<taskId>/SKILL.md (already on disk). Do not redo my X "
    "connection or project setup — only register the scheduled tasks. Keep replies short."
)

# A pending draft job older than this (seconds) with nothing claiming it means no
# routine is draining the queue — the worker would claim within a minute if it
# were firing. False-positive-free: an idle queue has no pending job at all, so a
# quiet pipeline (no candidates) never trips this. Comfortably above the host
# scheduler's per-minute cadence + a slow claim.
AUTOPILOT_STALL_SECONDS = 180

# A worker task whose lastRunAt is within this many seconds is "firing" — the host
# scheduler runs them every minute, so a fresh stamp means the live account's
# schedule is active. 7 min tolerates host throttling + a restart gap without
# false "not scheduled". Used by _schedule_state.
FIRING_WINDOW = 420


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
        # One-shot: on the first tick where the loopback is reachable, re-enqueue
        # any approvals the durable queue recorded but never confirmed posted (a
        # restart wiped the in-memory _post_q). Deferred until the loopback is up
        # so post_drafts can actually reach the server.
        self._resumed = False
        # Reliable self-check of our own Accessibility (TCC) grant — this is the
        # faithful reading (our launchd process identity, not a parent's). Logged
        # so menubar.err.log records whether keystroke posting will work.
        sys.stderr.write(
            f"[s4l-menubar] accessibility_trusted={st.accessibility_trusted()}\n"
        )
        sys.stderr.flush()
        self._timer = rumps.Timer(self._tick, POLL_SECONDS)
        self._timer.start()
        # Light 1s poll for server activity (scanning/drafting/posting/…); it
        # spins up the fast title-spinner on demand. Idle cost is one tiny file
        # read per second.
        self._act_poll = rumps.Timer(self._poll_activity, 1.0)
        self._act_poll.start()
        # Self-check for a newer published .mcpb, independent of the MCP server's
        # npm check (which has been flaky). Slow timer + cache; drives the title
        # indicator + the "Please update now" menu item.
        self._update_available = False
        self._latest_version = None
        # Poll every 15 min, NOT every 60s. _check_update hits GitHub's
        # releases/latest UNAUTHENTICATED (60 req/hr per IP); a 60s poll = 60/hr,
        # right at the cap, so it got 403-rate-limited and silently stopped
        # detecting updates (the "update available" badge never showed). 15 min =
        # 4/hr leaves ample headroom; updates aren't urgent enough to poll faster.
        self._upd_timer = rumps.Timer(self._check_update, 900)
        self._upd_timer.start()
        self._check_update(None)  # one check at launch (badge appears ~immediately)
        # One-shot self-heal: if the autopilot scheduled tasks are running in the
        # wrong folder (or the deprecated single autopilot task still exists),
        # relocate them to ~/.s4l-worker so their once-a-minute runs stop polluting
        # the user's interactive Claude Code history. Needs a single Claude restart
        # (the app caches the registry in memory), so it is capped per process.
        self._relocating = False
        self._cwd_healed = False
        self._relocate_attempts = 0
        # One-shot guard so the "autopilot not running" notification fires once per
        # stall episode, not every poll. Reset when the stall clears.
        self._stall_notified = False
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
        subprocess.run(["open", "-a", CLAUDE_APP], capture_output=True)

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

    def _send_to_claude(self, prompt):
        """Type a prompt into the Claude Desktop composer and submit it via
        AppleScript GUI scripting. The menu bar can't use the in-iframe
        sendMessage bridge, so this drives the keyboard instead. On any failure
        (most often Accessibility not yet effective) it ALWAYS degrades to copying
        the prompt to the clipboard + opening Claude, so the user can paste it
        manually rather than hitting a dead end."""
        # If we can't post keystrokes, don't silently go nowhere: request the grant
        # AND copy the prompt to the clipboard so the user can paste it right now,
        # without waiting for the TCC grant to take effect (it often needs a
        # restart of this process to register).
        if not st.accessibility_trusted():
            try:
                st.request_accessibility()
            except Exception:
                pass
            return self._manual_paste_fallback(
                prompt,
                "Couldn't auto-type (enable S4L under System Settings → Privacy & "
                "Security → Accessibility to automate next time).",
            )
        try:
            r = subprocess.run(
                ["osascript", "-e", _claude_send_script(prompt)],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception:
            r = None
        if r is not None and r.returncode == 0:
            return True
        # Automation failed despite a trusted check (stale grant / transient): fall
        # back to the clipboard so the action still completes by a manual paste.
        err = (r.stderr or "").lower() if r else ""
        if "1743" in err or "assistive" in err or "not allowed" in err or "-25211" in err:
            reason = "Accessibility isn't effective yet (it can need a restart)."
        else:
            reason = "Couldn't auto-type into Claude."
        return self._manual_paste_fallback(prompt, reason)

    # Model-driven actions: type the matching prompt into Claude's composer.
    def _setup(self, _=None):
        self._send_to_claude(SETUP_PROMPT)


    def _rearm(self, _=None):
        """Register the draft schedule for the CURRENT account via the host
        create_scheduled_task flow (same as onboarding) — it registers under
        whatever account is logged in and shows in Routines. The host tool only
        runs inside a chat turn, and the menu bar CANNOT inject into the chat (that
        bridge is panel-only), so the reliable path here is: copy the prompt to the
        clipboard, open Claude, and tell the user to paste it. (The dashboard
        widget's button does this in one click via app.sendMessage — no paste.) We
        do NOT auto-type (focus/timing flaky) and do NOT write the registry directly
        (can't reliably target a just-switched-into account)."""
        copied = self._copy_to_clipboard(REARM_PROMPT)
        self._open_claude()
        # Use a MODAL alert, not _notify: osascript `display notification` from this
        # bundle-less launchd menu-bar process silently no-ops (no notification
        # permission), so the user got zero feedback. rumps.alert is an NSAlert that
        # always shows and requires an explicit dismiss — unmissable.
        try:
            if copied:
                rumps.alert(
                    title="Paste in Claude to finish",
                    message=(
                        "The setup prompt is copied to your clipboard.\n\n"
                        "Click into the Claude chat, paste it (Cmd+V), and press Enter. "
                        "That schedules the draft tasks for this account."
                    ),
                    ok="Got it",
                )
            else:
                rumps.alert(
                    title="Set up the draft schedule",
                    message="Open Claude and ask it to set up the draft schedule for this account.",
                    ok="OK",
                )
        except Exception:
            # Best-effort fallback if the modal can't show for some reason.
            self._notify(
                "S4L · setup prompt copied" if copied else "S4L",
                "Paste it into Claude (Cmd+V) and press Enter to schedule the draft tasks.",
            )

    # ---- schedule-state detection ----------------------------------------
    # Identify the LIVE account's registry by which one the host is actually
    # FIRING (freshest lastRunAt — only the active account's scheduler advances
    # it), then read THAT registry's enabled state. This is robust across the
    # session-id churn that restarts cause; the old "active session = newest
    # config.json allowlist key" heuristic mis-reported "missing" after a restart
    # even while the tasks were firing.
    @staticmethod
    def _iso_to_epoch(s):
        if not s:
            return None
        try:
            import calendar
            return calendar.timegm(
                time.strptime(str(s).strip().rstrip("Z").split(".")[0], "%Y-%m-%dT%H:%M:%S")
            )
        except Exception:
            return None

    def _schedule_state(self):
        """Is the draft schedule registered AND running for the live account?
          'ok'       — worker tasks present+enabled and FIRING (lastRunAt within
                       FIRING_WINDOW) — the host is actively running them.
          'disabled' — present but a worker task is disabled.
          'missing'  — not firing anywhere (orphaned / not registered for the live
                       account) -> offer re-arm.
        The active account is identified by the freshest-firing registry, NOT a
        session id (which churns on restart)."""
        newest_epoch, newest_enabled = None, False
        any_present, any_enabled = False, False
        for f in glob.glob(SCHED_REGISTRY_GLOB):
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except Exception:
                continue
            by_id = {t.get("id"): t for t in (d.get("scheduledTasks") or [])}
            recs = [by_id.get(tid) for tid in WORKER_TASK_IDS]
            if any(r is None for r in recs):
                continue
            any_present = True
            enabled = all(r.get("enabled") for r in recs)
            any_enabled = any_enabled or enabled
            epochs = [self._iso_to_epoch(r.get("lastRunAt")) for r in recs]
            e = max([x for x in epochs if x is not None], default=None)
            if e is not None and (newest_epoch is None or e > newest_epoch):
                newest_epoch, newest_enabled = e, enabled
        # Firing recently => the live account's schedule is active and healthy.
        if newest_epoch is not None and (time.time() - newest_epoch) <= FIRING_WINDOW:
            return "ok" if newest_enabled else "disabled"
        # Not firing anywhere. Registered-but-disabled => disabled; else missing.
        if any_present and not any_enabled:
            return "disabled"
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
            for f in recent[:5]:
                try:
                    txt = open(f).read()
                except Exception:
                    continue
                low = txt.lower()
                if "weekly limit" in low or "usage limit" in low or "hit your limit" in low:
                    import re
                    m = re.search(r"resets [^\"\\]{0,40}", txt)
                    limit_msg = m.group(0).strip().rstrip(".") if m else "Claude usage limit reached"
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

    def _toggle_mode(self, _=None):
        """Flip personal-brand <-> promotion. Pure local state write (no model,
        no network): the cycle reads mode.json on its next run. Rebuild the menu
        right away so the checkmark + sublabel reflect the new mode instantly."""
        new = st.toggle_mode()
        self._notify(
            "S4L engagement mode",
            "Personal brand: organic, link-free"
            if new == st.MODE_PERSONAL_BRAND
            else "Promotion: marketing your products",
        )
        # Force the next tick to rebuild (mode is in the signature, but null it so
        # the rebuild can't be skipped) and rebuild now for snappy feedback.
        self._sig = None
        try:
            self._tick(None)
        except Exception as e:
            sys.stderr.write(f"[s4l-menubar] mode toggle rebuild failed: {e}\n")
            sys.stderr.flush()

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
        """
        root = os.path.expanduser(
            "~/Library/Application Support/Claude/Claude Extensions"
        )
        best, best_mtime = None, -1.0
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
            pass
        return best or os.path.join(root, "local.mcpb.m13v.social-autoposter")

    MCPB_URL = (
        "https://github.com/m13v/social-autoposter/releases/latest/download/"
        "social-autoposter.mcpb"
    )
    RELEASE_API = (
        "https://api.github.com/repos/m13v/social-autoposter/releases/latest"
    )

    @staticmethod
    def _vtuple(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except Exception:
            return (0,)

    def _check_update(self, _=None):
        """Is a newer .mcpb published than what's installed? Compare the GitHub
        latest release tag to the installed extension manifest. Offline / API
        errors leave the cached value untouched (never flips to 'no update' on a
        transient failure). A change forces a title + menu repaint."""
        installed = None
        try:
            with open(os.path.join(self._ext_dir(), "manifest.json")) as f:
                installed = (json.load(f) or {}).get("version")
        except Exception:
            return  # not a .mcpb install (or no manifest) -> nothing to offer
        try:
            r = subprocess.run(
                ["curl", "-fsSL", "-m", "15", self.RELEASE_API],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode != 0:
                return
            latest = ((json.loads(r.stdout) or {}).get("tag_name") or "").lstrip("v")
        except Exception:
            return
        if not latest:
            return
        available = bool(installed) and self._vtuple(latest) > self._vtuple(installed)
        if available != self._update_available or latest != self._latest_version:
            self._update_available = available
            self._latest_version = latest
            self._sig = None  # repaint on next tick

    def _do_mcpb_update(self, _=None):
        """User clicked 'Please update now'. Pull the latest .mcpb, unpack it over
        the Desktop extension dir in place, and restart Claude so the new server
        loads. The menu bar is a launchd process (not a Claude child), so the
        restart is clean. Heavy work runs on a background thread."""
        self._notify("S4L", "Updating… Claude will restart in a moment.")
        threading.Thread(target=self._mcpb_update_work, daemon=True).start()

    def _mcpb_update_work(self):
        tmpd = tempfile.mkdtemp(prefix="s4l-update-")
        mcpb = os.path.join(tmpd, "social-autoposter.mcpb")
        try:
            r = subprocess.run(["curl", "-fLs", "-m", "300", self.MCPB_URL, "-o", mcpb],
                               capture_output=True, timeout=320)
            if r.returncode != 0 or not os.path.exists(mcpb) or os.path.getsize(mcpb) < 100000:
                self._notify("S4L update failed", "Couldn't download the update — check your connection.")
                return
            r = subprocess.run(["unzip", "-oq", mcpb, "-d", self._ext_dir()],
                               capture_output=True, timeout=180)
            if r.returncode != 0:
                self._notify("S4L update failed", "Couldn't unpack the update.")
                return
            # Restart Claude so the refreshed server loads (we're decoupled from it).
            subprocess.run(["osascript", "-e", 'tell application "Claude" to quit'],
                           capture_output=True, timeout=20)
            time.sleep(4)
            subprocess.run(["killall", "Claude"], capture_output=True)      # if quit was blocked
            time.sleep(2)
            subprocess.run(["killall", "-9", "Claude"], capture_output=True)
            time.sleep(1)
            # Claude is fully down now — relocate the autopilot scheduled tasks'
            # cwd so their once-a-minute runs stop flooding the user's interactive
            # `claude --resume` history. MUST happen while Claude is down (it caches
            # the registry in memory and clobbers live edits). See queueWorkerCwd()
            # in mcp/src/index.ts and the same routine in scripts/s4l_box_update.sh.
            self._rewrite_scheduled_task_cwd()
            subprocess.run(["open", "-a", CLAUDE_APP], capture_output=True, timeout=20)
            self._update_available = False
            self._sig = None
            self._notify("S4L updated", "Claude restarted on the latest version.")
        except Exception as e:
            self._notify("S4L update failed", str(e)[:140])
        finally:
            try:
                import shutil
                shutil.rmtree(tmpd, ignore_errors=True)
            except Exception:
                pass

    @staticmethod
    def _scheduled_task_cwd_needs_fix():
        """Read-only: True if any worker task runs in the wrong folder OR the
        deprecated autopilot task still exists. Drives the one-shot self-heal."""
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
                    if tid in WORKER_TASK_IDS and t.get("cwd") != WORKER_CWD:
                        return True
        except Exception:
            pass
        return False

    def _rewrite_scheduled_task_cwd(self):
        """Point the queue-worker tasks' cwd at ~/.s4l-worker and REMOVE the
        deprecated single autopilot task, across every scheduled-tasks.json
        registry. Caller MUST invoke this only while Claude is DOWN — the running
        app caches the registry in memory and clobbers a live edit on the next
        fire. Best-effort: never raises. Kept in sync with scripts/s4l_box_update.sh
        and queueWorkerCwd()/QUEUE_WORKERS in mcp/src/index.ts."""
        try:
            os.makedirs(WORKER_CWD, exist_ok=True)
        except Exception:
            pass
        try:
            for f in glob.glob(SCHED_REGISTRY_GLOB):
                try:
                    with open(f) as fh:
                        d = json.load(fh)
                except Exception:
                    continue
                tasks = d.get("scheduledTasks") or []
                new_tasks = []
                dirty = False
                for t in tasks:
                    tid = t.get("id")
                    if tid in DEPRECATED_TASK_IDS:
                        dirty = True          # drop it
                        continue
                    if tid in WORKER_TASK_IDS and t.get("cwd") != WORKER_CWD:
                        t["cwd"] = WORKER_CWD
                        dirty = True
                    new_tasks.append(t)
                if not dirty:
                    continue
                d["scheduledTasks"] = new_tasks
                try:
                    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(f))
                    with os.fdopen(fd, "w") as fh:
                        json.dump(d, fh, indent=2)
                    os.replace(tmp, f)
                except Exception:
                    pass
        except Exception:
            pass
        # Remove the deprecated task's on-disk SKILL.md dir too, so it can't be
        # re-registered from a stale prompt file.
        try:
            for tid in DEPRECATED_TASK_IDS:
                import shutil
                shutil.rmtree(os.path.join(os.path.expanduser("~"), ".claude",
                                           "scheduled-tasks", tid), ignore_errors=True)
        except Exception:
            pass

    def _maybe_relocate_tasks(self, _=None):
        """Timer callback: one-shot self-heal. If the autopilot tasks are in the
        wrong folder (or the deprecated task lingers), relocate them once, which
        needs a single Claude restart (the app caches the registry). Capped at a
        couple of attempts per process so a persistent failure can't restart-loop."""
        if self._relocating or self._cwd_healed or self._relocate_attempts >= 2:
            return
        try:
            if not self._scheduled_task_cwd_needs_fix():
                return
            self._relocating = True
            self._relocate_attempts += 1
            self._notify("S4L", "Tidying autopilot… Claude will restart once.")
            threading.Thread(target=self._relocate_restart_work, daemon=True).start()
        except Exception:
            self._relocating = False

    def _relocate_restart_work(self):
        """Restart Claude with the tasks relocated. Mirror of _mcpb_update_work's
        restart block: quit/kill Claude, rewrite the registry while it's down, then
        relaunch. The menu bar is a separate launchd process, so killing Claude does
        not kill us."""
        try:
            subprocess.run(["osascript", "-e", 'tell application "Claude" to quit'],
                           capture_output=True, timeout=20)
            time.sleep(4)
            subprocess.run(["killall", "Claude"], capture_output=True)
            time.sleep(2)
            subprocess.run(["killall", "-9", "Claude"], capture_output=True)
            time.sleep(1)
            self._rewrite_scheduled_task_cwd()
            subprocess.run(["open", "-a", CLAUDE_APP], capture_output=True, timeout=20)
            time.sleep(8)  # let Claude reload the registry before we re-check
            if not self._scheduled_task_cwd_needs_fix():
                self._cwd_healed = True
        except Exception:
            pass
        finally:
            self._relocating = False

    def _open_dashboard(self, _=None):
        url = st.panel_url()
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
        return f"posting · {sent} sent"

    def _spin(self, _):
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
        pending = st.approved_queue_pending()
        if not pending:
            return
        posted_ns = set()
        try:
            req = st.read_review_request()
            plan_path = (req or {}).get("plan_path") or "/tmp/twitter_cycle_plan_review-queue.json"
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
    def _tick(self, _):
        # Restart recovery (one-shot, once the loopback is up so posting can reach
        # the server): resume any approved-but-unposted drafts the durable queue
        # recorded, instead of stranding them and re-presenting the cards.
        if not self._resumed and st.loopback_reachable():
            self._resumed = True
            try:
                self._resume_approved_queue()
            except Exception as e:
                sys.stderr.write(f"[s4l-menubar] resume approved queue failed: {e}\n")
                sys.stderr.flush()
        # The activity spinner owns the TITLE while a tool runs (we don't fight it at
        # 0.12s), but the menu + update indicator must still refresh mid-run —
        # otherwise the "Please update now" item never appears on a box that's always
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
        # PRIMARY signal: read the ACTUAL schedule for the CURRENT account
        # (missing/disabled/ok) — reliable, NOT inferred from whether it fired
        # ("not fired" != "not scheduled"). SECONDARY: if scheduled+enabled but
        # still not draining, work out why (rate-limit vs other).
        schedule_state = self._schedule_state() if setup_complete else "ok"
        stalled = setup_complete and self._autopilot_stalled()
        self._schedule_state_cache = schedule_state
        # Stall reason only matters when the schedule IS on but drafts still aren't
        # draining (rate-limit / other) — not when it's simply unscheduled.
        self._stall_reason_info = (
            self._stall_reason() if (stalled and schedule_state in ("ok", "unknown")) else ("", "")
        )
        # Any non-healthy state drops the stale "drafting" spinner so the ⚠ in the
        # title isn't masked (see _poll_activity).
        attention = setup_complete and (schedule_state in ("missing", "disabled") or stalled)
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
        # Notify once per episode, naming the actual problem + the right action.
        if attention and not self._stall_notified:
            kind, detail = self._stall_reason_info
            if schedule_state == "missing":
                self._notify(
                    "S4L draft autopilot not scheduled",
                    "No draft tasks are scheduled on this Claude account (switching "
                    "accounts clears them). Open the S4L menu → “Set up draft schedule”.",
                )
            elif schedule_state == "disabled":
                self._notify(
                    "S4L draft tasks disabled",
                    "The draft tasks are scheduled but disabled. Open the S4L menu → "
                    "“Set up draft schedule” to re-enable.",
                )
            elif kind == "rate_limited":
                self._notify(
                    "S4L autopilot paused",
                    "Claude usage limit reached" + (f" ({detail})" if detail else "")
                    + ". Drafting resumes at reset, or sign into an account with quota.",
                )
            else:
                self._notify(
                    "S4L autopilot stalled",
                    "Drafts aren't being produced though the schedule is on. Open the "
                    "S4L menu for options.",
                )
            self._stall_notified = True
        elif not attention:
            self._stall_notified = False

        # Only rebuild the menu when something user-visible changed, so an open
        # menu isn't torn down under the user's cursor every poll.
        done = (
            sum(1 for m in ob["milestones"] if m.get("status") == "complete")
            if ob
            else 0
        )
        # _update_available / _latest_version are in the signature so a freshly
        # detected update rebuilds the menu (adding "Please update now") even mid-run.
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
            st.read_mode(),
            attention,
            schedule_state,
            self._stall_reason_info,
        )
        if sig != self._sig:
            self._sig = sig
            self._build_menu(runtime_ready, setup_complete, ob, blocker, snap, attention, schedule_state)

        # Draft-review pop-ups: if a draft cycle left a review request, present the
        # cards. Don't start a review mid-run (the spinner means a tool is active).
        if not busy:
            self._maybe_start_review()

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

            s4l_card.present_review(
                drafts,
                on_decision=lambda d: self._on_card_decision(batch, d),
                on_complete=lambda decisions: self._on_review_closed(batch, decisions),
            )
            # Record as shown only AFTER the cards are actually up, so a transient
            # card-UI failure never permanently suppresses this pending set.
            self._last_review_sig = sig
        except Exception as e:
            # Card UI unavailable — don't strand the batch; chat review still works.
            self._review_active = False
            self._panel_open = False
            sys.stderr.write(f"[s4l-menubar] review cards failed: {e}\n")
            sys.stderr.flush()
            _capture(e, phase="review_cards")

    def _on_card_decision(self, batch, decision):
        # Runs on the main thread the INSTANT a card is approved/rejected. An
        # approved card is enqueued for immediate posting; a REJECTED card is
        # persisted (marked done so it's never re-shown for review) on a quick
        # background thread. We never block inline here — posting can take minutes
        # and would freeze the card UI while the user reviews the rest of the stack.
        if not decision.get("approved"):
            n = decision.get("n")

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
        st.approved_queue_add(
            batch,
            n,
            text=decision.get("text") or "",
            edited=bool(decision.get("edited")),
            drop_link=bool(decision.get("drop_link")),
        )
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
                self._notify("S4L", f"Posting draft {n}…")
                st.approved_queue_set_status(batch, n, "posting")
                with self._review_lock:
                    activity_label = self._posting_activity_label_locked()
                cl = [n] if decision.get("drop_link") else None
                if decision.get("edited"):
                    res = st.post_drafts(
                        batch,
                        edits=[{"n": n, "text": decision.get("text") or ""}],
                        clear_link=cl,
                        activity_label=activity_label,
                    )
                else:
                    res = st.post_drafts(batch, post=[n], clear_link=cl, activity_label=activity_label)
                if res is None:
                    # Loopback unreachable (Claude closed). Mark failed so the card
                    # falls back to manual review rather than silently vanishing.
                    st.approved_queue_set_status(batch, n, "failed", error="loopback_unreachable")
                    self._notify(
                        "S4L", "Couldn't post — open Claude Desktop and try the draft again."
                    )
                else:
                    posted = res.get("posted") if isinstance(res, dict) else None
                    if posted == 0:
                        st.approved_queue_set_status(batch, n, "failed", error="posted_0")
                        self._notify("S4L", f"Draft {n} didn't post — see the dashboard for why.")
                    else:
                        st.approved_queue_set_status(batch, n, "posted")
                        self._notify("S4L", f"Posted draft {n}.")
            except Exception as e:
                st.approved_queue_set_status(batch, n, "failed", error=str(e)[:200])
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

        ver = snap.get("version") or st.version()
        header = rumps.MenuItem(f"S4L · v{ver}" if ver else "S4L")
        header.set_callback(None)  # non-clickable label
        items.append(header)
        items.append(rumps.separator)

        # Autopilot needs attention: surface the right action FIRST, above the
        # normal state items. PRIMARY driver = the actual schedule state for THIS
        # account (missing/disabled) — that's what "Set up draft schedule" fixes
        # (host create_scheduled_task). Only when the schedule IS on but drafts
        # aren't draining do we fall to the stall-reason wording (rate-limit/other),
        # where re-creating the schedule wouldn't help.
        if attention:
            if schedule_state == "missing":
                items.append(self._label("⚠ Draft tasks aren’t scheduled on this account"))
                items.append(rumps.MenuItem("Set up draft schedule for this account", callback=self._rearm))
            elif schedule_state == "disabled":
                items.append(self._label("⚠ Draft tasks are scheduled but disabled"))
                items.append(rumps.MenuItem("Set up draft schedule for this account", callback=self._rearm))
            else:
                kind, detail = self._stall_reason_info
                if kind == "rate_limited":
                    items.append(self._label("⚠ Autopilot paused — Claude usage limit"))
                    if detail:
                        items.append(self._label(f"   {detail}"))
                    items.append(self._label("   Resumes at reset, or switch Claude account"))
                else:
                    items.append(self._label("⚠ Scheduled, but no drafts are being produced"))
                    items.append(rumps.MenuItem("Set up draft schedule for this account", callback=self._rearm))
            items.append(rumps.separator)

        if not runtime_ready:
            items += self._state_a()
        elif not setup_complete:
            items += self._state_b(ob, blocker)
        else:
            items += self._state_c(snap)

        items.append(rumps.separator)
        items.append(rumps.MenuItem("Open dashboard", callback=self._open_dashboard))
        if self._update_available and self._latest_version:
            items.append(rumps.separator)
            items.append(self._label(f"⬆ Update available · v{self._latest_version}"))
            items.append(
                rumps.MenuItem("Please update now", callback=self._do_mcpb_update)
            )
        items.append(rumps.MenuItem("Quit", callback=rumps.quit_application))

        for it in items:
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

    # State C — setup complete: the mini dashboard.
    def _state_c(self, snap):
        out = []
        handle = snap.get("x_handle")
        out.append(
            self._label(f"@{handle.lstrip('@')}" if handle else "X connected")
        )
        out.append(
            self._label(
                f"Ready · {snap.get('projects_ready', 0)}/"
                f"{snap.get('projects_total', 0)} projects"
            )
        )
        stats = st.stats_7d()
        if stats:
            out.append(
                self._label(
                    f"7d: {stats['posts']} posts · {stats['views']} views "
                    f"· {stats['replies']} replies"
                )
            )
        else:
            out.append(self._label("7d stats — open dashboard"))

        out.append(rumps.separator)
        # Engagement-mode toggle (2026-06-26). A checkmark = personal-brand mode
        # (link-free organic engagement for the user's own brand); unchecked =
        # the default promotion pipeline (marketing the configured products).
        # The cycle reads this on its next run via scripts/saps_mode.py, so the
        # flip takes effect without restarting anything.
        mode = st.read_mode()
        personal = mode == st.MODE_PERSONAL_BRAND
        mode_item = rumps.MenuItem(
            "Personal brand mode", callback=self._toggle_mode
        )
        mode_item.state = 1 if personal else 0
        out.append(mode_item)
        out.append(
            self._label(
                "   organic, link-free engagement"
                if personal
                else "   promoting your products"
            )
        )

        # No "Run draft cycle" item: the autopilot drafts on its own (launchd
        # kicker + queue worker), so a manual draft-now action is redundant.
        # No "Post approved drafts" item: approving a review card already posts
        # that card directly + programmatically (_on_card_decision -> queue ->
        # _post_worker_loop -> st.post_drafts -> the CDP poster). A menu button
        # that types a prompt into the chat to do the same thing was a redundant
        # detour through the model, so it's gone.
        return out


if __name__ == "__main__":
    try:
        S4LMenuBar().run()
    except Exception as _run_err:
        # The run loop dying is the other "menu bar didn't start / vanished" case.
        # Report + flush before the KeepAlive relaunch so it isn't lost on teardown.
        _capture(_run_err, phase="run")
        _flush()
        raise
