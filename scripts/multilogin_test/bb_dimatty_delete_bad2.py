"""Find the overflow button by walking ALL shadow roots looking for a button
or element with icon-name='overflow-horizontal' near the bad comment, then
click it to expose the Delete menu. The previous attempt scoped its button
query to shreddit-comment, missing siblings."""
import json, random, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
SESSION_ID = "730b7ab3-7d3c-4fd7-8e19-91cccd277b79"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}


def api(method, path):
    req = urllib.request.Request(BASE + path, headers=HDR)
    with urllib.request.urlopen(req, None, timeout=40) as r:
        return json.load(r)


def trusted_click(page, cx, cy):
    page.mouse.move(cx - 6, cy - 3)
    time.sleep(random.uniform(0.15, 0.3))
    page.mouse.move(cx, cy)
    time.sleep(random.uniform(0.08, 0.18))
    page.mouse.down()
    time.sleep(random.uniform(0.05, 0.1))
    page.mouse.up()


s = api("GET", f"/sessions/{SESSION_ID}")
log = []

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    page = browser.contexts[0].pages[0]

    page.goto("https://www.reddit.com/r/CasualConversation/comments/1te9lt2/comment/om2coy3/?context=3",
              wait_until="domcontentloaded", timeout=45000)
    time.sleep(5)
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1.5)

    # Hover over the bad comment to expose hidden controls
    target = page.evaluate(r"""
() => {
  let found = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || found) return;
    root.querySelectorAll('shreddit-comment').forEach(el => {
      const t = (el.innerText || '').toLowerCase();
      if ((t.includes('three years ago and you have to remember') || t.startsWith('der'))
          && !t.includes('a barista sim')) {
        const r = el.getBoundingClientRect();
        found = {y: r.top, h: r.height, cx: r.left + r.width/2};
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !found) walk(n.shadowRoot); });
  }
  walk(document);
  return found;
}
""")
    print("bad comment loc:", target)
    if target:
        page.mouse.move(target["cx"], target["y"] + 30)
        time.sleep(1.0)

    # Now scan EVERY clickable element on the page for icon-name overflow-horizontal
    # or aria attributes hinting at the menu trigger. Don't scope to shreddit-comment.
    overflow_candidates = page.evaluate(r"""
() => {
  const out = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    // Look for elements with svg icon-name="overflow-horizontal" or similar
    root.querySelectorAll('svg').forEach(svg => {
      const name = svg.getAttribute('icon-name') || '';
      if (/overflow/i.test(name)) {
        // walk up to a clickable parent (button, a, [role=button])
        let cur = svg;
        while (cur && cur.tagName) {
          if (cur.tagName === 'BUTTON' || cur.tagName === 'A' || cur.getAttribute('role') === 'button' || cur.tagName?.toLowerCase() === 'faceplate-dropdown-menu') break;
          cur = cur.parentElement;
        }
        if (cur) {
          const r = cur.getBoundingClientRect();
          out.push({
            iconName: name,
            tag: cur.tagName.toLowerCase(),
            cx: r.left + r.width/2,
            cy: r.top + r.height/2,
            w: r.width,
            h: r.height,
            aria: cur.getAttribute('aria-label'),
          });
        }
      }
    });
    // also look for buttons with aria-label containing 'comment options' / 'more'
    root.querySelectorAll('button, [role="button"]').forEach(b => {
      const aria = (b.getAttribute('aria-label') || '').toLowerCase();
      if (aria.includes('comment options') || aria.includes('comment actions') || aria.includes('more options') || aria.includes('overflow')) {
        const r = b.getBoundingClientRect();
        out.push({tag: b.tagName.toLowerCase(), aria, cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height});
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
""")
    print(f"overflow candidates ({len(overflow_candidates)}):")
    for c in overflow_candidates[:20]:
        print(f"  {c}")

    # Choose the candidate closest to the bad-comment y (target['y'] + target['h']/2)
    if target and overflow_candidates:
        bad_y = target["y"] + target["h"] / 2
        scored = sorted(overflow_candidates, key=lambda c: abs(c.get("cy", 0) - bad_y))
        best = scored[0]
        print(f"  best by proximity: {best}")
        trusted_click(page, best["cx"], best["cy"])
        time.sleep(1.5)
        page.screenshot(path="bb_dimatty_after_overflow_click.png", full_page=False)

        # Find Delete menu item
        delete_item = page.evaluate(r"""
() => {
  let found = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || found) return;
    root.querySelectorAll('*').forEach(el => {
      const t = (el.innerText || '').trim();
      if ((t === 'Delete' || t === 'Delete comment') && !found) {
        const r = el.getBoundingClientRect();
        if (r.width > 20 && r.height > 10 && r.width < 400) found = {cx: r.left + r.width/2, cy: r.top + r.height/2, text: t};
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !found) walk(n.shadowRoot); });
  }
  walk(document);
  return found;
}
""")
        print(f"  delete item: {delete_item}")
        if delete_item:
            trusted_click(page, delete_item["cx"], delete_item["cy"])
            time.sleep(2.0)
            page.screenshot(path="bb_dimatty_after_delete_click.png", full_page=False)

            # Confirm
            confirm = page.evaluate(r"""
() => {
  let found = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || found) return;
    root.querySelectorAll('button').forEach(b => {
      const t = (b.innerText || '').trim();
      if ((t === 'Yes, delete' || t === 'Delete' || t === 'Confirm') && !found) {
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) found = {cx: r.left + r.width/2, cy: r.top + r.height/2, text: t};
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !found) walk(n.shadowRoot); });
  }
  walk(document);
  return found;
}
""")
            print(f"  confirm: {confirm}")
            if confirm:
                trusted_click(page, confirm["cx"], confirm["cy"])
                time.sleep(3.0)
                page.screenshot(path="bb_dimatty_after_confirm.png", full_page=False)
                log.append({"step": "delete", "ok": True})
            else:
                log.append({"step": "confirm", "ok": False, "reason": "no confirm button"})
        else:
            log.append({"step": "delete_item", "ok": False, "reason": "no Delete in menu"})
    else:
        log.append({"step": "find_overflow", "ok": False})
    browser.close()

print("\n=== LOG ===")
print(json.dumps(log, indent=2))
