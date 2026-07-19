# Scratch preview: renders one Reddit review card with a title + selftext,
# to visually verify the bold-title-over-body thread box (2026-07-17).
import os, sys
os.environ["S4L_STATE_DIR"] = "/tmp/s4l-card-preview-state"
os.makedirs("/tmp/s4l-card-preview-state", exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp", "menubar"))
from AppKit import NSApplication
import s4l_card

drafts = [{
    "n": 1,
    "platform": "reddit",
    "thread_author": "SureThing07",
    "thread_text": "Im fu**ed. I've realised the whole game is a placeholder",
    "thread_selftext": (
        "I've been building this solo for eight months and I just opened the "
        "codebase after a week off. Half of it is stubs I meant to come back "
        "to. The save system? Placeholder. The combat? Placeholder wired to a "
        "real animation so it LOOKED done. I genuinely can't tell how much of "
        "this game actually exists anymore. How do you all deal with this?"
    ),
    "reply_text": (
        "my rule of thumb from solo projects: by month six, maybe a third of "
        "the code is actually load-bearing and the rest is scaffolding you "
        "forgot you wrote."
    ),
    "thread_url": "https://www.reddit.com/r/gamedev/comments/abc123/placeholder/",
    "candidate_id": "rd-preview1",
    "project": "podlog",
    "engagement_style": "empathize",
    "search_topic": "solo dev burnout",
    "stats": {"replies": 34, "author_followers": 0,
              "tweet_posted_at": "2026-07-17T14:00:00Z"},
}]
app = NSApplication.sharedApplication()
s4l_card.present_review(drafts, on_decision=lambda d: None,
                        on_complete=lambda ds: app.terminate_(None))
app.run()
