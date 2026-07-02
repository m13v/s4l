"""Corner pop-up review cards for draft approval (AppKit / pyobjc).

`present_review(drafts, on_decision, on_complete)` shows one small floating panel
per draft in the top-right corner: thread context, an EDITABLE reply field, a
counter, and Reject / Approve. `on_decision` fires the INSTANT each card is
approved/rejected (so an approved draft can post right away), and `on_complete`
fires once the last card is decided or the window is closed. The whole AppKit
surface is isolated behind that one function so the menu bar wiring doesn't
depend on the windowing details.

Decision shape: {"n": int, "approved": bool, "text": str, "edited": bool, "drop_link": bool}

Must be driven on the main thread (the menu bar's rumps timer is on the main
run loop, so that holds).
"""

import re

import objc
from Foundation import NSObject, NSMakeRect, NSAttributedString, NSURL
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
    NSTextAlignmentLeft,
    NSTextAlignmentRight,
)

# Strong reference to the live controller so pyobjc doesn't GC it mid-review
# (the classic "button click crashes" footgun).
_active = None

W = 380
H = 336
M = 16
NS_BEZEL_BORDER = 2  # NSBezelBorder


class _ReviewPanel(NSPanel):
    """A status-bar app panel that can actually own text input."""

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True


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


def _stats_line(stats):
    """One muted line from the discovery-time candidate stats: author followers
    (profile stat) then the thread's engagement counts. Fields the pipeline
    didn't capture are simply omitted; returns '' when nothing is known."""
    stats = stats or {}
    parts = []
    followers = _fmt_count(stats.get("author_followers"))
    if followers is not None:
        parts.append(f"{followers} followers")
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
        # profile, and "View thread" (right) opens the thread being replied to.
        # Both use data the pipeline already carries; no scraping happens here.
        self._link_targets = {}
        handle = (d.get("thread_author") or "").lstrip("@").strip()
        content.addSubview_(
            _label(NSMakeRect(M, H - 70, 78, 18), "Replying to", size=12, bold=True)
        )
        if handle:
            self._add_link(
                content,
                NSMakeRect(M + 78, H - 70, W - 2 * M - 78 - 88, 18),
                f"@{handle}",
                f"https://x.com/{handle}",
                bold=True,
            )
        else:
            content.addSubview_(
                _label(NSMakeRect(M + 78, H - 70, W - 2 * M - 78 - 88, 18), "thread", size=12, bold=True)
            )
        thread_url = d.get("thread_url")
        if thread_url:
            self._add_link(
                content,
                NSMakeRect(W - M - 88, H - 70, 88, 18),
                "View thread",
                thread_url,
                right=True,
            )
        # Discovery-time stats: author followers + thread engagement — muted.
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 88, W - 2 * M, 14),
                _stats_line(d.get("stats")),
                size=11,
                muted=True,
            )
        )
        # Thread text — black.
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 168, W - 2 * M, 74),
                _truncate(d.get("thread_text")),
                size=12,
            )
        )
        # Reply heading — black.
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 190, W - 2 * M, 16),
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
            NSMakeRect(M, M, W - 2 * M, H - 190 - M - 6)
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
    {n, thread_author, thread_text, reply_text, link_url}.
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
