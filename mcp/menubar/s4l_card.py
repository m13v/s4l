"""Corner pop-up review cards for draft approval (AppKit / pyobjc).

`present_review(drafts, on_decision, on_complete)` shows one small floating panel
per draft in the top-right corner: thread context, an EDITABLE reply field, a
counter, and Reject / Approve. `on_decision` fires the INSTANT each card is
approved/rejected (so an approved draft can post right away), and `on_complete`
fires once the last card is decided or the window is closed. The whole AppKit
surface is isolated behind that one function so the menu bar wiring doesn't
depend on the windowing details.

The panel is NONACTIVATING and auto-presented cards never take keyboard focus
(2026-07-09 customer complaint: cards stole the caret mid-typing); the reply
field becomes editable on first click, and only a user-initiated open
(present_review(focus=True), the menu bar's "Review N pending drafts")
activates the app and seats the caret immediately. A title-bar "Snooze 1h"
button sits top-left where the traffic lights would be (they're hidden: the
panel can't minimize or zoom, and the cross duplicated Snooze); it just
closes the panel, and the menu bar interprets any close with undecided
drafts (including Cmd-W) as a snooze (drafts stay pending, re-present after
REVIEW_SNOOZE_SECONDS).

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
reason picker (three one-tap categories, all optional, plus an optional
free-text note). The category feeds the feedback-digest loop that distills
human rejections into each project's learned_preferences config block; the
picker's red Reject button commits with no category (the note, if typed, is
still sent). Link clicks (author profile,
thread ↗) and per-card dwell time ride along on the decision so the digest can
infer intent (e.g. profile-checked-then-rejected = author-quality signal) even
when the reason is skipped.

Must be driven on the main thread (the menu bar's rumps timer is on the main
run loop, so that holds).
"""

import datetime
import json
import os
import re
import time

import objc
from Foundation import (
    NSObject,
    NSMakeRect,
    NSMakeRange,
    NSMakeSize,
    NSAttributedString,
    NSMutableAttributedString,
    NSURL,
    NSTimer,
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
    NSLineBreakByTruncatingTail,
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
    NSColorSpace,
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
# Nonactivating panel: an auto-presented card must never yank keyboard focus
# from whatever the user is typing into (customer complaint 2026-07-09); the
# panel becomes key only when the user clicks into one of its text fields.
# Bit value 1 << 7 per AppKit; 0 degrades to the old always-activating panel.
try:
    from AppKit import NSWindowStyleMaskNonactivatingPanel
except Exception:
    NSWindowStyleMaskNonactivatingPanel = 0
# Title-bar "Snooze 1h" button (top-left, where the traffic lights would be;
# those are hidden because minimize/zoom are disabled no-ops on this panel and
# the close cross just duplicated Snooze). Missing on very old AppKit; the
# card then degrades to the stock cross (closing still snoozes, the labeled
# button is just absent) because the traffic lights are only hidden when the
# accessory actually mounted.
try:
    from AppKit import (
        NSTitlebarAccessoryViewController,
        NSLayoutAttributeLeft,
        NSLayoutAttributeRight,
    )
except Exception:
    NSTitlebarAccessoryViewController = None
    NSLayoutAttributeLeft = None
    NSLayoutAttributeRight = None
try:
    from AppKit import (
        NSWindowCloseButton,
        NSWindowMiniaturizeButton,
        NSWindowZoomButton,
    )
except Exception:
    NSWindowCloseButton = NSWindowMiniaturizeButton = NSWindowZoomButton = None
try:
    from AppKit import NSBezelStyleInline
except Exception:
    NSBezelStyleInline = None
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
# 2026-07-08: raised 300 -> 420 so two-draft cards can show both drafts at
# full, comfortable editing height simultaneously (see _render()'s dual
# branch); single-draft cards get the same extra room, which is a net
# improvement there too, not a regression.
H = 420
M = 16
NS_BEZEL_BORDER = 2  # NSBezelBorder

# Reject-reason categories, in display order. Tags are 1-based button tags on
# the reason picker; values must be a subset of the review_events.reject_category
# CHECK constraint server-side (wrong_author | off_topic | bad_draft | other).
# "other" is still valid server-side but no longer offered as a button: picking
# a category is optional (the red Reject button commits without one) and the
# free-text note covers everything a generic Other chip used to.
REJECT_REASONS = (
    ("wrong_author", "Wrong author / audience"),
    ("off_topic", "Off-topic thread"),
    ("bad_draft", "Feels like AI writing"),
)

# Client-side cap on tracked interactions per card (server clips at 50 too).
MAX_INTERACTIONS = 50


def _snooze_secs():
    """Snooze duration, for the title-bar button LABEL only. The menu bar owns
    the actual timer (its REVIEW_SNOOZE_SECONDS reads the same env var)."""
    try:
        return max(60, int(float(os.environ.get("S4L_REVIEW_SNOOZE_S", "3600"))))
    except Exception:
        return 3600


def _snooze_title():
    s = _snooze_secs()
    return f"Snooze {s // 3600}h" if s % 3600 == 0 else f"Snooze {s // 60}m"

# Inline approve row: glyph + tooltip per approval level (button tag =
# level). Level 1 = plain approve; 2 = loved=True on the decision, with the
# exact level shipped as an approve_level_N interaction for the feedback
# digest. Monochrome text-presentation glyphs (U+FE0E) so the buttons stay
# black-and-white in both appearances instead of rendering as color emoji.
APPROVE_EMOJIS = (
    ("✓︎", "Approve"),
    ("♥︎", "Approve, best of the best"),
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


def _fit_thread_body(thread_tv, text, link_url, *, font_size=12, step=15, floor=10):
    """Set the thread-quote text on `thread_tv` ('text… ↗' when link_url is
    given), shrinking `text` -- never the trailing link -- until the arrow
    actually lands inside the view's visible box.

    thread_tv is fixed-height and not vertically resizable, with no
    scrollview around it, so a glyph laid out below its frame is simply
    never drawn and never clickable. The old fixed `_truncate(text, 200)`
    assumed ~4 lines always fit 200 characters, which holds most of the
    time but not when word/URL lengths wrap more per line -- the trailing
    ↗ link, the card's only way to open the source thread, could then sit
    past the visible box (2026-07-15 user report: link "sometimes appears
    outside the visible area", "doesn't fit into the card"). Checking the
    arrow glyph's own bounding rect (rather than comparing total laid-out
    height to the box) is what makes this correct whether or not the text
    container itself turns out to be height-bounded."""
    text = (text or "").strip()
    box_h = thread_tv.frame().size.height
    try:
        inset = thread_tv.textContainerInset()
        available_h = box_h - 2 * inset.height
    except Exception:
        available_h = box_h

    def _attributed(shown_text):
        b = NSMutableAttributedString.alloc().initWithString_attributes_(
            shown_text,
            {
                NSFontAttributeName: NSFont.systemFontOfSize_(font_size),
                NSForegroundColorAttributeName: NSColor.labelColor(),
            },
        )
        if link_url:
            b.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    " ↗",
                    {
                        NSFontAttributeName: NSFont.systemFontOfSize_(font_size),
                        # Delegate (textView:clickedOnLink:atIndex:) tracks the
                        # click as a thread_click interaction, then opens the
                        # URL itself via NSWorkspace.
                        NSLinkAttributeName: NSURL.URLWithString_(link_url),
                    },
                )
            )
        return b

    length = min(len(text), 200)
    lm = thread_tv.layoutManager()
    tc = thread_tv.textContainer()
    # Bounded loop: each pass is a cheap native layout of at most a couple
    # hundred characters. The common case (thread fits in ~200 chars)
    # exits after the first pass; only pathological long-word wrapping
    # iterates further, and it always terminates at `floor`.
    for _ in range(20):
        shown = text[:length].rstrip()
        if length < len(text):
            shown += "…"
        attributed = _attributed(shown)
        thread_tv.textStorage().setAttributedString_(attributed)
        if not link_url:
            return
        total_len = attributed.length()
        if total_len == 0:
            return
        lm.ensureLayoutForTextContainer_(tc)
        glyph_range = lm.glyphRangeForTextContainer_(tc)
        laid_out_all = (glyph_range.location + glyph_range.length) >= total_len
        fits = False
        if laid_out_all:
            last_rect = lm.boundingRectForGlyphRange_inTextContainer_(
                NSMakeRange(total_len - 1, 1), tc
            )
            fits = (last_rect.origin.y + last_rect.size.height) <= available_h
        if fits or length <= floor:
            return
        length = max(floor, length - step)


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
    # Project and lane are independent concepts most of the time (project
    # "fazm" drafted under lane "personal_brand"), but the operator's own
    # PersonalBrand project IS the personal_brand lane, so project=="PersonalBrand"
    # + lane=="personal_brand" is the SAME fact twice, not two facts — compare
    # with punctuation/case stripped so "PersonalBrand" vs "personal_brand"
    # collapses to one token instead of "PersonalBrand/personal_brand".
    same = project and lane and re.sub(r"[^a-z0-9]", "", project.lower()) == re.sub(
        r"[^a-z0-9]", "", lane.lower()
    )
    if same:
        bits.append(project)
    elif project and lane:
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
    # Two-draft cards (2026-07-11): name each slot's style so the reviewer
    # can tell WHICH style produced the draft they are picking (Draft B is
    # the exploration slot; its source arm renders separately below via the
    # generic experiments lines). Single-draft cards keep the old one-liner.
    _dual = d.get("drafts")
    if isinstance(_dual, list) and len(_dual) == 2:
        _slot_labels = {"a": "Draft A", "b": "Draft B"}
        for _draft in _dual:
            _s = (_draft.get("style") or "").strip()
            if _s:
                _label = _slot_labels.get(
                    (_draft.get("variant") or "").strip().lower(), "Draft"
                )
                lines.append(f"{_label} style: {_s}")
    elif style:
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
        # Two-draft cards: reply_text_en only ever mirrors Draft A (the
        # canonical single-draft field), so Draft B's translation would be
        # silently dropped from the popover unless we read each slot's own
        # text_en directly off the drafts array.
        dual_drafts = d.get("drafts")
        if isinstance(dual_drafts, list) and len(dual_drafts) == 2:
            slot_labels = {"a": "Draft A", "b": "Draft B"}
            for draft in dual_drafts:
                text_en = (draft.get("text_en") or "").strip()
                if text_en:
                    label = slot_labels.get(
                        (draft.get("variant") or "").strip().lower(), "Draft"
                    )
                    lines.append(f"{label} in English: {_truncate(text_en, 280)}")
        elif (d.get("reply_text_en") or "").strip():
            lines.append(
                f"Draft in English: {_truncate(d.get('reply_text_en'), 280)}"
            )
    if d.get("link_url"):
        kw = (d.get("link_keyword") or "").strip()
        lines.append(f"Link: {kw}" if kw else f"Link: {d['link_url']}")
    elif (d.get("link_source") or "").strip():
        lines.append(f"Link: none ({d['link_source'].strip().replace('_', ' ')})")
    return lines


# Twitter's hard-expire ceiling: skill/run-twitter-cycle.sh's FRESHNESS_HOURS,
# a fixed constant with "NO env-var knobs" per the 2026-07-06 decision (2h
# steady-state; widened to 48h while first-run-boost.json exists in the state
# dir, mirroring the exact marker run-draft-and-publish.sh reads to decide the
# same thing). The real Phase 0 gate compares discovered_at, not
# tweet_posted_at, but logic D caps discovery freshness at 1h so the two are
# within ~1h of each other, close enough for an on-card countdown. Reddit
# cards never carry a `stats` dict (see _reddit_plan_to_candidates), so
# _expiry_str never fires for them; reddit's own 24h ceiling (post_reddit.py)
# has no card-facing clock yet.
_TWITTER_EXPIRE_HOURS = 2
_TWITTER_EXPIRE_HOURS_BOOST = 48


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


def _first_run_boost_active():
    try:
        from pathlib import Path

        import s4l_state

        return (Path(s4l_state.state_dir()) / "first-run-boost.json").exists()
    except Exception:
        return False


def _expiry_secs_left(iso, platform):
    """Seconds remaining until the Phase 0 hard-expire cutoff, shared by the
    header's minute-granular label and the hover popover's second-granular
    live countdown. None when there's nothing to count down: no timestamp,
    or a platform this doesn't apply to."""
    if not iso or (platform or "twitter").lower() != "twitter":
        return None
    try:
        t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        hours = (
            _TWITTER_EXPIRE_HOURS_BOOST
            if _first_run_boost_active()
            else _TWITTER_EXPIRE_HOURS
        )
        deadline = t + datetime.timedelta(hours=hours)
        return int(
            (deadline - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        )
    except Exception:
        return None


def _expiry_str(iso, platform):
    """Minute-granular countdown for the card header ('1h22m left', '4m
    left'), or 'expired' once past it (a laggy sync can leave a stale card
    showing for a few seconds before the backend prune catches up and drops
    it). None when _expiry_secs_left is None."""
    secs_left = _expiry_secs_left(iso, platform)
    if secs_left is None:
        return None
    if secs_left <= 0:
        return "expired"
    mins_left = secs_left // 60
    if mins_left < 60:
        return f"{mins_left}m left"
    h, m = divmod(mins_left, 60)
    return f"{h}h left" if m == 0 else f"{h}h{m:02d}m left"


def _expiry_seconds_str(iso, platform):
    """Second-granular countdown ('1h22m03s left', '4m09s left', '38s
    left'), or 'expired'. Only the hover popover uses this -- it visibly
    ticks down (2026-07-15 per user) while the header label itself stays
    minute-granular so it isn't re-laid-out every second."""
    secs_left = _expiry_secs_left(iso, platform)
    if secs_left is None:
        return None
    if secs_left <= 0:
        return "expired"
    h, rem = divmod(secs_left, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s left"
    if m:
        return f"{m}m{s:02d}s left"
    return f"{s}s left"


# Hover popover on the header's age/expiry label (2026-07-15 per user): the
# reviewer sees the countdown but not necessarily WHY it exists, so the
# popover pairs the live seconds-granular clock with the reasoning behind the
# freshness gate itself.
_EXPIRY_EDUCATION_TEXT = (
    "What we care about is not a post that has a lot of engagement, but the "
    "fresh ones: ideally we're the first to comment and like a post, to have "
    "the highest share of voice and be the first-ranking comment on a thread."
)


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


def _solid(color):
    """Bake a dynamic/semantic NSColor (textBackgroundColor, controlAccentColor,
    etc.) down to concrete sRGB components. The card sits inside an
    NSVisualEffectView (see _frosted); dynamic system colors drawn in that
    vibrant context render partially see-through against whatever is behind
    the window instead of the flat opaque/solid color they look like in a
    normal window (2026-07-08 feedback: the selection ring and its "opaque"
    backing both still blended into a dark desktop behind the card). Once
    converted to a plain sRGB color it is no longer a vibrancy-aware dynamic
    color, so CALayer draws it as flat, fully opaque pixels."""
    try:
        return color.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace()) or color
    except Exception:
        return color


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


def _label(frame, text, *, size=12, bold=False, muted=False, truncates=False):
    f = NSTextField.alloc().initWithFrame_(frame)
    f.setStringValue_(text or "")
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setFont_(_font(size, bold))
    if muted:
        f.setTextColor_(NSColor.secondaryLabelColor())
    # truncates=True: single line, "…" at the end when it overflows its frame,
    # instead of word-wrapping into a second line the fixed-height frame then
    # silently clips. For tight one-line strips (e.g. the reply heading's
    # inline project/lane/virality tag) where an exact pixel width can't be
    # guaranteed against every value.
    if truncates:
        f.setLineBreakMode_(NSLineBreakByTruncatingTail)
        try:
            f.cell().setWraps_(False)
            f.cell().setScrollable_(False)
        except Exception:
            pass
        return f
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
    def initWithDrafts_onDecision_onComplete_focus_(
        self, drafts, on_decision, on_complete, focus
    ):
        self = objc.super(_ReviewController, self).init()
        if self is None:
            return None
        self._drafts = list(drafts)
        self._on_decision = on_decision
        self._on_complete = on_complete
        # focus=True only for a USER-initiated open (the menu bar's "Review N
        # pending drafts"): activate the app and seat the caret in the reply
        # field. Auto-presented cards (the timer tick) must appear without
        # taking keyboard focus from whatever the user is doing.
        self._focus = bool(focus)
        self._close_reason = None  # "snooze" when the title-bar button closed us
        self._idx = 0
        self._decisions = []
        self._panel = None
        self._textview = None
        self._link_targets = {}
        self._eye_btn = None
        self._details_btn = None
        self._stats_popover = None
        self._age_expiry_label = None
        self._expiry_timer = None
        self._expiry_secs_label = None
        # Per-card telemetry, reset when a NEW card renders (not on the
        # card <-> reason-picker swap, which is the same card).
        self._rendered_idx = -1
        self._interactions = []
        self._card_shown_at = None
        self._reason_field = None
        # Two-draft cards (2026-07-08 redesign, no-recommendation pass same
        # day): both drafts render at once as separate editable boxes;
        # `_selected_draft` (0=a, 1=b) is "whichever box the reviewer is
        # currently in", driven by caret movement (see textViewDidChangeSelection_ below),
        # not a button. None = not yet chosen this card, _render() defaults
        # it to slot 0 (Draft A) — the model never picks a favorite, so
        # there's no recommendation to default to instead. Reset to None on
        # every NEW card (see _render()'s "fresh card" branch below), so a
        # switch made on one card never bleeds into the next.
        # _draft_textviews/_draft_scrolls map slot -> its NSTextView/
        # NSScrollView so the delegate callback and border-styling helper can
        # find them; both empty on single-draft cards.
        self._selected_draft = None
        self._draft_textviews = {}
        self._draft_scrolls = {}
        # Per-draft hover dwell (two-draft cards, 2026-07-10): accumulated
        # milliseconds the pointer spent over each draft box, so the feedback
        # digest can tell an informed keep of Draft A (they read B and stayed)
        # from a fast approve that says nothing about B. Raw ms ship on the
        # decision; the read-vs-skim threshold lives digest-side so it can be
        # tuned without a client release. _draft_hover_open holds the enter
        # timestamp of any hover still in progress (flushed on decision).
        self._draft_hover_ms = {0: 0, 1: 0}
        self._draft_hover_open = {}
        # Slots the caret has actually been in this card (2026-07-10 follow-up):
        # lets the decision distinguish "clicked into B, then came BACK to A"
        # (an explicit head-to-head choice of A, per user) from "never touched
        # B at all". Only the UNCHOSEN slot's membership matters at decision
        # time; the selected slot is trivially visited.
        self._draft_visited = set()
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
            | NSWindowStyleMaskNonactivatingPanel
        )
        panel = _ReviewPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setFloatingPanel_(True)
        # Key only when a text field is clicked: buttons (Approve/Reject) work
        # without ever taking keyboard focus, and clicking into the reply field
        # makes this nonactivating panel key without activating the app, so
        # the user's frontmost app keeps its state. The old False + activate-
        # on-present combination stole the keyboard mid-typing (2026-07-09
        # customer complaint).
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setDelegate_(self)
        self._add_snooze_accessory(panel)
        self._panel = panel
        self._render()
        panel.orderFrontRegardless()
        self._log_surface("presented")
        if self._focus:
            # User asked for the cards (menu item): behave like a normal open,
            # activate and put the caret in the reply field. Deferred second
            # pass because activation/key promotion lands on the run loop.
            panel.makeKeyAndOrderFront_(None)
            self.focusReply_(None)
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
    def _add_snooze_accessory(self, panel):
        """Title-bar controls, replacing the traffic lights: "Snooze 1h" at
        the top-left (minimize/zoom were disabled no-op dots on this panel,
        and the close cross meant snooze anyway, so one labeled button says
        what the only dismissal actually does) and "Discard all…" at the
        top-right (moved here from the menu bar dropdown 2026-07-10; the
        ellipsis is honest, the handler opens a confirmation alert first).
        Snooze just closes the panel; the menu bar treats any close with
        undecided drafts as a snooze. The traffic lights are hidden ONLY once
        the snooze accessory mounts, so a failure on old AppKit leaves the
        stock cross as the fallback dismissal. Cmd-W keeps working either way
        (the Closable mask stays on)."""
        if NSTitlebarAccessoryViewController is None or NSLayoutAttributeLeft is None:
            return
        try:
            self._add_titlebar_button(
                panel,
                title=_snooze_title(),
                action="snoozeClicked:",
                tooltip=(
                    "Put these drafts away for now. They stay pending and the "
                    "card comes back later (or sooner from the S4L menu)."
                ),
                layout=NSLayoutAttributeLeft,
            )
            self._hide_traffic_lights(panel)
        except Exception as e:
            _log(f"snooze accessory unavailable: {e}")
        if _discard_all_handler is not None and NSLayoutAttributeRight is not None:
            try:
                self._add_titlebar_button(
                    panel,
                    title="Discard all…",
                    action="discardAllClicked:",
                    tooltip=(
                        "Throw away every pending draft (asks first). Nothing "
                        "posts, and unlike a per-card reject this sends no "
                        "feedback signal to the AI."
                    ),
                    layout=NSLayoutAttributeRight,
                )
            except Exception as e:
                _log(f"discard-all accessory unavailable: {e}")

    @objc.python_method
    def _add_titlebar_button(self, panel, title, action, tooltip, layout):
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 17))
        btn.setTitle_(title)
        if NSBezelStyleInline is not None:
            btn.setBezelStyle_(NSBezelStyleInline)
        else:
            btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setFont_(NSFont.systemFontOfSize_(10.0))
        btn.setToolTip_(tooltip)
        btn.setTarget_(self)
        btn.setAction_(action)
        btn.sizeToFit()
        bf = btn.frame()
        holder = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, bf.size.width + 8, max(19, bf.size.height + 2))
        )
        btn.setFrameOrigin_(
            (4, max(0, (holder.frame().size.height - bf.size.height) / 2.0))
        )
        holder.addSubview_(btn)
        acc = NSTitlebarAccessoryViewController.alloc().init()
        acc.setView_(holder)
        acc.setLayoutAttribute_(layout)
        panel.addTitlebarAccessoryViewController_(acc)

    @objc.python_method
    def _hide_traffic_lights(self, panel):
        """Hide close/minimize/zoom once the Snooze accessory is up: minimize
        and zoom are disabled dots (no Miniaturizable/Resizable in the mask)
        and the cross duplicated Snooze. Hiding, not removing: performClose_
        (Cmd-W and snoozeClicked_) still routes through the hidden close
        button's machinery, and windowShouldClose_ still fires."""
        if NSWindowCloseButton is None:
            return
        for which in (
            NSWindowCloseButton,
            NSWindowMiniaturizeButton,
            NSWindowZoomButton,
        ):
            try:
                b = panel.standardWindowButton_(which)
                if b is not None:
                    b.setHidden_(True)
            except Exception:
                pass

    def snoozeClicked_(self, sender):
        self._track("snooze")
        self._close_reason = "snooze"
        try:
            self._panel.performClose_(None)
        except Exception:
            self._finish()

    def discardAllClicked_(self, sender):
        """Hand off to the menu bar's bulk-discard handler (registered via
        set_discard_all_handler); it confirms with the user, flips the store,
        and dismisses this panel via dismiss_active(), so nothing more happens
        here. On cancel the card simply stays up."""
        self._track("discard_all")
        cb = _discard_all_handler
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            _log(f"discard-all handler failed: {e}")

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
            self._draft_hover_ms = {0: 0, 1: 0}
            self._draft_hover_open = {}
            self._draft_visited = set()
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
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, H - 42, 38, 30))
            btn.setTitle_(emoji)
            btn.setBezelStyle_(NSBezelStyleRounded)
            # 16pt fits the rounded bezel's ~22px content lane for these
            # text-presentation glyphs (the old color emoji clipped at 16pt,
            # which is why this used to be 13pt).
            btn.setFont_(NSFont.systemFontOfSize_(16))
            btn.setTag_(i + 1)  # tag = approval level
            btn.setTarget_(self)
            btn.setAction_("approve:")
            try:
                btn.setToolTip_(tip)
            except Exception:
                pass
            content.addSubview_(btn)
            x += 42

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
        self._age_expiry_label = None
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
        expiry = _expiry_str(stats.get("tweet_posted_at"), d.get("platform"))
        # Age reads as "how old is this thread"; the bracketed countdown reads
        # as "how urgent is reviewing it" -- kept as one combined label in the
        # header row rather than a second display elsewhere on the card
        # (2026-07-15 per user).
        if age and expiry:
            age_expiry = f"{age} ({expiry})"
        else:
            age_expiry = age or expiry
        if age_expiry:
            # Urgent state (<=15min left, or already past the cutoff) drops
            # the muted gray and goes bold+full-strength instead of adding a
            # color: this repo's severity convention is weight, never a new
            # chromatic accent (see CLAUDE.md "Dashboard colors").
            _mins_only = re.fullmatch(r"(\d+)m left", expiry or "")
            urgent = expiry == "expired" or (
                _mins_only and int(_mins_only.group(1)) <= 15
            )
            age_w = int(
                NSAttributedString.alloc().initWithString_attributes_(
                    age_expiry, {NSFontAttributeName: _font(11, urgent)}
                ).size().width
            ) + 8
            age_label = _label(
                NSMakeRect(right_x - age_w, H - 70, age_w, 18),
                age_expiry,
                size=11,
                bold=urgent,
                muted=not urgent,
            )
            age_label.setAlignment_(NSTextAlignmentRight)
            content.addSubview_(age_label)
            right_x -= age_w + 4
            if expiry:
                # Hover shows a second-granular ticking countdown plus the
                # freshness-matters explanation (2026-07-15 per user); the
                # header label itself stays minute-granular so it isn't
                # re-laid-out every second.
                age_label.addTrackingArea_(
                    NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                        age_label.bounds(),
                        NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways,
                        self,
                        {"kind": "expiry"},
                    )
                )
                self._age_expiry_label = age_label
        # Platform mark (brand identification, inline with the author row):
        # Reddit's orange "r/" vs X's glyph, so a mixed-platform review queue
        # reads at a glance which network each card posts to.
        _plat = (d.get("platform") or "twitter").lower()
        _mark = "r/" if _plat == "reddit" else "\U0001d54f"
        _mark_w = 18
        _mark_label = _label(
            NSMakeRect(M + 78, H - 70, _mark_w, 18), _mark, size=12, bold=True
        )
        if _plat == "reddit":
            try:
                # Reddit orangered (#FF4500); best effort, falls back to the
                # default label color on any AppKit hiccup.
                _mark_label.setTextColor_(
                    NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.27, 0.0, 1.0)
                )
            except Exception:
                pass
        content.addSubview_(_mark_label)
        _author_x = M + 78 + _mark_w
        handle_w = right_x - _author_x - 4
        if handle:
            # Size the link to its text so the follower count can sit right
            # after the handle instead of at a fixed column.
            text = f"u/{handle}" if _plat == "reddit" else f"@{handle}"
            measured = NSAttributedString.alloc().initWithString_attributes_(
                text, {NSFontAttributeName: _font(12, True)}
            ).size().width
            link_w = min(int(measured) + 8, handle_w)
            _prof_url = (
                f"https://www.reddit.com/user/{handle}"
                if _plat == "reddit"
                else f"https://x.com/{handle}"
            )
            self._add_link(
                content,
                NSMakeRect(_author_x, H - 70, link_w, 18),
                text,
                _prof_url,
                bold=True,
                kind="profile_click",
            )
            followers = _followers_str(stats)
            fol_w = handle_w - link_w
            if followers and fol_w > 20:
                content.addSubview_(
                    _label(
                        NSMakeRect(_author_x + link_w, H - 70, fol_w, 18),
                        f"· {followers}",
                        size=11,
                        muted=True,
                    )
                )
        else:
            content.addSubview_(
                _label(NSMakeRect(_author_x, H - 70, handle_w, 18), "thread", size=12, bold=True, muted=True)
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
        # UP). Pin the frame, then _fit_thread_body shrinks the text (never
        # the trailing ↗ link) until the arrow actually lands inside it.
        thread_tv.setVerticallyResizable_(False)
        thread_tv.setHorizontallyResizable_(False)
        if thread_url:
            thread_tv.setDelegate_(self)
        _fit_thread_body(thread_tv, thread_en or d.get("thread_text"), thread_url)
        content.addSubview_(thread_tv)
        # Reply heading — bold. A concise "project/lane · viral N" tag rides
        # right after it in a SEPARATE, regular-weight label (2026-07-08:
        # was folded into the same bold string, which made the metadata read
        # as loudly as the heading itself; split + measured-width placement
        # mirrors the "@handle · followers" pattern in the author row above).
        # Right-aligned on the same line: an eye icon whose hover/click
        # popover carries the remaining DRAFTING metadata (style, discovery
        # query, link choice), mirroring the author row's engagement-stats
        # eye. Skipped when the plan carries none of it (older plans).
        heading_text = (
            f"Reply (posts in {_lang_name(card_lang)}, editable):"
            if is_foreign
            else "Reply (editable):"
        )
        # +8 (not +4): NSTextField's own cellSize() for this text measures
        # ~4px wider than the raw NSAttributedString width used here, so a
        # tight +4 pad was landing sub-pixel under the real render width and
        # wrapping "(editable):" onto a clipped, invisible second line.
        # Matches the +8 pad the @handle link width uses below for the same
        # reason.
        heading_w = min(
            int(
                NSAttributedString.alloc().initWithString_attributes_(
                    heading_text, {NSFontAttributeName: _font(12, True)}
                ).size().width
            ) + 8,
            W - 2 * M - 24,
        )
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 172, heading_w, 16),
                heading_text,
                size=12,
                bold=True,
                muted=True,
            )
        )
        # Long headings (foreign-language variant) can leave very little room
        # for the suffix; truncates=True ellipsizes it instead of the old
        # word-wrap silently clipping mid-word ("fazm/" with no "…") into the
        # fixed-height frame's invisible second line.
        heading_suffix = _reply_heading_suffix(d)
        suffix_w = W - 2 * M - 24 - heading_w
        if heading_suffix and suffix_w > 20:
            content.addSubview_(
                _label(
                    NSMakeRect(M + heading_w, H - 172, suffix_w, 16),
                    f" · {heading_suffix}",
                    size=11,
                    muted=True,
                    truncates=True,
                )
            )
        if _details_lines(d):
            # No y nudge needed here (unlike the author row): this label is
            # 16px, so an 18px button at the same y already centers its image
            # on the top-aligned text line.
            deye = self._eye_button(NSMakeRect(W - M - 20, H - 172, 20, 18), "details")
            content.addSubview_(deye)
            self._details_btn = deye

        # Two-draft cards (2026-07-08 redesign; no-recommendation pass same
        # day): show BOTH drafts at once, stacked, each independently
        # editable, rather than a toggle that swaps one field's content. No
        # buttons: selection is "whichever box the reviewer is currently in",
        # shown via a dedicated outline view wrapping that box (an accent
        # outline vs a plain hairline, mirroring a standard focus ring) and
        # updated live by textViewDidChangeSelection_ below. The model never picks a favorite
        # (removed 2026-07-08 per user: no ask-the-model-to-recommend), so
        # Draft A (slot 0) is simply the fixed default until the reviewer
        # clicks into Draft B. Absent/short (reused stale draft, legacy plan)
        # falls back to one full-height editable box, same as before.
        drafts = d.get("drafts")
        dual = isinstance(drafts, list) and len(drafts) == 2
        sel_idx = self._selected_draft if (dual and self._selected_draft in (0, 1)) else 0
        if dual:
            self._selected_draft = sel_idx

        edit_top = H - 172 - 3
        link = d.get("link_url")

        # Tail link baked at draft time is normally already in each draft's
        # text (scripts/twitter_gen_links.py + the drafts-link-sync step in
        # run-twitter-cycle.sh); only append link_url when a text is
        # genuinely link-free (older/fallback plans), else it'd show twice.
        def _compose(text):
            text = text or ""
            return text if (not link or link in text) else f"{text} {link}"

        self._draft_textviews = {}
        self._draft_scrolls = {}
        self._draft_outlines = {}
        if dual:
            avail_h = edit_top - M
            gap = 5
            # Reserved only for foreign-language cards, which show a muted
            # "EN:" line above each box; English drafts (the common case)
            # have nothing to put there, so reserving it unconditionally used
            # to leave a dead 15px gap above every box for no reason.
            label_h = 15 if is_foreign else 0
            unit = (avail_h - gap) / 2.0
            box_h = unit - label_h
            for slot in (0, 1):
                # slot 0 (Draft A) always renders on top, slot 1 (Draft B)
                # always on the bottom: a fixed, predictable order so cards
                # don't reshuffle as the reviewer switches between them.
                box_y = M if slot == 1 else M + unit + gap
                label_y = box_y + box_h
                draft = drafts[slot]
                draft_text_en = (draft.get("text_en") or "").strip()
                if is_foreign and draft_text_en:
                    content.addSubview_(
                        _label(
                            NSMakeRect(M, label_y, W - 2 * M, label_h),
                            f"EN: {_truncate(draft_text_en, 90)}",
                            size=10,
                            muted=True,
                            truncates=True,
                        )
                    )
                # The selection ring can't be drawn on the scroll view's own
                # layer: NSScrollView's opaque clip/document view fully paints
                # over its parent layer's border, so setBorderWidth/Color on
                # scroll.layer() is accepted (no error) but never visibly
                # renders, at any width or color (verified empirically). A
                # separate outline view, sized to the box and holding the
                # scroll view inset a few px inside it, keeps the ring
                # unobstructed since nothing opaque reaches its edge.
                # The inset gap between this wrapper's edge (where the ring is
                # drawn) and the scroll view's own background otherwise sits
                # directly on the translucent frosted panel behind it, so a
                # plain hairline there read as barely visible (2026-07-08
                # feedback: "outline not obvious, background is mostly
                # transparent"). _update_draft_borders (called once the
                # content view is installed, below) backs the whole wrapper a
                # solid color so box + ring read as one opaque card; no need
                # to pre-seed it here since that call always follows.
                outline_frame = NSMakeRect(M, box_y, W - 2 * M, box_h)
                outline = NSView.alloc().initWithFrame_(outline_frame)
                _round_rect(outline)
                inset = 4
                scroll, tv = _editable_scroll(
                    NSMakeRect(
                        inset,
                        inset,
                        outline_frame.size.width - 2 * inset,
                        outline_frame.size.height - 2 * inset,
                    ),
                    _compose(draft.get("text")),
                )
                tv.setDelegate_(self)
                outline.addSubview_(scroll)
                content.addSubview_(outline)
                # Hover dwell per draft box (same NSTrackingArea pattern as the
                # eye buttons): enter/exit timestamps accumulate into
                # _draft_hover_ms[slot] so the decision can say whether the
                # reviewer actually READ the draft they didn't pick. slot rides
                # on userInfo, mirroring the eyes' `kind` routing.
                outline.addTrackingArea_(
                    NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                        outline.bounds(),
                        NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways,
                        self,
                        {"kind": "draft", "slot": slot},
                    )
                )
                self._draft_scrolls[slot] = scroll
                self._draft_outlines[slot] = outline
                self._draft_textviews[slot] = tv
            tv = self._draft_textviews[sel_idx]
        else:
            reply = d.get("reply_text") or ""
            # Non-English draft with a stamped translation: a muted read-only
            # "EN:" block sits between the heading and the editable field so
            # the reviewer reads the draft in English while still editing
            # (and posting) the original-language text below. Full
            # translation lives in the details popover; inline is truncated
            # to what ~3 lines fit.
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
                NSMakeRect(M, M, W - 2 * M, edit_top - M), _compose(reply)
            )
            content.addSubview_(scroll)
        self._textview = tv

        self._panel.setContentView_(_frosted(content))
        if dual:
            # Layer border properties set before a view is attached to its
            # eventual window get silently dropped when AppKit backs the view
            # for real on attach, so the accent outline must be (re)applied
            # only after setContentView_ installs the view tree, not during
            # construction above.
            self._update_draft_borders()
        # Counter lives in the native title bar, not inside the content, with
        # the product name so a stray card is identifiable at a glance.
        self._panel.setTitle_(
            f"s4l · Review draft {self._idx + 1} of {len(self._drafts)}"
        )
        # setContentView_ rebuilds the view tree, so the caret would otherwise
        # default to the Approve button. Re-seat it in the reply field for every
        # card the user is ACTIVELY reviewing (they opened the stack from the
        # menu, or already decided/touched something), so each one is
        # immediately editable. NOT on the initial auto-presented render:
        # focusReply_ activates the app, and yanking the caret out of whatever
        # the user was typing was the 2026-07-09 "too distracting" complaint.
        # An untouched card's field becomes editable on first click instead.
        if self._focus or self._decisions or self._last_interaction_at is not None:
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
        # The expiry popover's live tick is the only popover that owns a
        # timer; every close path (hover-out, card advance, window close)
        # already routes through here, so stopping it here means one
        # dangling repeating NSTimer can't outlive its popover.
        if self._expiry_timer is not None:
            try:
                self._expiry_timer.invalidate()
            except Exception:
                pass
            self._expiry_timer = None
        self._expiry_secs_label = None

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

    @objc.python_method
    def _show_expiry_popover(self):
        """Hover popover for the header's age/expiry label: a live,
        second-granular countdown (ticked by an NSTimer, unlike the header's
        own minute-granular text) paired with the fixed explanation of why
        freshness matters (2026-07-15 per user). Bypasses the generic
        _show_popover helper because that one renders static text only; this
        needs to keep a reference to the countdown label so the timer can
        update it in place."""
        if self._age_expiry_label is None or (
            self._stats_popover is not None and self._stats_popover.isShown()
        ):
            return
        d = self._drafts[self._idx]
        stats = d.get("stats") or {}
        seconds_str = _expiry_seconds_str(stats.get("tweet_posted_at"), d.get("platform"))
        if not seconds_str:
            return
        rows = [seconds_str, _EXPIRY_EDUCATION_TEXT]
        font = NSFont.systemFontOfSize_(12)
        heights = []
        pw = 0
        for row in rows:
            s = NSAttributedString.alloc().initWithString_attributes_(
                row, {NSFontAttributeName: font}
            )
            measured = s.boundingRectWithSize_options_(NSMakeSize(260, 10_000), 1)
            heights.append(int(measured.size.height) + 3)
            pw = max(pw, int(measured.size.width))
        pw += 34
        row_gap = 8
        ph = sum(heights) + row_gap + 16
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, pw, ph))
        y = ph - 8 - heights[0]
        secs_label = _label(NSMakeRect(13, y, pw - 26, heights[0]), seconds_str, size=12, bold=True)
        view.addSubview_(secs_label)
        y -= row_gap + heights[1]
        view.addSubview_(
            _label(NSMakeRect(13, y, pw - 26, heights[1]), _EXPIRY_EDUCATION_TEXT, size=12, muted=True)
        )
        vc = NSViewController.alloc().init()
        vc.setView_(view)
        pop = NSPopover.alloc().init()
        pop.setBehavior_(NSPopoverBehaviorApplicationDefined)
        pop.setContentViewController_(vc)
        pop.setContentSize_((pw, ph))
        try:
            NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            pass
        pop.showRelativeToRect_ofView_preferredEdge_(
            self._age_expiry_label.frame(), self._age_expiry_label.superview(), 1
        )
        self._stats_popover = pop
        self._expiry_secs_label = secs_label
        self._expiry_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "tickExpiryPopover:", None, True
        )
        _log("expiry popover shown")

    def tickExpiryPopover_(self, timer):
        """NSTimer target (2026-07-15): re-renders the popover's seconds-left
        label every second so the reviewer visibly sees it counting down.
        Not a python_method -- NSTimer invokes this through the ObjC runtime."""
        if self._expiry_secs_label is None:
            return
        try:
            d = self._drafts[self._idx]
            stats = d.get("stats") or {}
            seconds_str = _expiry_seconds_str(stats.get("tweet_posted_at"), d.get("platform"))
            if seconds_str:
                self._expiry_secs_label.setStringValue_(seconds_str)
        except Exception:
            pass

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
    def _hover_info(self, event):
        """(kind, slot) a tracking-area event belongs to, from the userInfo
        stamped at creation: ('stats'|'details', None) for the eye icons,
        ('expiry', None) for the age/expiry label, ('draft', 0|1) for the two
        draft boxes. Defaults to ('stats', None), the original single-eye
        behavior, if the area carries no info."""
        try:
            info = event.trackingArea().userInfo()
            if info:
                kind = info.get("kind")
                if kind == "draft":
                    return "draft", int(info.get("slot"))
                if kind == "details":
                    return "details", None
                if kind == "expiry":
                    return "expiry", None
        except Exception:
            pass
        return "stats", None

    # NSTrackingArea owner callbacks (hover over either eye icon, the
    # age/expiry label, or, on two-draft cards, either draft box). Draft
    # hovers only bank dwell time (no popover, no logging: the boxes are big
    # and enter/exit fires on every pass of the pointer).
    def mouseEntered_(self, event):
        kind, slot = self._hover_info(event)
        if kind == "draft":
            self._draft_hover_open[slot] = time.time()
            return
        _log(f"{kind} eye hover enter" if kind != "expiry" else "expiry label hover enter")
        if kind == "details":
            self._show_details_popover()
        elif kind == "expiry":
            self._show_expiry_popover()
        else:
            self._show_stats_popover()

    def mouseExited_(self, event):
        kind, slot = self._hover_info(event)
        if kind == "draft":
            started = self._draft_hover_open.pop(slot, None)
            if started is not None:
                self._draft_hover_ms[slot] = self._draft_hover_ms.get(slot, 0) + int(
                    (time.time() - started) * 1000
                )
            return
        _log("eye hover exit")
        self._close_stats_popover()

    @objc.python_method
    def _flush_draft_hovers(self):
        """Bank any hover still in progress (pointer inside a draft box at
        decision time, e.g. a keyboard approve) so _record reads final
        totals."""
        now = time.time()
        for slot, started in list(self._draft_hover_open.items()):
            self._draft_hover_ms[slot] = self._draft_hover_ms.get(slot, 0) + int(
                (now - started) * 1000
            )
            # Keep the hover open (re-anchored at now) rather than deleting
            # it: the pointer really is still inside the box, so a later
            # mouseExited_ must not double-count the pre-flush span.
            self._draft_hover_open[slot] = now

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

    # NSTextView delegate: fires the instant the caret/selection moves inside
    # a text view, which includes a plain click-to-place-cursor with no
    # keystroke (unlike NSTextDidBeginEditingNotification/textDidBeginEditing_,
    # which only fires once an actual edit starts and does NOT fire from
    # clicking into a box to merely place the cursor, verified empirically —
    # that gap was the "clicking the other draft doesn't select it" bug).
    # Two-draft cards use this as the ONLY selection mechanism: whichever
    # draft box the reviewer is in IS the selected one. Only the two draft
    # boxes set self as delegate for this notification (the read-only thread
    # quote never fires it), so no candidate_id lookup is needed, just a slot
    # match against self._draft_textviews.
    def textViewDidChangeSelection_(self, notification):
        try:
            tv = notification.object()
        except Exception:
            return
        for slot, cand_tv in (self._draft_textviews or {}).items():
            if cand_tv is not tv:
                continue
            # Visited even when it's already the selected slot: membership of
            # the eventually-UNCHOSEN slot is what _record reads, and that
            # slot only ever gets the caret via a deliberate user click (the
            # auto-focus seat in _render targets the selected slot only).
            self._draft_visited.add(slot)
            if slot != self._selected_draft:
                self._selected_draft = slot
                self._textview = cand_tv
                self._update_draft_borders()
            break

    @objc.python_method
    def _update_draft_borders(self):
        """Redraw the two draft boxes' selection ring in place (no re-render,
        so an in-progress edit/caret in either box is never disturbed): the
        selected box's outline view gets a thick, solid black outline
        (deliberately NOT the user's system accent color — on a Graphite
        accent it renders as plain gray and is indistinguishable from chrome;
        2026-07-08 feedback wanted "a stronger color", then specifically
        black), the other a plain hairline. Every color here is baked via
        _solid() first: drawn as-is, dynamic system colors render partially
        see-through against whatever is behind the card's frosted/vibrant
        panel, which was why an earlier pass still looked washed out over a
        dark desktop. Applied to the dedicated outline wrapper, not the
        scroll view itself; see the comment at its construction in _render
        for why."""
        selected_color = NSColor.blackColor()
        for slot, outline in (self._draft_outlines or {}).items():
            try:
                layer = outline.layer()
                if layer is None:
                    continue
                if slot == self._selected_draft:
                    layer.setBorderWidth_(3.0)
                    layer.setBorderColor_(selected_color.CGColor())
                    # A ring alone read as barely-there against the frosted
                    # panel. The margin between this wrapper's edge and the
                    # inset scroll view is otherwise plain background, so
                    # tinting it toward black turns that margin into a visible
                    # halo, not just a hairline — obvious at a glance, not
                    # just on close inspection.
                    tint = _solid(
                        NSColor.textBackgroundColor()
                    ).blendedColorWithFraction_ofColor_(0.30, selected_color)
                    layer.setBackgroundColor_((tint or selected_color).CGColor())
                else:
                    layer.setBorderWidth_(1.0)
                    layer.setBorderColor_(_solid(NSColor.separatorColor()).CGColor())
                    layer.setBackgroundColor_(_solid(NSColor.textBackgroundColor()).CGColor())
            except Exception:
                pass

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
            # Full pairwise context for the feedback digest (2026-07-10): the
            # UNCHOSEN draft's text+style ride along so "picked B over A" (or
            # "kept A after reading B", per the hover dwell) is a usable
            # preference PAIR, not just a winner with no loser. Shipped as one
            # nested dict end to end (decision -> review event -> jsonb column)
            # so adding a field never needs another schema hop.
            self._flush_draft_hovers()
            other = drafts[1 - sel_idx]
            draft_choice = {
                "variant": draft_variant,
                "index": sel_idx,
                "auto_selected": bool(sel_idx == 0),
                "style": chosen_draft.get("style") or None,
                "unchosen_text": (other.get("text") or "").strip() or None,
                "unchosen_style": other.get("style") or None,
                "hover_a_ms": int(self._draft_hover_ms.get(0, 0)),
                "hover_b_ms": int(self._draft_hover_ms.get(1, 0)),
                # True = the caret was in the unchosen box at some point, i.e.
                # they tried the other draft and came back: an explicit choice
                # even when the winner is the preselected default.
                "visited_other": bool((1 - sel_idx) in self._draft_visited),
            }
        else:
            orig = (d.get("reply_text") or "").strip()
            draft_variant = None
            draft_choice = None
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
                "platform": d.get("platform"),
                "project": d.get("project"),
                "thread_url": d.get("thread_url"),
                "thread_author": d.get("thread_author"),
                # Draft language (ISO 639-1) so the feedback digest can scope
                # edit/reject patterns per language. draft_text/original_text
                # stay in the ORIGINAL language (that is what posts).
                "language": d.get("language"),
                # Two-draft cards: which draft ("a"|"b") was showing when this
                # decision was made, its 0/1 index, and whether it was the
                # fixed default (Draft A / slot 0) rather than a reviewer
                # switch to Draft B. None/False on single-draft candidates.
                # Kept separate from `edited` above by design.
                "draft_variant": draft_variant,
                "draft_index": sel_idx,
                "draft_auto_selected": bool(dual and sel_idx == 0),
                # Nested pairwise record (chosen vs unchosen text/style plus
                # per-box hover dwell); None on single-draft candidates. The
                # flat three fields above stay for their existing consumers
                # (edit-learning variant stamp in s4l_menubar).
                "draft_choice": draft_choice,
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
    def prune_drafts(self, ns):
        """Drop not-yet-reached drafts (plan index `n` in `ns`) from the stack,
        e.g. a card the backend already retired (expired freshness gate, etc.)
        while it was still waiting to be shown. Only ever removes entries AFTER
        the current index, so the card on screen right now and everything
        already decided are untouched -- nothing visible disappears out from
        under the user. Refreshes the title-bar counter live. Returns the
        count actually removed."""
        if self._panel is None or not ns:
            return 0
        ns = set(ns)
        kept = []
        removed = 0
        for i, d in enumerate(self._drafts):
            if i > self._idx and d.get("n") in ns:
                removed += 1
                continue
            kept.append(d)
        if not removed:
            return 0
        self._drafts = kept
        try:
            self._panel.setTitle_(
                f"s4l · Review draft {self._idx + 1} of {len(self._drafts)}"
            )
        except Exception:
            pass
        self._log_surface(f"pruned {removed} expired")
        return removed

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
        # row so the cursor doesn't have to travel between steps: "Reject"
        # sits exactly where the card's Reject was (one-click double-tap
        # keeps the old zero-friction reject) and commits with no category,
        # since picking one is optional; Back sits at the left where Approve
        # was.
        back = NSButton.alloc().initWithFrame_(NSMakeRect(M, H - 42, 90, 30))
        back.setTitle_("Back")
        back.setBezelStyle_(NSBezelStyleRounded)
        back.setTarget_(self)
        back.setAction_("rejectBack:")
        content.addSubview_(back)

        skip = NSButton.alloc().initWithFrame_(NSMakeRect(W - M - 66, H - 42, 66, 30))
        skip.setTitle_("Reject")
        skip.setBezelStyle_(NSBezelStyleRounded)
        try:
            skip.setHasDestructiveAction_(True)  # red title on macOS 12+
        except Exception:
            pass
        skip.setTarget_(self)
        skip.setAction_("rejectSkip:")
        content.addSubview_(skip)

        content.addSubview_(
            _label(NSMakeRect(M, H - 72, W - 2 * M, 20), "Why reject this draft? (optional)", size=13, bold=True)
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
            note.setPlaceholderString_("Optional note (sent with or without a reason)")
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
        # (not posted). Finish with whatever was decided so far. The menu bar
        # treats a close with undecided drafts as SNOOZE (they re-present after
        # REVIEW_SNOOZE_SECONDS, or sooner via the menu), whether it came from
        # the cross or the title-bar Snooze button.
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
        reason = self._close_reason or "closed"
        _log(f"closed: {len(self._decisions)} decided of {len(self._drafts)} ({reason})")
        _write_review_state(last_event="snoozed" if reason == "snooze" else "closed")


def present_review(drafts, on_decision=None, on_complete=None, focus=False):
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
    project, the experiments["lane"] arm, and stats.virality_score render
    inline on the "Reply (editable):" heading itself as a concise
    "project/lane · viral N" tag (2026-07-07). The rest of the optional
    drafting metadata (engagement_style, style_description, search_topic,
    language, link_source, link_keyword, remaining experiments) feeds a
    second eye on that same row whose popover explains how the draft was
    made; experiments other than "lane" render generically, one line per
    active experiment/scenario arm.
    on_decision(decision) fires the instant each card is approved/rejected (so an
    approved draft posts right away); on_complete(decisions) fires when the user
    finishes the last card or closes the window. Both run on the main thread.
    focus=True (user-initiated open, e.g. the menu bar's "Review N pending
    drafts") activates the app and seats the caret in the reply field; the
    default False shows the card without taking keyboard focus."""
    global _active
    if not drafts:
        if on_complete is not None:
            on_complete([])
        return
    _active = _ReviewController.alloc().initWithDrafts_onDecision_onComplete_focus_(
        drafts, on_decision, on_complete, focus
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


def prune_active(ns):
    """Remove not-yet-reached drafts (by plan index `n`) from the open review
    card, if one is up -- e.g. a card that expired on the backend mid-review
    (see merge_review_queue.py's backend sync). Never touches the card
    currently on screen or any already-decided one, so nothing visible is
    yanked out from under the user. Returns the count actually removed (0 if
    no card is open or none of `ns` are still ahead in the stack). Main thread
    only (called from the menu bar's rumps timer)."""
    if _active is None or not ns:
        return 0
    try:
        return _active.prune_drafts(ns)
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


def dismiss_active():
    """Force-close the open review panel WITHOUT firing on_decision/on_complete,
    for a bulk discard whose fate for every remaining card was already decided
    elsewhere (the menu bar's store-level "Discard all pending drafts"). A
    normal windowShouldClose_ close still fires on_complete (leftover cards are
    just undecided); this path skips both callbacks entirely so the bulk
    discard's own bookkeeping is the only thing that runs. Returns True if a
    panel was actually open."""
    global _active
    c = _active
    if c is None or c._panel is None:
        return False
    try:
        c._close_stats_popover()
    except Exception:
        pass
    try:
        c._panel.setDelegate_(None)
        c._panel.close()
    except Exception:
        pass
    c._panel = None
    c._on_complete = None
    c._on_decision = None
    _active = None
    _log(f"closed: dismissed (bulk discard, {len(c._decisions)} decided of {len(c._drafts)})")
    _write_review_state(last_event="dismissed")
    return True


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


def focus_active():
    """Bring the open card to the user deliberately (menu 'Review pending
    drafts' while a card is already up): activate + caret in the reply field.
    Main thread only. Returns True if a card was focused."""
    c = _active
    if c is None or c._panel is None:
        return False
    try:
        c.focusReply_(None)
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


# Bulk-discard hook for the title bar's "Discard all…" button, same
# registration pattern as _feedback_handler above. The menu bar registers
# _discard_all_pending here (it owns the confirmation alert, the store flip,
# and dismissing the panel). Cards built while this is None simply don't get
# the button.
_discard_all_handler = None


def set_discard_all_handler(cb):
    """Register the title-bar "Discard all…" action. The menu bar calls this
    before presenting cards with its bulk-discard handler."""
    global _discard_all_handler
    _discard_all_handler = cb


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
