"""Large centered grid of review cards (AppKit / pyobjc).

`present_review_canvas(drafts, on_decision, on_complete, focus)` is the canvas
counterpart to `s4l_card.present_review`: same `drafts` input shape (see
s4l_state.review_drafts), same decision-dict contract fired via
on_decision/on_complete -- the menu bar's existing wiring
(_on_card_decision / _on_review_closed) drives either surface unchanged.

Where the corner card walks the backlog one draft at a time, this shows
EVERY pending draft at once in a large centered window, arranged as a grid
of tiles, ranked highest-first, scrolling when there are more than fit on
screen. The only real difference from the corner card is that difference --
"all at once instead of one at a time" -- so each tile IS a real
s4l_card._ReviewController, mounted into a grid slot via its
host_view/host_window init variant instead of its own floating panel (see
that class's initWithDrafts_onDecision_onComplete_focus_hostView_hostWindow_
and the _mount_content/_seat_first_responder split it uses internally).
Approve, the loved-emoji row, the full two-step reject-reason picker, the
eye/stats/details popovers, draft A/B click-to-arm, translations, the live
expiry countdown -- all of it is the EXACT same code the corner card runs,
not a re-derived approximation. This module owns only the grid bookkeeping
around that: `self._order` is every currently-known pending draft, ranked,
1:1 with `self._slots`.

When a tile is decided, it's popped from self._order and every slot AFTER
it is rebuilt one position earlier -- cards shift into the gap in rank
order like Snake, rather than each slot independently refilling in place
(2026-07-16 product direction). The grid shrinks by one slot each time (no
artificial capacity to backfill); extend_drafts grows it by appending new
slots at the end. There is no separate "backlog" -- everything the canvas
knows about is always laid out in the grid; the scroll view (always
present) handles whatever doesn't fit the window.

Each tile is single-draft (drafts=[one]), so a tile's own on_complete fires
immediately after its on_decision -- that firing IS the "remove this one
and reflow" signal. A tile's on_decision forwards to this controller's own
on_decision (so posting starts right away, same as the corner card) and
appends to the running self._decisions list; the OUTER on_complete (this
module's own contract with the menu bar) fires once, when the whole canvas
window closes, carrying every decision made across every tile shown during
the session -- never confused with a single tile's internal completion.

No checkboxes, no "Select all" (selection happens by acting on a specific
tile directly, exactly like the corner card, and only one draft per thread
can ever be armed since each tile owns exactly one thread). Approve All /
Discard All live as two buttons inline in the header row, next to the
pending count -- not their own dedicated card, not a bulk-select flow.

Rows are ranked by stats.virality_score descending when a candidate has one
(Twitter only, today, per score_twitter_candidates.py); candidates without a
score sort as -1 and keep review_drafts()'s existing newest-first order
among themselves (Python's stable sort) at the bottom of the list -- no new
scoring is invented for platforms that don't have one.

Must be driven on the main thread (mirrors s4l_card.py).
"""

import time

import objc
from Foundation import NSObject, NSMakeRect, NSMakeSize
from AppKit import (
    NSApp,
    NSButton,
    NSColor,
    NSFont,
    NSScrollView,
    NSView,
    NSWindowOcclusionStateVisible,
    NSBackingStoreBuffered,
    NSNormalWindowLevel,
    NSApplicationActivationPolicyAccessory,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskUtilityWindow,
    NSWindowStyleMaskResizable,
    NSBezelStyleRounded,
    NSViewWidthSizable,
    NSViewHeightSizable,
    NSViewMinXMargin,
    NSViewMinYMargin,
)

# Reuse the corner card's panel class (Cmd+V/C/X/A/Z routing for a status-bar
# app with no Edit menu), its per-card controller, its styling helpers, and
# its tile dimensions instead of duplicating any of them -- this module is a
# second PRESENTATION only; the drafts data, decision contract, and every
# bit of actual card UI/behavior stay single-sourced in s4l_card.py.
from s4l_card import (
    _ReviewPanel,
    _ReviewController,
    _label,
    _round_rect,
    _fill_color,
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

# Bulk-action hooks for the header's two buttons, same registration pattern
# as s4l_card.py's set_discard_all_handler. The menu bar registers BOTH
# modules' set_discard_all_handler with the SAME _discard_all_pending (it's
# already surface-agnostic -- reads the store, not the card UI) before
# presenting either surface; set_approve_all_handler has no corner-card
# equivalent to mirror since "approve everything with no review" only makes
# sense once several drafts are visible at once. A canvas built while either
# is None simply has an inert button.
_discard_all_handler = None
_approve_all_handler = None


def set_discard_all_handler(cb):
    global _discard_all_handler
    _discard_all_handler = cb


def set_approve_all_handler(cb):
    global _approve_all_handler
    _approve_all_handler = cb


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
    deliberate review session, not a glanceable corner widget. Width scales
    with the screen (2026-07-16 user report: it should adapt, not sit at a
    fixed size), bounded so it never gets absurdly wide on a huge display
    nor too cramped to fit GRID_COLS columns on a typical one -- 3 columns
    of TILE_W=380 tiles + GRID_GAP=16 gaps need ~1172px of grid width alone,
    so a cap below ~1250 silently drops to 2 columns even on a normal
    laptop screen (the exact bug reported)."""
    vf = screen.visibleFrame() if screen is not None else NSMakeRect(0, 0, 1440, 900)
    w = min(1320.0, max(900.0, vf.size.width * 0.85))
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
        # Every currently-known pending draft, ranked, 1:1 with self._slots.
        # No separate "backlog" -- everything the canvas knows about is
        # always laid out in the grid (2026-07-16 product direction); the
        # scroll view handles whatever doesn't fit the window.
        self._order = sorted(list(drafts), key=_sort_key, reverse=True)
        self._on_decision = on_decision
        self._on_complete = on_complete
        self._focus = bool(focus)
        self._decisions = []  # every decision from every tile, whole session
        self._slots = []  # [{"view": NSView, "tile": _ReviewController|None, "n": int|None}]
        self._cols = GRID_COLS
        self._doc_w = 0.0
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
            | NSWindowStyleMaskResizable
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
        # Don't let a resize (user drag or heal_active moving to a smaller
        # screen) shrink below one column + the header.
        try:
            panel.setContentMinSize_(NSMakeSize(TILE_W + 2 * WIN_MARGIN + 40, 420))
        except Exception:
            pass
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
        # Bulk actions live INLINE with the pending count, not their own
        # dedicated card -- a whole grid slot spent on a heading +
        # description paragraph + two big buttons was more than this needs
        # (2026-07-16 user direction: "just two buttons ... that's it").
        # Buttons on the right, count label filling the space to their left.
        btn_w, btn_h, btn_gap = 110.0, 26.0, 8.0
        btn_y = header_y + (HEADER_H - btn_h) / 2.0
        approve_x = w - WIN_MARGIN - btn_w
        discard_x = approve_x - btn_gap - btn_w

        # Header widgets pin to the TOP edge (flexible bottom margin) so a
        # window-frame change after build -- heal_active recentering on a
        # different-sized screen, or a user resize -- can never clip the
        # header under the title bar again (2026-07-16 user report: "the top
        # bar gets stuck"; layout was built once and never followed the
        # frame). Buttons additionally pin RIGHT (flexible left margin).
        discard_btn = NSButton.alloc().initWithFrame_(NSMakeRect(discard_x, btn_y, btn_w, btn_h))
        discard_btn.setTitle_("Discard All")
        discard_btn.setBezelStyle_(NSBezelStyleRounded)
        try:
            discard_btn.setHasDestructiveAction_(True)
        except Exception:
            pass
        discard_btn.setFont_(NSFont.systemFontOfSize_(12))
        discard_btn.setTarget_(self)
        discard_btn.setAction_("discardAllClicked:")
        discard_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        content.addSubview_(discard_btn)

        approve_btn = NSButton.alloc().initWithFrame_(NSMakeRect(approve_x, btn_y, btn_w, btn_h))
        approve_btn.setTitle_("Approve All")
        approve_btn.setBezelStyle_(NSBezelStyleRounded)
        approve_btn.setFont_(NSFont.systemFontOfSize_(12))
        approve_btn.setTarget_(self)
        approve_btn.setAction_("approveAllClicked:")
        approve_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        content.addSubview_(approve_btn)

        self._count_label = _label(
            NSMakeRect(WIN_MARGIN, header_y + 10, discard_x - WIN_MARGIN - 12, 20),
            "",
            size=13,
            bold=True,
        )
        self._count_label.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        content.addSubview_(self._count_label)

        body_y = WIN_MARGIN
        body_h = header_y - WIN_MARGIN - 8
        body_w = w - 2 * WIN_MARGIN
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(WIN_MARGIN, body_y, body_w, body_h)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        # OPAQUE background, deliberately (2026-07-16 scroll-perf fix): a
        # transparent scroll view can't blit already-drawn pixels on scroll
        # (copy-on-scroll and responsive-scrolling overdraw are both off for
        # it), so every wheel tick re-rendered the entire visible grid --
        # dozens of tiles, each with 2-3 nested text views -- and scrolling
        # visibly lagged. Same reason the canvas window skips the corner
        # card's _frosted vibrancy underlay below: compositing the whole
        # grid over a live behind-window blur was most of the per-frame
        # cost, for an effect barely visible behind a full grid of tiles.
        scroll.setDrawsBackground_(True)
        try:
            scroll.setBackgroundColor_(NSColor.windowBackgroundColor())
        except Exception:
            pass
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        content.addSubview_(scroll)
        self._scroll = scroll

        self._build_grid(scroll)

        # Plain content view, no _frosted wrapper (see the opaque-background
        # comment above). NSPanel's own windowBackgroundColor fills the rest.
        self._panel.setContentView_(content)
        self._refresh_header()

    # ---- grid layout ---------------------------------------------------------
    # One slot per entry in self._order, always (no spare capacity, no hidden
    # placeholders). _slot_frame/_resize_doc compute geometry from whatever
    # self._cols/len(self._slots) currently are; _add_slot/_drop_last_slot
    # grow/shrink the slot list; _reflow_from rebuilds tile CONTENT (not
    # positions) starting at a given index after the ranking shifts.

    @objc.python_method
    def _build_grid(self, scroll):
        cs = scroll.contentSize()
        self._doc_w = cs.width
        # As many columns as fit, up to GRID_COLS -- a small screen just
        # gets fewer columns, not smaller tiles (tiles stay the corner
        # card's own fixed W x H so nothing about the reused rendering code
        # has to change). Rows are NOT capped to what fits in the visible
        # height: every pending draft gets a slot, and the doc grows taller
        # than the window as needed -- the scroll view (already present)
        # handles the rest (2026-07-16 user direction: "if not all cards are
        # visible in the window, we need a scroll bar").
        self._cols = max(1, min(GRID_COLS, int((self._doc_w + GRID_GAP) // (TILE_W + GRID_GAP))))
        doc = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, self._doc_w, cs.height))
        # Layer-backed explicitly: scrolling then moves composited layers
        # instead of re-drawing views through the window backing store,
        # which with this many tiles is the difference between smooth and
        # laggy (2026-07-16 scroll-perf fix; pairs with the opaque scroll
        # background in _render).
        try:
            doc.setWantsLayer_(True)
        except Exception:
            pass
        self._doc = doc
        self._slots = []
        scroll.setDocumentView_(doc)
        for _ in self._order:
            self._add_slot()
        self._reflow_from(0)
        self._resize_doc()
        self._refresh_header()

    @objc.python_method
    def _slot_frame(self, i):
        grid_w = self._cols * TILE_W + (self._cols - 1) * GRID_GAP
        left_pad = max(0.0, (self._doc_w - grid_w) / 2.0)
        r, c = divmod(i, self._cols)
        x = left_pad + c * (TILE_W + GRID_GAP)
        y = GRID_PAD + r * (TILE_H + GRID_GAP)
        return NSMakeRect(x, y, TILE_W, TILE_H)

    @objc.python_method
    def _add_slot(self):
        i = len(self._slots)
        slot_view = NSView.alloc().initWithFrame_(self._slot_frame(i))
        # Outline every slot -- a tile mounted here has no window of its own
        # anymore (it's a subview, not a floating panel), so the visual card
        # separation the corner card got for free from being its own OS
        # window has to be drawn explicitly here instead (2026-07-16 user
        # report: "no outline around each card").
        _round_rect(slot_view)
        try:
            slot_view.layer().setBackgroundColor_(_fill_color().CGColor())
        except Exception:
            pass
        self._doc.addSubview_(slot_view)
        self._slots.append({"view": slot_view, "tile": None, "n": None})

    @objc.python_method
    def _resize_doc(self):
        rows = max(1, -(-len(self._slots) // self._cols)) if self._slots else 1
        needed_h = rows * TILE_H + (rows - 1) * GRID_GAP + 2 * GRID_PAD
        cs = self._scroll.contentSize()
        doc_h = max(cs.height, needed_h)
        self._doc.setFrameSize_(NSMakeSize(self._doc_w, doc_h))

    @objc.python_method
    def _reflow_from(self, start_idx):
        """Rebuild every slot's CONTENT from start_idx onward from the
        current self._order -- slot POSITIONS never move, only which draft
        occupies each one. Used after a decision removes one entry so
        everything after it shifts into the vacated grid position ("snake"
        reflow, 2026-07-16 user direction) instead of independently
        refilling in place. In-progress edits on any rebuilt tile are lost
        -- ranking order takes priority over preserving an edit on a card
        the reviewer hadn't yet acted on."""
        for i in range(start_idx, len(self._slots)):
            slot = self._slots[i]
            for sv in list(slot["view"].subviews()):
                sv.removeFromSuperview()
            d = self._order[i]
            tile = _ReviewController.alloc().initWithDrafts_onDecision_onComplete_focus_hostView_hostWindow_(
                [d],
                self._tile_decision_cb(i),
                self._tile_complete_cb(i),
                True,
                slot["view"],
                self._panel,
            )
            slot["tile"] = tile
            slot["n"] = d.get("n")

    @objc.python_method
    def _remove_and_reflow(self, n):
        """Pop draft `n` out of self._order, drop one slot (the grid is one
        shorter -- no spare capacity to backfill), and reflow every slot
        from its old position onward so subsequent cards shift up into the
        gap in rank order."""
        try:
            idx = next(i for i, d in enumerate(self._order) if d.get("n") == n)
        except StopIteration:
            return
        self._order.pop(idx)
        if self._slots:
            last = self._slots.pop()
            last["view"].removeFromSuperview()
        self._reflow_from(idx)
        self._resize_doc()
        self._refresh_header()

    # Approve All / Discard All buttons live in the header row (see
    # _render()), not a dedicated card -- these two actions just target the
    # module-level handlers (registered by the menu bar) regardless of where
    # the buttons that trigger them physically sit.
    def approveAllClicked_(self, sender):
        cb = _approve_all_handler
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            _log(f"canvas approve-all handler failed: {e}")

    def discardAllClicked_(self, sender):
        cb = _discard_all_handler
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            _log(f"canvas discard-all handler failed: {e}")

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
            # The tile's own single-draft stack finished -- remove it from
            # the ranking and let everything after it shift up ("snake"
            # reflow; see _remove_and_reflow). slot_idx is read lazily at
            # call time (not captured as a fixed n) because a still-live
            # tile ahead of it could have already shifted this slot's
            # content since the closure was created.
            slot = self._slots[slot_idx] if slot_idx < len(self._slots) else None
            n = slot["n"] if slot else None
            if n is not None:
                self._remove_and_reflow(n)

        return _cb

    @objc.python_method
    def _refresh_header(self):
        total = len(self._order)
        try:
            plural = "s" if total != 1 else ""
            self._count_label.setStringValue_(f"{total} pending draft{plural}")
        except Exception:
            pass
        try:
            self._panel.setTitle_(f"s4l · Review {total} drafts (canvas)")
        except Exception:
            pass

    # ---- live backlog changes -------------------------------------------------

    @objc.python_method
    def extend_drafts(self, drafts):
        """Append newly-queued drafts to the end of the ranking, growing the
        grid by one slot each -- existing slots/tiles are never touched, so
        an in-progress edit on screen is never disturbed, mirroring the
        corner card's own extend_drafts."""
        if self._panel is None:
            return 0
        have = {d.get("n") for d in self._order}
        have.update(dec.get("n") for dec in self._decisions)
        added = [d for d in drafts if d.get("n") not in have]
        if not added:
            return 0
        added.sort(key=_sort_key, reverse=True)
        start_idx = len(self._order)
        self._order.extend(added)
        for _ in added:
            self._add_slot()
        self._reflow_from(start_idx)
        self._resize_doc()
        self._refresh_header()
        self._log_surface("extended")
        return len(added)

    @objc.python_method
    def prune_drafts(self, ns):
        """Drop drafts whose plan index `n` is in `ns` (a candidate that
        expired backend-side mid-review) and reflow the rest into place.
        Every draft the canvas knows about has a live tile in this design
        (no hidden backlog), so unlike the corner card's own prune_drafts
        this DOES tear down a visible tile when it's the pruned one -- the
        candidate is gone server-side either way; leaving a stale,
        still-editable tile on screen would be worse."""
        if self._panel is None or not ns:
            return 0
        ns = set(ns)
        removed = 0
        for n in [d.get("n") for d in list(self._order) if d.get("n") in ns]:
            self._remove_and_reflow(n)
            removed += 1
        if removed:
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
        pending = len(self._order)
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

    @objc.python_method
    def _relayout_grid(self):
        """Recompute geometry after the window's frame changed (user resize,
        or heal_active recentering on a different-sized screen): column
        count and slot POSITIONS only -- tiles are subviews of their slot
        views and move with them, so in-progress edits survive a resize
        untouched (unlike _reflow_from, which rebuilds content)."""
        if self._scroll is None or self._doc is None:
            return
        cs = self._scroll.contentSize()
        self._doc_w = cs.width
        self._cols = max(
            1, min(GRID_COLS, int((self._doc_w + GRID_GAP) // (TILE_W + GRID_GAP)))
        )
        for i, slot in enumerate(self._slots):
            slot["view"].setFrame_(self._slot_frame(i))
        self._resize_doc()

    def windowDidResize_(self, notification):
        try:
            self._relayout_grid()
        except Exception as e:
            _log(f"canvas relayout failed: {e}")

    def windowDidMove_(self, notification):
        now = time.time()
        if now - self._last_move_log < 1.0:
            return
        self._last_move_log = now
        self._log_surface("moved")

    def windowDidChangeOcclusionState_(self, notification):
        self._log_surface("occlusion_changed")

    def windowShouldClose_(self, sender):
        # Closing leaves undecided drafts pending; the menu bar treats this
        # exactly like an unfinished corner-card stack (snooze, re-present
        # later), same as the corner card's own windowShouldClose_.
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
