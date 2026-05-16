"""Warmup browse on u/Negative_Spell899 via Browserbase (dimatty01 context).

Opens the home feed, scrolls organically, upvotes 1 post, downvotes 1 post,
then scrolls through 10 more posts. Prints the Live View URL early so the
user can watch in real time.
"""
import json, random, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
PROJECT_ID = "a95115ba-3653-4376-bff7-c6744d4ea18c"
CONTEXT_ID = "6274b94a-66cd-4735-8c19-0e45d313637f"  # dimatty01 / Negative_Spell899
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}


def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers=HDR)
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=40) as r:
        return json.load(r)


def slow_scroll(page, total_px, step=180):
    moved = 0
    while moved < total_px:
        delta = step + random.randint(-40, 60)
        page.mouse.wheel(0, delta)
        moved += delta
        time.sleep(random.uniform(0.45, 1.1))


def find_post_cards(page):
    # Reddit "shreddit" web component uses <shreddit-post> as the post card root
    return page.locator("shreddit-post").all()


def click_vote(page, post, kind):
    """kind = 'upvote' or 'downvote'. Reddit's vote buttons are inside a
    nested <shreddit-post-vote> shadow root in practice, but the host element
    exposes ariaLabel-based <button> children via slotted templates.
    """
    label = "upvote" if kind == "upvote" else "downvote"
    btn = post.locator(f'button[aria-label*="{label}" i]').first
    if not btn.count():
        return False
    try:
        btn.scroll_into_view_if_needed(timeout=4000)
        time.sleep(random.uniform(0.4, 0.9))
        btn.click(timeout=5000)
        return True
    except Exception as e:
        print(f"  click_vote({kind}) failed: {e}")
        return False


# 1) start a session on the dimatty context
s = api("POST", "/sessions", {
    "projectId": PROJECT_ID,
    "browserSettings": {"context": {"id": CONTEXT_ID, "persist": True}},
    "proxies": [{"type": "browserbase",
                 "geolocation": {"country": "US", "state": "CA", "city": "San Francisco"}}],
    "keepAlive": True,
    "timeout": 900,
})
print("session:", s["id"])
dbg = api("GET", f"/sessions/{s['id']}/debug")
live = dbg.get("debuggerFullscreenUrl") or dbg.get("debuggerUrl")
print("=== LIVE VIEW URL ===")
print(live)
print()

result = {"sessionId": s["id"], "liveViewUrl": live, "actions": []}

try:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(s["connectUrl"])
        c = browser.contexts[0]
        page = c.pages[0] if c.pages else c.new_page()

        # 2) open the home feed (logged-in best subreddits feed)
        page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        try:
            page.wait_for_selector("shreddit-post", timeout=20000)
        except Exception:
            print("no shreddit-post on initial load; current url:", page.url)

        # initial settle scroll
        slow_scroll(page, total_px=900)
        time.sleep(1.0)

        posts = find_post_cards(page)
        print(f"initial posts visible: {len(posts)}")

        # 3) upvote one organic-feeling post (skip pinned/promoted)
        upvoted = False
        for i, post in enumerate(posts):
            try:
                if post.get_attribute("promoted") in ("true", ""):
                    continue
            except Exception:
                pass
            print(f"  trying upvote on post #{i}")
            if click_vote(page, post, "upvote"):
                upvoted = True
                result["actions"].append({"type": "upvote", "index": i})
                print(f"  upvoted post #{i}")
                break
        if not upvoted:
            print("  WARN: could not upvote any visible post")

        # gap between actions, more scroll
        time.sleep(random.uniform(2.0, 4.0))
        slow_scroll(page, total_px=1100)
        time.sleep(1.0)

        posts = find_post_cards(page)
        print(f"posts after scroll: {len(posts)}")

        # 4) downvote one organic post, must be different from the upvoted one
        downvoted = False
        for i, post in enumerate(posts):
            if upvoted and i == result["actions"][0]["index"]:
                continue
            try:
                if post.get_attribute("promoted") in ("true", ""):
                    continue
            except Exception:
                pass
            print(f"  trying downvote on post #{i}")
            if click_vote(page, post, "downvote"):
                downvoted = True
                result["actions"].append({"type": "downvote", "index": i})
                print(f"  downvoted post #{i}")
                break
        if not downvoted:
            print("  WARN: could not downvote any visible post")

        # 5) scroll through 10 more posts (organic skim)
        target_more = 10
        seen_ids = set()
        for post in find_post_cards(page):
            pid = post.get_attribute("id") or post.get_attribute("permalink") or ""
            if pid:
                seen_ids.add(pid)
        baseline = len(seen_ids)
        print(f"baseline post ids seen: {baseline}, target {target_more} more")

        attempts = 0
        while attempts < 12:
            slow_scroll(page, total_px=random.randint(800, 1500))
            time.sleep(random.uniform(1.2, 2.4))
            cur = find_post_cards(page)
            for post in cur:
                pid = post.get_attribute("id") or post.get_attribute("permalink") or ""
                if pid:
                    seen_ids.add(pid)
            new_count = len(seen_ids) - baseline
            print(f"  scroll {attempts+1}: total ids={len(seen_ids)} ({new_count} new)")
            if new_count >= target_more:
                break
            attempts += 1

        result["actions"].append({"type": "scroll_more", "new_posts_seen": len(seen_ids) - baseline})
        time.sleep(2.0)
        page.screenshot(path="bb_dimatty_warmup_final.png", full_page=False)
        print("final url:", page.url)
        browser.close()
finally:
    open("bb_dimatty_warmup.json", "w").write(json.dumps(result, indent=2))
    print("=== RESULT ===")
    print(json.dumps(result, indent=2))
