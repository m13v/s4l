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
H = 340
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
        total = len(self._drafts)
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))

        content.addSubview_(
            _label(
                NSMakeRect(M, H - 40, W - 2 * M, 18),
                f"Review draft  {self._idx + 1}/{total}",
                size=13,
                bold=True,
            )
        )
        author = d.get("thread_author") or "thread"
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 62, W - 2 * M, 16),
                f"Replying to {author}:",
                size=11,
                muted=True,
            )
        )
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 148, W - 2 * M, 82),
                _truncate(d.get("thread_text")),
                size=11,
                muted=True,
            )
        )
        content.addSubview_(
            _label(
                NSMakeRect(M, H - 170, W - 2 * M, 16),
                "Your reply (editable):",
                size=11,
                bold=True,
            )
        )

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(M, 64, W - 2 * M, 96))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NS_BEZEL_BORDER)
        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 2 * M, 96))
        tv.setFont_(NSFont.systemFontOfSize_(12))
        tv.setRichText_(False)
        tv.setEditable_(True)
        tv.setString_(d.get("reply_text") or "")
        scroll.setDocumentView_(tv)
        content.addSubview_(scroll)
        self._textview = tv

        if d.get("link_url"):
            content.addSubview_(
                _label(
                    NSMakeRect(M, 44, W - 2 * M, 14),
                    f"link: {d['link_url']}",
                    size=10,
                    muted=True,
                )
            )

        approve = NSButton.alloc().initWithFrame_(NSMakeRect(W - M - 96, 12, 96, 28))
        approve.setTitle_("Approve")
        approve.setBezelStyle_(NSBezelStyleRounded)
        approve.setTarget_(self)
        approve.setAction_("approve:")
        content.addSubview_(approve)

        reject = NSButton.alloc().initWithFrame_(NSMakeRect(W - M - 96 - 8 - 84, 12, 84, 28))
        reject.setTitle_("Reject")
        reject.setBezelStyle_(NSBezelStyleRounded)
        reject.setTarget_(self)
        reject.setAction_("reject:")
        content.addSubview_(reject)

        reject_all = NSButton.alloc().initWithFrame_(NSMakeRect(M, 12, 96, 28))
        reject_all.setTitle_("Reject all")
        reject_all.setBezelStyle_(NSBezelStyleRounded)
        reject_all.setTarget_(self)
        reject_all.setAction_("rejectAll:")
        content.addSubview_(reject_all)

        self._panel.setContentView_(content)
        self._panel.setTitle_(f"Review drafts  {self._idx + 1}/{total}")

    @objc.python_method
    def _current_text(self):
        try:
            return str(self._textview.string())
        except Exception:
            return self._drafts[self._idx].get("reply_text") or ""

    @objc.python_method
    def _record(self, approved):
        d = self._drafts[self._idx]
        orig = d.get("reply_text") or ""
        text = self._current_text() if approved else orig
        self._decisions.append(
            {
                "n": d["n"],
                "approved": bool(approved),
                "text": text,
                "edited": bool(approved and text.strip() != orig.strip()),
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

    def rejectAll_(self, sender):
        while self._idx < len(self._drafts):
            self._record(False)
            self._idx += 1
        self._finish()

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
