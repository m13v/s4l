# Scratch preview: verify the hover popover on the header's age/expiry label
# -- a second-granular ticking countdown plus the fixed education message.
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
        "thread_text": "Card 1: hover over the age/expiry label top-right to see the live seconds countdown + education message.",
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
app.run()
