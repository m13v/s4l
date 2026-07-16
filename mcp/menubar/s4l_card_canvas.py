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

Rows/backlog are sorted by stats.virality_score descending when a
candidate has one (Twitter only, today, per score_twitter_candidates.py);
candidates without a score sort as -1 and keep review_drafts()'s existing
newest-first order among themselves (Python's stable sort) at the bottom
of the queue -- no new scoring is invented for platforms that don't have
one.

Must be driven on the main thread (mirrors s4l_card.py).
"""

import time

import objc
from Foundation import NSMakeRect, NSMakeSize
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


class _CanvasController(_ReviewPanel.__base__ if False else object):
    pass
