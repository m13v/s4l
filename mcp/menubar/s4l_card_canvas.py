"""Large centered grid of review cards (AppKit / pyobjc).

`present_review_canvas(drafts, on_decision, on_complete, focus)` is the
canvas counterpart to `s4l_card.present_review`: same `drafts` input shape
(see s4l_state.review_drafts), same decision-dict contract fired via
on_decision/on_complete -- the menu bar's existing wiring
(_on_card_decision / _on_review_closed) drives either surface unchanged.

Where the corner card walks the backlog one draft at a time, this shows
SEVERAL drafts at once in a large centered window, arranged as a grid of
tiles. The only real difference from the corner card is that difference --
"several at once instead of one at a time" -- so each tile IS a real
s4l_card._ReviewController, mounted into a fixed-size grid slot via its
host_view/host_window init variant instead of its own floating panel (see
that class's initWithDrafts_onDecision_onComplete_focus_hostView_hostWindow_
and the _mount_content/_seat_first_responder split it uses internally).
Approve, the loved-emoji row, the full two-step reject-reason picker, the
eye/stats/details popovers, draft A/B click-to-arm, translations, the live
expiry countdown -- all of it is the EXACT same code the corner card runs,
not a re-derived approximation. This module owns only the grid/backlog
bookkeeping around that: which draft sits in which slot, and refilling a
slot from the backlog the instant its tile is decided (2026-07-16 product
direction: "the only difference is we show multiple cards at the same
time... maybe the cards should even be the same format").

Each tile is single-draft (drafts=[one]), so a tile's own on_complete fires
immediately after its on_decision -- that firing IS the "this slot is free,
put the next backlog draft here" signal (_fill_slot). A tile's on_decision
forwards to this controller's own on_decision (so posting starts right
away, same as the corner card) and appends to the running self._decisions
list; the OUTER on_complete (this module's own contract with the menu bar)
fires once, when the whole canvas window closes, carrying every decision
made across every tile shown during the session -- never confused with a
single tile's internal completion.

No checkboxes, no "Select all", no bulk Approve/Discard buttons (all
present in an earlier custom-rendered version of this file; removed
2026-07-16 per product direction -- selection now happens by acting on a
specific tile directly, exactly like the corner card, and only one draft
per thread can ever be armed since each tile owns exactly one thread).
The header shows "Showing N of Total pending" (visible tiles vs. visible +
backlog) instead of a selection count.

The backlog is sorted by stats.virality_score descending when a candidate
has one (Twitter only, today, per score_twitter_candidates.py); candidates
without a score sort as -1 and keep review_drafts()'s existing newest-first
order among themselves (Python's stable sort) at the bottom of the queue --
no new scoring is invented for platforms that don't have one.

Must be driven on the main thread (mirrors s4l_card.py).
"""

import time

import objc
from Foundation import NSObject, NSMakeRect, NSMakeSize
from AppKit import (
    NSApp,
    NSScrollView,
    NSView,
    NSWindowOcclusionStateVisible,
    NSBackingStoreBuffered,
    NSNormalWindowLevel,
    NSApplicationActivationPolicyAccessory,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskUtilityWindow,
    NSTextAlignmentCenter,
)

# Reuse the corner card's panel class (Cmd+V/C/X/A/Z routing for a status-bar
# app with no Edit menu), its per-card controller, and its tile dimensions
# instead of duplicating any of them -- this module is a second
# PRESENTATION only; the drafts data, decision contract, and every bit of
# actual card UI/behavior stay single-sourced in s4l_card.py.
from s4l_card import (
    _ReviewPanel,
    _ReviewController,
    _label,
    _frosted,
    _mouse_screen,
    _write_review_state,
    _log,
    NSWindowStyleMaskNonactivatingPanel,
    W as TILE_W,
    H as TILE_H,
)

# Strong reference to the live controller so pyobjc doesn't GC it mid-review,
# mirroring s4l_card._active. Separate global -- only one of the two surfaces
# is ever open at a time (the menu bar's _review_active/_panel_open flags
# already enforce that), but each module owns its own reference regardless.
_active = None

WIN_MARGIN = 20.0
HEADER_H = 40.0
GRID_GAP = 16.0
GRID_COLS = 3
GRID_PAD = 14.0


def _sort_key(d):
    """virality_score descending when present (Twitter only today); -1 for
    everything else so unscored candidates sort last, keeping their existing
    review_drafts() order among themselves (stable sort). No score is
    invented for platforms that don't have one."""
    v = ((d or {}).get("stats") or {}).get("virality_score")
    try:
        return float(v)
    except (TypeError, ValueError):
        return -1.0


def _center_frame(screen):
    """Large centered frame on the given screen -- the canvas is a
    deliberate review session, not a glanceable corner widget."""
    vf = screen.visibleFrame() if screen is not None else NSMakeRect(0, 0, 1440, 900)
    w = min(1180.0, vf.size.width * 0.88)
    h = vf.size.height * 0.86
    x = vf.origin.x + (vf.size.width - w) / 2.0
    y = vf.origin.y + (vf.size.height - h) / 2.0
    return NSMakeRect(x, y, w, h)


class _FlippedView(NSView):
    """Top-down document view: origin at the top-left, y grows downward.
    Makes the grid's row math simple arithmetic, and the scroll view opens
    showing the top of the grid by default."""

    def isFlipped(self):
        return True


class _CanvasController(NSObject):
    def initWithDrafts_onDecision_onComplete_focus_(
        self, drafts, on_decision, on_complete, focus
    ):
        self = objc.super(_CanvasController, self).init()
        if self is None:
            return None
        self._backlog = sorted(list(drafts), key=_sort_key, reverse=True)
        self._on_decision = on_decision
        self._on_complete = on_complete
        self._focus = bool(focus)
        self._decisions = []  # every decision from every tile, whole session
        self._slots = []  # [{"view": NSView, "tile": _ReviewController|None, "n": int|None}]
        self._panel = None
        self._scroll = None
        self._doc = None
        self._count_label = None
        self._presented_at = time.time()
        self._last_decision_at = None
        self._last_interaction_at = None
        self._last_move_log = 0.0
        self._build()
        return self

    # ---- surface lifecycle -------------------------------------------------

    @objc.python_method
    def _build(self):
        frame = _center_frame(_mouse_screen())
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskUtilityWindow
            | NSWindowStyleMaskNonactivatingPanel
        )
        panel = _ReviewPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        # `frame` here IS the content rect (that's what initWithContentRect_
        # takes) -- panel.frame() later would instead return the WINDOW
        # frame, taller by the title bar's height, and sizing the content
        # view to that overlapped the header row under the title bar
        # (2026-07-16 user report). Keep the real content size around for
        # _render().
        self._content_size = NSMakeSize(frame.size.width, frame.size.height)
        # Normal window level, NOT floating (2026-07-16 user report): this is
        # a large, deliberate review session the reviewer works IN, unlike
        # the corner card's small always-on-top notification -- it should
        # behave like any other window (other apps can come to front over
        # it) rather than staying glued above everything.
        panel.setLevel_(NSNormalWindowLevel)
        panel.setFloatingPanel_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setDelegate_(self)
        self._panel = panel
        self._render()
        panel.orderFrontRegardless()
        self._log_surface("presented")
        if self._focus:
            try:
                NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
                NSApp.activateIgnoringOtherApps_(True)
            except Exception:
                pass
            panel.makeKeyAndOrderFront_(None)
            panel.orderFrontRegardless()

    @objc.python_method
    def _render(self):
        w, h = self._content_size.width, self._content_size.height
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))

        header_y = h - HEADER_H
        self._count_label = _label(
            NSMakeRect(WIN_MARGIN, header_y + 10, w - 2 * WIN_MARGIN, 20),
            "",
            size=13,
            bold=True,
        )
        content.addSubview_(self._count_label)

        body_y = WIN_MARGIN
        body_h = header_y - WIN_MARGIN - 8
        body_w = w - 2 * WIN_MARGIN
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(WIN_MARGIN, body_y, body_w, body_h)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        content.addSubview_(scroll)
        self._scroll = scroll

        self._build_grid(scroll)

        self._panel.setContentView_(_frosted(content))
        self._refresh_header()

    @objc.python_method
    def _build_grid(self, scroll):
        cs = scroll.contentSize()
        doc_w = cs.width
        # As many columns as fit, up to GRID_COLS; as many rows as fit the
        # visible height (a small screen just gets fewer slots, not smaller
        # tiles -- tiles stay the corner card's own fixed W x H so nothing
        # about the reused rendering code has to change).
        cols = max(1, min(GRID_COLS, int((doc_w + GRID_GAP) // (TILE_W + GRID_GAP))))
        rows = max(1, int((cs.height - 2 * GRID_PAD + GRID_GAP) // (TILE_H + GRID_GAP)))
        grid_w = cols * TILE_W + (cols - 1) * GRID_GAP
        left_pad = max(0.0, (doc_w - grid_w) / 2.0)
        doc_h = max(cs.height, rows * TILE_H + (rows - 1) * GRID_GAP + 2 * GRID_PAD)
        doc = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, doc_w, doc_h))
        self._doc = doc

        self._slots = []
        for i in range(cols * rows):
            r, c = divmod(i, cols)
            x = left_pad + c * (TILE_W + GRID_GAP)
            y = GRID_PAD + r * (TILE_H + GRID_GAP)
            slot_view = NSView.alloc().initWithFrame_(NSMakeRect(x, y, TILE_W, TILE_H))
            doc.addSubview_(slot_view)
            self._slots.append({"view": slot_view, "tile": None, "n": None})

        scroll.setDocumentView_(doc)
        for i in range(len(self._slots)):
            self._fill_slot(i)

    # ---- backlog / slot management ------------------------------------------

    @objc.python_method
    def _fill_slot(self, slot_idx):
        """Mount the next backlog draft into this slot, or leave it empty
        with a placeholder if the backlog is exhausted. Called at startup
        for every slot, and again for one slot the instant its tile is
        decided (a tile's on_complete IS that signal, see
        s4l_card._ReviewController -- single-draft, so its own on_complete
        fires right after its on_decision)."""
        slot = self._slots[slot_idx]
        for sv in list(slot["view"].subviews()):
            sv.removeFromSuperview()
        if not self._backlog:
            slot["tile"] = None
            slot["n"] = None
            self._show_empty_slot(slot["view"])
            self._refresh_header()
            return
        d = self._backlog.pop(0)
        tile = _ReviewController.alloc().initWithDrafts_onDecision_onComplete_focus_hostView_hostWindow_(
            [d],
            self._tile_decision_cb(slot_idx),
            self._tile_complete_cb(slot_idx),
            True,
            slot["view"],
            self._panel,
        )
        slot["tile"] = tile
        slot["n"] = d.get("n")
        self._refresh_header()

    @objc.python_method
    def _show_empty_slot(self, view):
        w, h = view.frame().size.width, view.frame().size.height
        lbl = _label(
            NSMakeRect(0, h / 2.0 - 10, w, 20),
            "No more drafts",
            size=12,
            muted=True,
        )
        try:
            from AppKit import NSTextAlignmentCenter

            lbl.setAlignment_(NSTextAlignmentCenter)
        except Exception:
            pass
        view.addSubview_(lbl)

    @objc.python_method
    def _tile_decision_cb(self, slot_idx):
        def _cb(decision):
            self._decisions.append(decision)
            self._last_decision_at = time.time()
            cb = self._on_decision
            if cb is not None:
                try:
                    cb(dict(decision))
                except Exception:
                    pass

        return _cb

    @objc.python_method
    def _tile_complete_cb(self, slot_idx):
        def _cb(_tile_decisions):
            # The tile's own single-draft stack finished -- free the slot
            # and immediately pull in the next backlog draft, exactly per
            # 2026-07-16 product direction ("it gets taken away from the
            # window, and other cards move into its place").
            self._fill_slot(slot_idx)

        return _cb

    @objc.python_method
    def _known_ns(self):
        known = {dec.get("n") for dec in self._decisions}
        known.update(d.get("n") for d in self._backlog)
        known.update(s["n"] for s in self._slots if s["n"] is not None)
        return known

    @objc.python_method
    def _refresh_header(self):
        showing = sum(1 for s in self._slots if s["tile"] is not None)
        total = showing + len(self._backlog)
        try:
            self._count_label.setStringValue_(f"Showing {showing} of {total} pending")
        except Exception:
            pass
        try:
            self._panel.setTitle_(f"s4l · Review {total} drafts (canvas)")
        except Exception:
            pass

    # ---- live backlog changes -------------------------------------------------

    @objc.python_method
    def extend_drafts(self, drafts):
        """Append newly-queued drafts to the backlog, refilling any empty
        slots immediately (the backlog was exhausted, now it isn't).
        Existing slots/tiles are never touched -- an in-progress edit on
        screen is never disturbed, mirroring the corner card's own
        extend_drafts."""
        if self._panel is None:
            return 0
        have = self._known_ns()
        added = [d for d in drafts if d.get("n") not in have]
        if not added:
            return 0
        added.sort(key=_sort_key, reverse=True)
        self._backlog.extend(added)
        for i, slot in enumerate(self._slots):
            if slot["tile"] is None and self._backlog:
                self._fill_slot(i)
        self._refresh_header()
        self._log_surface("extended")
        return len(added)

    @objc.python_method
    def prune_drafts(self, ns):
        """Drop not-yet-shown drafts whose plan index `n` is in `ns` (a
        candidate that expired backend-side mid-review) from the backlog
        only -- never a currently-visible tile, mirroring the corner card's
        own prune_drafts ("never touches the card currently on screen")."""
        if self._panel is None or not ns:
            return 0
        ns = set(ns)
        before = len(self._backlog)
        self._backlog = [d for d in self._backlog if d.get("n") not in ns]
        removed = before - len(self._backlog)
        if removed:
            self._refresh_header()
            self._log_surface("pruned")
        return removed

    # ---- status / observability (same shape as s4l_card, see _write_review_state) --

    @objc.python_method
    def _occlusion_visible(self):
        try:
            return bool(self._panel.occlusionState() & NSWindowOcclusionStateVisible)
        except Exception:
            return None

    @objc.python_method
    def status_dict(self):
        frame = None
        try:
            fr = self._panel.frame()
            frame = [int(fr.origin.x), int(fr.origin.y), int(fr.size.width), int(fr.size.height)]
        except Exception:
            pass
        screen_name = None
        try:
            scr = self._panel.screen()
            if scr is not None:
                screen_name = str(scr.localizedName())
        except Exception:
            pass
        showing = sum(1 for s in self._slots if s["tile"] is not None)
        pending = showing + len(self._backlog)
        return {
            "open": self._panel is not None,
            "total": pending,
            "pending": pending,
            "decided": len(self._decisions),
            "presented_at": self._presented_at,
            "last_decision_at": self._last_decision_at,
            "last_interaction_at": self._last_interaction_at,
            "occlusion_visible": self._occlusion_visible(),
            "frame": frame,
            "screen": screen_name,
            "layout": "canvas",
        }

    @objc.python_method
    def _log_surface(self, event):
        s = self.status_dict()
        _log(
            f"canvas {event}: {s['pending']} pending, "
            f"frame={s['frame']} screen={s['screen']} visible={s['occlusion_visible']}"
        )
        _write_review_state(controller=self, last_event=event)

    def windowDidMove_(self, notification):
        now = time.time()
        if now - self._last_move_log < 1.0:
            return
        self._last_move_log = now
        self._log_surface("moved")

    def windowDidChangeOcclusionState_(self, notification):
        self._log_surface("occlusion_changed")

    def windowShouldClose_(self, sender):
        # Closing leaves undecided (still-showing or backlog) drafts
        # pending; the menu bar treats this exactly like an unfinished
        # corner-card stack (snooze, re-present later), same as the corner
        # card's own windowShouldClose_.
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
        _log(f"canvas closed: {len(self._decisions)} decided")
        _write_review_state(controller=self, last_event="closed")


# ---- module-level API, mirrors s4l_card.py's public surface -----------------


def present_review_canvas(drafts, on_decision=None, on_complete=None, focus=False):
    """Show the canvas review surface (main thread only). Same `drafts`
    shape and decision contract as s4l_card.present_review -- see that
    docstring and this module's for the differences. focus=True (the menu's
    "Review N pending drafts") activates the app; a fresh/auto presentation
    does not steal focus, same posture as the corner card."""
    global _active
    if not drafts:
        if on_complete is not None:
            on_complete([])
        return
    _active = _CanvasController.alloc().initWithDrafts_onDecision_onComplete_focus_(
        drafts, on_decision, on_complete, focus
    )


def extend_active(drafts):
    if _active is None:
        return 0
    try:
        return _active.extend_drafts(drafts)
    except Exception:
        return 0


def prune_active(ns):
    if _active is None or not ns:
        return 0
    try:
        return _active.prune_drafts(ns)
    except Exception:
        return 0


def active_status():
    c = _active
    if c is None or c._panel is None:
        return None
    try:
        return c.status_dict()
    except Exception:
        return None


def heal_active():
    """Move the open canvas window to the pointer's current screen and raise
    it. Returns True if a window was open."""
    c = _active
    if c is None or c._panel is None:
        return False
    try:
        c._panel.setFrame_display_(_center_frame(_mouse_screen()), True)
        c._panel.orderFrontRegardless()
        c._log_surface("healed")
        return True
    except Exception:
        return False


def focus_active():
    c = _active
    if c is None or c._panel is None:
        return False
    try:
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:
        pass
    try:
        c._panel.makeKeyAndOrderFront_(None)
        c._panel.orderFrontRegardless()
        c._panel.makeMainWindow()
        return True
    except Exception:
        return False


def dismiss_active():
    """Force-close the open canvas WITHOUT firing on_decision/on_complete,
    for a bulk discard whose fate for every remaining draft was already
    decided elsewhere (mirrors s4l_card.dismiss_active). Returns True if a
    window was actually open."""
    global _active
    c = _active
    if c is None or c._panel is None:
        return False
    try:
        c._panel.setDelegate_(None)
        c._panel.close()
    except Exception:
        pass
    c._panel = None
    c._on_complete = None
    c._on_decision = None
    _active = None
    _log(f"canvas closed: dismissed (bulk discard, {len(c._decisions)} decided)")
    _write_review_state(controller=c, last_event="dismissed")
    return True
