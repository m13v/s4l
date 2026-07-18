"""Headless logic test for per-card serialized posting in the menu bar.

Stubs the heavy deps (rumps / sentry_init / s4l_state) so s4l_menubar imports
without AppKit, then drives the REAL _on_card_decision + _post_worker_loop on an
instance built via object.__new__ (bypassing rumps.App.__init__). Verifies:
  1. posts run strictly one-at-a-time (no overlap on the shared browser)
  2. order is preserved (FIFO)
  3. plain approvals -> post=[n]; edited approvals -> edits=[{n,text}]
  4. rejected cards never post
  5. _posts_outstanding / _review_active settle to idle once drained
  6. activity progress reflects the approved burst total, not each 1-item call
"""
import os
import queue
import sys
import threading
import time
import types

HERE = os.path.join(os.path.dirname(__file__), "..", "mcp", "menubar")
sys.path.insert(0, os.path.abspath(HERE))

# --- stub heavy deps so the import is headless --------------------------------
rumps = types.ModuleType("rumps")
class _App:
    def __init__(self, *a, **k):
        pass
rumps.App = _App
rumps.Timer = lambda *a, **k: types.SimpleNamespace(start=lambda: None, stop=lambda: None)
rumps.MenuItem = lambda *a, **k: object()
rumps.separator = object()
rumps.notification = lambda *a, **k: None
sys.modules["rumps"] = rumps

sentry_init = types.ModuleType("sentry_init")
sentry_init.init_sentry = lambda *a, **k: None
sentry_init.capture = lambda *a, **k: None
sys.modules["sentry_init"] = sentry_init

# Track concurrency + record every approve_drafts call.
overlap_detected = []
inflight = {"n": 0}
inflight_lock = threading.Lock()
calls = []
activity_events = []

def fake_approve_drafts(batch_id, post=None, edits=None, timeout=900, activity_label=None):
    with inflight_lock:
        inflight["n"] += 1
        if inflight["n"] > 1:
            overlap_detected.append(True)
    calls.append(
        {
            "batch": batch_id,
            "post": post or [],
            "edits": edits or [],
            "activity_label": activity_label,
        }
    )
    time.sleep(0.15)  # simulate a slow post so overlaps would be caught
    with inflight_lock:
        inflight["n"] -= 1
    # mimic the real shape: posted count
    n_posted = len(post or []) + len(edits or [])
    return {"posted": n_posted}

st = types.ModuleType("s4l_state")
st.approve_drafts = fake_approve_drafts
st.write_activity = lambda state, label: activity_events.append((state, label))
st.accessibility_trusted = lambda: True
st.clear_review_request = lambda: None
sys.modules["s4l_state"] = st

import s4l_menubar  # noqa: E402

# --- build an instance without running rumps.App.__init__ ---------------------
app = object.__new__(s4l_menubar.S4LMenuBar)
app._post_q = queue.Queue()
app._post_worker = None
app._review_lock = threading.Lock()
app._panel_open = True
app._posts_outstanding = 0
app._posting_batch_total = 0
app._posting_batch_done = 0
app._review_active = False
app._notify = lambda title, msg: None  # silence Notification Center

BATCH = "review-queue"

# Approve a quick burst (as if the user clicked Approve on several cards fast),
# one edited, plus a rejected card that must NOT post.
decisions = [
    {"n": 1, "approved": True, "text": "reply one", "edited": False},
    {"n": 2, "approved": True, "text": "edited two", "edited": True},
    {"n": 3, "approved": False, "text": "skip", "edited": False},
    {"n": 4, "approved": True, "text": "reply four", "edited": False},
]
for d in decisions:
    app._on_card_decision(BATCH, d)
    time.sleep(0.02)  # tight succession -> overlap would happen if not serialized

# Panel closes while posts may still be draining.
app._on_review_closed(BATCH, decisions)

# Wait for the queue to drain.
deadline = time.time() + 10
while time.time() < deadline:
    with app._review_lock:
        if app._posts_outstanding == 0 and app._post_q.empty():
            break
    time.sleep(0.05)

# --- assertions ---------------------------------------------------------------
fail = []
if overlap_detected:
    fail.append(f"posts overlapped ({len(overlap_detected)} times) — not serialized")
posted_ns = [(c["post"], c["edits"]) for c in calls]
expected = [([1], []), ([], [{"n": 2, "text": "edited two"}]), ([4], [])]
if posted_ns != expected:
    fail.append(f"wrong calls/order: got {posted_ns}\n  expected {expected}")
labels = [c["activity_label"] for c in calls if c.get("activity_label")]
if not any(label == "posting 2/3" for label in labels):
    fail.append(f"second post did not carry burst progress 2/3: labels={labels}")
if not any(label == "posting 3/3" for label in labels):
    fail.append(f"third post did not carry burst progress 3/3: labels={labels}")
if not any(event == ("posting", "posting 1/3") for event in activity_events):
    fail.append(f"activity never expanded first post to 1/3: events={activity_events}")
if any(c["post"] == [3] or any(e.get("n") == 3 for e in c["edits"]) for c in calls):
    fail.append("rejected card #3 was posted")
with app._review_lock:
    if app._posts_outstanding != 0:
        fail.append(f"_posts_outstanding leaked: {app._posts_outstanding}")
    if app._review_active:
        fail.append("_review_active stuck true after drain + panel closed")

if fail:
    print("FAIL:")
    for f in fail:
        print("  -", f)
    sys.exit(1)
print("PASS: 3 posts, serialized, FIFO order, #3 skipped, flags settled idle.")
print("  calls:", posted_ns)
