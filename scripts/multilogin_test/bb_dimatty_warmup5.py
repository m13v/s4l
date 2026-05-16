"""Warmup v5: programmatic .click() didn't register on shreddit vote buttons
(aria-pressed stayed false). Use real mouse coordinates via Playwright's
trusted-input pipeline instead. Reconnect to same keepAlive session."""
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


# Probe AND scroll-to-center the chosen button via the page before reporting
# its viewport-relative coords. Returns {x, y, pressed}.
PROBE_JS = r"""
() => {
  const seen = new Set();
  const out = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('shreddit-vote-animations').forEach(el => {
      const pid = el.getAttribute('thing-id');
      if (!pid || seen.has(pid)) return;
      seen.add(pid);
      const up = el.querySelector('button[upvote]');
      const dn = el.querySelector('button[downvote]');
      if (!up || !dn) return;
      const ur = up.getBoundingClientRect();
      const dr = dn.getBoundingClientRect();
      out.push({
        pid,
        upPressed: up.getAttribute('aria-pressed'),
        dnPressed: dn.getAttribute('aria-pressed'),
        upCx: ur.left + ur.width/2, upCy: ur.top + ur.height/2, upW: ur.width, upH: ur.height,
        dnCx: dr.left + dr.width/2, dnCy: dr.top + dr.height/2, dnW: dr.width, dnH: dr.height,
      });
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
"""

# Scroll a specific button into the middle of the viewport.
SCROLL_INTO_VIEW_JS = r"""
({pid, kind}) => {
  let found = null;
  function walk(root) {
    if (!root || found) return;
    root.querySelectorAll('shreddit-vote-animations').forEach(el => {
      if (el.getAttribute('thing-id') === pid) {
        found = el.querySelector(kind === 'upvote' ? 'button[upvote]' : 'button[downvote]');
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !found) walk(n.shadowRoot); });
  }
  walk(document);
  if (!found) return {ok: false};
  found.scrollIntoView({block: 'center', behavior: 'instant'});
  const r = found.getBoundingClientRect();
  return {ok: true, cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height, pressed: found.getAttribute('aria-pressed')};
}
"""

READ_PRESSED_JS = r"""
({pid}) => {
  let pressed = {up: null, dn: null};
  function walk(root) {
    if (!root) return;
    root.querySelectorAll('shreddit-vote-animations').forEach(el => {
      if (el.getAttribute('thing-id') === pid) {
        const up = el.querySelector('button[upvote]');
        const dn = el.querySelector('button[downvote]');
        if (up) pressed.up = up.getAttribute('aria-pressed');
        if (dn) pressed.dn = dn.getAttribute('aria-pressed');
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return pressed;
}
"""


def slow_scroll(page, total_px, step=180):
    moved = 0
    while moved < total_px:
        delta = step + random.randint(-40, 60)
        page.mouse.wheel(0, delta)
        moved += delta
        time.sleep(random.uniform(0.4, 0.95))


def trusted_click(page, pid, kind):
    """Scroll the target button to viewport center, then click at its real
    coordinates via mouse.move + mouse.click (trusted gesture)."""
    pos = page.evaluate(SCROLL_INTO_VIEW_JS, {"pid": pid, "kind": kind})
    if not pos.get("ok"):
        return {"ok": False, "reason": "no element"}
    time.sleep(random.uniform(0.4, 0.9))
    x, y = pos["cx"], pos["cy"]
    page.mouse.move(x - 8, y - 4)
    time.sleep(random.uniform(0.15, 0.3))
    page.mouse.move(x, y)
    time.sleep(random.uniform(0.1, 0.25))
    page.mouse.down()
    time.sleep(random.uniform(0.04, 0.12))
    page.mouse.up()
    time.sleep(random.uniform(0.6, 1.2))
    after = page.evaluate(READ_PRESSED_JS, {"pid": pid})
    return {"ok": True, "x": x, "y": y, "before_pressed": pos["pressed"], "after": after}


s = api("GET", f"/sessions/{SESSION_ID}")
print("status:", s.get("status"))
result = {"sessionId": SESSION_ID, "actions": []}

try:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(s["connectUrl"])
        page = browser.contexts[0].pages[0]
        print("url:", page.url)
        if "reddit.com" not in page.url:
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1.0)
        slow_scroll(page, total_px=500)
        time.sleep(1.2)

        probe = page.evaluate(PROBE_JS)
        print(f"found {len(probe)} feed-card vote pairs")
        if probe:
            print("first sample:", probe[0])

        # 1) upvote first not-yet-voted post
        upvoted_pid = None
        for c in probe:
            if c["upPressed"] == "true" or c["dnPressed"] == "true":
                continue
            print(f"  trusted-click upvote on {c['pid']}")
            res = trusted_click(page, c["pid"], "upvote")
            print(f"    -> {res}")
            if res.get("after", {}).get("up") == "true":
                upvoted_pid = c["pid"]
                result["actions"].append({"type": "upvote", "pid": c["pid"], "verified": True})
                break
            else:
                print(f"    not registered, retrying next post")

        time.sleep(random.uniform(2.5, 4.0))
        slow_scroll(page, total_px=900)
        time.sleep(1.0)

        # 2) downvote different post
        probe = page.evaluate(PROBE_JS)
        for c in probe:
            if c["pid"] == upvoted_pid:
                continue
            if c["upPressed"] == "true" or c["dnPressed"] == "true":
                continue
            print(f"  trusted-click downvote on {c['pid']}")
            res = trusted_click(page, c["pid"], "downvote")
            print(f"    -> {res}")
            if res.get("after", {}).get("dn") == "true":
                result["actions"].append({"type": "downvote", "pid": c["pid"], "verified": True})
                break

        # 3) scroll 10 more posts (look for new post ids)
        seen = {c["pid"] for c in probe}
        baseline = len(seen)
        print(f"baseline post ids: {baseline}")
        for attempt in range(16):
            slow_scroll(page, total_px=random.randint(900, 1500))
            time.sleep(random.uniform(1.2, 2.4))
            cur = page.evaluate(PROBE_JS)
            for c2 in cur:
                seen.add(c2["pid"])
            new_count = len(seen) - baseline
            print(f"  scroll {attempt+1}: total={len(seen)} (+{new_count})")
            if new_count >= 10:
                break

        result["actions"].append({"type": "scroll_more", "new_posts_seen": len(seen) - baseline, "total_ids_seen": len(seen)})
        time.sleep(2.0)
        page.screenshot(path="bb_dimatty_warmup5_final.png", full_page=False)
        print("final url:", page.url)
        browser.close()
finally:
    open("bb_dimatty_warmup5.json", "w").write(json.dumps(result, indent=2))
    print("=== RESULT ===")
    print(json.dumps(result, indent=2))
