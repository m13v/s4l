"""Warmup browse on u/Negative_Spell899 via Browserbase. Pierces shadow DOM
of <shreddit-vote-animations> (where Reddit's vote buttons actually live).

Plan: scroll feed organically, upvote 1 post, downvote 1 different post,
then scroll through 10 more posts. Reconnects to the keepAlive session
created by bb_dimatty_warmup.py.
"""
import json, random, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
SESSION_ID = "a442c5c1-ae41-4d5f-a9ec-d0bd0b9d0268"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}


def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers=HDR)
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=40) as r:
        return json.load(r)


# Pierce shadow DOM of shreddit-vote-animations to find upvote/downvote
# buttons. Each <shreddit-vote-animations> is rendered once per post-card
# (in feed cards, post detail headers, and comment trees), so we filter to
# the ones inside a feed-card row.
PROBE_JS = r"""
() => {
  const out = [];
  const anims = document.querySelectorAll('shreddit-vote-animations');
  anims.forEach((el, i) => {
    const rect = el.getBoundingClientRect();
    const shadow = el.shadowRoot;
    const btns = shadow ? shadow.querySelectorAll('button') : [];
    const btnInfo = Array.from(btns).map(b => ({
      aria: b.getAttribute('aria-label'),
      cls: (b.className || '').slice(0, 60),
    }));
    // Find what post-card this anim belongs to. The anim is rendered as a
    // sibling/descendant of <shreddit-post>; walk up to find it.
    let host = el;
    let post = null;
    while (host && host.tagName?.toLowerCase() !== 'body') {
      if (host.tagName?.toLowerCase() === 'shreddit-post') { post = host; break; }
      host = host.parentElement || host.getRootNode().host;
    }
    out.push({
      i,
      visible: rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight + 200 && rect.bottom > -200,
      y: Math.round(rect.top),
      postId: post?.getAttribute('id') || null,
      postPermalink: post?.getAttribute('permalink') || null,
      promoted: post?.getAttribute('promoted') || null,
      btns: btnInfo,
    });
  });
  return out;
}
"""


CLICK_JS = r"""
({i, kind}) => {
  const anims = document.querySelectorAll('shreddit-vote-animations');
  const el = anims[i];
  if (!el || !el.shadowRoot) return {ok: false, reason: 'no shadow'};
  const sel = kind === 'upvote'
    ? 'button[aria-label*="upvote" i], button.upvote, button[upvote]'
    : 'button[aria-label*="downvote" i], button.downvote, button[downvote]';
  const btn = el.shadowRoot.querySelector(sel);
  if (!btn) {
    const allBtns = Array.from(el.shadowRoot.querySelectorAll('button')).map(b => b.getAttribute('aria-label'));
    return {ok: false, reason: 'no btn', allBtns};
  }
  btn.scrollIntoView({block: 'center'});
  btn.click();
  return {ok: true, aria: btn.getAttribute('aria-label'), pressed: btn.getAttribute('aria-pressed')};
}
"""


def slow_scroll(page, total_px, step=180):
    moved = 0
    while moved < total_px:
        delta = step + random.randint(-40, 60)
        page.mouse.wheel(0, delta)
        moved += delta
        time.sleep(random.uniform(0.4, 1.0))


def find_card_anims(page):
    """Return list of {i, postId, ...} for shreddit-vote-animations that are
    attached to a feed-card post (postId not null)."""
    info = page.evaluate(PROBE_JS)
    return [x for x in info if x["postId"] and not x.get("promoted")]


# Reconnect to live session
s = api("GET", f"/sessions/{SESSION_ID}")
print("session status:", s.get("status"))
result = {"sessionId": SESSION_ID, "actions": []}

try:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(s["connectUrl"])
        c = browser.contexts[0]
        page = c.pages[0] if c.pages else c.new_page()
        print("current url:", page.url)

        # Ensure on home feed
        if "reddit.com" not in page.url or "/login" in page.url:
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)
        # Scroll back to top for clean baseline
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1.5)

        # initial probe + a settle scroll
        slow_scroll(page, total_px=600)
        time.sleep(1.0)

        cards = find_card_anims(page)
        print(f"found {len(cards)} feed-card vote widgets")
        if not cards:
            print("DEBUG: probe all anims:")
            print(json.dumps(page.evaluate(PROBE_JS)[:6], indent=2))

        # 1) upvote first eligible card
        upvoted_post_id = None
        for card in cards:
            print(f"  trying upvote on post {card['postId']} (anim #{card['i']})")
            res = page.evaluate(CLICK_JS, {"i": card["i"], "kind": "upvote"})
            print(f"    -> {res}")
            if res.get("ok"):
                upvoted_post_id = card["postId"]
                result["actions"].append({"type": "upvote", "postId": card["postId"], "result": res})
                break

        time.sleep(random.uniform(2.0, 3.5))
        slow_scroll(page, total_px=900)
        time.sleep(1.2)

        # 2) downvote a different card
        cards = find_card_anims(page)
        for card in cards:
            if card["postId"] == upvoted_post_id:
                continue
            print(f"  trying downvote on post {card['postId']} (anim #{card['i']})")
            res = page.evaluate(CLICK_JS, {"i": card["i"], "kind": "downvote"})
            print(f"    -> {res}")
            if res.get("ok"):
                result["actions"].append({"type": "downvote", "postId": card["postId"], "result": res})
                break

        # 3) scroll through 10 more posts (organic skim, post IDs)
        seen_ids = {c["postId"] for c in cards}
        baseline = len(seen_ids)
        print(f"baseline post ids seen: {baseline}, target 10 more")
        for attempt in range(14):
            slow_scroll(page, total_px=random.randint(800, 1400))
            time.sleep(random.uniform(1.0, 2.3))
            cur = find_card_anims(page)
            for c2 in cur:
                seen_ids.add(c2["postId"])
            new_count = len(seen_ids) - baseline
            print(f"  scroll {attempt+1}: total ids={len(seen_ids)} ({new_count} new)")
            if new_count >= 10:
                break

        result["actions"].append({"type": "scroll_more", "new_posts_seen": len(seen_ids) - baseline, "total_ids": len(seen_ids)})
        time.sleep(2.0)
        page.screenshot(path="bb_dimatty_warmup2_final.png", full_page=False)
        print("final url:", page.url)
        browser.close()
finally:
    open("bb_dimatty_warmup2.json", "w").write(json.dumps(result, indent=2))
    print("=== RESULT ===")
    print(json.dumps(result, indent=2))
