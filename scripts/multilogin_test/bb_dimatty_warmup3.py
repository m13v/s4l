"""Warmup v3: deep-walks shadow DOMs to find vote buttons.
Reddit's vote action row sits inside multiple nested shadow roots, so we walk
the entire DOM/shadow tree and collect every <button aria-label="...upvote...">
along with the closest enclosing <shreddit-post>'s id."""
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


# Probe: deep-walk every element + every shadow root, collect buttons whose
# aria-label matches up/downvote, dedupe by closest shreddit-post id.
PROBE_JS = r"""
() => {
  const upvotes = new Map();   // postId -> {sig}
  const downvotes = new Map(); // postId -> {sig}
  function host(node) {
    // walk from node up through light + shadow tree until <shreddit-post> or null
    let cur = node;
    while (cur) {
      if (cur.tagName?.toLowerCase() === 'shreddit-post') return cur;
      cur = cur.assignedSlot || cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
    }
    return null;
  }
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    const all = root.querySelectorAll('*');
    all.forEach(n => {
      if (n.tagName === 'BUTTON') {
        const aria = (n.getAttribute('aria-label') || '').toLowerCase();
        if (aria.includes('upvote') || aria.includes('downvote')) {
          const post = host(n);
          const postId = post?.getAttribute('id') || null;
          if (postId && !post.hasAttribute('promoted')) {
            const map = aria.includes('upvote') ? upvotes : downvotes;
            if (!map.has(postId)) {
              const rect = n.getBoundingClientRect();
              map.set(postId, {
                sig: aria,
                y: Math.round(rect.top),
                x: Math.round(rect.left),
                w: Math.round(rect.width),
                h: Math.round(rect.height),
                visible: rect.width > 0 && rect.height > 0,
                permalink: post.getAttribute('permalink'),
              });
            }
          }
        }
      }
      if (n.shadowRoot) walk(n.shadowRoot);
    });
  }
  walk(document);
  return {
    upvotes: Array.from(upvotes.entries()).map(([k, v]) => ({postId: k, ...v})),
    downvotes: Array.from(downvotes.entries()).map(([k, v]) => ({postId: k, ...v})),
  };
}
"""


CLICK_JS = r"""
({postId, kind}) => {
  // Re-walk because nodes may have been re-rendered.
  function host(node) {
    let cur = node;
    while (cur) {
      if (cur.tagName?.toLowerCase() === 'shreddit-post') return cur;
      cur = cur.assignedSlot || cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
    }
    return null;
  }
  let found = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || found) return;
    const all = root.querySelectorAll('*');
    for (let i = 0; i < all.length; i++) {
      const n = all[i];
      if (n.tagName === 'BUTTON') {
        const aria = (n.getAttribute('aria-label') || '').toLowerCase();
        const wanted = kind === 'upvote' ? aria.includes('upvote') && !aria.includes('downvote') : aria.includes('downvote');
        if (wanted) {
          const post = host(n);
          if (post?.getAttribute('id') === postId) { found = n; return; }
        }
      }
      if (n.shadowRoot) walk(n.shadowRoot);
    }
  }
  walk(document);
  if (!found) return {ok: false, reason: 'not found'};
  found.scrollIntoView({block: 'center'});
  found.click();
  return {ok: true, aria: found.getAttribute('aria-label'), pressed: found.getAttribute('aria-pressed')};
}
"""


def slow_scroll(page, total_px, step=180):
    moved = 0
    while moved < total_px:
        delta = step + random.randint(-40, 60)
        page.mouse.wheel(0, delta)
        moved += delta
        time.sleep(random.uniform(0.4, 0.95))


s = api("GET", f"/sessions/{SESSION_ID}")
print("session status:", s.get("status"))
result = {"sessionId": SESSION_ID, "actions": []}

try:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(s["connectUrl"])
        c = browser.contexts[0]
        page = c.pages[0] if c.pages else c.new_page()
        print("url:", page.url)

        if "reddit.com" not in page.url or "/login" in page.url:
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1.5)

        slow_scroll(page, total_px=600)
        time.sleep(1.0)

        probe = page.evaluate(PROBE_JS)
        print(f"upvote buttons: {len(probe['upvotes'])}, downvote buttons: {len(probe['downvotes'])}")
        if probe["upvotes"]:
            print("  sample upvote:", json.dumps(probe["upvotes"][0], indent=2))

        # 1) upvote first eligible
        upvoted_post = None
        for cand in probe["upvotes"]:
            print(f"  trying upvote on {cand['postId']}")
            res = page.evaluate(CLICK_JS, {"postId": cand["postId"], "kind": "upvote"})
            print(f"    -> {res}")
            if res.get("ok"):
                upvoted_post = cand["postId"]
                result["actions"].append({"type": "upvote", "postId": cand["postId"], "permalink": cand["permalink"], "result": res})
                break

        time.sleep(random.uniform(2.0, 3.5))
        slow_scroll(page, total_px=900)
        time.sleep(1.2)

        # 2) downvote different post
        probe = page.evaluate(PROBE_JS)
        for cand in probe["downvotes"]:
            if cand["postId"] == upvoted_post:
                continue
            print(f"  trying downvote on {cand['postId']}")
            res = page.evaluate(CLICK_JS, {"postId": cand["postId"], "kind": "downvote"})
            print(f"    -> {res}")
            if res.get("ok"):
                result["actions"].append({"type": "downvote", "postId": cand["postId"], "permalink": cand["permalink"], "result": res})
                break

        # 3) scroll 10 more
        seen = {c["postId"] for c in probe["upvotes"]} | {c["postId"] for c in probe["downvotes"]}
        baseline = len(seen)
        print(f"baseline: {baseline} unique post ids")
        for attempt in range(14):
            slow_scroll(page, total_px=random.randint(800, 1300))
            time.sleep(random.uniform(1.0, 2.2))
            cur = page.evaluate(PROBE_JS)
            for c2 in cur["upvotes"]:
                seen.add(c2["postId"])
            for c2 in cur["downvotes"]:
                seen.add(c2["postId"])
            new_count = len(seen) - baseline
            print(f"  scroll {attempt+1}: total={len(seen)} (+{new_count})")
            if new_count >= 10:
                break

        result["actions"].append({"type": "scroll_more", "new_posts_seen": len(seen) - baseline, "total_ids_seen": len(seen)})
        time.sleep(2.0)
        page.screenshot(path="bb_dimatty_warmup3_final.png", full_page=False)
        print("final url:", page.url)
        browser.close()
finally:
    open("bb_dimatty_warmup3.json", "w").write(json.dumps(result, indent=2))
    print("=== RESULT ===")
    print(json.dumps(result, indent=2))
