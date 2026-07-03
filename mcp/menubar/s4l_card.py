"""Corner pop-up review cards for draft approval (AppKit / pyobjc).

`present_review(drafts, on_decision, on_complete)` shows one small floating panel
per draft in the top-right corner: thread context, an EDITABLE reply field, a
counter, and Reject / Approve. `on_decision` fires the INSTANT each card is
approved/rejected (so an approved draft can post right away), and `on_complete`
fires once the last card is decided or the window is closed. The whole AppKit
surface is isolated behind that one function so the menu bar wiring doesn't
depend on the windowing details.

Decision shape: {"n": int, "approved": bool, "loved": bool, "text": str,
"edited": bool, "drop_link": bool, "reject_category": str|None,
"reject_note": str|None, "interactions": [{"type": str, "ts": str}],
"dwell_ms": int}

Approving comes in two strengths: the plain Approve button, and the 😄 button
right next to it, which approves AND stamps loved=True: the user's "this one
was a really good pick" signal, which the feedback digest treats as strong
positive evidence (a plain approve is merely counter-evidence against avoid
entries).

`present_feedback(on_submit)` is the OVERALL-feedback composer: a small
floating panel with one free-text field, reachable from the card's 💬 button
and from the menu bar's "Send feedback…" item. It carries guidance not tied to
any single thread; the menu bar registers a default submit handler via
`set_feedback_handler` (shipping decision='feedback' review events) so both
entry points behave identically.

Rejecting is a two-step flow: the Reject button swaps the card body for a
reason picker (three one-tap categories plus Other, with an optional free-text
note). The category feeds the feedback-digest loop that distills human
rejections into each project's learned_preferences config block; "Skip" keeps
the old zero-friction reject (no reason recorded). Link clicks (author profile,
thread ↗) and per-card dwell time ride along on the decision so the digest can
infer intent (e.g. profile-checked-then-rejected = author-quality signal) even
when the reason is skipped.

Must be driven on the main thread (the menu bar's rumps timer is on the main
run loop, so that holds).
"""

import datetime
import json
import re
import time

import objc
from Foundation import (
    NSObject,
    NSMakeRect,
    NSAttributedString,
    NSMutableAttributedString,
    NSURL,
)
from AppKit import (
    NSApp,
    NSPanel,
    NSButton,
    NSTextField,
    NSTextView,
    NSScrollView,
    NSScreen,
    NSEvent,
    NSWindowOcclusionStateVisible,
    NSColor,
    NSFont,
    NSView,
    NSWorkspace,
    NSBackingStoreBuffered,
    NSFloatingWindowLevel,
    NSApplicationActivationPolicyAccessory,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskUtilityWindow,
    NSLineBreakByWordWrapping,
    NSBezelStyleRounded,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSUnderlineStyleAttributeName,
    NSUnderlineStyleSingle,
    NSLinkAttributeName,
    NSTextAlignmentLeft,
    NSTextAlignmentRight,
    NSImage,
    NSPopover,
    NSPopoverBehaviorApplicationDefined,
    NSViewController,
    NSTrackingArea,
    NSTrackingMouseEnteredAndExited,
    NSTrackingActiveAlways,
    NSEventModifierFlagCommand,
    NSEventModifierFlagShift,
    NSEventModifierFlagDeviceIndependentFlagsMask,
)

# Strong reference to the live controller so pyobjc doesn't GC it mid-review
# (the classic "button click crashes" footgun).
_active = None

W = 380
H = 300
M = 16
NS_BEZEL_BORDER = 2  # NSBezelBorder

# Reject-reason categories, in display order. Tags are 1-based button tags on
# the reason picker; values must match the review_events.reject_category CHECK
# constraint server-side (wrong_author | off_topic | bad_draft | other).
REJECT_REASONS = (
    ("wrong_author", "Wrong author / audience"),
    ("off_topic", "Off-topic thread"),
    ("bad_draft", "Draft doesn't sound right"),
    ("other", "Other"),
)

# Client-side cap on tracked interactions per card (server clips at 50 too).
MAX_INTERACTIONS = 50

# Review-surface state mirrored to the state dir for out-of-process observers
# (the menu bar watchdog, the dashboard, a debugging session). In the 2026-07-02
# incident a card sat unseen for 3 hours and NOTHING on disk could distinguish
# "cards shown and ignored" from "cards never shown"; this file is that record.
REVIEW_STATE_FILE = "review-state.json"


def _write_review_state(controller=None, last_event=None):
    """Best-effort snapshot of the review surface to review-state.json. Passing
    the controller explicitly matters during _build, when the module-level
    _active has not been assigned yet."""
    try:
        from pathlib import Path

        import s4l_state

        c = controller if controller is not None else _active
        state = {"open": False}
        if c is not None and c._panel is not None:
            state = c.status_dict()
        if last_event:
            state["last_event"] = last_event
        state["updated_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        p = Path(s4l_state.state_dir()) / REVIEW_STATE_FILE
        p.write_text(json.dumps(state) + "\n")
    except Exception:
        pass


def _mouse_screen():
    """The screen the pointer is on right now, i.e. where the user is actually
    looking. Spawning on mainScreen() placed cards on whichever display last
    held key focus, which on a multi-monitor Mac can be a corner the user never
    checks. Falls back to mainScreen when the pointer is between screens."""
    try:
        loc = NSEvent.mouseLocation()
        for s in NSScreen.screens():
            f = s.frame()
            if (
                f.origin.x <= loc.x <= f.origin.x + f.size.width
                and f.origin.y <= loc.y <= f.origin.y + f.size.height
            ):
                return s
    except Exception:
        pass
    return NSScreen.mainScreen()


def _corner_frame(screen):
    """Top-right corner frame for the card on the given screen."""
    vf = (
        screen.visibleFrame()
        if screen is not None
        else NSMakeRect(0, 0, 1440, 900)
    )
    x = vf.origin.x + vf.size.width - W - M
    y = vf.origin.y + vf.size.height - H - M
    return NSMakeRect(x, y, W, H)


class _ReviewPanel(NSPanel):
    """A status-bar app panel that can actually own text input."""

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True

    def performKeyEquivalent_(self, event):
        # A rumps (status-bar) app has no Edit menu, so Cmd+V/C/X/A/Z have no
        # menu item to dispatch to and AppKit silently drops them; every text
        # field in this panel (reply editor, reject-reason note) was therefore
        # un-pasteable. Route the standard editing actions down the responder
        # chain ourselves.
        flags = event.modifierFlags() & NSEventModifierFlagDeviceIndependentFlagsMask
        if flags & NSEventModifierFlagCommand:
            key = (event.charactersIgnoringModifiers() or "").lower()
            shift = bool(flags & NSEventModifierFlagShift)
            sel = {
                "v": "paste:",
                "c": "copy:",
                "x": "cut:",
                "a": "selectAll:",
                "z": "redo:" if shift else "undo:",
            }.get(key)
            if sel is not None and NSApp.sendAction_to_from_(sel, None, self):
                return True
        return objc.super(_ReviewPanel, self).performKeyEquivalent_(event)


def _log(msg):
    """Stderr breadcrumb in the menubar.err.log style; card interactions are
    otherwise invisible when debugging a remote box."""
    import sys

    print(f"[s4l-card] {msg}", file=sys.stderr, flush=True)


def _truncate(s, n=320):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_count(n):
    """1234 -> '1.2k', 3400000 -> '3.4M'; None/garbage -> None (omit the stat)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n >= 1_000_000:
        s = f"{n / 1_000_000:.1f}M"
        return s.replace(".0M", "M")
    if n >= 1_000:
        s = f"{n / 1_000:.1f}k"
        return s.replace(".0k", "k")
    return str(n)


def _followers_str(stats):
    """Author followers (profile stat), shown INLINE next to the handle in the
    author row. None when the pipeline didn't capture it."""
    followers = _fmt_count((stats or {}).get("author_followers"))
    return None if followers is None else f"{followers} followers"


def _engagement_line(stats):
    """The thread's engagement counts, shown ONLY in the eye icon's hover/click
    popover, never inline on the card. Fields the pipeline didn't capture are
    omitted; returns '' when nothing is known (the eye icon is skipped then)."""
    stats = stats or {}
    parts = []
    for key, label in (
        ("likes", "likes"),
        ("retweets", "reposts"),
        ("replies", "replies"),
        ("views", "views"),
    ):
        v = _fmt_count(stats.get(key))
        if v is not None:
            parts.append(f"{v} {label}")
    return " · ".join(parts)


def _age_str(iso):
    """Thread age since tweet_posted_at, minute-granular for fresh threads
    ('38m'); rolls to hours/days only when minutes would be absurd."""
    if not iso:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        mins = int(
            (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() // 60
        )
    except Exception:
        return None
    mins = max(mins, 0)
    if mins < 100:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _label(frame, text, *, size=12, bold=False, muted=False):
    f = NSTextField.alloc().initWithFrame_(frame)
    f.setStringValue_(text or "")
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setFont_(
        NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    )
    if muted:
        f.setTextColor_(NSColor.secondaryLabelColor())
    f.setLineBreakMode_(NSLineBreakByWordWrapping)
    try:
        f.cell().setWraps_(True)
        f.cell().setScrollable_(False)
    except Exception:
        pass
    return f


class _ReviewController(NSObject):
    def initWithDrafts_onDecision_onComplete_(self, drafts, on_decision, on_complete):
        self = objc.super(_ReviewController, self).init()
        if self is None:
            return None
        self._drafts = list(drafts)
        self._on_decision = on_decision
        self._on_complete = on_complete
        self._idx = 0
        self._decisions = []
        self._panel = None
        self._textview = None
        self._link_targets = {}
        self._eye_btn = None
        self._stats_popover = None
        # Per-card telemetry, reset when a NEW card renders (not on the
        # card <-> reason-picker swap, which is the same card).
        self._rendered_idx = -1
        self._interactions = []
        self._card_shown_at = None
        self._reason_field = None
        # Attention anchors for the unattended-review watchdog: the stack counts
        # as "touched" on present, on any tracked interaction, and on any
        # decision. No touch past the watchdog threshold = the user is not
        # seeing this window, wherever AppKit thinks it is.
        self._presented_at = time.time()
        self._last_decision_at = None
        self._last_interaction_at = None
        self._last_move_log = 0.0
        self._build()
        return self

    @objc.python_method
    def _track(self, kind):
        """Append one interaction breadcrumb for the CURRENT card. Rides on the
        decision dict so the feedback loop can correlate behavior (opened the
        author profile, read the thread) with the eventual approve/reject."""
        self._last_interaction_at = time.time()
        if len(self._interactions) >= MAX_INTERACTIONS:
            return
        self._interactions.append(
            {
                "type": kind,
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )
        _log(f"interaction {kind} (card {self._idx + 1})")

    @objc.python_method
    def _dwell_ms(self):
        if not self._card_shown_at:
            return None
        return int((time.time() - self._card_shown_at) * 1000)

    @objc.python_method
    def _occlusion_visible(self):
        """True when macOS is drawing at least one pixel of the card (not fully
        covered, not on an inactive Space, not minimized). None if unknowable."""
        try:
            return bool(
                self._panel.occlusionState() & NSWindowOcclusionStateVisible
            )
        except Exception:
            return None

    @objc.python_method
    def status_dict(self):
        """Snapshot for the watchdog and review-state.json: is a card open, how
        many drafts are undecided, when it was last touched, where it is, and
        whether the user could physically see it."""
        frame = None
        try:
            fr = self._panel.frame()
            frame = [
                int(fr.origin.x),
                int(fr.origin.y),
                int(fr.size.width),
                int(fr.size.height),
            ]
        except Exception:
            pass
        screen_name = None
        try:
            scr = self._panel.screen()
            if scr is not None:
                screen_name = str(scr.localizedName())
        except Exception:
            pass
        return {
            "open": self._panel is not None,
            "total": len(self._drafts),
            "pending": max(0, len(self._drafts) - self._idx),
            "decided": len(self._decisions),
            "presented_at": self._presented_at,
            "last_decision_at": self._last_decision_at,
            "last_interaction_at": self._last_interaction_at,
            "occlusion_visible": self._occlusion_visible(),
            "frame": frame,
            "screen": screen_name,
        }

    @objc.python_method
    def _log_surface(self, event):
        """One stderr line + a review-state.json refresh per surface event
        (presented / moved / occlusion_changed / extended / decision / healed).
        This is the positive confirmation layer: silence in the log used to be
        ambiguous between "being reviewed" and "invisible for hours"."""
        s = self.status_dict()
        _log(
            f"{event}: {s['pending']} pending of {s['total']}, "
            f"frame={s['frame']} screen={s['screen']} "
            f"visible={s['occlusion_visible']}"
        )
        _write_review_state(controller=self, last_event=event)

    def windowDidMove_(self, notification):
        # Timestamped move history. The unanswerable question of the 2026-07-02
        # incident (how did the card end up at the bottom edge of a side
        # display) becomes greppable. Drags fire this repeatedly; throttle.
        now = time.time()
        if now - self._last_move_log < 1.0:
            return
        self._last_move_log = now
        self._log_surface("moved")

    def windowDidChangeOcclusionState_(self, notification):
        self._log_surface("occlusion_changed")

    @objc.python_method
    def _build(self):
        frame = _corner_frame(_mouse_screen())
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskUtilityWindow
        )
        panel = _ReviewPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(False)  # let the reply field be edited
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setDelegate_(self)
        self._panel = panel
        self._render()
        panel.makeKeyAndOrderFront_(None)
        panel.orderFrontRegardless()
        self._log_surface("presented")
        self.focusReply_(None)
        # App activation/key-window promotion lands on the run loop; do one
        # deferred pass so the first card is editable even when opened from the
        # status-bar timer while another app is frontmost.
        self.performSelector_withObject_afterDelay_("focusReply:", None, 0.05)

    def focusReply_(self, sender):
        panel = self._panel
        tv = self._textview
        if panel is None or tv is None:
            return
        # A launchd/rumps status item can show a window while remaining a
        # non-activating process. Accessory policy keeps it out of the Dock but
        # lets the review panel become the key/main recipient for NSTextView input.
        try:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        except Exception:
            pass
        try:
            NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            pass
        try:
            panel.makeKeyAndOrderFront_(None)
            panel.orderFrontRegardless()
            panel.makeMainWindow()
        except Exception:
            pass
        try:
            tv.setEditable_(True)
            tv.setSelectable_(True)
            panel.makeFirstResponder_(tv)
        except Exception:
            pass

    @objc.python_method
    def _render(self):
        d = self._drafts[self._idx]
        # Fresh card (not a card <-> reason-picker swap): reset telemetry.
        if self._rendered_idx != self._idx:
            self._rendered_idx = self._idx
            self._interactions = []
            self._card_shown_at = time.time()
        self._reason_field = None
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))

        # Buttons at the TOP, one line: Approve, 😄 (approve + loved), then a
        # small 💬 (overall feedback, decides nothing), Reject at the right.
        approve = NSButton.alloc().initWithFrame_(NSMakeRect(M, H - 42, 96, 30))
        approve.setTitle_("Approve")
        approve.setBezelStyle_(NSBezelStyleRounded)
        approve.setTarget_(self)
        approve.setAction_("approve:")
        content.addSubview_(approve)

        # 😄 = approve with the "really good one" signal (loved=True on the
        # decision). Same posting path as Approve; only the feedback rail sees
        # the difference.
        smile = NSButton.alloc().initWithFrame_(NSMakeRect(M + 100, H - 42, 44, 30))
        smile.setTitle_("😄")
        smile.setBezelStyle_(NSBezelStyleRounded)
        smile.setTarget_(self)
        smile.setAction_("approveLoved:")
        try:
            smile.setToolTip_("Approve and mark as a really good pick")
        except Exception:
            pass
        content.addSubview_(smile)

        # 💬 = overall feedback (about the pipeline, not this draft). Opens the
        # composer panel next to the card; the card itself stays undecided.
        fb = NSButton.alloc().initWithFrame_(NSMakeRect(M + 184, H - 41, 30, 28))
        fb.setTitle_("💬")
        fb.setBordered_(False)
        fb.setTarget_(self)
        fb.setAction_("feedbackOpen:")
        try:
            fb.setToolTip_("Send overall feedback (not about this draft)")
        except Exception:
            pass
        content.addSubview_(fb)

        reject = NSButton.alloc().initWithFrame_(NSMakeRect(W - M - 96, H - 42, 96, 30))
        reject.setTitle_("Reject")
        reject.setBezelStyle_(NSBezelStyleRounded)
        reject.setTarget_(self)
        reject.setAction_("reject:")
        content.addSubview_(reject)

        # "Replying to @author" row: the handle is a live link to the author's
        # profile, with the follower count muted inline right after it. Right
        # side: thread age (muted, minutes) and an eye icon whose hover/click
        # popover carries the thread's engagement counts — those are never
        # inline on the card. All from data the pipeline already carries; no
        # scraping happens here.
        self._link_targets = {}
        handle = (d.get("thread_author") or "").lstrip("@").strip()
        stats = d.get("stats") or {}
        thread_url = d.get("thread_url")
        content.addSubview_(
            _label(NSMakeRect(M, H - 70, 78, 18), "Replying to", size=12, bold=True)
        )
        right_x = W - M
        self._close_stats_popover()
        self._eye_btn = None
        if _engagement_line(stats):
            eye = NSButton.alloc().initWithFrame_(NSMakeRect(right_x - 20, H - 70, 20, 18))
            eye.setBordered_(False)
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "eye", "thread stats"
            )
            if img is not None:
                eye.setImage_(img)
                eye.setTitle_("")
            else:  # pre-Big Sur fallback: no SF Symbols
                eye.setTitle_("👁")
            # Stats surface in an NSPopover on hover OR click. A plain toolTip
            # was tried first and never fired: this panel belongs to a
            # non-activating accessory (status bar) app, where AppKit's tooltip
            # machinery is unreliable. The tracking area drives hover; the
            # button action covers click and any hover-tracking edge case.
            eye.setTarget_(self)
            eye.setAction_("statsToggle:")
            eye.addTrackingArea_(
                NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                    eye.bounds(),
                    NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways,
                    self,
                    None,
                )
            )
            content.addSubview_(eye)
            self._eye_btn = eye
            right_x -= 24
        age = _age_str(stats.get("tweet_posted_at"))
        if age:
            age_w = int(
                NSAttributedString.alloc().initWithString_attributes_(
                    age, {NSFontAttributeName: NSFont.systemFontOfSize_(11)}
                ).size().width
            ) + 8
            age_label = _label(
                NSMakeRect(right_x - age_w, H - 70, age_w, 18), age, size=11, muted=True
            )
            age_label.setAlignment_(NSTextAlignmentRight)
            content.addSubview_(age_label)
            right_x -= age_w + 4
        handle_w = right_x - (M + 78) - 4
        if handle:
            # Size the link to its text so the follower count can sit right
            # after the handle instead of at a fixed column.
            text = f"@{handle}"
            measured = NSAttributedString.alloc().initWithString_attributes_(
                text, {NSFontAttributeName: NSFont.boldSystemFontOfSize_(12)}
            ).size().width
            link_w = min(int(measured) + 8, handle_w)
            self._add_link(
                content,
                NSMakeRect(M + 78, H - 70, link_w, 18),
                text,
                f"https://x.com/{handle}",
                bold=True,
                kind="profile_click",
            )
            followers = _followers_str(stats)
            fol_w = handle_w - link_w
            if followers and fol_w > 20:
                content.addSubview_(
                    _label(
                        NSMakeRect(M + 78 + link_w, H - 70, fol_w, 18),
                        f"· {followers}",
                        size=11,
                        muted=True,
                    )
                )
        else:
            content.addSubview_(
                _label(NSMakeRect(M + 78, H - 70, handle_w, 18), "thread", size=12, bold=True)
            )
        # Thread text — black, with a small trailing ↗ link that opens the
        # thread (an NSTextView because NSTextField can't do clickable links).
        thread_tv = NSTextView.alloc().initWithFrame_(
            NSMakeRect(M, H - 150, W - 2 * M, 74)
        )
        thread_tv.setEditable_(False)
        thread_tv.setSelectable_(True)  # links only respond when selectable
        thread_tv.setDrawsBackground_(False)
        # An NSTextView grows vertically by default; long threads inflated the
        # frame over the author row above (non-flipped superview: growth goes
        # UP) and pushed the trailing ↗ out of the box. Pin the frame and
        # truncate to what 4 lines actually fit so the arrow stays visible.
        thread_tv.setVerticallyResizable_(False)
        thread_tv.setHorizontallyResizable_(False)
        body = NSMutableAttributedString.alloc().initWithString_attributes_(
            _truncate(d.get("thread_text"), 200),
            {
                NSFontAttributeName: NSFont.systemFontOfSize_(12),
                NSForegroundColorAttributeName: NSColor.labelColor(),
            },
        )
        if thread_url:
            body.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    " ↗",
                    {
                        NSFontAttributeName: NSFont.systemFontOfSize_(12),
                        # Delegate (textView:clickedOnLink:atIndex:) tracks the
                        # click as a thread_click interaction, then opens the
                        # URL itself via NSWorkspace.
                        NSLinkAttributeName: NSURL.URLWithString_(thread_url),
                    },
                )
            )
            thread_tv.setDelegate_(self)
        thread_tv.textStorage().setAttributedString_(body)
        content.addSubview_(thread_tv)
        # Reply heading — black.
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 172, W - 2 * M, 16),
                "Reply (editable):",
                size=12,
                bold=True,
            )
        )

        # Editable reply, with the attached link folded in as it'll post (no
        # separate link field). The trailing link is stripped on send so the
        # pipeline still mints the tracked /r/<code> short link (no double link).
        reply = d.get("reply_text") or ""
        link = d.get("link_url")
        composed = f"{reply} {link}" if link else reply
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(M, M, W - 2 * M, H - 172 - M - 6)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NS_BEZEL_BORDER)
        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 2 * M, 100))
        tv.setFont_(NSFont.systemFontOfSize_(12))
        tv.setRichText_(False)
        tv.setEditable_(True)
        tv.setSelectable_(True)
        tv.setString_(composed)
        scroll.setDocumentView_(tv)
        content.addSubview_(scroll)
        self._textview = tv

        self._panel.setContentView_(content)
        # Counter lives in the native title bar, not inside the content.
        self._panel.setTitle_(f"Review draft {self._idx + 1} of {len(self._drafts)}")
        # setContentView_ rebuilds the view tree, so the caret would otherwise
        # default to the Approve button. Re-seat it in the reply field for every
        # card (not just the first) so each one is immediately editable.
        self._panel.makeFirstResponder_(tv)
        self.performSelector_withObject_afterDelay_("focusReply:", None, 0.05)

    @objc.python_method
    def _close_stats_popover(self):
        try:
            if self._stats_popover is not None and self._stats_popover.isShown():
                self._stats_popover.close()
                _log("stats popover closed")
        except Exception:
            pass
        self._stats_popover = None

    @objc.python_method
    def _show_stats_popover(self):
        if self._eye_btn is None:
            return
        if self._stats_popover is not None and self._stats_popover.isShown():
            return
        line = _engagement_line(self._drafts[self._idx].get("stats"))
        if not line:
            return
        font = NSFont.systemFontOfSize_(12)
        s = NSAttributedString.alloc().initWithString_attributes_(
            line, {NSFontAttributeName: font}
        )
        # +34: 13px side insets plus NSTextField's own ~4px internal padding,
        # which otherwise clips the last word.
        pw, ph = int(s.size().width) + 34, 34
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, pw, ph))
        view.addSubview_(_label(NSMakeRect(13, ph - 26, pw - 26, 18), line, size=12))
        vc = NSViewController.alloc().init()
        vc.setView_(view)
        pop = NSPopover.alloc().init()
        # ApplicationDefined, NOT Transient: a transient popover auto-closes
        # whenever the owning app is inactive, and this accessory (status bar)
        # app usually is — on the box the popover opened and dismissed within
        # the same click. We own every close path instead (hover-out, click
        # toggle, card advance, window close).
        pop.setBehavior_(NSPopoverBehaviorApplicationDefined)
        pop.setContentViewController_(vc)
        pop.setContentSize_((pw, ph))
        try:
            NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            pass
        # Anchor to the eye's frame in the (non-flipped) content view, where
        # NSRectEdge 1 = NSMinYEdge is unambiguously the BOTTOM edge, so the
        # popover reliably opens below the icon. Anchoring to the button's own
        # bounds flips the edge meaning (NSButton is a flipped view) and the
        # popover appeared above the card's title bar.
        pop.showRelativeToRect_ofView_preferredEdge_(
            self._eye_btn.frame(), self._eye_btn.superview(), 1
        )
        self._stats_popover = pop
        _log(f"stats popover shown ({pw}x{ph})")

    # Click on the eye SHOWS the popover, never toggles it closed: a click is
    # physically preceded by hover (mouseEntered already opened it), so a
    # toggle would close what the hover just opened and the user sees nothing.
    # Closing is owned by hover-out, card advance, and window close.
    def statsToggle_(self, sender):
        _log("eye clicked")
        self._track("stats_open")
        self._show_stats_popover()

    # NSTrackingArea owner callbacks (hover over the eye icon).
    def mouseEntered_(self, event):
        _log("eye hover enter")
        self._show_stats_popover()

    def mouseExited_(self, event):
        _log("eye hover exit")
        self._close_stats_popover()

    @objc.python_method
    def _add_link(self, content, frame, text, url, *, size=12, bold=False, right=False, kind="link_click"):
        """Borderless button styled as a link (system link color, underlined).
        The URL and interaction kind ride on the button's integer tag via
        _link_targets so one openLink: selector serves every link on the card."""
        btn = NSButton.alloc().initWithFrame_(frame)
        btn.setBordered_(False)
        font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
        attrs = {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: NSColor.linkColor(),
            NSUnderlineStyleAttributeName: NSUnderlineStyleSingle,
        }
        btn.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        )
        btn.setAlignment_(NSTextAlignmentRight if right else NSTextAlignmentLeft)
        tag = len(self._link_targets) + 1
        btn.setTag_(tag)
        self._link_targets[tag] = (str(url), kind)
        btn.setTarget_(self)
        btn.setAction_("openLink:")
        content.addSubview_(btn)
        return btn

    def openLink_(self, sender):
        try:
            target = self._link_targets.get(sender.tag())
            if target:
                url, kind = target
                self._track(kind)
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
        except Exception:
            pass

    # NSTextView delegate: the thread ↗ link. Track, then open ourselves
    # (returning True suppresses the default handler).
    def textView_clickedOnLink_atIndex_(self, textview, link, charIndex):
        self._track("thread_click")
        try:
            url = link if hasattr(link, "absoluteString") else NSURL.URLWithString_(str(link))
            NSWorkspace.sharedWorkspace().openURL_(url)
        except Exception:
            pass
        return True

    @objc.python_method
    def _current_text(self):
        try:
            return str(self._textview.string())
        except Exception:
            return self._drafts[self._idx].get("reply_text") or ""

    @objc.python_method
    def _record(self, approved, reject_category=None, reject_note=None, loved=False):
        d = self._drafts[self._idx]
        orig = (d.get("reply_text") or "").strip()
        link = d.get("link_url") or ""
        drop_link = False
        if approved:
            text = self._current_text()
            # The card folds link_url into the editable field (see _render). On
            # send we must reconcile what the user did with that link:
            #   - link still present anywhere -> remove ALL occurrences so the
            #     poster mints the single tracked /r/<code> short link (no double
            #     link, no bare URL). Generalizes the old endswith() strip, which
            #     missed the link whenever the user typed anything after it.
            #   - link gone (user deleted it while editing) -> drop_link=True so
            #     the poster does NOT re-append it. Without this signal the post
            #     pipeline's forced TWITTER_TAIL_LINK_RATE=1.0 silently revived a
            #     link the user intentionally removed.
            if link:
                if link in text:
                    text = text.replace(link, "")
                else:
                    drop_link = True
            # Collapse only horizontal whitespace left by the removal; preserve
            # any newlines the user intended.
            body = re.sub(r"[ \t]{2,}", " ", text).strip()
        else:
            body = orig
        self._decisions.append(
            {
                "n": d["n"],
                "approved": bool(approved),
                "loved": bool(approved and loved),
                "text": body,
                "edited": bool(approved and body != orig),
                "drop_link": bool(approved and drop_link),
                "reject_category": reject_category,
                "reject_note": (reject_note or "").strip() or None,
                "interactions": list(self._interactions),
                "dwell_ms": self._dwell_ms(),
                # Ride-along candidate context (from review_drafts) so the
                # decision can be shipped to /api/v1/review-events without
                # re-reading the plan.
                "candidate_id": d.get("candidate_id"),
                "project": d.get("project"),
                "thread_url": d.get("thread_url"),
                "thread_author": d.get("thread_author"),
            }
        )
        self._last_decision_at = time.time()
        self._log_surface("decision")

    @objc.python_method
    def _advance(self):
        self._idx += 1
        if self._idx >= len(self._drafts):
            self._finish()
        else:
            self._render()

    @objc.python_method
    def extend_drafts(self, drafts):
        """Append newly-queued drafts to an OPEN card. Without this, a card built
        when N drafts were pending froze at "of N": every draft that arrived after
        the card opened was stranded behind it (the menu bar bailed while a panel
        was up). Dedups by plan index `n`, never disturbs the card on screen, and
        refreshes the title-bar counter live so the backlog is honest."""
        if self._panel is None:
            return 0
        have = {d.get("n") for d in self._drafts}
        added = [d for d in drafts if d.get("n") not in have]
        if not added:
            return 0
        self._drafts.extend(added)
        # Update only the "X of N" counter; do NOT re-render the body (that would
        # reset the caret / clobber an in-progress edit on the current card).
        try:
            self._panel.setTitle_(
                f"Review draft {self._idx + 1} of {len(self._drafts)}"
            )
        except Exception:
            pass
        self._log_surface(f"extended +{len(added)}")
        return len(added)

    @objc.python_method
    def _fire_decision(self):
        # Fire the per-card callback the instant a decision is made, so an
        # approved draft starts posting immediately instead of waiting for the
        # whole batch to be reviewed. A throwing callback must never break the
        # card flow (or the panel would wedge on the current card).
        cb = self._on_decision
        if cb is None or not self._decisions:
            return
        try:
            cb(dict(self._decisions[-1]))
        except Exception:
            pass

    # ObjC selectors (trailing underscore -> "approve:" etc.)
    def approve_(self, sender):
        self._record(True)
        self._fire_decision()
        self._advance()

    def approveLoved_(self, sender):
        _log("approved with love")
        self._record(True, loved=True)
        self._fire_decision()
        self._advance()

    def feedbackOpen_(self, sender):
        # Overall feedback is orthogonal to the card decision: the composer
        # opens alongside, the card stays as-is (including in-progress edits).
        self._track("feedback_open")
        present_feedback()

    def reject_(self, sender):
        # Two-step reject: swap the card body for the reason picker. The
        # decision is recorded when a reason chip (or Skip) is clicked. Any
        # in-progress edit of the reply is preserved for Back.
        self._pending_reply_text = self._current_text()
        self._render_reason()

    @objc.python_method
    def _render_reason(self):
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        content.addSubview_(
            _label(NSMakeRect(M, H - 46, W - 2 * M, 20), "Why reject this draft?", size=13, bold=True)
        )
        y = H - 82
        for i, (_, title) in enumerate(REJECT_REASONS):
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(M, y, W - 2 * M, 28))
            btn.setTitle_(title)
            btn.setBezelStyle_(NSBezelStyleRounded)
            btn.setTag_(i + 1)
            btn.setTarget_(self)
            btn.setAction_("rejectReason:")
            content.addSubview_(btn)
            y -= 32
        note = NSTextField.alloc().initWithFrame_(NSMakeRect(M, 56, W - 2 * M, 48))
        note.setEditable_(True)
        note.setBezeled_(True)
        note.setFont_(NSFont.systemFontOfSize_(12))
        try:
            note.setPlaceholderString_("Optional note (sent with whichever reason you pick)")
            note.cell().setWraps_(True)
        except Exception:
            pass
        content.addSubview_(note)
        self._reason_field = note

        back = NSButton.alloc().initWithFrame_(NSMakeRect(M, 14, 90, 30))
        back.setTitle_("Back")
        back.setBezelStyle_(NSBezelStyleRounded)
        back.setTarget_(self)
        back.setAction_("rejectBack:")
        content.addSubview_(back)

        skip = NSButton.alloc().initWithFrame_(NSMakeRect(W - M - 150, 14, 150, 30))
        skip.setTitle_("Reject (no reason)")
        skip.setBezelStyle_(NSBezelStyleRounded)
        skip.setTarget_(self)
        skip.setAction_("rejectSkip:")
        content.addSubview_(skip)

        self._textview = None
        self._panel.setContentView_(content)
        self._panel.makeFirstResponder_(note)

    @objc.python_method
    def _reason_note(self):
        try:
            return str(self._reason_field.stringValue()) if self._reason_field is not None else ""
        except Exception:
            return ""

    def rejectReason_(self, sender):
        try:
            category = REJECT_REASONS[int(sender.tag()) - 1][0]
        except Exception:
            category = "other"
        _log(f"reject reason: {category}")
        self._record(False, reject_category=category, reject_note=self._reason_note())
        self._fire_decision()
        self._advance()

    def rejectSkip_(self, sender):
        _log("reject reason: skipped")
        self._record(False, reject_note=self._reason_note())
        self._fire_decision()
        self._advance()

    def rejectBack_(self, sender):
        # Re-render the card and restore any reply edit made before Reject.
        pending = getattr(self, "_pending_reply_text", None)
        self._render()
        if pending is not None and self._textview is not None:
            try:
                self._textview.setString_(pending)
            except Exception:
                pass

    def windowShouldClose_(self, sender):
        # Closing the window stops review; remaining cards are left undecided
        # (not posted). Finish with whatever was decided so far.
        self._finish()
        return True

    @objc.python_method
    def _finish(self):
        global _active
        self._close_stats_popover()
        try:
            if self._panel is not None:
                self._panel.setDelegate_(None)
                self._panel.close()
        except Exception:
            pass
        self._panel = None
        cb, self._on_complete = self._on_complete, None
        if cb is not None:
            try:
                cb(list(self._decisions))
            except Exception:
                pass
        _active = None
        _log(f"closed: {len(self._decisions)} decided of {len(self._drafts)}")
        _write_review_state(last_event="closed")


def present_review(drafts, on_decision=None, on_complete=None):
    """Show the review cards (main thread only). drafts: list of
    {n, thread_author, thread_text, reply_text, link_url, thread_url?, stats?}
    where stats is the discovery-time candidate snapshot
    {author_followers, likes, retweets, replies, views, tweet_posted_at, ...}
    (optional). thread_url renders as a trailing ↗ link on the thread text;
    followers show inline next to the handle, age muted at the right, and the
    thread engagement counts live only in the eye icon's hover/click popover.
    on_decision(decision) fires the instant each card is approved/rejected (so an
    approved draft posts right away); on_complete(decisions) fires when the user
    finishes the last card or closes the window. Both run on the main thread."""
    global _active
    if not drafts:
        if on_complete is not None:
            on_complete([])
        return
    _active = _ReviewController.alloc().initWithDrafts_onDecision_onComplete_(
        drafts, on_decision, on_complete
    )


def extend_active(drafts):
    """Push newly-queued drafts into the open review card, if one is up. Returns
    the count actually added (0 if no card is open or nothing is new). Main thread
    only (called from the menu bar's rumps timer)."""
    if _active is None:
        return 0
    try:
        return _active.extend_drafts(drafts)
    except Exception:
        return 0


def active_status():
    """Live review-surface snapshot for the menu bar's unattended-review
    watchdog, or None when no card is open. Main thread only."""
    c = _active
    if c is None or c._panel is None:
        return None
    try:
        return c.status_dict()
    except Exception:
        return None


def heal_active():
    """Self-heal an unattended card: move it to the top-right of the screen the
    pointer is on and raise it, WITHOUT stealing keyboard focus (the user is
    mid-something elsewhere by definition). Main thread only. Returns True if a
    card was moved."""
    c = _active
    if c is None or c._panel is None:
        return False
    try:
        # setFrame_ takes a WINDOW frame (content + title bar), but
        # _corner_frame is a CONTENT-sized rect. Passing it directly shrank
        # the window by the title-bar height on every heal, and since content
        # anchors at the bottom, the top ~19px of the card (the button row)
        # got clipped under the title bar. Convert to a window frame first
        # and place THAT in the corner.
        panel = c._panel
        scr = _mouse_screen()
        vf = (
            scr.visibleFrame()
            if scr is not None
            else NSMakeRect(0, 0, 1440, 900)
        )
        wf = panel.frameRectForContentRect_(NSMakeRect(0, 0, W, H))
        panel.setFrame_display_(
            NSMakeRect(
                vf.origin.x + vf.size.width - wf.size.width - M,
                vf.origin.y + vf.size.height - wf.size.height - M,
                wf.size.width,
                wf.size.height,
            ),
            True,
        )
        panel.orderFrontRegardless()
        c._log_surface("healed")
        return True
    except Exception:
        return False


# ---- overall-feedback composer ----------------------------------------------
# One small floating panel with a free-text field, for guidance that is about
# the PIPELINE rather than any single draft ("less shilling", "more dev
# threads", ...). Reachable from the card's 💬 button and the menu bar's
# "Send feedback…" item; both call present_feedback(), which falls back to the
# handler the menu bar registered at boot via set_feedback_handler() (that
# handler ships a decision='feedback' review event down the same outbox rail
# as card decisions, so the digest processes it the same way).

FB_W = 380
FB_H = 200

# Default submit handler (menu bar's shipper). Module-level so the card's 💬
# button can open the composer without threading a callback through
# present_review's signature.
_feedback_handler = None
# Strong ref to the live composer, same pyobjc-GC footgun as _active.
_feedback_active = None


def set_feedback_handler(cb):
    """Register the default on_submit for present_feedback(). The menu bar
    calls this once at boot with its review-event shipper."""
    global _feedback_handler
    _feedback_handler = cb


def _feedback_frame():
    """Just below the open review card when there is one (so it never covers
    the card being reviewed), else the top-right corner of the pointer's
    screen."""
    scr = _mouse_screen()
    vf = (
        scr.visibleFrame()
        if scr is not None
        else NSMakeRect(0, 0, 1440, 900)
    )
    x = vf.origin.x + vf.size.width - FB_W - M
    y = vf.origin.y + vf.size.height - FB_H - M
    if _active is not None and _active._panel is not None:
        try:
            fr = _active._panel.frame()
            x = fr.origin.x
            y = max(fr.origin.y - FB_H - 10, vf.origin.y + M)
        except Exception:
            pass
    return NSMakeRect(x, y, FB_W, FB_H)


class _FeedbackController(NSObject):
    def initWithOnSubmit_(self, on_submit):
        self = objc.super(_FeedbackController, self).init()
        if self is None:
            return None
        self._on_submit = on_submit
        self._panel = None
        self._tv = None
        self._build()
        return self

    @objc.python_method
    def _build(self):
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskUtilityWindow
        )
        panel = _ReviewPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            _feedback_frame(), style, NSBackingStoreBuffered, False
        )
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(False)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setDelegate_(self)
        panel.setTitle_("Overall feedback")

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, FB_W, FB_H))
        content.addSubview_(
            _label(
                NSMakeRect(M, FB_H - 48, FB_W - 2 * M, 32),
                "Standing guidance for the drafting loop (thread choice, tone, "
                "audience), not about any single draft.",
                size=11,
                muted=True,
            )
        )
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(M, 54, FB_W - 2 * M, FB_H - 48 - 8 - 54)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NS_BEZEL_BORDER)
        tv = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, FB_W - 2 * M, 80)
        )
        tv.setFont_(NSFont.systemFontOfSize_(12))
        tv.setRichText_(False)
        tv.setEditable_(True)
        tv.setSelectable_(True)
        scroll.setDocumentView_(tv)
        content.addSubview_(scroll)
        self._tv = tv

        cancel = NSButton.alloc().initWithFrame_(NSMakeRect(M, 14, 90, 30))
        cancel.setTitle_("Cancel")
        cancel.setBezelStyle_(NSBezelStyleRounded)
        cancel.setTarget_(self)
        cancel.setAction_("feedbackCancel:")
        content.addSubview_(cancel)

        send = NSButton.alloc().initWithFrame_(NSMakeRect(FB_W - M - 90, 14, 90, 30))
        send.setTitle_("Send")
        send.setBezelStyle_(NSBezelStyleRounded)
        send.setTarget_(self)
        send.setAction_("feedbackSend:")
        content.addSubview_(send)

        panel.setContentView_(content)
        self._panel = panel
        panel.makeKeyAndOrderFront_(None)
        panel.orderFrontRegardless()
        try:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            pass
        panel.makeFirstResponder_(tv)
        _log("feedback composer opened")

    @objc.python_method
    def _close(self):
        global _feedback_active
        try:
            if self._panel is not None:
                self._panel.setDelegate_(None)
                self._panel.close()
        except Exception:
            pass
        self._panel = None
        _feedback_active = None

    def feedbackSend_(self, sender):
        text = ""
        try:
            text = str(self._tv.string()).strip()
        except Exception:
            pass
        cb = self._on_submit
        self._close()
        if not text:
            _log("feedback composer sent empty; dropped")
            return
        _log(f"feedback submitted ({len(text)} chars)")
        if cb is not None:
            try:
                cb(text)
            except Exception:
                pass

    def feedbackCancel_(self, sender):
        _log("feedback composer cancelled")
        self._close()

    def windowShouldClose_(self, sender):
        self._close()
        return True


def present_feedback(on_submit=None):
    """Open (or raise) the overall-feedback composer. Main thread only.
    on_submit(text) fires only when the user sends non-empty text; defaults to
    the handler registered via set_feedback_handler()."""
    global _feedback_active
    cb = on_submit if on_submit is not None else _feedback_handler
    if _feedback_active is not None and _feedback_active._panel is not None:
        try:
            _feedback_active._panel.makeKeyAndOrderFront_(None)
            _feedback_active._panel.orderFrontRegardless()
            return
        except Exception:
            pass
    _feedback_active = _FeedbackController.alloc().initWithOnSubmit_(cb)
