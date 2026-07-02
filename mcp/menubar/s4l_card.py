"""Corner pop-up review cards for draft approval (AppKit / pyobjc).

`present_review(drafts, on_decision, on_complete)` shows one small floating panel
per draft in the top-right corner: thread context, an EDITABLE reply field, a
counter, and Reject / Approve. `on_decision` fires the INSTANT each card is
approved/rejected (so an approved draft can post right away), and `on_complete`
fires once the last card is decided or the window is closed. The whole AppKit
surface is isolated behind that one function so the menu bar wiring doesn't
depend on the windowing details.

Decision shape: {"n": int, "approved": bool, "text": str, "edited": bool,
"drop_link": bool, "reject_category": str|None, "reject_note": str|None,
"interactions": [{"type": str, "ts": str}], "dwell_ms": int}

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


class _ReviewPanel(NSPanel):
    """A status-bar app panel that can actually own text input."""

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True


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
        self._build()
        return self

    @objc.python_method
    def _build(self):
        screen = NSScreen.mainScreen()
        vf = screen.visibleFrame() if screen is not None else NSMakeRect(0, 0, 1440, 900)
        x = vf.origin.x + vf.size.width - W - M
        y = vf.origin.y + vf.size.height - H - M
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskUtilityWindow
        )
        panel = _ReviewPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H), style, NSBackingStoreBuffered, False
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
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))

        # Buttons at the TOP: Approve (left), Reject (right).
        approve = NSButton.alloc().initWithFrame_(NSMakeRect(M, H - 42, 110, 30))
        approve.setTitle_("Approve")
        approve.setBezelStyle_(NSBezelStyleRounded)
        approve.setTarget_(self)
        approve.setAction_("approve:")
        content.addSubview_(approve)

        reject = NSButton.alloc().initWithFrame_(NSMakeRect(W - M - 110, H - 42, 110, 30))
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
                        # NSTextView's default clickedOnLink handler opens the
                        # URL via NSWorkspace; no delegate needed.
                        NSLinkAttributeName: NSURL.URLWithString_(thread_url),
                    },
                )
            )
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
        self._show_stats_popover()

    # NSTrackingArea owner callbacks (hover over the eye icon).
    def mouseEntered_(self, event):
        _log("eye hover enter")
        self._show_stats_popover()

    def mouseExited_(self, event):
        _log("eye hover exit")
        self._close_stats_popover()

    @objc.python_method
    def _add_link(self, content, frame, text, url, *, size=12, bold=False, right=False):
        """Borderless button styled as a link (system link color, underlined).
        The URL rides on the button's integer tag via _link_targets so one
        openLink: selector serves every link on the card."""
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
        self._link_targets[tag] = str(url)
        btn.setTarget_(self)
        btn.setAction_("openLink:")
        content.addSubview_(btn)
        return btn

    def openLink_(self, sender):
        try:
            url = self._link_targets.get(sender.tag())
            if url:
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
        except Exception:
            pass

    @objc.python_method
    def _current_text(self):
        try:
            return str(self._textview.string())
        except Exception:
            return self._drafts[self._idx].get("reply_text") or ""

    @objc.python_method
    def _record(self, approved):
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
                "text": body,
                "edited": bool(approved and body != orig),
                "drop_link": bool(approved and drop_link),
            }
        )

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

    def reject_(self, sender):
        self._record(False)
        self._fire_decision()
        self._advance()

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
