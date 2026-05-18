#!/usr/bin/env python3
"""
Octolens Twitter batch: wait for browser lock, read tweets, post replies, log to DB.

Each entry in TWEETS MUST include `engagement_style` (e.g. "critic",
"pattern_recognizer") so the A/B system can track which style was used.
See scripts/engagement_styles.py for the valid set and pick targets per
platform; the caller should choose a style that fits the thread AND is
under-represented vs target in recent posts.
"""
import sys, os, time, subprocess
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engagement_styles import VALID_STYLES
from http_api import api_post  # noqa: E402

# DB_URL kept as a placeholder for any downstream tooling that still expects
# it; not used by this script after the 2026-05-18 routes migration. Writes
# now go through /api/v1/posts via scripts/http_api.py.
DB_URL = None

PROFILE = os.path.expanduser('~/.claude/browser-profiles/twitter')
LOCK_FILE = os.path.expanduser('~/.claude/browser-profiles/twitter/SingletonLock')

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

def wait_for_browser_free(max_wait=300):
    """Wait until no other Chrome is using the twitter profile."""
    start = time.time()
    while time.time() - start < max_wait:
        result = subprocess.run(['pgrep', '-f', 'browser-profiles/twitter'], capture_output=True, text=True)
        if not result.stdout.strip():
            print("Browser is free.")
            return True
        elapsed = int(time.time() - start)
        print(f"Waiting for twitter browser to free up... ({elapsed}s)")
        time.sleep(5)
    print("Timeout waiting for browser lock.")
    return False

def post_reply_and_log(browser, tweet):
    url = tweet["url"]
    reply_text = tweet["reply"]
    style = tweet.get("engagement_style")
    if not style or style not in VALID_STYLES:
        raise ValueError(
            f"TWEETS entry for {url} is missing a valid engagement_style "
            f"(got {style!r}). Valid styles: {sorted(VALID_STYLES)}."
        )

    page = browser.new_page()
    try:
        print(f"\nNavigating to {url}")
        page.goto(url, wait_until='domcontentloaded', timeout=20000)
        page.wait_for_timeout(4000)

        # Read the thread context
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
            print(f"  [{i}] {t['author']}: {t['text'][:100]}")

        if not tweets_data:
            print("ERROR: Could not load tweet. Skipping.")
            page.close()
            return None

        thread_content = tweets_data[0]['text'] if tweets_data else ''
        thread_title = thread_content

        # Click reply button on the main tweet
        reply_btn = page.query_selector('[data-testid="tweet"] [data-testid="reply"]')
        if not reply_btn:
            print("ERROR: Could not find reply button. Skipping.")
            page.close()
            return None

        reply_btn.click()
        page.wait_for_timeout(2000)

        # Type in the reply box
        reply_box = page.query_selector('[data-testid="tweetTextarea_0"]')
        if not reply_box:
            print("ERROR: Could not find reply text box. Skipping.")
            page.close()
            return None

        reply_box.click()
        page.wait_for_timeout(500)
        page.keyboard.type(reply_text, delay=20)
        page.wait_for_timeout(1000)

        # Click the Reply/Post button
        post_btn = page.query_selector('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]')
        if not post_btn:
            print("ERROR: Could not find post button. Skipping.")
            page.close()
            return None

        post_btn.click()
        page.wait_for_timeout(3000)

        # Try to capture our reply URL
        our_url = url  # fallback
        print(f"Reply posted to {url}")

        # Log to database via /api/v1/posts. Same row shape, route handles
        # the (platform, thread_url) dedup so re-running this batch is safe.
        resp = api_post(
            "/api/v1/posts",
            {
                "platform": "twitter",
                "thread_url": url,
                "thread_author": tweet["author"],
                "thread_author_handle": tweet["author"],
                "thread_title": thread_title,
                "thread_content": thread_content,
                "our_url": our_url,
                "our_content": reply_text,
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
        print(f"Logged to DB with id={post_id}")

        page.close()
        return post_id

    except Exception as e:
        print(f"Error processing {url}: {e}")
        page.close()
        return None

def main():
    print("Octolens Twitter batch poster")
    print(f"Tweets to process: {len(TWEETS)}")

    if not wait_for_browser_free():
        # Try removing stale lock
        if os.path.exists(LOCK_FILE):
            result = subprocess.run(['pgrep', '-f', 'browser-profiles/twitter'], capture_output=True, text=True)
            if not result.stdout.strip():
                os.remove(LOCK_FILE)
                print("Removed stale SingletonLock")
            else:
                print("Browser still running, cannot proceed.")
                sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            PROFILE,
            headless=False,
            viewport={'width': 911, 'height': 1016},
            args=['--window-position=3042,-1032', '--window-size=911,1016']
        )

        results = []
        for tweet in TWEETS:
            post_id = post_reply_and_log(browser, tweet)
            results.append({"url": tweet["url"], "post_id": post_id})
            time.sleep(2)

        browser.close()

    print("\n=== Results ===")
    for r in results:
        status = "OK" if r["post_id"] else "FAILED"
        print(f"  {status}: {r['url']} (db id: {r.get('post_id')})")

if __name__ == '__main__':
    main()
