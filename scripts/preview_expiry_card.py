# Scratch preview: renders 4 review cards back-to-back to visually verify the
# new expiration-countdown label (replaces the old "age since posted" label).
# Card 1: fresh (~10min old) -> normal muted countdown, ~1h50m left.
# Card 2: near cutoff (1h50m old) -> urgent bold countdown, ~10m left.
# Card 3: past cutoff (3h old) -> "expired", urgent bold.
# Card 4: fresh but first-run-boost.json present -> 48h window, ~47h50m left.
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
        "thread_text": "Card 1: fresh thread, ~10min old. Expect a muted '1h49m left'.",
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
        "thread_text": "Card 2: 1h50m old thread. Expect a BOLD '9m left' (urgent, <=15min).",
        "reply_text": "draft reply 2",
        "thread_url": "https://x.com/near_cutoff/status/2",
        "candidate_id": 2,
        "project": "fazm",
        "platform": "twitter",
        "stats": {"likes": 34, "views": 900, "tweet_posted_at": _iso(110)},
    },
    {
        "n": 3,
        "thread_author": "past_cutoff",
        "thread_text": "Card 3: 3h old thread, past the 2h ceiling. Expect a BOLD 'expired'.",
        "reply_text": "draft reply 3",
        "thread_url": "https://x.com/past_cutoff/status/3",
        "candidate_id": 3,
        "project": "fazm",
        "platform": "twitter",
        "stats": {"likes": 5, "views": 100, "tweet_posted_at": _iso(180)},
    },
    {
        "n": 4,
        "thread_author": "reddit_thread",
        "thread_text": "Card 4: reddit platform. Expect NO countdown label at all (unchanged from before).",
        "reply_text": "draft reply 4",
        "thread_url": "https://reddit.com/r/test/comments/4",
        "candidate_id": 4,
        "project": "fazm",
        "platform": "reddit",
        "stats": {"likes": 5, "tweet_posted_at": _iso(10)},
    },
]

app = NSApplication.sharedApplication()
s4l_card.present_review(
    drafts, on_decision=lambda d: print("decision:", d), on_complete=lambda ds: app.terminate_(None)
)
app.run()
