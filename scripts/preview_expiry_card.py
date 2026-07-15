# Scratch preview: renders review cards to visually verify (1) the combined
# "age (countdown)" header label and (2) the dynamic-fit thread-link box that
# keeps the trailing ↗ link inside the visible, clickable area even when the
# thread text has many hard line breaks (previously could push the arrow off
# the bottom of the fixed-height, non-scrolling quote box).
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


# 12 hard-newline-separated short lines: well under the old 200-char cap, but
# far more vertical lines than the ~74pt box can show. Before the fix, the
# trailing " ↗" landed on line 13, entirely below the visible/clickable box.
many_lines_text = "\n".join(f"Line {i}: short bullet point here" for i in range(1, 13))

drafts = [
    {
        "n": 1,
        "thread_author": "fresh_thread",
        "thread_text": "Card 1: fresh thread, ~10min old. Expect muted '10m (1h49m left)'.",
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
        "thread_text": "Card 2: 1h50m old. Expect BOLD '1h50m (9m left)'.",
        "reply_text": "draft reply 2",
        "thread_url": "https://x.com/near_cutoff/status/2",
        "candidate_id": 2,
        "project": "fazm",
        "platform": "twitter",
        "stats": {"likes": 34, "views": 900, "tweet_posted_at": _iso(110)},
    },
    {
        "n": 3,
        "thread_author": "overflow_thread",
        "thread_text": many_lines_text,
        "reply_text": "draft reply 3",
        "thread_url": "https://x.com/overflow_thread/status/3",
        "candidate_id": 3,
        "project": "fazm",
        "platform": "twitter",
        "stats": {"likes": 5, "views": 100, "tweet_posted_at": _iso(5)},
    },
]

app = NSApplication.sharedApplication()
s4l_card.present_review(
    drafts, on_decision=lambda d: print("decision:", d), on_complete=lambda ds: app.terminate_(None)
)
app.run()
