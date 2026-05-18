#!/usr/bin/env python3
"""
Connect to running Twitter Chrome via CDP, read tweets, post replies, log to DB.

Each entry in TWEETS MUST include `engagement_style` (e.g. "critic",
"pattern_recognizer") so the A/B system can track which style was used.
See scripts/engagement_styles.py for the valid set and pick targets per
platform.
"""
import sys, os, time
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engagement_styles import VALID_STYLES
from http_api import api_post  # noqa: E402

# DB_URL retained for backward compat; no longer used. Writes route through
# /api/v1/posts via scripts/http_api.py since 2026-05-18.
DB_URL = None

CDP_PORT = sys.argv[1] if len(sys.argv) > 1 else '55363'

TWEETS = [
    {
        "url": "https://x.com/silva_ngozi/status/2042025663754822096",
        "author": "silva_ngozi",
        "octolens_id": 162091429,
        "keyword": "manus ai",
        "reply": "if you're hitting credit limits on manus you might want to check out fazm, it runs locally on your mac so there's no usage cap. open source too so you can just grab it from github. I switched from cloud based agents to local ones and it's way less stressful not watching credits drain",
        "project_name": "fazm"
    },
    {
        "url": "https://x.com/antonioventre_/status/2042005321044901941",
        "author": "antonioventre_",
        "octolens_id": 162078806,
        "keyword": "manus ai",
        "reply": "this is exactly why we built our agent to run fully local on your machine. no data leaves your computer, no one rewrites your copy or redesigns your visuals. the moment you hand control to a cloud platform they optimize for their interests not yours",
        "project_name": "fazm"
    }
]

def post_reply(page, tweet):
    url = tweet["url"]
    reply_text = tweet["reply"]

    print(f"\nNavigating to {url}")
    page.goto(url, wait_until='domcontentloaded', timeout=20000)
    page.wait_for_timeout(4000)

    # Read thread context
    tweets_data = page.evaluate('''() => {
        const tweets = document.querySelectorAll('[data-testid="tweet"]');
        const results = [];
        for (let i = 0; i < Math.min(tweets.length, 5); i++) {
            const t = tweets[i];
            const author = t.querySelector('[data-testid="User-Name"]')?.textContent || '';
            const text = t.querySelector('[data-testid="tweetText"]')?.textContent || '';
            results.push({ author, text });
        }
        return results;
    }''')

    print("Thread context:")
    for i, t in enumerate(tweets_data):
        print(f"  [{i}] {t['author']}: {t['text'][:120]}")

    if not tweets_data:
        print("ERROR: Could not load tweet content. Skipping.")
        return None

    thread_content = tweets_data[0]['text'] if tweets_data else ''
    thread_title = thread_content

    # Click reply on the main tweet
    reply_btn = page.query_selector('[data-testid="tweet"] [data-testid="reply"]')
    if not reply_btn:
        print("ERROR: No reply button found. Skipping.")
        return None

    reply_btn.click()
    page.wait_for_timeout(2000)

    # Type reply
    reply_box = page.query_selector('[data-testid="tweetTextarea_0"]')
    if not reply_box:
        print("ERROR: No reply text box. Skipping.")
        return None

    reply_box.click()
    page.wait_for_timeout(500)
    page.keyboard.type(reply_text, delay=15)
    page.wait_for_timeout(1000)

    # Post
    post_btn = page.query_selector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]')
    if not post_btn:
        print("ERROR: No post button. Skipping.")
        return None

    post_btn.click()
    page.wait_for_timeout(3000)

    print(f"Reply posted to {url}")
    return {
        "thread_content": thread_content,
        "thread_title": thread_title
    }

def main():
    print(f"Connecting to Chrome CDP on port {CDP_PORT}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{CDP_PORT}')
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        for tweet in TWEETS:
            style = tweet.get("engagement_style")
            if not style or style not in VALID_STYLES:
                print(f"SKIPPED (missing/invalid engagement_style): {tweet['url']}")
                continue
            page = context.new_page()
            try:
                result = post_reply(page, tweet)
                if result:
                    resp = api_post(
                        "/api/v1/posts",
                        {
                            "platform": "twitter",
                            "thread_url": tweet["url"],
                            "thread_author": tweet["author"],
                            "thread_author_handle": tweet["author"],
                            "thread_title": result["thread_title"],
                            "thread_content": result["thread_content"],
                            "our_url": tweet["url"],
                            "our_content": tweet["reply"],
                            "our_account": "@m13v_",
                            "source_summary": f"octolens: {tweet['keyword']}",
                            "project": tweet["project_name"],
                            "engagement_style": style,
                            "status": "active",
                        },
                        ok_on_conflict=True,
                    )
                    data = resp.get("data") or {}
                    post = data.get("post") or {}
                    if not post and resp.get("error"):
                        details = (resp.get("error") or {}).get("details") or {}
                        post = details.get("post") or {}
                    post_id = post.get("id")
                    print(f"Logged to DB: id={post_id}")
                else:
                    print(f"SKIPPED: {tweet['url']}")
            except Exception as e:
                print(f"Error: {e}")
            finally:
                page.close()
            time.sleep(2)

        # Don't close the browser - it belongs to the other agent
        browser.close()  # disconnects CDP, doesn't close browser

    print("\nDone.")

if __name__ == '__main__':
    main()
