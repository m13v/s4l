"""Warmup v4: now that we know vote buttons use `upvote=""` / `downvote=""`
attributes (no aria-label) and live inside shadow roots, target them directly.

Plan: scroll, upvote 1 post, downvote 1 different post, view 10 more posts.
Reconnects to keepAlive session a442c5c1-ae41-4d5f-a9ec-d0bd0b9d0268.
"""
import json, random, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
SESSION_ID = "a442c5c1-ae41-4d5f-a9ec-d0bd0b9d0268"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}


def api(method, path):
    req = urllib.request.Request(BASE + path, headers=HDR)
    with urllib.request.urlopen(req, None, timeout=40) as r:
        return json.load(r)


# Walk every shadow root to find shreddit-vote-animations elements, then
# their light-DOM <button upvote> / <button downvote> children, dedupe by
# thing-id (post id).
PROBE_JS = r"""
() => {
  const upvotes = new Map();   // postId -> button info
  const downvotes = new Map();
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('*').forEach(n => {
      if (n.tagName && n.tagName.toLowerCase() === 'shreddit-vote-animations') {
        const pid = n.getAttribute('thing-id');
        if (pid) {
          const up = n.querySelector('button[upvote]');
          const dn = n.querySelector('button[downvote]');
          if (up && !upvotes.has(pid)) {
            const r = up.getBoundingClientRect();
            upvotes.set(pid, {pid, pressed: up.getAttribute('aria-pressed'), y: Math.round(r.top), x: Math.round(r.left), visible: r.width > 0});
          }
          if (dn && !downvotes.has(pid)) {
            const r = dn.getBoundingClientRect();
            downvotes.set(pid, {pid, pressed: dn.getAttribute('aria-pressed'), y: Math.round(r.top), x: Math.round(r.left), visible: r.width > 0});
          }
        }
      }
      if (n.shadowRoot) walk(n.shadowRoot);
    });
  }
  walk(document);
  return {
    upvotes: Array.from(upvotes.values()),
    downvotes: Array.from(downvotes.values()),
  };
}
"""


CLICK_JS = r"""
({pid, kind}) => {
  let found = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || found) return;
    const anims = root.querySelectorAll('shreddit-vote-animations');
    for (let i = 0; i < anims.length; i++) {
      const el = anims[i];
      if (el.getAttribute('thing-id') === pid) {
        found = el.querySelector(kind === 'upvote' ? 'button[upvote]' : 'button[downvote]');
        if (found) return;
      }
    }
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !found) walk(n.shadowRoot); });
  }
  walk(document);
  if (!found) return {ok: false, reason: 'not found'};
  found.scrollIntoView({block: 'center'});
  // Reddit's vote button toggles aria-pressed on click.
  const before = found.getAttribute('aria-pressed');
  found.click();
  // After: read the same attribute
  const after = found.getAttribute('aria-pressed');
  return {ok: true, before, after};
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
        page = browser.contexts[0].pages[0]
        print("url:", page.url)

        if "reddit.com" not in page.url or "/login" in page.url:
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1.5)
        slow_scroll(page, total_px=700)
        time.sleep(1.0)

        probe = page.evaluate(PROBE_JS)
        print(f"found {len(probe['upvotes'])} upvote buttons / {len(probe['downvotes'])} downvote buttons")
        if probe["upvotes"]:
            print("  sample:", probe["upvotes"][0])

        # 1) upvote first visible, not already pressed
        upvoted_pid = None
        for cand in probe["upvotes"]:
            if cand.get("pressed") == "true":
                print(f"  skip {cand['pid']} (already upvoted)")
                continue
            if not cand.get("visible"):
                continue
            print(f"  -> click upvote on {cand['pid']}")
            res = page.evaluate(CLICK_JS, {"pid": cand["pid"], "kind": "upvote"})
            print(f"     result: {res}")
            if res.get("ok"):
                upvoted_pid = cand["pid"]
                result["actions"].append({"type": "upvote", "pid": cand["pid"], "result": res})
                break

        time.sleep(random.uniform(2.5, 4.0))
        slow_scroll(page, total_px=900)
        time.sleep(1.2)

        # 2) downvote a different post
        probe = page.evaluate(PROBE_JS)
        for cand in probe["downvotes"]:
            if cand["pid"] == upvoted_pid:
                continue
            if cand.get("pressed") == "true":
                continue
            if not cand.get("visible"):
                continue
            print(f"  -> click downvote on {cand['pid']}")
            res = page.evaluate(CLICK_JS, {"pid": cand["pid"], "kind": "downvote"})
            print(f"     result: {res}")
            if res.get("ok"):
                result["actions"].append({"type": "downvote", "pid": cand["pid"], "result": res})
                break

        # 3) scroll 10 more
        seen = {c["pid"] for c in probe["upvotes"]} | {c["pid"] for c in probe["downvotes"]}
        baseline = len(seen)
        print(f"baseline post ids: {baseline}")
        for attempt in range(14):
            slow_scroll(page, total_px=random.randint(800, 1300))
            time.sleep(random.uniform(1.0, 2.2))
            cur = page.evaluate(PROBE_JS)
            for c2 in cur["upvotes"]:
                seen.add(c2["pid"])
            for c2 in cur["downvotes"]:
                seen.add(c2["pid"])
            new_count = len(seen) - baseline
            print(f"  scroll {attempt+1}: total={len(seen)} (+{new_count})")
            if new_count >= 10:
                break

        result["actions"].append({"type": "scroll_more", "new_posts_seen": len(seen) - baseline, "total_ids_seen": len(seen)})
        time.sleep(2.0)
        page.screenshot(path="bb_dimatty_warmup4_final.png", full_page=False)
        print("final url:", page.url)
        browser.close()
finally:
    open("bb_dimatty_warmup4.json", "w").write(json.dumps(result, indent=2))
    print("=== RESULT ===")
    print(json.dumps(result, indent=2))
