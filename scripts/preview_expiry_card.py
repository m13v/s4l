# Scratch preview: verify the header's age/expiry label now ticks LIVE every
# second, inline on the card ("10m ago (1h49m20s left)"), independent of
# hover, and that hovering over it shows the fixed education-message popover.
import datetime
import os
import sys

os.environ["S4L_STATE_DIR"] = "/tmp/s4l-card-preview-state"
os.makedirs("/tmp/s4l-card-preview-state", exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "social-autoposter", "mcp", "menubar"))
from AppKit import NSApplication
import s4l_card

now = datetime.datetime.now(datetime.timezone.utc)


def _iso(minutes_ago):
    return (now - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


drafts = [
    {
        "n": 1,
        "thread_author": "fresh_thread",
        "thread_text": "Card 1: fresh (~10min old). Header should read '10m ago (1h49mXXs left)' and tick every second.",
        "reply_text": "draft reply 1",
        "thread_url": "https://x.com/fresh_thread/status/1",
        "candidate_id": 1,
        "project": "fazm",
        "platform": "twitter",
        "stats": {"likes": 12, "views": 500, "tweet_posted_at": _iso(10)},
    },
    {
        "n": 2,
        "thread_author": "near_cutoff",
        "thread_text": "Card 2: 1h50m old. Header should be BOLD (<=15min left).",
        "reply_text": "draft reply 2",
        "thread_url": "https://x.com/near_cutoff/status/2",
        "candidate_id": 2,
        "project": "fazm",
        "platform": "twitter",
        "stats": {"likes": 34, "views": 900, "tweet_posted_at": _iso(110)},
    },
]

app = NSApplication.sharedApplication()
s4l_card.present_review(
    drafts, on_decision=lambda d: print("decision:", d), on_complete=lambda ds: app.terminate_(None)
)
app.run()
