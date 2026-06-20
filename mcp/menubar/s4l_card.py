"""Corner pop-up review cards for draft approval (AppKit / pyobjc).

`present_review(drafts, on_complete)` shows one small floating panel per draft in
the top-right corner: thread context, an EDITABLE reply field, a counter, and
Reject / Approve (+ Reject all). Advancing through every card calls on_complete
with the decisions. The whole AppKit surface is isolated behind that one
function so the menu bar wiring doesn't depend on the windowing details.

Decision shape: {"n": int, "approved": bool, "text": str, "edited": bool}

Must be driven on the main thread (the menu bar's rumps timer is on the main
run loop, so that holds).
"""

import objc
from Foundation import NSObject, NSMakeRect
from AppKit import (
    NSPanel,
    NSButton,
    NSTextField,
    NSTextView,
    NSScrollView,
    NSScreen,
    NSColor,
    NSFont,
    NSView,
    NSBackingStoreBuffered,
    NSFloatingWindowLevel,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskUtilityWindow,
    NSLineBreakByWordWrapping,
    NSBezelStyleRounded,
)

# Strong reference to the live controller so pyobjc doesn't GC it mid-review
# (the classic "button click crashes" footgun).
_active = None

W = 380
H = 300
M = 16
NS_BEZEL_BORDER = 2  # NSBezelBorder


def _truncate(s, n=320):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


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
    def initWithDrafts_onComplete_(self, drafts, on_complete):
        self = objc.super(_ReviewController, self).init()
        if self is None:
            return None
        self._drafts = list(drafts)
        self._on_complete = on_complete
        self._idx = 0
        self._decisions = []
        self._panel = None
        self._textview = None
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
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
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

        # "Replying to @author:" — bold, black.
        author = d.get("thread_author") or "thread"
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 70, W - 2 * M, 18),
                f"Replying to {author}:",
                size=12,
                bold=True,
            )
        )
        # Thread text — black.
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 150, W - 2 * M, 74),
                _truncate(d.get("thread_text")),
                size=12,
            )
        )
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
        tv.setString_(composed)
        scroll.setDocumentView_(tv)
        content.addSubview_(scroll)
        self._textview = tv

        self._panel.setContentView_(content)
        self._panel.setTitle_("")

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
        if approved:
            text = self._current_text()
            # Strip the link we folded into the field so the pipeline mints the
            # tracked short link itself (avoids a double link / bare URL).
            if link and text.rstrip().endswith(link):
                text = text.rstrip()[: -len(link)]
            body = text.strip()
        else:
            body = orig
        self._decisions.append(
            {
                "n": d["n"],
                "approved": bool(approved),
                "text": body,
                "edited": bool(approved and body != orig),
            }
        )

    @objc.python_method
    def _advance(self):
        self._idx += 1
        if self._idx >= len(self._drafts):
            self._finish()
        else:
            self._render()

    # ObjC selectors (trailing underscore -> "approve:" etc.)
    def approve_(self, sender):
        self._record(True)
        self._advance()

    def reject_(self, sender):
        self._record(False)
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


def present_review(drafts, on_complete):
    """Show the review cards (main thread only). drafts: list of
    {n, thread_author, thread_text, reply_text, link_url}. on_complete(decisions)
    fires on the main thread when the user finishes or closes the window."""
    global _active
    if not drafts:
        on_complete([])
        return
    _active = _ReviewController.alloc().initWithDrafts_onComplete_(drafts, on_complete)
