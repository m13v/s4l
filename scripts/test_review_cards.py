"""Standalone UI smoke test for the draft-review corner cards.

Pops the s4l_card review panels with fake drafts and just PRINTS the decisions —
no posting, no menu bar, no permissions. Run it, click through the 3 cards
(edit one if you like), and it prints REVIEW_DONE then quits.
"""

import os
import sys

sys.path.insert(0, os.path.expanduser("~/social-autoposter/mcp/menubar"))

from AppKit import (  # noqa: E402
    NSApplication,
    NSApplicationActivationPolicyRegular,
)
import s4l_card  # noqa: E402

DRAFTS = [
    {
        "n": 1,
        "thread_author": "@founderfomo",
        "thread_text": "anyone else drowning in unread Slack threads? I lose ~2h/day just catching up after meetings.",
        "reply_text": "this is exactly why we built overnight digests: it summarizes the threads you missed so you start at inbox zero. happy to show how it works.",
        "link_url": "https://s4l.ai/r/abc123",
    },
    {
        "n": 2,
        "thread_author": "@devtoolsdaily",
        "thread_text": "what's your favorite way to track competitor launches without living in RSS?",
        "reply_text": "we watch 40+ launch feeds and ping you the moment a competitor ships, so you skip the manual RSS slog entirely.",
        "link_url": "https://s4l.ai/r/def456",
    },
    {
        "n": 3,
        "thread_author": "@nocodeneha",
        "thread_text": "is there a no-code way to auto-post to X without it looking like a bot?",
        "reply_text": "the trick is human-in-the-loop approval before anything posts. nothing goes out until you ok it. that's the whole design.",
        "link_url": None,
    },
]


def done(decisions):
    print("REVIEW_DONE:", decisions, flush=True)
    NSApplication.sharedApplication().terminate_(None)


app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
s4l_card.present_review(DRAFTS, done)
app.activateIgnoringOtherApps_(True)
app.run()
