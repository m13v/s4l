"""S4L menu bar app — a tiny live mini-dashboard for social-autoposter.

A status-bar companion that mirrors the in-chat dashboard's three states, but
much smaller: the menu bar title carries the at-a-glance state and the dropdown
is a flat native list. It NEVER duplicates pipeline logic — it reads state via
s4l_state (loopback tools when Claude Desktop is up, raw state files when it's
down).

The one capability it cannot have is injecting a prompt into the Claude Desktop
chat (that bridge only exists for the inline panel iframe). So the model-driven
actions (Set up, Run draft cycle) degrade to focusing Claude Desktop; the
no-model actions (autopilot toggle, open dashboard) work standalone.

Runs as a LaunchAgent off the owned venv (rumps is installed there by the
runtime install step). No .app bundle, so notifications go through osascript
rather than rumps.notification (which needs a bundle id).
"""

import json
import subprocess
import sys
import threading

import rumps

import s4l_state as st

CLAUDE_APP = "Claude"
POLL_SECONDS = 5

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
DRAFT_PROMPT = "Run a social-autoposter draft cycle and show me the drafts to review."
POST_PROMPT = "Post my approved social-autoposter drafts."
UPDATE_PROMPT = "Update social-autoposter to the latest version."


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
        self._reviewed_batches = set()  # batch_ids already handled this run
        self._spin_i = 0
        self._spinner = None  # fast rumps.Timer animating the title while busy
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
        self._tick(None)

    # ---- side effects -----------------------------------------------------
    def _open_claude(self, _=None):
        subprocess.run(["open", "-a", CLAUDE_APP], capture_output=True)

    def _send_to_claude(self, prompt):
        """Type a prompt into the Claude Desktop composer and submit it via
        AppleScript GUI scripting. The menu bar can't use the in-iframe
        sendMessage bridge, so this drives the keyboard instead. On failure
        (most often Accessibility permission not yet granted) it degrades to
        just focusing Claude and tells the user what to do."""
        # Reliably know up front whether we can post keystrokes; if not, prompt
        # for the grant + open Settings instead of a paste that would silently
        # go nowhere.
        if not st.accessibility_trusted():
            st.request_accessibility()
            self._open_claude()
            self._notify(
                "S4L needs Accessibility",
                "Enable S4L (python) under System Settings → Privacy & Security "
                "→ Accessibility, then click again.",
            )
            return False
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
        # Failed: bring Claude forward anyway, then explain.
        self._open_claude()
        err = (r.stderr or "").lower() if r else ""
        if "1743" in err or "assistive" in err or "not allowed" in err or "-25211" in err:
            self._notify(
                "S4L needs Accessibility",
                "Enable the S4L menu bar app under System Settings → Privacy & "
                "Security → Accessibility, then try again.",
            )
        else:
            self._notify("S4L", "Opened Claude — type your request there.")
        return False

    # Model-driven actions: type the matching prompt into Claude's composer.
    def _setup(self, _=None):
        self._send_to_claude(SETUP_PROMPT)

    def _draft(self, _=None):
        self._send_to_claude(DRAFT_PROMPT)

    def _post(self, _=None):
        self._send_to_claude(POST_PROMPT)

    def _update(self, _=None):
        self._send_to_claude(UPDATE_PROMPT)

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

    def _toggle_ap(self, sender):
        desired = not bool(sender.state)
        if st.set_autopilot(desired):
            sender.state = 1 if desired else 0
        else:
            self._notify("S4L", "Open Claude Desktop to change autopilot.")

    # ---- activity spinner -------------------------------------------------
    # The server writes activity.json while a tool runs (scanning/drafting/
    # posting/…). _poll_activity (1s) starts the fast spinner; _spin (0.12s)
    # animates the title with the label and stops itself when activity clears.
    # Both run on the main thread (rumps timers).
    def _poll_activity(self, _):
        act = st.read_activity()
        if act and act.get("label") and self._spinner is None:
            self._spin_i = 0
            self._spinner = rumps.Timer(self._spin, 0.12)
            self._spinner.start()

    def _spin(self, _):
        act = st.read_activity()
        label = act.get("label") if act else None
        if label:
            self._spin_i = (self._spin_i + 1) % len(SPINNER)
            self.title = f"S4L {label} {SPINNER[self._spin_i]}"
            return
        try:
            if self._spinner is not None:
                self._spinner.stop()
        except Exception:
            pass
        self._spinner = None
        self.title = "S4L"
        self._sig = None  # force the next tick to repaint title + menu

    # ---- tick: read state, set title, (re)build menu ----------------------
    def _tick(self, _):
        # While a tool is running, the spinner owns the title/menu; don't fight it
        # or start a review mid-run.
        if self._spinner is not None:
            return
        snap = st.snapshot()
        ob = snap.get("onboarding") or st.read_onboarding()
        runtime_ready = bool(snap.get("runtime_ready"))
        if snap.get("_live"):
            setup_complete = (
                runtime_ready
                and snap.get("projects_ready", 0) > 0
                and bool(snap.get("x_connected"))
            )
        else:
            # Offline: the ledger's "complete" (all 7 milestones) is the proxy.
            setup_complete = bool(ob and ob.get("complete"))
        blocker = (ob or {}).get("current_blocker")
        blocker_code = (blocker or {}).get("code")

        self._render_title(setup_complete, ob, blocker)

        # Blocker notification only on transition into a new blocker.
        if blocker and blocker_code != self._last_blocker_code:
            self._notify(
                "S4L setup needs you",
                blocker.get("message", "Setup is blocked"),
            )
        self._last_blocker_code = blocker_code

        # Only rebuild the menu when something user-visible changed, so an open
        # menu isn't torn down under the user's cursor every poll.
        done = (
            sum(1 for m in ob["milestones"] if m.get("status") == "complete")
            if ob
            else 0
        )
        sig = (
            runtime_ready,
            setup_complete,
            blocker_code,
            done,
            bool(snap.get("autopilot_on")),
            snap.get("version"),
            snap.get("update_available"),
            snap.get("x_handle"),
            snap.get("projects_ready"),
            snap.get("projects_total"),
        )
        if sig != self._sig:
            self._sig = sig
            self._build_menu(runtime_ready, setup_complete, ob, blocker, snap)

        # Draft-review pop-ups: if a draft cycle left a review request, present the
        # cards. Independent of the menu rebuild above.
        self._maybe_start_review()

    # ---- draft review pop-ups ---------------------------------------------
    def _maybe_start_review(self):
        if self._review_active:
            return
        req = st.read_review_request()
        if not req:
            return
        batch = req.get("batch_id")
        if not batch or batch in self._reviewed_batches:
            return
        plan = st.read_plan(req.get("plan_path") or "")
        drafts = st.review_drafts(plan)
        # Nothing left to review (empty, missing plan, or all already posted via
        # the chat surface) — mark handled and clear the signal.
        if not drafts:
            self._reviewed_batches.add(batch)
            st.clear_review_request()
            return
        self._review_active = True
        self._reviewed_batches.add(batch)
        try:
            import s4l_card

            s4l_card.present_review(
                drafts, lambda decisions: self._on_review_done(batch, decisions)
            )
        except Exception as e:
            # Card UI unavailable — don't strand the batch; chat review still works.
            self._review_active = False
            sys.stderr.write(f"[s4l-menubar] review cards failed: {e}\n")
            sys.stderr.flush()

    def _on_review_done(self, batch, decisions):
        # Runs on the main thread (from the card controller). Translate decisions
        # into post_drafts args and post on a background thread so the UI stays
        # responsive (posting can take minutes).
        approved = [d for d in decisions if d.get("approved")]
        post_nums = [d["n"] for d in approved if not d.get("edited")]
        edits = [{"n": d["n"], "text": d["text"]} for d in approved if d.get("edited")]
        st.clear_review_request()
        if not approved:
            self._review_active = False
            self._notify("S4L", "No drafts approved — nothing posted.")
            return
        self._notify("S4L", f"Posting {len(approved)} draft(s)…")

        # The server's post_drafts writes "posting" activity, so the activity
        # spinner shows automatically while this runs — no local spinner needed.
        def work():
            res = st.post_drafts(batch, post=post_nums, edits=edits)
            if res is None:
                self._notify(
                    "S4L", "Couldn't post — open Claude Desktop and try the draft again."
                )
            else:
                posted = res.get("posted") if isinstance(res, dict) else None
                self._notify(
                    "S4L",
                    f"Posted {posted if posted is not None else len(approved)} draft(s).",
                )
            self._review_active = False

        threading.Thread(target=work, daemon=True).start()

    def _render_title(self, setup_complete, ob, blocker):
        if blocker:
            self.title = "S4L ⚠"  # warning sign
        elif not setup_complete and ob and not ob.get("complete"):
            done = sum(1 for m in ob["milestones"] if m.get("status") == "complete")
            self.title = f"S4L {done}/{len(ob['milestones'])}"
        else:
            self.title = "S4L"

    # ---- menu construction ------------------------------------------------
    def _build_menu(self, runtime_ready, setup_complete, ob, blocker, snap):
        self.menu.clear()
        items = []

        ver = snap.get("version") or st.version()
        header = rumps.MenuItem(f"S4L · v{ver}" if ver else "S4L")
        header.set_callback(None)  # non-clickable label
        items.append(header)
        items.append(rumps.separator)

        if not runtime_ready:
            items += self._state_a()
        elif not setup_complete:
            items += self._state_b(ob, blocker)
        else:
            items += self._state_c(snap)

        items.append(rumps.separator)
        items.append(rumps.MenuItem("Open dashboard", callback=self._open_dashboard))
        if snap.get("update_available") and snap.get("latest_version"):
            items.append(
                rumps.MenuItem(
                    f"Update to v{snap['latest_version']} in Claude",
                    callback=self._update,
                )
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
        ap = rumps.MenuItem("Autopilot", callback=self._toggle_ap)
        ap.state = 1 if snap.get("autopilot_on") else 0
        out.append(ap)
        out.append(
            rumps.MenuItem("Run draft cycle in Claude", callback=self._draft)
        )
        out.append(
            rumps.MenuItem("Post approved drafts in Claude", callback=self._post)
        )
        return out


if __name__ == "__main__":
    S4LMenuBar().run()
