"""Warmup v6: continue from v5. Upvote on t3_1taa51r was confirmed. Now do
the downvote on a different post and the 10-more-scroll. Recovers if the
page navigated away."""
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
      out.push({pid, upPressed: up.getAttribute('aria-pressed'), dnPressed: dn.getAttribute('aria-pressed'),
                upCx: ur.left + ur.width/2, upCy: ur.top + ur.height/2,
                dnCx: dr.left + dr.width/2, dnCy: dr.top + dr.height/2,
                visible: ur.top > 50 && ur.bottom < (window.innerHeight - 40)});
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
"""

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
  // Nudge a bit if center is right at bottom — Reddit's footer overlap can
  // place it under floating UI
  if (window.innerHeight - found.getBoundingClientRect().bottom < 80) {
    window.scrollBy(0, 80);
  }
  const r = found.getBoundingClientRect();
  return {ok: true, cx: r.left + r.width/2, cy: r.top + r.height/2, pressed: found.getAttribute('aria-pressed')};
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
        try:
            page.mouse.wheel(0, delta)
        except Exception as e:
            print(f"  wheel err (continuing): {e}")
            return
        moved += delta
        time.sleep(random.uniform(0.4, 0.95))


def trusted_click(page, pid, kind):
    pos = page.evaluate(SCROLL_INTO_VIEW_JS, {"pid": pid, "kind": kind})
    if not pos.get("ok"):
        return {"ok": False}
    time.sleep(random.uniform(0.4, 0.9))
    x, y = pos["cx"], pos["cy"]
    page.mouse.move(x - 6, y - 3)
    time.sleep(random.uniform(0.12, 0.28))
    page.mouse.move(x, y)
    time.sleep(random.uniform(0.1, 0.22))
    page.mouse.down()
    time.sleep(random.uniform(0.04, 0.11))
    page.mouse.up()
    time.sleep(random.uniform(0.7, 1.3))
    after = page.evaluate(READ_PRESSED_JS, {"pid": pid})
    return {"ok": True, "x": x, "y": y, "before_pressed": pos["pressed"], "after": after}


s = api("GET", f"/sessions/{SESSION_ID}")
print("status:", s.get("status"))
result = {"sessionId": SESSION_ID, "actions": []}

try:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(s["connectUrl"])
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print("starting url:", page.url)

        # If we navigated away from home (or to a post detail), come back
        if "/comments/" in page.url or "reddit.com" not in page.url or page.url.rstrip("/") != "https://www.reddit.com":
            print("  navigating back to home")
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)

        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1.0)
        slow_scroll(page, total_px=500)
        time.sleep(1.2)

        probe = page.evaluate(PROBE_JS)
        print(f"feed-card vote pairs: {len(probe)}")
        already = [c["pid"] for c in probe if c["upPressed"] == "true" or c["dnPressed"] == "true"]
        print(f"already voted: {already}")
        result["already_voted_before_run"] = already

        # downvote a NOT already-voted post, scroll into view, trusted-click
        downvoted_pid = None
        for c in probe:
            if c["upPressed"] == "true" or c["dnPressed"] == "true":
                continue
            print(f"  trusted-click downvote on {c['pid']}")
            res = trusted_click(page, c["pid"], "downvote")
            print(f"    -> {res}")
            if res.get("after", {}).get("dn") == "true":
                downvoted_pid = c["pid"]
                result["actions"].append({"type": "downvote", "pid": c["pid"], "verified": True})
                break
            # if page navigated away, recover
            try:
                _ = page.url
            except Exception:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
                time.sleep(3)

        # if upvote on t3_1taa51r still confirmed from v5
        result["actions"].insert(0, {"type": "upvote", "pid": "t3_1taa51r", "verified_in_previous_run": True})

        # scroll 10 more posts
        seen = {c["pid"] for c in probe}
        baseline = len(seen)
        print(f"baseline ids seen: {baseline}")
        for attempt in range(16):
            slow_scroll(page, total_px=random.randint(900, 1500))
            time.sleep(random.uniform(1.2, 2.4))
            try:
                cur = page.evaluate(PROBE_JS)
            except Exception as e:
                print(f"  probe err: {e}; reloading home")
                page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
                time.sleep(3)
                cur = page.evaluate(PROBE_JS)
            for c2 in cur:
                seen.add(c2["pid"])
            new_count = len(seen) - baseline
            print(f"  scroll {attempt+1}: total={len(seen)} (+{new_count})")
            if new_count >= 10:
                break

        result["actions"].append({"type": "scroll_more", "new_posts_seen": len(seen) - baseline, "total_ids_seen": len(seen)})
        time.sleep(2.0)
        try:
            page.screenshot(path="bb_dimatty_warmup6_final.png", full_page=False)
        except Exception:
            pass
        try:
            print("final url:", page.url)
        except Exception:
            pass
        browser.close()
finally:
    open("bb_dimatty_warmup6.json", "w").write(json.dumps(result, indent=2))
    print("=== RESULT ===")
    print(json.dumps(result, indent=2))
