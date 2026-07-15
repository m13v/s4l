# Scratch preview: verify the hover popover on the header's age/expiry label
# -- a second-granular ticking countdown plus the fixed education message.
# Triggers _show_expiry_popover() directly (in-process, on the main thread
# via NSTimer) instead of simulating OS mouse hover, so the test isolates the
# popover/timer logic itself from synthetic-input quirks.
import datetime
import os
import sys

os.environ["S4L_STATE_DIR"] = "/tmp/s4l-card-preview-state"
os.makedirs("/tmp/s4l-card-preview-state", exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "social-autoposter", "mcp", "menubar"))
from AppKit import NSApplication
from Foundation import NSTimer
import s4l_card

now = datetime.datetime.now(datetime.timezone.utc)


def _iso(minutes_ago):
    return (now - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


drafts = [
    {
        "n": 1,
        "thread_author": "fresh_thread",
        "thread_text": "Card 1: the hover popover is triggered programmatically for this test.",
        "reply_text": "draft reply 1",
        "thread_url": "https://x.com/fresh_thread/status/1",
        "candidate_id": 1,
        "project": "fazm",
        "platform": "twitter",
        "stats": {"likes": 12, "views": 500, "tweet_posted_at": _iso(10)},
    },
]

app = NSApplication.sharedApplication()
s4l_card.present_review(
    drafts, on_decision=lambda d: print("decision:", d), on_complete=lambda ds: app.terminate_(None)
)


def _trigger(timer):
    if s4l_card._active is not None:
        s4l_card._active._show_expiry_popover()
        print("TRIGGERED popover", flush=True)


_t = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(1.0, False, _trigger)

app.run()
