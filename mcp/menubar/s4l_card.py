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

Approving is ONE horizontal row of outlined emoji buttons (👍 😄 ❤️‍🔥 after
an "Approve" label): a single click on an emoji approves at that strength
and advances immediately. Two side-by-side worded approve buttons were
tried first and read as clutter; click-again-to-escalate was tried next and
its commit delay made every plain approve feel laggy while the escalation
stayed undiscoverable; a segmented control read as one squat multi-line
button; borderless inline emoji read as decoration (2026-07-03/04
feedback). Any emoji past 👍 stamps
loved=True: the user's "this one was a really good pick" signal, which the
feedback digest treats as strong positive evidence (a plain approve is
merely counter-evidence against avoid entries); the exact level rides along
as an approve_level_N interaction.

Overall feedback (guidance not tied to any single thread) lives in the MENU
BAR only (the card had its own Feedback button + in-window composer, but it
crowded the action row and was orthogonal to the per-draft decision, so it
moved out, 2026-07-03 feedback): the menu bar's feedback item opens the
standalone `present_feedback(on_submit)` panel, whose submit handler the
menu bar registers at boot via `set_feedback_handler` (it ships
decision='feedback' review events).

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
    NSMakeSize,
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
    NSViewWidthSizable,
)

# Styling extras that may be missing on older AppKit; every consumer degrades
# to the stock look when these are None (same posture as the SF Symbols
# fallback in _eye_button).
try:
    from AppKit import NSFontWeightSemibold
except Exception:
    NSFontWeightSemibold = None
try:
    from AppKit import NSVisualEffectView
except Exception:
    NSVisualEffectView = None
try:
    # Registers the CGColor bridge type; without it every NSColor.CGColor()
    # call in _round_rect logs an ObjCPointerWarning to menubar.err.log.
    import Quartz  # noqa: F401
except Exception:
    pass

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

# Inline approve row: emoji + tooltip per approval level (button tag =
# level). Level 1 = plain approve; 2+ = loved=True on the decision, with the
# exact level shipped as an approve_level_N interaction for the feedback
# digest.
APPROVE_EMOJIS = (
    ("👍", "Approve"),
    ("😄", "Approve, really good pick"),
    ("❤️‍🔥", "Approve, best of the best"),
)

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


# Human names for the ISO 639-1 codes the drafting model actually emits, so the
# card can say "posts in Japanese" instead of "posts in ja". Unknown codes fall
# back to the bare code; never raises.
_LANG_NAMES = {
    "ar": "Arabic", "de": "German", "es": "Spanish", "fr": "French",
    "hi": "Hindi", "id": "Indonesian", "it": "Italian", "ja": "Japanese",
    "ko": "Korean", "nl": "Dutch", "pl": "Polish", "pt": "Portuguese",
    "ru": "Russian", "th": "Thai", "tr": "Turkish", "uk": "Ukrainian",
    "vi": "Vietnamese", "zh": "Chinese",
}


def _lang_name(code):
    c = (code or "").strip().lower()
    return _LANG_NAMES.get(c, c)


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


def _reply_heading_suffix(d):
    """Concise 'project/lane · viral N' readout appended straight onto the
    'Reply (editable):' heading (2026-07-07), so the two facts a reviewer
    checks first don't require opening the details eye. Pulled from the same
    fields _details_lines used to render as 'Project:'/'Lane:'/'Virality
    score:' rows; those three are omitted from the popover now to avoid
    showing the same numbers twice. Empty string when neither is known."""
    d = d or {}
    project = (d.get("project") or "").strip()
    lane = ((d.get("experiments") or {}).get("lane") or "").strip()
    bits = []
    if project and lane:
        bits.append(f"{project}/{lane}")
    elif project:
        bits.append(project)
    elif lane:
        bits.append(lane)
    v = (d.get("stats") or {}).get("virality_score")
    if v is not None:
        try:
            bits.append(f"viral {float(v):g}")
        except (TypeError, ValueError):
            pass
    return " · ".join(bits)


def _details_lines(d):
    """Drafting metadata for the reply row's eye popover: how this draft came
    to be (engagement style, discovery query, link choice). Project, lane,
    and virality score live inline on the heading itself (_reply_heading_suffix)
    and are deliberately NOT repeated here. Everything else comes from fields
    the plan candidate already carries; fields the pipeline didn't stamp are
    omitted, and an empty list skips the icon."""
    d = d or {}
    lines = []
    style = (d.get("engagement_style") or "").strip()
    if style:
        lines.append(f"Style: {style}")
    desc = (d.get("style_description") or "").strip()
    if desc:
        # Present only when the model INVENTED this style on the fly (the
        # new_style registration payload); registry styles carry no description
        # on the plan.
        lines.append(f"New style: {desc}")
    # Experiment/scenario arms active when this draft was written, stamped by
    # merge_review_queue.py from scripts/active_experiments.py. Rendered
    # generically so every future experiment surfaces here with no card change.
    # 'lane' reads as a scenario, not an A/B arm, so it gets its own label.
    # Each arm carries its meaning from the DESCRIPTIONS registry in
    # active_experiments.py (scripts/ is on sys.path via s4l_state's
    # S4L_REPO_DIR insertion); the reviewer needs what the variant DOES, not
    # just its name. Unknown arms fall back to the bare name.
    exps = d.get("experiments") or {}
    if exps:
        try:
            from active_experiments import describe as _exp_describe
        except Exception:
            def _exp_describe(_name, _variant):
                return None
    for name in sorted(exps):
        if name == "lane":
            # Already surfaced inline on the heading via _reply_heading_suffix.
            continue
        label = f"Experiment {str(name).replace('_', ' ')}"
        line = f"{label}: {exps[name]}"
        exp_desc = _exp_describe(name, exps[name])
        if exp_desc:
            line += f" ({exp_desc})"
        lines.append(line)
    topic = (d.get("search_topic") or "").strip()
    if topic:
        lines.append(f"Found via search: {topic}")
    lang = (d.get("language") or "").strip()
    if lang and lang.lower() != "en":
        lines.append(
            f"Language: {_lang_name(lang)} ({lang.lower()}), "
            f"reply will POST in {_lang_name(lang)}"
        )
        # When the card body shows English translations (stamped at draft
        # time), the popover carries the originals + the full English draft so
        # nothing is lost to the inline truncation.
        if (d.get("thread_text_en") or "").strip() and (d.get("thread_text") or "").strip():
            lines.append(
                f"Original thread ({lang.lower()}): {_truncate(d.get('thread_text'), 280)}"
            )
        if (d.get("reply_text_en") or "").strip():
            lines.append(
                f"Draft in English: {_truncate(d.get('reply_text_en'), 280)}"
            )
    if d.get("link_url"):
        kw = (d.get("link_keyword") or "").strip()
        lines.append(f"Link: {kw}" if kw else f"Link: {d['link_url']}")
    elif (d.get("link_source") or "").strip():
        lines.append(f"Link: none ({d['link_source'].strip().replace('_', ' ')})")
    return lines


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


# ---- contemporary styling helpers --------------------------------------------
# Style-only layer (2026-07-07): frames, sizes, and control positions are
# untouched; these helpers change nothing but the skin. The card reads as a
# modern frosted macOS panel (vibrancy background, quiet semibold section
# labels, a quote-style fill behind the thread text, a rounded-rect reply
# editor, destructive-red reject) instead of the stock square-bezel utility
# look. Every hook degrades to the stock look on AppKit versions that lack
# the API, so none of these try/excepts may be "simplified" away.


def _font(size, bold=False):
    """Semibold (not heavy bold) for emphasis, matching modern macOS forms.
    Used by BOTH _label and _add_link so link-width measurement in _render
    stays in sync with the rendered title font."""
    if bold and NSFontWeightSemibold is not None:
        try:
            return NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold)
        except Exception:
            pass
    return NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)


def _frosted(content):
    """Wrap a content view in a behind-window vibrancy underlay (the frosted
    look of Notification Center widgets). Same frame as the content, so
    nothing moves. Bare view when NSVisualEffectView is unavailable."""
    if NSVisualEffectView is None:
        return content
    try:
        v = NSVisualEffectView.alloc().initWithFrame_(content.frame())
        v.setMaterial_(6)  # NSVisualEffectMaterialPopover: adaptive light/dark
        v.setBlendingMode_(0)  # behind-window
        v.setState_(1)  # active even while this accessory app is inactive
        v.addSubview_(content)
        return v
    except Exception:
        return content


def _fill_color():
    """Subtle adaptive fill for the thread quote block."""
    try:
        return NSColor.quaternarySystemFillColor()
    except Exception:
        return NSColor.labelColor().colorWithAlphaComponent_(0.06)


def _round_rect(view, *, border=True):
    """Rounded-rect skin: 8px corners, optional hairline border. Returns True
    on success so callers can restore their square-bezel fallback when the
    layer API is unavailable. Border color is resolved to a CGColor at render
    time; a mid-card system appearance flip keeps the stale shade until the
    next card renders, which is acceptable for a short-lived panel."""
    try:
        view.setWantsLayer_(True)
        layer = view.layer()
        layer.setCornerRadius_(8.0)
        layer.setMasksToBounds_(True)
        if border:
            layer.setBorderWidth_(1.0)
            layer.setBorderColor_(NSColor.separatorColor().CGColor())
        return True
    except Exception:
        return False


def _label(frame, text, *, size=12, bold=False, muted=False):
    f = NSTextField.alloc().initWithFrame_(frame)
    f.setStringValue_(text or "")
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setFont_(_font(size, bold))
    if muted:
        f.setTextColor_(NSColor.secondaryLabelColor())
    f.setLineBreakMode_(NSLineBreakByWordWrapping)
    try:
        f.cell().setWraps_(True)
        f.cell().setScrollable_(False)
    except Exception:
        pass
    return f


def _editable_scroll(frame, text=""):
    """Rounded-rect scrollable text editor (hairline border, solid text
    background over the frosted panel; falls back to the old square bezel when
    layers are unavailable). The document view must be sized to the scroll
    view's contentSize (NOT the outer frame) and track its width; sized to the
    outer frame the text runs underneath the scroller. The scroller itself
    auto-hides so it only appears when the text overflows.
    Returns (scroll, textview)."""
    scroll = NSScrollView.alloc().initWithFrame_(frame)
    scroll.setHasVerticalScroller_(True)
    scroll.setAutohidesScrollers_(True)
    if _round_rect(scroll):
        scroll.setBorderType_(0)  # NSNoBorder; the layer draws the outline
        try:
            scroll.setDrawsBackground_(True)
            scroll.setBackgroundColor_(NSColor.textBackgroundColor())
        except Exception:
            pass
    else:
        scroll.setBorderType_(NS_BEZEL_BORDER)
    cs = scroll.contentSize()
    tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, cs.width, cs.height))
    tv.setFont_(NSFont.systemFontOfSize_(12))
    tv.setRichText_(False)
    tv.setEditable_(True)
    tv.setSelectable_(True)
    tv.setVerticallyResizable_(True)
    tv.setHorizontallyResizable_(False)
    tv.setMinSize_(NSMakeSize(0, cs.height))
    tv.setMaxSize_(NSMakeSize(1e7, 1e7))
    tv.setAutoresizingMask_(NSViewWidthSizable)
    tv.textContainer().setWidthTracksTextView_(True)
    tv.textContainer().setContainerSize_(NSMakeSize(cs.width, 1e7))
    try:
        # Breathing room inside the rounded rect; text no longer hugs the border.
        tv.setTextContainerInset_(NSMakeSize(4, 6))
    except Exception:
        pass
    if text:
        tv.setString_(text)
    scroll.setDocumentView_(tv)
    return scroll, tv


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
        self._details_btn = None
        self._stats_popover = None
        # Per-card telemetry, reset when a NEW card renders (not on the
        # card <-> reason-picker swap, which is the same card).
        self._rendered_idx = -1
        self._interactions = []
        self._card_shown_at = None
        self._reason_field = None
        # Two-draft cards: which slot (0=a, 1=b) is currently showing in the
        # editable field. None = not yet chosen this card, _render() defaults
        # it to the candidate's recommended_draft_index. Reset to None on
        # every NEW card (see _render()'s "fresh card" branch below), so a
        # switch made on one card never bleeds into the next.
        self._selected_draft = None
        self._draft_toggle_btns = {}
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
    def _eye_button(self, frame, kind):
        """Borderless SF-Symbol eye whose hover/click opens a popover. A plain
        toolTip was tried first and never fired: this panel belongs to a
        non-activating accessory (status bar) app, where AppKit's tooltip
        machinery is unreliable. The tracking area drives hover; the button
        action covers click and any hover-tracking edge case. kind ('stats' |
        'details') rides on the tracking area's userInfo so the shared
        mouseEntered_ owner can route to the right popover."""
        eye = NSButton.alloc().initWithFrame_(frame)
        eye.setBordered_(False)
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "eye", "thread stats" if kind == "stats" else "draft details"
        )
        if img is not None:
            eye.setImage_(img)
            eye.setTitle_("")
        else:  # pre-Big Sur fallback: no SF Symbols
            eye.setTitle_("👁")
        eye.setTarget_(self)
        eye.setAction_("statsToggle:" if kind == "stats" else "detailsToggle:")
        eye.addTrackingArea_(
            NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                eye.bounds(),
                NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways,
                self,
                {"kind": kind},
            )
        )
        return eye

    @objc.python_method
    def _render(self):
        d = self._drafts[self._idx]
        # Fresh card (not a card <-> reason-picker swap): reset telemetry.
        if self._rendered_idx != self._idx:
            self._rendered_idx = self._idx
            self._interactions = []
            self._card_shown_at = time.time()
            self._selected_draft = None
        self._reason_field = None
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))

        # Buttons at the TOP, one line: "Approve" label then the inline emoji
        # row extending horizontally at the left (one click on an emoji =
        # approve at that strength), Reject at the right. Overall feedback
        # moved to the menu bar item.
        # Section labels render quiet (semibold + secondary color): the modern
        # macOS form look where chrome recedes and content (thread, draft)
        # carries the full-strength label color.
        content.addSubview_(
            _label(NSMakeRect(M, H - 38, 60, 20), "Approve", size=12, bold=True, muted=True)
        )
        x = M + 62
        for i, (emoji, tip) in enumerate(APPROVE_EMOJIS):
            # Bezeled, not borderless: bare emoji read as decoration and
            # users doubted the click registered (2026-07-03/04 feedback,
            # twice now — the outline is what says "button").
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, H - 42, 44, 30))
            btn.setTitle_(emoji)
            btn.setBezelStyle_(NSBezelStyleRounded)
            # 13pt = the size Reject's title renders at; anything bigger
            # overflows the rounded bezel's ~22px content lane (16pt did).
            btn.setFont_(NSFont.systemFontOfSize_(13))
            btn.setTag_(i + 1)  # tag = approval level
            btn.setTarget_(self)
            btn.setAction_("approve:")
            try:
                btn.setToolTip_(tip)
            except Exception:
                pass
            content.addSubview_(btn)
            x += 48

        reject = NSButton.alloc().initWithFrame_(NSMakeRect(W - M - 66, H - 42, 66, 30))
        reject.setTitle_("Reject")
        reject.setBezelStyle_(NSBezelStyleRounded)
        try:
            reject.setHasDestructiveAction_(True)  # red title on macOS 12+
        except Exception:
            pass
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
            _label(NSMakeRect(M, H - 70, 78, 18), "Replying to", size=12, bold=True, muted=True)
        )
        right_x = W - M
        self._close_stats_popover()
        self._eye_btn = None
        self._details_btn = None
        if _engagement_line(stats):
            # y is nudged 2px above the label row: the label's 12pt text draws
            # top-aligned in its 18px frame while the button centers its image,
            # so a same-y frame rendered the eye visibly below the text line.
            eye = self._eye_button(
                NSMakeRect(right_x - 20, H - 68, 20, 18), "stats"
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
                text, {NSFontAttributeName: _font(12, True)}
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
                _label(NSMakeRect(M + 78, H - 70, handle_w, 18), "thread", size=12, bold=True, muted=True)
            )
        # Non-English drafts: the prep step stamps display-only English
        # translations (thread_text_en / reply_text_en) on the candidate. The
        # card shows the ENGLISH text so the reviewer understands the thread
        # and the draft, while the editable field keeps the ORIGINAL-language
        # reply_text, because that exact text is what posts (the poster reads
        # reply_text verbatim; there is no re-translation at post time). The
        # originals + full English draft live in the details-eye popover.
        card_lang = (d.get("language") or "").strip().lower()
        is_foreign = bool(card_lang and card_lang != "en")
        thread_en = (d.get("thread_text_en") or "").strip() if is_foreign else ""
        reply_en = (d.get("reply_text_en") or "").strip() if is_foreign else ""
        # Thread text — black, with a small trailing ↗ link that opens the
        # thread (an NSTextView because NSTextField can't do clickable links).
        thread_tv = NSTextView.alloc().initWithFrame_(
            NSMakeRect(M, H - 150, W - 2 * M, 74)
        )
        thread_tv.setEditable_(False)
        thread_tv.setSelectable_(True)  # links only respond when selectable
        # Quote-style block (subtle rounded fill, like a quoted tweet) so the
        # thread reads as *their* content and the editor below as *ours*.
        # Same frame as the old flat text; only the skin changed.
        if _round_rect(thread_tv, border=False):
            thread_tv.setDrawsBackground_(True)
            thread_tv.setBackgroundColor_(_fill_color())
            try:
                thread_tv.setTextContainerInset_(NSMakeSize(6, 5))
            except Exception:
                pass
        else:
            thread_tv.setDrawsBackground_(False)
        # An NSTextView grows vertically by default; long threads inflated the
        # frame over the author row above (non-flipped superview: growth goes
        # UP) and pushed the trailing ↗ out of the box. Pin the frame and
        # truncate to what 4 lines actually fit so the arrow stays visible.
        thread_tv.setVerticallyResizable_(False)
        thread_tv.setHorizontallyResizable_(False)
        body = NSMutableAttributedString.alloc().initWithString_attributes_(
            _truncate(thread_en or d.get("thread_text"), 200),
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
        # Reply heading — black, with a concise "project/lane · viral N" tag
        # folded straight into the text (2026-07-07) so those two numbers
        # don't require opening the eye popover. Right-aligned on the same
        # line: an eye icon whose hover/click popover carries the remaining
        # DRAFTING metadata (style, discovery query, link choice), mirroring
        # the author row's engagement-stats eye. Skipped when the plan carries
        # none of it (older plans).
        heading = (
            f"Reply (posts in {_lang_name(card_lang)}, editable)"
            if is_foreign
            else "Reply (editable)"
        )
        heading_suffix = _reply_heading_suffix(d)
        if heading_suffix:
            heading += f" · {heading_suffix}"
        heading += ":"
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 172, W - 2 * M - 24, 16),
                heading,
                size=12,
                bold=True,
                muted=True,
            )
        )
        if _details_lines(d):
            # No y nudge needed here (unlike the author row): this label is
            # 16px, so an 18px button at the same y already centers its image
            # on the top-aligned text line.
            deye = self._eye_button(NSMakeRect(W - M - 20, H - 172, 20, 18), "details")
            content.addSubview_(deye)
            self._details_btn = deye

        # Two-draft cards (2026-07-07): a fresh candidate carries d["drafts"]
        # (2 entries) + recommended_draft_index. Render a small A/B toggle
        # between the heading and the editable field; clicking it (selectDraft_
        # below) swaps which draft's text seeds the editable field and
        # re-renders. Absent/short (reused stale draft, legacy plan) falls back
        # to the single-draft reply_text path, no toggle rendered.
        drafts = d.get("drafts")
        dual = isinstance(drafts, list) and len(drafts) == 2
        rec_idx = d.get("recommended_draft_index")
        rec_idx = rec_idx if rec_idx in (0, 1) else 0
        sel_idx = self._selected_draft if (dual and self._selected_draft in (0, 1)) else rec_idx
        if dual:
            self._selected_draft = sel_idx

        edit_top = H - 172 - 6
        self._draft_toggle_btns = {}
        if dual:
            toggle_h = 24
            toggle_y = edit_top - toggle_h
            gap = 6
            btn_w = (W - 2 * M - gap) // 2
            for slot in (0, 1):
                label = "Draft A" if slot == 0 else "Draft B"
                if slot == rec_idx:
                    label += " · recommended"
                selected = slot == sel_idx
                if selected:
                    label = "✓ " + label
                bx = M if slot == 0 else M + btn_w + gap
                btn = NSButton.alloc().initWithFrame_(NSMakeRect(bx, toggle_y, btn_w, toggle_h))
                btn.setTitle_(label)
                btn.setBezelStyle_(NSBezelStyleRounded)
                btn.setFont_(_font(11, bold=selected))
                btn.setTag_(slot)
                btn.setTarget_(self)
                btn.setAction_("selectDraft:")
                content.addSubview_(btn)
                self._draft_toggle_btns[slot] = btn
            edit_top = toggle_y - 6

        # Editable reply. Since 2026-07-06 the tail link is folded into
        # reply_text at DRAFT time (scripts/twitter_gen_links.py::apply_tail_link),
        # so it's normally already there — only append link_url when reply_text
        # is genuinely link-free (older/fallback plans), else it shows twice.
        if dual:
            chosen = drafts[sel_idx]
            reply = chosen.get("text") or ""
            reply_en = (chosen.get("text_en") or "").strip() if is_foreign else ""
        else:
            reply = d.get("reply_text") or ""
        link = d.get("link_url")
        composed = reply if (not link or link in reply) else f"{reply} {link}"
        # Non-English draft with a stamped translation: a muted read-only
        # "EN:" block sits between the heading (or toggle) and the editable
        # field so the reviewer reads the draft in English while still
        # editing (and posting) the original-language text below. Full
        # translation lives in the details popover; inline is truncated to
        # what ~3 lines fit.
        if reply_en:
            tr_h = 42
            content.addSubview_(
                _label(
                    NSMakeRect(M, edit_top - tr_h, W - 2 * M, tr_h),
                    f"EN: {_truncate(reply_en, 150)}",
                    size=11,
                    muted=True,
                )
            )
            edit_top -= tr_h + 4
        scroll, tv = _editable_scroll(
            NSMakeRect(M, M, W - 2 * M, edit_top - M), composed
        )
        content.addSubview_(scroll)
        self._textview = tv

        self._panel.setContentView_(_frosted(content))
        # Counter lives in the native title bar, not inside the content, with
        # the product name so a stray card is identifiable at a glance.
        self._panel.setTitle_(
            f"s4l · Review draft {self._idx + 1} of {len(self._drafts)}"
        )
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
    def _show_popover(self, content, anchor, what):
        """One popover surface for both eyes (thread stats, draft details).
        Only one is ever open: the two anchors are far apart, so hover-out
        closes the first before hover-in opens the other. `content` is a
        single string (stats: one compact line) or a list of strings
        (details: one row per field, gapped vertically so multi-line fields
        stay visually separated instead of running into the next one)."""
        if anchor is None or not content:
            return
        if self._stats_popover is not None and self._stats_popover.isShown():
            return
        rows = content if isinstance(content, list) else [content]
        row_gap = 8 if len(rows) > 1 else 0
        font = NSFont.systemFontOfSize_(12)
        # Wrap-aware measurement per row (option 1 = NSStringDrawingUsesLine
        # FragmentOrigin; a row can itself be multi-line, e.g. the truncated
        # original-thread text). +34 on width: 13px side insets plus
        # NSTextField's own ~4px internal padding, which otherwise clips the
        # last word. +3 on each row's height: buffer against descender
        # clipping (matches the single-line sizing this replaces).
        heights = []
        pw = 0
        for row in rows:
            s = NSAttributedString.alloc().initWithString_attributes_(
                row, {NSFontAttributeName: font}
            )
            measured = s.boundingRectWithSize_options_(NSMakeSize(300, 10_000), 1)
            heights.append(int(measured.size.height) + 3)
            pw = max(pw, int(measured.size.width))
        pw += 34
        ph = sum(heights) + row_gap * (len(rows) - 1) + 16
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, pw, ph))
        y = ph - 8
        for row, h in zip(rows, heights):
            y -= h
            view.addSubview_(_label(NSMakeRect(13, y, pw - 26, h), row, size=12))
            y -= row_gap
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
            anchor.frame(), anchor.superview(), 1
        )
        self._stats_popover = pop
        _log(f"{what} popover shown ({pw}x{ph})")

    @objc.python_method
    def _show_stats_popover(self):
        line = _engagement_line(self._drafts[self._idx].get("stats"))
        self._show_popover(line, self._eye_btn, "stats")

    @objc.python_method
    def _show_details_popover(self):
        lines = _details_lines(self._drafts[self._idx])
        self._show_popover(lines, self._details_btn, "details")

    # Click on an eye SHOWS its popover, never toggles it closed: a click is
    # physically preceded by hover (mouseEntered already opened it), so a
    # toggle would close what the hover just opened and the user sees nothing.
    # Closing is owned by hover-out, card advance, and window close.
    def statsToggle_(self, sender):
        _log("eye clicked")
        self._track("stats_open")
        self._show_stats_popover()

    def detailsToggle_(self, sender):
        _log("details eye clicked")
        self._track("details_open")
        self._show_details_popover()

    @objc.python_method
    def _hover_kind(self, event):
        """Which eye a tracking-area event belongs to ('stats' | 'details'),
        from the userInfo stamped in _eye_button. Defaults to stats (the
        original single-eye behavior) if the area carries no info."""
        try:
            info = event.trackingArea().userInfo()
            if info and info.get("kind") == "details":
                return "details"
        except Exception:
            pass
        return "stats"

    # NSTrackingArea owner callbacks (hover over either eye icon).
    def mouseEntered_(self, event):
        kind = self._hover_kind(event)
        _log(f"{kind} eye hover enter")
        if kind == "details":
            self._show_details_popover()
        else:
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
        font = _font(size, bold)
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
            d = self._drafts[self._idx]
            drafts = d.get("drafts")
            if isinstance(drafts, list) and len(drafts) == 2 and self._selected_draft in (0, 1):
                return drafts[self._selected_draft].get("text") or ""
            return d.get("reply_text") or ""

    @objc.python_method
    def _record(self, approved, reject_category=None, reject_note=None, loved=False):
        d = self._drafts[self._idx]
        # Two-draft cards: `orig` (what counts as "unedited") is whichever
        # draft is CURRENTLY SELECTED, not always the plan's default
        # reply_text. Otherwise picking the non-recommended draft without
        # further hand-editing would show up as `edited=True` with the
        # recommended draft as `original_text`, which would look exactly
        # like a human rewrite to the edit-learning digest and pollute it.
        drafts = d.get("drafts")
        dual = isinstance(drafts, list) and len(drafts) == 2
        sel_idx = self._selected_draft if (dual and self._selected_draft in (0, 1)) else None
        if sel_idx is not None:
            chosen_draft = drafts[sel_idx]
            orig = (chosen_draft.get("text") or "").strip()
            draft_variant = chosen_draft.get("variant") or ("a" if sel_idx == 0 else "b")
        else:
            orig = (d.get("reply_text") or "").strip()
            draft_variant = None
        link = d.get("link_url") or ""
        drop_link = False
        if approved:
            text = self._current_text()
            # reply_text already carries the link embedded at draft time (see
            # _render) — post it as-is, don't strip it back out. tail_link_variant
            # is stamped before the card is ever shown, so the post pipeline now
            # trusts reply_text verbatim and never re-adds a missing link; the old
            # strip-then-hope-the-poster-revives-it logic predates that move and
            # was mislabeling every approval as "edited" (body != orig once the
            # link was stripped) and occasionally shipping a tweet with no link
            # at all. Only signal drop_link when the user actually deleted the
            # link while editing, so the poster clears link_url instead of
            # reviving it.
            if link and link not in text:
                drop_link = True
            # Collapse only horizontal whitespace runs; preserve intended newlines.
            body = re.sub(r"[ \t]{2,}", " ", text).strip()
        else:
            body = orig
        rec_idx = d.get("recommended_draft_index")
        rec_idx = rec_idx if rec_idx in (0, 1) else 0
        self._decisions.append(
            {
                "n": d["n"],
                "approved": bool(approved),
                "loved": bool(approved and loved),
                "text": body,
                "edited": bool(approved and body != orig),
                # Pre-edit draft, shipped only when the user actually rewrote
                # it, so the feedback digest can diff original vs final and
                # learn the edit patterns (not just THAT an edit happened).
                "original_text": orig if (approved and body != orig) else None,
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
                # Draft language (ISO 639-1) so the feedback digest can scope
                # edit/reject patterns per language. draft_text/original_text
                # stay in the ORIGINAL language (that is what posts).
                "language": d.get("language"),
                # Two-draft cards: which draft ("a"|"b") was showing when this
                # decision was made, its 0/1 index, and whether that matches
                # the model's own recommendation. None/False on single-draft
                # candidates. Kept separate from `edited` above by design.
                "draft_variant": draft_variant,
                "draft_index": sel_idx,
                "draft_auto_selected": bool(dual and sel_idx is not None and sel_idx == rec_idx),
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
                f"s4l · Review draft {self._idx + 1} of {len(self._drafts)}"
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
    def selectDraft_(self, sender):
        # Two-draft cards: switch which draft (0=a, 1=b) seeds the editable
        # field. Discards any in-progress hand-edit of the previously-shown
        # draft (the toggle is a "look at the other one" action, not a merge)
        # and re-renders the card fresh from the newly-selected draft's text.
        try:
            slot = int(sender.tag())
        except Exception:
            return
        if slot not in (0, 1) or slot == self._selected_draft:
            return
        self._selected_draft = slot
        self._track(f"draft_select_{'a' if slot == 0 else 'b'}")
        self._render()

    def approve_(self, sender):
        # One emoji row, one click: the clicked emoji's tag IS the approval
        # level. Commits and advances immediately; level 2+ is the loved
        # signal, with the exact strength riding along as an approve_level_N
        # interaction.
        try:
            level = int(sender.tag())
        except Exception:
            level = 1
        level = max(1, min(level, len(APPROVE_EMOJIS)))
        _log(f"approved at level {level}")
        if level > 1:
            self._track(f"approve_level_{level}")
        self._record(True, loved=level > 1)
        self._fire_decision()
        self._advance()

    def reject_(self, sender):
        # Two-step reject: swap the card body for the reason picker. The
        # decision is recorded when a reason chip (or the no-reason button)
        # is clicked. Any in-progress edit of the reply is preserved for
        # Back.
        self._pending_reply_text = self._current_text()
        self._render_reason()

    @objc.python_method
    def _render_reason(self):
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))

        # Buttons at the TOP, above the title, mirroring the card's action
        # row so the cursor doesn't have to travel between steps: "Reject,
        # no reason" sits right-aligned exactly where Reject was (one-click
        # double-tap keeps the old zero-friction reject), Back sits at the
        # left where Approve was.
        back = NSButton.alloc().initWithFrame_(NSMakeRect(M, H - 42, 90, 30))
        back.setTitle_("Back")
        back.setBezelStyle_(NSBezelStyleRounded)
        back.setTarget_(self)
        back.setAction_("rejectBack:")
        content.addSubview_(back)

        skip = NSButton.alloc().initWithFrame_(NSMakeRect(W - M - 150, H - 42, 150, 30))
        skip.setTitle_("Reject, no reason")
        skip.setBezelStyle_(NSBezelStyleRounded)
        try:
            skip.setHasDestructiveAction_(True)  # red title on macOS 12+
        except Exception:
            pass
        skip.setTarget_(self)
        skip.setAction_("rejectSkip:")
        content.addSubview_(skip)

        content.addSubview_(
            _label(NSMakeRect(M, H - 72, W - 2 * M, 20), "Why reject this draft?", size=13, bold=True)
        )
        y = H - 106
        for i, (_, title) in enumerate(REJECT_REASONS):
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(M, y, W - 2 * M, 28))
            btn.setTitle_(title)
            btn.setBezelStyle_(NSBezelStyleRounded)
            btn.setTag_(i + 1)
            btn.setTarget_(self)
            btn.setAction_("rejectReason:")
            content.addSubview_(btn)
            y -= 32
        # Optional-note field, skinned like the reply editor: a rounded-rect
        # wrapper carries the fill + hairline border while the borderless
        # field sits inset inside it so the text doesn't hug the border (an
        # unbezeled NSTextField has zero internal padding). Same outer frame
        # as the old bezeled field, which remains the fallback.
        note_frame = NSMakeRect(M, 14, W - 2 * M, y + 28 - 14 - 8)
        wrap = NSView.alloc().initWithFrame_(note_frame)
        if _round_rect(wrap):
            try:
                wrap.layer().setBackgroundColor_(
                    NSColor.textBackgroundColor().CGColor()
                )
            except Exception:
                pass
            note = NSTextField.alloc().initWithFrame_(
                NSMakeRect(
                    6, 4, note_frame.size.width - 12, note_frame.size.height - 8
                )
            )
            note.setBezeled_(False)
            note.setDrawsBackground_(False)
            try:
                # The square focus ring fights the rounded skin (renders as a
                # heavy outline); the border already marks the field.
                note.setFocusRingType_(1)  # NSFocusRingTypeNone
            except Exception:
                pass
            wrap.addSubview_(note)
            content.addSubview_(wrap)
        else:
            note = NSTextField.alloc().initWithFrame_(note_frame)
            note.setBezeled_(True)
            content.addSubview_(note)
        note.setEditable_(True)
        note.setFont_(NSFont.systemFontOfSize_(12))
        try:
            note.setPlaceholderString_("Optional note (sent with whichever reason you pick)")
            note.cell().setWraps_(True)
        except Exception:
            pass
        self._reason_field = note

        self._textview = None
        self._panel.setContentView_(_frosted(content))
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
    Non-English drafts may carry thread_text_en / reply_text_en (stamped at
    draft time): the card then shows the English thread text, a muted read-only
    "EN:" translation of the reply, and a "posts in <language>" heading, while
    the editable field keeps the original-language reply_text (what posts).
    Optional drafting metadata (project, engagement_style, style_description,
    search_topic, language, link_source, link_keyword, experiments) feeds a
    second eye on the "Reply (editable):" row whose popover explains how the
    draft was made; experiments is a generic {name: variant} dict rendered
    as-is, one line per active experiment/scenario arm.
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
# threads", ...). Reachable ONLY from the menu bar's feedback item, which
# calls present_feedback(); that falls back to the handler the menu bar
# registered at boot via set_feedback_handler() (the handler ships a
# decision='feedback' review event down the same outbox rail as card
# decisions, so the digest processes it the same way).

FB_W = 380
FB_H = 200

# Default submit handler (menu bar's shipper). Module-level so the card's
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
        scroll, tv = _editable_scroll(
            NSMakeRect(M, 54, FB_W - 2 * M, FB_H - 48 - 8 - 54)
        )
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

        panel.setContentView_(_frosted(content))
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
