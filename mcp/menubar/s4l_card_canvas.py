"""Large centered multi-select canvas for draft review (AppKit / pyobjc).

`present_review_canvas(drafts, on_decision, on_complete, focus)` is the canvas
counterpart to `s4l_card.present_review`: same `drafts` input shape (see
s4l_state.review_drafts), same decision-dict contract fired via
on_decision/on_complete -- the menu bar's existing wiring
(_on_card_decision / _on_review_closed) drives either surface unchanged.
Where the corner card walks the backlog one draft at a time, this shows the
WHOLE pending backlog at once in one large centered window: one scrollable
row per thread (full thread text + its draft(s)), a checkbox per row, and a
bulk "Approve selected" / "Discard selected" action bar, for a reviewer who
wants to skim many threads and act on a handful rather than read one card
at a time. Rows the reviewer never touches are simply left pending when the
window closes (same as closing the corner card mid-stack); the menu bar's
existing snooze handling re-presents them later.

Rows are sorted by stats.virality_score descending when a candidate has one
(Twitter only, today, per score_twitter_candidates.py); candidates without a
score sort as -1 and keep review_drafts()'s existing newest-first order
among themselves (Python's stable sort) at the bottom of the list -- no new
scoring is invented for platforms that don't have one.

Two-draft threads (candidate["drafts"], Draft A / Draft B) render as two
side-by-side editable boxes in the same row (the wider canvas has room,
unlike the corner card's stacked layout, per 2026-07-15 product direction).
Neither box is visually pre-selected: the bold "armed" border only appears
once the reviewer actually clicks into one (row["touched"]), so nothing on
a fresh row looks pre-chosen before the reviewer decides anything
(2026-07-16 user report -- the row's checkbox, the actual post/skip
decision, already starts unchecked, but the bold border on Draft A read as
"already chosen" regardless). Clicking into a box is the exact same
textViewDidChangeSelection_ convention the corner card uses (see
s4l_card.py's _update_draft_borders), just duplicated per-row and gated on
first touch; armed_slot still defaults to 0 (Draft A) internally as the
fallback if a row is approved via its checkbox without ever touching either
box, matching the corner card.

Each row's thread quote shows the FULL text, never truncated with an
ellipsis -- sized to its own measured height up to a cap, scrolling in
place beyond that (2026-07-16 user report: the old fixed-height box cut
threads off with no way to read the rest short of opening the external
link). The row's eye icon opens the same popover content the corner card's
eye icons show (engagement stats + drafting details, combined into one
icon/popover per row instead of the corner card's two).

Decided rows (approved/discarded) stay visible but frozen -- disabled
controls, dimmed, a status line -- rather than being removed and the rest
reflowed: correctness and a stable scroll position matter more here than
tidying the list. There is no per-row Discard button (2026-07-16 user
report: redundant with the checkbox + header's "Discard selected" -- check
just the one row and click that to discard a single thread).

Deliberately NOT reimplemented here (present on the corner card, cut for
this surface's first pass, neither a decision-contract change so either
can land later): per-second live expiry countdowns (age/expiry is a static
label computed at render/extend time), and hover-dwell telemetry on
two-draft boxes (draft_choice ships with hover_*_ms=0 and
visited_other=False). The categorized reject-reason picker has no per-row
entry point either -- discard here always ships reject_category=None, same
as the corner card's own "Reject commits with no category" fallback path.

Must be driven on the main thread (mirrors s4l_card.py).
"""

import datetime
import re
import time

import objc
from Foundation import (
    NSObject,
    NSMakeRect,
    NSMakeSize,
    NSURL,
    NSAttributedString,
    NSMutableAttributedString,
    NSNotificationCenter,
)
from AppKit import (
    NSApp,
    NSButton,
    NSFont,
    NSImage,
    NSScrollView,
    NSTextView,
    NSView,
    NSColor,
    NSWorkspace,
    NSPopover,
    NSPopoverBehaviorApplicationDefined,
    NSViewController,
    NSTrackingArea,
    NSTrackingMouseEnteredAndExited,
    NSTrackingActiveAlways,
    NSTextStorage,
    NSLayoutManager,
    NSTextContainer,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSLinkAttributeName,
    NSWindowOcclusionStateVisible,
    NSBackingStoreBuffered,
    NSFloatingWindowLevel,
    NSApplicationActivationPolicyAccessory,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskUtilityWindow,
    NSTextAlignmentRight,
    NSBezelStyleRounded,
)

try:
    from AppKit import NSButtonTypeSwitch
except Exception:
    try:
        from AppKit import NSSwitchButton as NSButtonTypeSwitch
    except Exception:
        NSButtonTypeSwitch = 3  # raw NSSwitchButton value, pre-10.12 AppKit

# Reuse the corner card's panel class (Cmd+V/C/X/A/Z routing for a status-bar
# app with no Edit menu) and its styling/rendering helpers instead of
# duplicating them -- this module is a second PRESENTATION only, the drafts
# data, decision contract, and low-level AppKit skinning stay single-sourced
# in s4l_card.py.
from s4l_card import (
    _ReviewPanel,
    _label,
    _editable_scroll,
    _round_rect,
    _frosted,
    _fill_color,
    _solid,
    _engagement_line,
    _details_lines,
    _reply_heading_suffix,
    _followers_str,
    _age_expiry_display,
    _mouse_screen,
    _write_review_state,
    _log,
    NSWindowStyleMaskNonactivatingPanel,
)

# Strong reference to the live controller so pyobjc doesn't GC it mid-review,
# mirroring s4l_card._active. Separate global -- only one of the two surfaces
# is ever open at a time (the menu bar's _review_active/_panel_open flags
# already enforce that), but each module owns its own reference regardless.
_active = None

WIN_MARGIN = 20.0
HEADER_H = 46.0
ROW_GAP = 16.0
ROW_PAD = 14.0
ROW_RM = 14.0          # inner row margin (left/right)
CHECKBOX_COL_W = 28.0  # space reserved for the row checkbox before content starts
TOP_CHROME_H = 34.0    # checkbox/heading/age/eye row, reserved above the thread box
THREAD_GAP = 10.0      # gap between the thread quote and the draft box(es) below it
MIN_THREAD_H = 24.0
MAX_THREAD_H = 150.0   # thread text is never truncated; beyond this it scrolls in place
DRAFT_LABEL_H = 14.0
DRAFT_BOX_H = 130.0
BOTTOM_MARGIN = 14.0   # room for the post-decision status line


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _content_w_for(doc_w):
    """Usable row width after the left checkbox column and both margins --
    the single formula _build_rows/extend_drafts (pre-pass sizing) and
    _add_row (actual construction) both call, so they can never drift."""
    return doc_w - ROW_RM - CHECKBOX_COL_W - ROW_RM


def _measure_text_height(text, width, font_size=12.0):
    """Wrapped height a block of text needs at `width`, via a standalone
    TextKit stack (no view attachment needed). Used to size each row's
    thread-quote box to its ACTUAL content instead of a fixed height +
    ellipsis truncation."""
    text = (text or "").strip()
    if not text or width <= 0:
        return 0.0
    storage = NSTextStorage.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: NSFont.systemFontOfSize_(font_size)}
    )
    lm = NSLayoutManager.alloc().init()
    storage.addLayoutManager_(lm)
    tc = NSTextContainer.alloc().initWithContainerSize_(NSMakeSize(width, 1.0e7))
    tc.setLineFragmentPadding_(0.0)
    lm.addTextContainer_(tc)
    lm.glyphRangeForTextContainer_(tc)  # force layout
    return float(lm.usedRectForTextContainer_(tc).size.height)


def _set_thread_text(tv, text, link_url, font_size=12.0):
    """Full thread text, never truncated (see _measure_text_height's
    docstring) -- a trailing ' ↗' opens the source thread when clicked, same
    glyph/behavior as the corner card's thread quote."""
    text = (text or "").strip()
    body = NSMutableAttributedString.alloc().initWithString_attributes_(
        text,
        {
            NSFontAttributeName: NSFont.systemFontOfSize_(font_size),
            NSForegroundColorAttributeName: NSColor.labelColor(),
        },
    )
    if link_url:
        body.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                " ↗",
                {
                    NSFontAttributeName: NSFont.systemFontOfSize_(font_size),
                    NSLinkAttributeName: NSURL.URLWithString_(link_url),
                },
            )
        )
    tv.textStorage().setAttributedString_(body)


def _row_geometry(d, content_w):
    """Every vertical measurement for one row, computed ONCE and shared by
    the pre-pass sizing (_build_rows/extend_drafts) and the actual row
    construction (_add_row) so the two can never drift apart."""
    text = (d.get("thread_text_en") or d.get("thread_text") or "").strip()
    measured = _measure_text_height(text, max(10.0, content_w - 8.0), font_size=12.0)
    thread_h = min(MAX_THREAD_H, max(MIN_THREAD_H, measured + 10.0))
    drafts_field = d.get("drafts")
    dual = isinstance(drafts_field, list) and len(drafts_field) == 2
    label_h = DRAFT_LABEL_H if dual else 0.0
    row_h = TOP_CHROME_H + thread_h + THREAD_GAP + label_h + DRAFT_BOX_H + BOTTOM_MARGIN
    return {"thread_h": thread_h, "dual": dual, "label_h": label_h, "row_h": row_h}


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
    Makes row stacking (and appending more rows via extend_drafts) simple
    arithmetic independent of the document's current height, and the scroll
    view opens showing the top of the list by default."""

    def isFlipped(self):
        return True


class _CanvasController(NSObject):
    def initWithDrafts_onDecision_onComplete_focus_(
        self, drafts, on_decision, on_complete, focus
    ):
        self = objc.super(_CanvasController, self).init()
        if self is None:
            return None
        self._sorted = sorted(list(drafts), key=_sort_key, reverse=True)
        self._on_decision = on_decision
        self._on_complete = on_complete
        self._focus = bool(focus)
        self._decisions = []
        self._rows = []
        self._panel = None
        self._scroll = None
        self._doc = None
        self._doc_bottom = 0.0  # next free y for extend_drafts' new rows
        self._select_all_btn = None
        self._count_label = None
        self._approve_btn = None
        self._discard_btn = None
        self._stats_popover = None  # one shared popover, any row's eye icon
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
        # takes) -- panel.frame() later would instead return the WINDOW frame,
        # taller by the title bar's height, and sizing the content view to
        # that overlapped the header row under the title bar (2026-07-16 user
        # report). Keep the real content size around for _render().
        self._content_size = NSMakeSize(frame.size.width, frame.size.height)
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setFloatingPanel_(True)
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
        select_all = NSButton.alloc().initWithFrame_(
            NSMakeRect(WIN_MARGIN, header_y + 12, 110, 22)
        )
        select_all.setButtonType_(NSButtonTypeSwitch)
        select_all.setTitle_("Select all")
        select_all.setTarget_(self)
        select_all.setAction_("selectAllToggled:")
        content.addSubview_(select_all)
        self._select_all_btn = select_all

        self._count_label = _label(
            NSMakeRect(WIN_MARGIN + 130, header_y + 14, 320, 18), "", size=12, bold=True
        )
        content.addSubview_(self._count_label)

        approve_w, discard_w, btn_gap = 150.0, 140.0, 10.0
        approve_x = w - WIN_MARGIN - approve_w
        discard_x = approve_x - btn_gap - discard_w

        discard_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(discard_x, header_y + 8, discard_w, 28)
        )
        discard_btn.setTitle_("Discard selected")
        discard_btn.setBezelStyle_(NSBezelStyleRounded)
        try:
            discard_btn.setHasDestructiveAction_(True)
        except Exception:
            pass
        discard_btn.setTarget_(self)
        discard_btn.setAction_("discardSelected:")
        content.addSubview_(discard_btn)
        self._discard_btn = discard_btn

        approve_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(approve_x, header_y + 8, approve_w, 28)
        )
        approve_btn.setTitle_("Approve selected")
        approve_btn.setBezelStyle_(NSBezelStyleRounded)
        approve_btn.setKeyEquivalent_("\r")
        approve_btn.setTarget_(self)
        approve_btn.setAction_("approveSelected:")
        content.addSubview_(approve_btn)
        self._approve_btn = approve_btn

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
        # A row's eye popover anchors to that row's own button; once the list
        # scrolls, that anchor may no longer be under (or even near) the
        # popover -- close it rather than leave it floating disconnected.
        clip = scroll.contentView()
        clip.setPostsBoundsChangedNotifications_(True)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "scrollDidChange:", "NSViewBoundsDidChangeNotification", clip
        )

        self._rows = []
        self._build_rows(scroll)

        self._panel.setContentView_(_frosted(content))
        self._refresh_header()

    @objc.python_method
    def _build_rows(self, scroll):
        # Two-pass layout: row heights vary with each thread's actual text
        # (see _row_geometry), so every row's height is measured FIRST, then
        # rows are stacked using running cumulative offsets -- a fixed
        # per-row height would either clip long threads or leave dead space
        # under short ones (2026-07-16 user report).
        cs = scroll.contentSize()
        doc_w = cs.width
        content_w = _content_w_for(doc_w)
        geoms = [_row_geometry(d, content_w) for d in self._sorted]
        total_h = ROW_PAD + sum(g["row_h"] + ROW_GAP for g in geoms)
        doc_h = max(cs.height, total_h)
        doc = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, doc_w, doc_h))
        self._doc = doc
        y = ROW_PAD
        for i, d in enumerate(self._sorted):
            g = geoms[i]
            self._add_row(doc, d, i, NSMakeRect(0, y, doc_w, g["row_h"]), g)
            y += g["row_h"] + ROW_GAP
        self._doc_bottom = y
        scroll.setDocumentView_(doc)

    @objc.python_method
    def _add_row(self, doc, d, idx, frame):
        row_view = NSView.alloc().initWithFrame_(frame)
        _round_rect(row_view)
        try:
            row_view.layer().setBackgroundColor_(_fill_color().CGColor())
        except Exception:
            pass
        doc.addSubview_(row_view)

        rw, rh = frame.size.width, frame.size.height
        rm = 14

        checkbox = NSButton.alloc().initWithFrame_(NSMakeRect(rm, rh - 30, 20, 20))
        checkbox.setButtonType_(NSButtonTypeSwitch)
        checkbox.setTitle_("")
        checkbox.setTag_(idx)
        checkbox.setTarget_(self)
        checkbox.setAction_("rowCheckToggled:")
        row_view.addSubview_(checkbox)

        content_x = rm + 28
        content_w = rw - content_x - rm

        handle = (d.get("thread_author") or "").lstrip("@").strip()
        followers = _followers_str(d.get("stats"))
        heading = f"@{handle}" if handle else "(unknown author)"
        if followers:
            heading += f"  ·  {followers}"
        tag = _reply_heading_suffix(d)
        if tag:
            heading += f"   —   {tag}"
        age_text, urgent = _age_expiry_display(
            (d.get("stats") or {}).get("tweet_posted_at"), d.get("platform")
        )
        age_w = 150
        age_x = rw - rm - age_w
        heading_w = (age_x - 10 - content_x) if age_text else (content_w - 10)
        row_view.addSubview_(
            _label(
                NSMakeRect(content_x, rh - 28, max(60, heading_w), 18),
                heading,
                size=12,
                bold=True,
                truncates=True,
            )
        )
        if age_text:
            age_label = _label(
                NSMakeRect(age_x, rh - 28, age_w, 18),
                age_text,
                size=11,
                bold=urgent,
                muted=not urgent,
                truncates=True,
            )
            age_label.setAlignment_(NSTextAlignmentRight)
            row_view.addSubview_(age_label)

        tooltip_bits = []
        eng = _engagement_line(d.get("stats"))
        if eng:
            tooltip_bits.append(eng)
        tooltip_bits.extend(_details_lines(d))
        if tooltip_bits:
            info_btn = NSButton.alloc().initWithFrame_(NSMakeRect(content_x, rh - 50, 18, 16))
            if NSBezelStyleInline is not None:
                try:
                    info_btn.setBezelStyle_(NSBezelStyleInline)
                except Exception:
                    pass
            info_btn.setTitle_("ⓘ")
            info_btn.setFont_(NSFont.systemFontOfSize_(10))
            try:
                info_btn.setToolTip_("\n".join(tooltip_bits))
            except Exception:
                pass
            row_view.addSubview_(info_btn)

        thread_top = rh - 54
        thread_h = 56
        thread_scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(content_x, thread_top - thread_h, content_w, thread_h)
        )
        thread_scroll.setHasVerticalScroller_(False)
        thread_scroll.setDrawsBackground_(False)
        thread_scroll.setBorderType_(0)
        tcs = thread_scroll.contentSize()
        thread_tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, tcs.width, tcs.height))
        thread_tv.setEditable_(False)
        thread_tv.setSelectable_(True)
        thread_tv.setDrawsBackground_(False)
        thread_tv.setDelegate_(self)
        thread_tv.textContainer().setWidthTracksTextView_(True)
        thread_scroll.setDocumentView_(thread_tv)
        row_view.addSubview_(thread_scroll)
        # thread_text_en (stamped at draft time for non-English drafts) is
        # shown here so the reviewer reads English; the editable box below
        # always keeps the ORIGINAL-language reply_text, which is what posts.
        thread_text = (d.get("thread_text_en") or d.get("thread_text") or "").strip()
        _fit_thread_body(thread_tv, thread_text, d.get("thread_url"), font_size=12, step=15, floor=10)

        row = {
            "d": d,
            "n": d.get("n"),
            "idx": idx,
            "container": row_view,
            "checkbox": checkbox,
            "selected": False,
            "decided": False,
            "hidden": False,
            "dual": False,
            "armed_slot": 0,
            "outlines": {},
            "textviews": {},
            "orig_texts": {},
            "thread_tv": thread_tv,
            "thread_url": d.get("thread_url"),
            "interactions": [],
            "discard_btn": None,
            "status_label": None,
        }

        drafts_field = d.get("drafts")
        dual = isinstance(drafts_field, list) and len(drafts_field) == 2
        row["dual"] = dual
        link = d.get("link_url")

        def _compose(text):
            text = text or ""
            return text if (not link or link in text) else f"{text} {link}"

        bottom_bar_h = 26
        edit_bottom = bottom_bar_h + 4
        edit_top = thread_top - thread_h - 8
        label_h = 14 if dual else 0
        edit_h = edit_top - edit_bottom - label_h

        if dual:
            gap2 = 10
            box_w = (content_w - gap2) / 2.0
            for slot in (0, 1):
                box_x = content_x + slot * (box_w + gap2)
                outline = NSView.alloc().initWithFrame_(NSMakeRect(box_x, edit_bottom, box_w, edit_h))
                _round_rect(outline)
                inset = 4
                escroll, etv = _editable_scroll(
                    NSMakeRect(inset, inset, box_w - 2 * inset, edit_h - 2 * inset),
                    _compose(drafts_field[slot].get("text")),
                )
                etv.setDelegate_(self)
                outline.addSubview_(escroll)
                row_view.addSubview_(outline)
                row_view.addSubview_(
                    _label(
                        NSMakeRect(box_x, edit_bottom + edit_h, box_w, label_h),
                        "Draft A" if slot == 0 else "Draft B",
                        size=10,
                        muted=True,
                    )
                )
                row["outlines"][slot] = outline
                row["textviews"][slot] = etv
                row["orig_texts"][slot] = (drafts_field[slot].get("text") or "").strip()
        else:
            outline = NSView.alloc().initWithFrame_(NSMakeRect(content_x, edit_bottom, content_w, edit_h))
            _round_rect(outline)
            inset = 4
            escroll, etv = _editable_scroll(
                NSMakeRect(inset, inset, content_w - 2 * inset, edit_h - 2 * inset),
                _compose(d.get("reply_text")),
            )
            etv.setDelegate_(self)
            outline.addSubview_(escroll)
            row_view.addSubview_(outline)
            row["outlines"][0] = outline
            row["textviews"][0] = etv
            row["orig_texts"][0] = (d.get("reply_text") or "").strip()

        discard_btn = NSButton.alloc().initWithFrame_(NSMakeRect(content_x, 2, 90, 22))
        discard_btn.setTitle_("Discard")
        if NSBezelStyleInline is not None:
            try:
                discard_btn.setBezelStyle_(NSBezelStyleInline)
            except Exception:
                discard_btn.setBezelStyle_(NSBezelStyleRounded)
        else:
            discard_btn.setBezelStyle_(NSBezelStyleRounded)
        try:
            discard_btn.setHasDestructiveAction_(True)
        except Exception:
            pass
        discard_btn.setFont_(NSFont.systemFontOfSize_(11))
        discard_btn.setTag_(idx)
        discard_btn.setTarget_(self)
        discard_btn.setAction_("rowDiscard:")
        row_view.addSubview_(discard_btn)
        row["discard_btn"] = discard_btn

        status_label = _label(
            NSMakeRect(content_x + 100, 2, content_w - 100, 22), "", size=11, bold=True
        )
        status_label.setHidden_(True)
        row_view.addSubview_(status_label)
        row["status_label"] = status_label

        self._rows.append(row)
        if dual:
            self._update_row_borders(row)

    # ---- two-draft arm selection --------------------------------------------

    def textViewDidChangeSelection_(self, notification):
        # Same convention as the corner card (s4l_card._ReviewController):
        # whichever draft box the reviewer's caret is in becomes the armed
        # slot for that row. Linear `is` scan, not a dict keyed by the
        # NSTextView itself -- mirrors the corner card's own defensive
        # pattern rather than trusting pyobjc object identity for hashing.
        try:
            tv = notification.object()
        except Exception:
            return
        for row in self._rows:
            if not row["dual"] or row["decided"] or row["hidden"]:
                continue
            for slot, cand_tv in row["textviews"].items():
                if cand_tv is not tv:
                    continue
                if slot != row["armed_slot"]:
                    row["armed_slot"] = slot
                    self._update_row_borders(row)
                return

    @objc.python_method
    def _update_row_borders(self, row):
        if not row["dual"]:
            return
        selected_color = NSColor.blackColor()
        for slot, outline in row["outlines"].items():
            try:
                layer = outline.layer()
                if layer is None:
                    continue
                if slot == row["armed_slot"]:
                    layer.setBorderWidth_(3.0)
                    layer.setBorderColor_(selected_color.CGColor())
                    tint = _solid(NSColor.textBackgroundColor()).blendedColorWithFraction_ofColor_(
                        0.30, selected_color
                    )
                    layer.setBackgroundColor_((tint or selected_color).CGColor())
                else:
                    layer.setBorderWidth_(1.0)
                    layer.setBorderColor_(_solid(NSColor.separatorColor()).CGColor())
                    layer.setBackgroundColor_(_solid(NSColor.textBackgroundColor()).CGColor())
            except Exception:
                pass

    # ---- thread link click ---------------------------------------------------

    def textView_clickedOnLink_atIndex_(self, tv, link, idx):
        url = None
        for row in self._rows:
            if row.get("thread_tv") is tv:
                url = row.get("thread_url")
                break
        try:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url or str(link)))
        except Exception:
            pass
        self._last_interaction_at = time.time()
        return True

    # ---- selection / decisions ------------------------------------------------

    def rowCheckToggled_(self, sender):
        try:
            idx = int(sender.tag())
        except Exception:
            return
        if not (0 <= idx < len(self._rows)):
            return
        row = self._rows[idx]
        if row["decided"] or row["hidden"]:
            return
        row["selected"] = bool(sender.state())
        row["interactions"].append(
            {"type": "canvas_select" if row["selected"] else "canvas_deselect", "ts": _now_iso()}
        )
        self._last_interaction_at = time.time()
        self._refresh_header()

    def selectAllToggled_(self, sender):
        want = bool(sender.state())
        for row in self._rows:
            if row["decided"] or row["hidden"]:
                continue
            row["selected"] = want
            try:
                row["checkbox"].setState_(1 if want else 0)
            except Exception:
                pass
        self._last_interaction_at = time.time()
        self._refresh_header()

    def rowDiscard_(self, sender):
        try:
            idx = int(sender.tag())
        except Exception:
            return
        if not (0 <= idx < len(self._rows)):
            return
        self._apply_decision_to_row(self._rows[idx], False)
        self._refresh_header()

    def approveSelected_(self, sender):
        acted = [r for r in self._rows if not r["decided"] and not r["hidden"] and r["selected"]]
        for row in acted:
            self._apply_decision_to_row(row, True)
        self._refresh_header()

    def discardSelected_(self, sender):
        acted = [r for r in self._rows if not r["decided"] and not r["hidden"] and r["selected"]]
        for row in acted:
            self._apply_decision_to_row(row, False)
        self._refresh_header()

    @objc.python_method
    def _current_text(self, row):
        slot = row["armed_slot"] if row["dual"] else 0
        tv = row["textviews"].get(slot)
        if tv is not None:
            try:
                return str(tv.string())
            except Exception:
                pass
        d = row["d"]
        if row["dual"]:
            drafts = d.get("drafts")
            return (drafts[slot].get("text") or "") if drafts else ""
        return d.get("reply_text") or ""

    @objc.python_method
    def _build_decision(self, row, approved, loved=False, reject_category=None, reject_note=None):
        # Faithful port of s4l_card._ReviewController._record, adapted to
        # per-row state instead of a single current-card index. Same field
        # shape end to end so _ship_review_event / post_drafts need no
        # changes to consume either surface's decisions.
        d = row["d"]
        dual = row["dual"]
        if dual:
            sel_idx = row["armed_slot"] if row["armed_slot"] in (0, 1) else 0
            drafts = d.get("drafts")
            chosen = drafts[sel_idx]
            orig = (row["orig_texts"].get(sel_idx) or "").strip()
            draft_variant = chosen.get("variant") or ("a" if sel_idx == 0 else "b")
            other = drafts[1 - sel_idx]
            draft_choice = {
                "variant": draft_variant,
                "index": sel_idx,
                "auto_selected": bool(sel_idx == 0),
                "style": chosen.get("style") or None,
                "unchosen_text": (other.get("text") or "").strip() or None,
                "unchosen_style": other.get("style") or None,
                # No per-box hover tracking on this surface (see module
                # docstring) -- shipped as known-zero, not fabricated.
                "hover_a_ms": 0,
                "hover_b_ms": 0,
                "visited_other": False,
            }
        else:
            sel_idx = None
            orig = (row["orig_texts"].get(0) or "").strip()
            draft_variant = None
            draft_choice = None
        link = d.get("link_url") or ""
        drop_link = False
        if approved:
            text = self._current_text(row)
            if link and link not in text:
                drop_link = True
            body = re.sub(r"[ \t]{2,}", " ", text).strip()
        else:
            body = orig
        return {
            "n": d.get("n"),
            "approved": bool(approved),
            "loved": bool(approved and loved),
            "text": body,
            "edited": bool(approved and body != orig),
            "original_text": orig if (approved and body != orig) else None,
            "drop_link": bool(approved and drop_link),
            "reject_category": reject_category,
            "reject_note": (reject_note or "").strip() or None,
            "interactions": list(row.get("interactions") or []),
            "dwell_ms": self._dwell_ms(),
            "candidate_id": d.get("candidate_id"),
            "platform": d.get("platform"),
            "project": d.get("project"),
            "thread_url": d.get("thread_url"),
            "thread_author": d.get("thread_author"),
            "language": d.get("language"),
            "draft_variant": draft_variant,
            "draft_index": sel_idx,
            "draft_auto_selected": bool(dual and sel_idx == 0),
            "draft_choice": draft_choice,
        }

    @objc.python_method
    def _apply_decision_to_row(self, row, approved, loved=False, reject_category=None, reject_note=None):
        if row["decided"] or row["hidden"]:
            return
        decision = self._build_decision(
            row, approved, loved=loved, reject_category=reject_category, reject_note=reject_note
        )
        self._decisions.append(decision)
        row["decided"] = True
        self._freeze_row(row, approved)
        self._last_decision_at = time.time()
        self._log_surface("decision")
        cb = self._on_decision
        if cb is not None:
            try:
                cb(dict(decision))
            except Exception:
                pass

    @objc.python_method
    def _freeze_row(self, row, approved):
        try:
            row["checkbox"].setEnabled_(False)
            row["checkbox"].setState_(0)
        except Exception:
            pass
        for tv in row["textviews"].values():
            try:
                tv.setEditable_(False)
            except Exception:
                pass
        try:
            row["discard_btn"].setHidden_(True)
        except Exception:
            pass
        try:
            row["status_label"].setStringValue_("Approved ✓" if approved else "Discarded ✕")
            row["status_label"].setHidden_(False)
        except Exception:
            pass
        try:
            row["container"].setAlphaValue_(0.55)
        except Exception:
            pass
        row["selected"] = False

    @objc.python_method
    def _refresh_header(self):
        pending = [r for r in self._rows if not r["decided"] and not r["hidden"]]
        selected = [r for r in pending if r["selected"]]
        try:
            self._count_label.setStringValue_(f"{len(selected)} selected of {len(pending)} pending")
        except Exception:
            pass
        try:
            self._approve_btn.setEnabled_(bool(selected))
            self._discard_btn.setEnabled_(bool(selected))
        except Exception:
            pass
        try:
            self._panel.setTitle_(f"s4l · Review {len(pending)} drafts (canvas)")
        except Exception:
            pass

    # ---- live backlog changes -------------------------------------------------

    @objc.python_method
    def extend_drafts(self, drafts):
        """Append newly-queued drafts as new rows below the current bottom.
        Never touches an existing row's frame or content -- an in-progress
        edit/selection on screen is never disturbed, mirroring the corner
        card's extend_drafts. New drafts are sorted among THEMSELVES only
        (not merged back into the whole list's order)."""
        if self._panel is None or self._doc is None:
            return 0
        have = {r["n"] for r in self._rows}
        added = [d for d in drafts if d.get("n") not in have]
        if not added:
            return 0
        added.sort(key=_sort_key, reverse=True)
        self._sorted.extend(added)
        doc = self._doc
        doc_w = doc.frame().size.width
        base_idx = len(self._rows)
        new_h = ROW_PAD + (base_idx + len(added)) * (ROW_H + ROW_GAP)
        doc.setFrameSize_(NSMakeSize(doc_w, new_h))
        for j, d in enumerate(added):
            i = base_idx + j
            row_y = ROW_PAD + i * (ROW_H + ROW_GAP)
            self._add_row(doc, d, i, NSMakeRect(0, row_y, doc_w, ROW_H))
        self._refresh_header()
        self._log_surface("extended")
        return len(added)

    @objc.python_method
    def prune_drafts(self, ns):
        """Hide not-yet-decided rows whose plan index `n` is in `ns` (a
        candidate that expired backend-side mid-review). The row's vertical
        slot is left in place rather than reflowed -- a small visible gap is
        an acceptable trade for never risking a mislaid, still-editable row
        underneath it."""
        if self._panel is None:
            return 0
        ns = set(ns)
        removed = 0
        for row in self._rows:
            if row["decided"] or row["hidden"]:
                continue
            if row["n"] in ns:
                row["hidden"] = True
                row["selected"] = False
                try:
                    row["container"].setHidden_(True)
                except Exception:
                    pass
                removed += 1
        if removed:
            self._refresh_header()
            self._log_surface("pruned")
        return removed

    # ---- status / observability (same shape as s4l_card, see _write_review_state) --

    @objc.python_method
    def _dwell_ms(self):
        if not self._presented_at:
            return None
        return int((time.time() - self._presented_at) * 1000)

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
        pending = sum(1 for r in self._rows if not r["decided"] and not r["hidden"])
        return {
            "open": self._panel is not None,
            "total": len(self._rows),
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
            f"canvas {event}: {s['pending']} pending of {s['total']}, "
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
        # Closing leaves undecided rows pending (not posted); the menu bar
        # treats this exactly like an unfinished corner-card stack (snooze,
        # re-present later), same as the corner card's own windowShouldClose_.
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
        _log(f"canvas closed: {len(self._decisions)} decided of {len(self._rows)}")
        _write_review_state(controller=self, last_event="closed")


# ---- module-level API, mirrors s4l_card.py's public surface -----------------


def present_review_canvas(drafts, on_decision=None, on_complete=None, focus=False):
    """Show the canvas review surface (main thread only). Same `drafts` shape
    and decision contract as s4l_card.present_review -- see that docstring
    and this module's for the differences. focus=True (the menu's "Review N
    pending drafts") activates the app; a fresh/auto presentation does not
    steal focus, same posture as the corner card."""
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
    for a bulk discard whose fate for every remaining row was already decided
    elsewhere (mirrors s4l_card.dismiss_active). Returns True if a window was
    actually open."""
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
    _log(f"canvas closed: dismissed (bulk discard, {len(c._decisions)} decided of {len(c._rows)})")
    _write_review_state(controller=c, last_event="dismissed")
    return True
