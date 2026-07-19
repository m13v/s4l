# Scratch preview: renders one review card with a Japanese draft carrying
# thread_text_en / reply_text_en, to visually verify the translation UI.
import os, sys
os.environ["S4L_STATE_DIR"] = "/tmp/s4l-card-preview-state"
os.makedirs("/tmp/s4l-card-preview-state", exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp", "menubar"))
from AppKit import NSApplication
import s4l_card

drafts = [{
    "n": 1,
    "thread_author": "yamada_dev",
    "thread_text": "AIツールで開発ワークフローを自動化する方法について考えている。特にコードレビューの自動化が気になる。誰か実際に使っている人いますか？",
    "thread_text_en": "Thinking about how to automate dev workflows with AI tools. Especially curious about automating code review. Anyone actually using this?",
    "reply_text": "コードレビュー自動化、実際に使ってみると指摘の8割はスタイルの話でした。本質的なバグはまだ人間の目が必要ですね。",
    "reply_text_en": "Tried code review automation; 80% of its comments were style nits. Real bugs still need human eyes.",
    "language": "ja",
    "thread_url": "https://x.com/yamada_dev/status/123",
    "candidate_id": 1,
    "project": "fazm",
    "engagement_style": "contrarian_take",
    "search_topic": "ai code review",
    "stats": {"likes": 120, "views": 5400, "author_followers": 3200,
              "tweet_posted_at": "2026-07-06T19:00:00Z"},
    "experiments": {"draft_prompt": "control_v2"},
}]
app = NSApplication.sharedApplication()
s4l_card.present_review(drafts, on_decision=lambda d: None,
                        on_complete=lambda ds: app.terminate_(None))
app.run()
