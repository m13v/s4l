"""Delete the partial broken comment ('dered ONCE...') from u/Negative_Spell899.
Strategy:
  1. From profile/comments, find the permalink of the bad comment (innerText
     starts with 'der' / contains 'three years ago').
  2. Navigate to that comment's permalink.
  3. Find the overflow ('...') button on THAT specific comment, click it.
  4. Click 'Delete' from the dropdown.
  5. Confirm.
"""
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
print("status:", s.get("status"))
log = []

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    page = browser.contexts[0].pages[0]

    # 1) Find permalink of the bad comment from the profile page
    page.goto("https://www.reddit.com/user/Negative_Spell899/comments/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(4)

    permalink = page.evaluate(r"""
() => {
  let found = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || found) return;
    root.querySelectorAll('shreddit-profile-comment').forEach(el => {
      if (found) return;
      const text = (el.innerText || '').toLowerCase();
      // partial bad one starts with 'der' or contains 'three years ago' and lacks 'a barista sim'
      if ((text.includes('three years ago and you have to remember') || /commented \d+ min\. ago der/.test(text))
          && !text.includes('a barista sim')) {
        // find any <a> with /comments/ in href inside this card
        const anchors = el.querySelectorAll('a[href*="/comments/"]');
        for (const a of anchors) {
          const h = a.getAttribute('href') || '';
          if (h.includes('/comment/')) { found = h; return; }
        }
        // fallback: first comments anchor
        if (anchors.length) found = anchors[0].getAttribute('href');
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !found) walk(n.shadowRoot); });
  }
  walk(document);
  return found;
}
""")
    print("bad comment permalink:", permalink)
    if not permalink:
        log.append({"step": "find_permalink", "ok": False, "reason": "not found"})
    else:
        # navigate directly to the bad comment
        full = "https://www.reddit.com" + permalink if permalink.startswith("/") else permalink
        print("navigating:", full)
        page.goto(full, wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)

        # 2) find the overflow button next to the bad comment
        # Look for shreddit-comment whose innerText matches the bad pattern AND
        # locate its overflow trigger (button with aria-label or icon)
        overflow_info = page.evaluate(r"""
() => {
  let target = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || target) return;
    root.querySelectorAll('shreddit-comment').forEach(el => {
      if (target) return;
      const t = (el.innerText || '').toLowerCase();
      if ((t.includes('three years ago and you have to remember') || t.startsWith('der'))
          && !t.includes('a barista sim')) {
        // overflow trigger: any button with aria-label containing 'options' or 'menu' or
        // a button slotted into faceplate-dropdown-menu
        let trigger = el.querySelector('button[aria-label*="options" i], button[aria-label*="menu" i], button[aria-label*="actions" i]');
        if (!trigger) {
          // Look for buttons that contain icon-name="overflow-horizontal" svg
          el.querySelectorAll('button').forEach(b => {
            if (trigger) return;
            const svg = b.querySelector('svg');
            if (svg && (svg.getAttribute('icon-name') || '').includes('overflow')) trigger = b;
          });
        }
        if (trigger) {
          const r = trigger.getBoundingClientRect();
          target = {cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height,
                    aria: trigger.getAttribute('aria-label'),
                    commentId: el.id, commentY: el.getBoundingClientRect().top};
        }
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !target) walk(n.shadowRoot); });
  }
  walk(document);
  return target;
}
""")
        print("overflow trigger:", overflow_info)
        if not overflow_info:
            log.append({"step": "overflow_trigger", "ok": False, "reason": "no overflow button"})
        else:
            # ensure in view
            if overflow_info["cy"] > 600 or overflow_info["cy"] < 150:
                page.evaluate("(y) => window.scrollTo(0, Math.max(0, y - 300))", overflow_info["commentY"])
                time.sleep(1.2)
                # re-locate
                overflow_info = page.evaluate(r"""
() => {
  let target = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || target) return;
    root.querySelectorAll('shreddit-comment').forEach(el => {
      if (target) return;
      const t = (el.innerText || '').toLowerCase();
      if ((t.includes('three years ago and you have to remember') || t.startsWith('der'))
          && !t.includes('a barista sim')) {
        let trigger = el.querySelector('button[aria-label*="options" i], button[aria-label*="menu" i], button[aria-label*="actions" i]');
        if (!trigger) {
          el.querySelectorAll('button').forEach(b => {
            if (trigger) return;
            const svg = b.querySelector('svg');
            if (svg && (svg.getAttribute('icon-name') || '').includes('overflow')) trigger = b;
          });
        }
        if (trigger) {
          const r = trigger.getBoundingClientRect();
          target = {cx: r.left + r.width/2, cy: r.top + r.height/2};
        }
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !target) walk(n.shadowRoot); });
  }
  walk(document);
  return target;
}
""")
            print("repositioned overflow:", overflow_info)
            if overflow_info:
                trusted_click(page, overflow_info["cx"], overflow_info["cy"])
                time.sleep(1.5)

                # 3) click Delete
                delete_btn = page.evaluate(r"""
() => {
  let found = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || found) return;
    root.querySelectorAll('*').forEach(el => {
      const t = (el.innerText || '').trim();
      if (t === 'Delete') {
        const r = el.getBoundingClientRect();
        if (r.width > 20 && r.height > 10 && r.width < 400) found = {cx: r.left + r.width/2, cy: r.top + r.height/2};
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !found) walk(n.shadowRoot); });
  }
  walk(document);
  return found;
}
""")
                print("delete menu item:", delete_btn)
                if delete_btn:
                    trusted_click(page, delete_btn["cx"], delete_btn["cy"])
                    time.sleep(2.0)

                    # 4) confirm dialog: Yes, delete / Confirm / Delete
                    confirm = page.evaluate(r"""
() => {
  let found = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || found) return;
    root.querySelectorAll('button').forEach(b => {
      const t = (b.innerText || '').trim();
      if ((t === 'Yes, delete' || t === 'Confirm' || t === 'Delete') && !found) {
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
                    print("confirm button:", confirm)
                    if confirm:
                        trusted_click(page, confirm["cx"], confirm["cy"])
                        time.sleep(3.0)
                        log.append({"step": "delete", "ok": True})
                    else:
                        log.append({"step": "confirm", "ok": False})
                else:
                    log.append({"step": "delete_item", "ok": False})

        page.screenshot(path="bb_dimatty_delete_after.png", full_page=False)

    browser.close()

print("\n=== LOG ===")
print(json.dumps(log, indent=2))
