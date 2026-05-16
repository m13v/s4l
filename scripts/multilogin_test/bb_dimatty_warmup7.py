"""Warmup v7: start a FRESH Browserbase session on the dimatty01 context
(previous one auto-completed). Do the full sequence: upvote 1 post, downvote
1 different post, view 10 more posts in the feed. Print Live View URL early.
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
      out.push({pid, upPressed: up.getAttribute('aria-pressed'), dnPressed: dn.getAttribute('aria-pressed'),
                y: ur.top});
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
  // Nudge to keep clearly inside viewport (avoid bottom edge / floating UI)
  const r0 = found.getBoundingClientRect();
  if (window.innerHeight - r0.bottom < 120) window.scrollBy(0, 120);
  if (r0.top < 100) window.scrollBy(0, -120);
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
        except Exception:
            return
        moved += delta
        time.sleep(random.uniform(0.4, 0.95))


def trusted_click(page, pid, kind):
    pos = page.evaluate(SCROLL_INTO_VIEW_JS, {"pid": pid, "kind": kind})
    if not pos.get("ok"):
        return {"ok": False, "reason": "no element"}
    time.sleep(random.uniform(0.4, 0.9))
    x, y = pos["cx"], pos["cy"]
    page.mouse.move(x - 6, y - 3)
    time.sleep(random.uniform(0.12, 0.28))
    page.mouse.move(x, y)
    time.sleep(random.uniform(0.1, 0.22))
    page.mouse.down()
    time.sleep(random.uniform(0.04, 0.11))
    page.mouse.up()
    time.sleep(random.uniform(0.8, 1.4))
    try:
        after = page.evaluate(READ_PRESSED_JS, {"pid": pid})
    except Exception:
        after = {"err": "page closed"}
    return {"ok": True, "x": x, "y": y, "before_pressed": pos["pressed"], "after": after}


# 1) Start fresh session
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
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1.5)
        slow_scroll(page, total_px=600)
        time.sleep(1.2)

        probe = page.evaluate(PROBE_JS)
        print(f"vote pairs: {len(probe)}")
        already = [c["pid"] for c in probe if c["upPressed"] == "true" or c["dnPressed"] == "true"]
        print(f"already voted (carry-over from previous session): {already}")
        result["already_voted_before_run"] = already

        # --- 1) upvote one NEW post ---
        upvoted_pid = None
        for c in probe:
            if c["upPressed"] == "true" or c["dnPressed"] == "true":
                continue
            print(f"  upvote -> {c['pid']}")
            res = trusted_click(page, c["pid"], "upvote")
            print(f"    {res}")
            after = res.get("after", {})
            if isinstance(after, dict) and after.get("up") == "true":
                upvoted_pid = c["pid"]
                result["actions"].append({"type": "upvote", "pid": c["pid"], "verified": True})
                break

        time.sleep(random.uniform(2.5, 4.0))
        slow_scroll(page, total_px=700)
        time.sleep(1.0)

        # --- 2) downvote a different NEW post ---
        try:
            probe = page.evaluate(PROBE_JS)
        except Exception:
            # page might have nav'd via overlay anchor; come back
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)
            probe = page.evaluate(PROBE_JS)
        downvoted_pid = None
        for c in probe:
            if c["pid"] == upvoted_pid:
                continue
            if c["upPressed"] == "true" or c["dnPressed"] == "true":
                continue
            print(f"  downvote -> {c['pid']}")
            res = trusted_click(page, c["pid"], "downvote")
            print(f"    {res}")
            after = res.get("after", {})
            if isinstance(after, dict) and after.get("dn") == "true":
                downvoted_pid = c["pid"]
                result["actions"].append({"type": "downvote", "pid": c["pid"], "verified": True})
                break

        # --- 3) scroll past 10 more posts ---
        try:
            seen = {c["pid"] for c in page.evaluate(PROBE_JS)}
        except Exception:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)
            seen = {c["pid"] for c in page.evaluate(PROBE_JS)}
        baseline = len(seen)
        print(f"baseline ids seen: {baseline}")
        for attempt in range(18):
            slow_scroll(page, total_px=random.randint(900, 1400))
            time.sleep(random.uniform(1.2, 2.4))
            try:
                cur = page.evaluate(PROBE_JS)
            except Exception:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
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
            page.screenshot(path="bb_dimatty_warmup7_final.png", full_page=False)
            print("final url:", page.url)
        except Exception:
            pass
        browser.close()
finally:
    open("bb_dimatty_warmup7.json", "w").write(json.dumps(result, indent=2))
    print("=== RESULT ===")
    print(json.dumps(result, indent=2))
