"""Cleanup + retry:
  1. Delete the partial broken comment on r/CasualConversation (first text
     bytes leaked into the search bar before the composer focused).
  2. Post the two intended comments cleanly. This time, find the actual
     comment composer via the "Join the conversation" placeholder text and
     click that to expand it before typing.

Targets:
  - r/CasualConversation/comments/1te9lt2 (game-pitch)
  - r/AskReddit/comments/1tau246 (Andy Serkis as Gollum)
"""
import json, random, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
PROJECT_ID = "a95115ba-3653-4376-bff7-c6744d4ea18c"
CONTEXT_ID = "6274b94a-66cd-4735-8c19-0e45d313637f"
SESSION_ID = "730b7ab3-7d3c-4fd7-8e19-91cccd277b79"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}


COMMENTS = [
    {
        "permalink": "/r/CasualConversation/comments/1te9lt2/i_build_small_instant_web_games_for_a_living_if/",
        "label": "casualconversation_gamepitch",
        "text": "a barista sim where every regular has an absurdly specific off-menu drink they ordered ONCE three years ago and you have to remember it perfectly or they leave forever. names range from \"bumbleberry three-pump oat lavender no foam\" to \"steve.\" the only menu in the shop is your own memory.",
    },
    {
        "permalink": "/r/AskReddit/comments/1tau246/whos_an_actor_that_nailed_a_role_so_hard_that/",
        "label": "askreddit_serkis",
        "text": "Andy Serkis as Gollum. He didn't just play the part, he basically wrote the rulebook for how mocap acting is supposed to work. 20+ years and better tech later and nobody else's motion-capture performance has hit that same level of physical commitment plus actual character.",
    },
]


def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers=HDR)
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=40) as r:
        return json.load(r)


def ensure_session():
    try:
        s = api("GET", f"/sessions/{SESSION_ID}")
        if s.get("status") == "RUNNING":
            return s
    except Exception:
        pass
    s = api("POST", "/sessions", {
        "projectId": PROJECT_ID,
        "browserSettings": {"context": {"id": CONTEXT_ID, "persist": True}},
        "proxies": [{"type": "browserbase",
                     "geolocation": {"country": "US", "state": "CA", "city": "San Francisco"}}],
        "keepAlive": True,
        "timeout": 1800,
    })
    return s


def slow_scroll(page, total_px, step=180):
    moved = 0
    while moved < total_px:
        delta = step + random.randint(-30, 50)
        try:
            page.mouse.wheel(0, delta)
        except Exception:
            return
        moved += delta
        time.sleep(random.uniform(0.35, 0.85))


def human_type(page, text):
    for ch in text:
        page.keyboard.type(ch)
        if ch in '.,!?':
            time.sleep(random.uniform(0.18, 0.38))
        elif ch == ' ':
            time.sleep(random.uniform(0.05, 0.14))
        else:
            time.sleep(random.uniform(0.03, 0.08))


# Find the "Join the conversation" placeholder box (the COMMENT composer placeholder,
# NOT the top search bar). It's typically a focusable div or button with that text.
FIND_PLACEHOLDER_JS = r"""
() => {
  const results = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('*').forEach(n => {
      const t = (n.innerText || '').trim();
      if (t === 'Join the conversation' || t === 'Add a comment' || t === 'What are your thoughts?') {
        const r = n.getBoundingClientRect();
        if (r.width > 100 && r.height > 20) {
          results.push({tag: n.tagName.toLowerCase(), text: t, cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height, role: n.getAttribute('role')});
        }
      }
      if (n.shadowRoot) walk(n.shadowRoot);
    });
  }
  walk(document);
  return results;
}
"""

# Find the activated comment composer (contenteditable inside the comment area,
# below the post body, NOT the page header search input). Filter by:
#  - y position must be > 100 (below header)
#  - parent chain must NOT include shreddit-app-header / faceplate-search
FIND_ACTIVE_COMPOSER_JS = r"""
() => {
  const results = [];
  function inHeader(node) {
    let cur = node;
    while (cur) {
      const tag = cur.tagName?.toLowerCase();
      if (tag === 'header' || tag === 'shreddit-app-header' || tag === 'faceplate-search-input' || tag === 'reddit-search-large') return true;
      const cls = (cur.className || '').toString();
      if (typeof cls === 'string' && (cls.includes('search') && cls.includes('header'))) return true;
      cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
    }
    return false;
  }
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('[contenteditable="true"]').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.top < 80) return;  // above the header
      if (inHeader(el)) return;
      if (r.width < 200 || r.height < 24) return;
      results.push({type: 'contenteditable', cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height, role: el.getAttribute('role'), aria: el.getAttribute('aria-label'), placeholder: el.getAttribute('data-placeholder')});
    });
    // also handle <textarea name="text"> in case shreddit fall-back form is used
    root.querySelectorAll('textarea').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.top < 80) return;
      if (inHeader(el)) return;
      if (r.width < 200) return;
      results.push({type: 'textarea', cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height, name: el.name, placeholder: el.getAttribute('placeholder')});
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return results;
}
"""

# Find Comment submit button (only after composer has text)
FIND_SUBMIT_JS = r"""
() => {
  const results = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('button').forEach(b => {
      const t = (b.innerText || '').trim();
      const aria = b.getAttribute('aria-label') || '';
      const r = b.getBoundingClientRect();
      if ((t === 'Comment' || aria === 'Comment') && r.width > 0 && r.height > 0 && r.top > 80) {
        results.push({cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height, text: t, aria, disabled: b.disabled});
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return results;
}
"""


def trusted_click(page, cx, cy):
    page.mouse.move(cx - 6, cy - 3)
    time.sleep(random.uniform(0.15, 0.3))
    page.mouse.move(cx, cy)
    time.sleep(random.uniform(0.08, 0.18))
    page.mouse.down()
    time.sleep(random.uniform(0.05, 0.1))
    page.mouse.up()


def post_comment(page, permalink, text, label, log):
    full_url = "https://www.reddit.com" + permalink
    print(f"\n[{label}] navigating to {full_url}")
    page.goto(full_url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(random.uniform(3.5, 5.0))

    # scroll down a bit so the placeholder is in viewport
    slow_scroll(page, total_px=random.randint(350, 500))
    time.sleep(1.2)

    # find the placeholder (Join the conversation / Add a comment)
    holders = page.evaluate(FIND_PLACEHOLDER_JS)
    print(f"  placeholders found: {holders}")
    if not holders:
        # try scrolling more, the placeholder might be lower
        slow_scroll(page, total_px=300)
        time.sleep(1.0)
        holders = page.evaluate(FIND_PLACEHOLDER_JS)
        print(f"  placeholders after extra scroll: {holders}")
    if not holders:
        log.append({"label": label, "ok": False, "reason": "no placeholder"})
        return False

    h = holders[0]
    # nudge into clear viewport (avoid being too close to top/bottom)
    if h["cy"] < 200:
        page.mouse.wheel(0, -150); time.sleep(0.6)
    if h["cy"] > 600:
        page.mouse.wheel(0, int(h["cy"] - 400)); time.sleep(0.6)
    holders = page.evaluate(FIND_PLACEHOLDER_JS)
    h = holders[0]
    print(f"  clicking placeholder at ({h['cx']}, {h['cy']})")
    trusted_click(page, h["cx"], h["cy"])
    time.sleep(random.uniform(1.5, 2.5))

    # composer should now be active. Find it (excluding the search bar).
    composers = page.evaluate(FIND_ACTIVE_COMPOSER_JS)
    print(f"  active composers: {composers}")
    if not composers:
        # composer may not yet be focused; click placeholder coords again
        trusted_click(page, h["cx"], h["cy"])
        time.sleep(2.0)
        composers = page.evaluate(FIND_ACTIVE_COMPOSER_JS)
        print(f"  composers retry: {composers}")
    if not composers:
        log.append({"label": label, "ok": False, "reason": "no composer after placeholder"})
        return False

    comp = composers[0]
    print(f"  composer at ({comp['cx']}, {comp['cy']}) type={comp['type']}")
    # explicitly click into the composer to focus before typing
    trusted_click(page, comp["cx"], comp["cy"])
    time.sleep(random.uniform(0.8, 1.4))

    # type
    human_type(page, text)
    time.sleep(random.uniform(1.2, 2.0))

    # find submit
    submits = page.evaluate(FIND_SUBMIT_JS)
    print(f"  submit candidates: {submits}")
    enabled = [s for s in submits if not s.get("disabled")]
    if enabled:
        s = enabled[0]
        print(f"  clicking submit at ({s['cx']}, {s['cy']})")
        trusted_click(page, s["cx"], s["cy"])
        time.sleep(random.uniform(3.5, 5.0))
    else:
        print("  no enabled submit, trying Ctrl+Enter")
        page.keyboard.press("Control+Enter")
        time.sleep(3.5)

    # verify by searching for distinctive snippet of comment text
    snippet = text[:40]
    found = page.evaluate("(s) => document.body.innerText.includes(s)", snippet)
    print(f"  comment present after submit: {found}")
    page.screenshot(path=f"bb_dimatty_recomment_{label}.png", full_page=False)
    log.append({"label": label, "ok": found, "url": page.url, "snippet": snippet})
    return found


def delete_bad_comment(page, log):
    """Visit u/Negative_Spell899's comment history, find the bad comment
    (text contains 'bumbleberry'), open its overflow menu, click Delete."""
    print("\n[cleanup] visiting profile to delete bad comment")
    page.goto("https://www.reddit.com/user/Negative_Spell899/comments/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(4.0)

    # Find a comment whose text contains "bumbleberry" or "dered ONCE"
    # Walk shadow DOMs for shreddit-profile-comment elements.
    info = page.evaluate(r"""
() => {
  const found = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('shreddit-profile-comment, shreddit-comment').forEach(el => {
      const t = (el.innerText || '').toLowerCase();
      if (t.includes('bumbleberry') || t.includes('dered once') || t.includes('three years ago and you have to remember')) {
        const r = el.getBoundingClientRect();
        // find the overflow menu button inside this comment
        const overflowBtn = el.querySelector('button[aria-label*="more options" i], button[aria-label*="more" i], button[id*="overflow" i], faceplate-dropdown-menu button');
        const ob = overflowBtn ? overflowBtn.getBoundingClientRect() : null;
        found.push({tag: el.tagName.toLowerCase(), id: el.id, y: r.top, overflowAt: ob ? {cx: ob.left+ob.width/2, cy: ob.top+ob.height/2} : null, snippet: el.innerText.slice(0, 80)});
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return found;
}
""")
    print(f"  bad comments found: {info}")
    if not info:
        log.append({"label": "delete_bad", "ok": False, "reason": "no matching comment"})
        return False
    bad = info[0]
    # scroll the bad comment into view
    page.evaluate("(y) => window.scrollTo(0, Math.max(0, y - 200))", bad["y"])
    time.sleep(1.0)
    # re-locate overflow button (positions shift after scroll)
    info = page.evaluate(r"""
() => {
  function walk(root) {
    if (!root || !root.querySelectorAll) return null;
    const items = Array.from(root.querySelectorAll('shreddit-profile-comment, shreddit-comment'));
    for (const el of items) {
      const t = (el.innerText || '').toLowerCase();
      if (t.includes('bumbleberry') || t.includes('dered once')) {
        const overflowBtn = el.querySelector('button[aria-label*="more options" i], button[aria-label*="more" i], button[id*="overflow" i]');
        if (overflowBtn) {
          const r = overflowBtn.getBoundingClientRect();
          return {cx: r.left + r.width/2, cy: r.top + r.height/2};
        }
      }
    }
    for (const n of root.querySelectorAll('*')) {
      if (n.shadowRoot) {
        const sub = walk(n.shadowRoot);
        if (sub) return sub;
      }
    }
    return null;
  }
  return walk(document);
}
""")
    if not info:
        log.append({"label": "delete_bad", "ok": False, "reason": "no overflow button"})
        return False
    print(f"  overflow at {info}")
    trusted_click(page, info["cx"], info["cy"])
    time.sleep(1.5)

    # Now find a Delete menu item
    delete_item = page.evaluate(r"""
() => {
  let found = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || found) return;
    root.querySelectorAll('*').forEach(el => {
      const t = (el.innerText || '').trim();
      if (t === 'Delete') {
        const r = el.getBoundingClientRect();
        if (r.width > 20 && r.height > 10) {
          found = {cx: r.left + r.width/2, cy: r.top + r.height/2};
        }
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !found) walk(n.shadowRoot); });
  }
  walk(document);
  return found;
}
""")
    print(f"  delete item: {delete_item}")
    if not delete_item:
        log.append({"label": "delete_bad", "ok": False, "reason": "no Delete menu item"})
        return False
    trusted_click(page, delete_item["cx"], delete_item["cy"])
    time.sleep(2.0)

    # Confirm dialog: click 'Yes, delete' or 'Delete'
    confirm = page.evaluate(r"""
() => {
  const btns = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('button').forEach(b => {
      const t = (b.innerText || '').trim();
      if (t === 'Yes, delete' || t === 'Delete' || t === 'Confirm') {
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          btns.push({cx: r.left + r.width/2, cy: r.top + r.height/2, text: t});
        }
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return btns;
}
""")
    print(f"  confirm buttons: {confirm}")
    if confirm:
        # take the 'Yes, delete' first if present
        chosen = next((c for c in confirm if c["text"] == "Yes, delete"), confirm[0])
        trusted_click(page, chosen["cx"], chosen["cy"])
        time.sleep(2.5)
    page.screenshot(path="bb_dimatty_delete_done.png", full_page=False)
    log.append({"label": "delete_bad", "ok": True})
    return True


def unhide_post(page, permalink, log):
    print(f"\n[unhide] {permalink}")
    page.goto("https://www.reddit.com" + permalink, wait_until="domcontentloaded", timeout=45000)
    time.sleep(3.5)
    # If the page shows "Post hidden" with Undo, click Undo
    undo = page.evaluate(r"""
() => {
  let found = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || found) return;
    root.querySelectorAll('button, a').forEach(b => {
      const t = (b.innerText || '').trim();
      if (t === 'Undo') {
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) found = {cx: r.left + r.width/2, cy: r.top + r.height/2};
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !found) walk(n.shadowRoot); });
  }
  walk(document);
  return found;
}
""")
    print(f"  undo button: {undo}")
    if undo:
        trusted_click(page, undo["cx"], undo["cy"])
        time.sleep(2.5)
        log.append({"label": "unhide", "ok": True})
        return True
    log.append({"label": "unhide", "ok": False, "reason": "no undo found (post may not be hidden)"})
    return False


s = ensure_session()
print("session:", s["id"], "status:", s.get("status"))
dbg = api("GET", f"/sessions/{s['id']}/debug")
print("LIVE:", dbg.get("debuggerFullscreenUrl"))

log = []
try:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(s["connectUrl"])
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print("starting url:", page.url)

        # 1) delete bad partial comment
        delete_bad_comment(page, log)

        # 2) unhide the post we accidentally hid
        unhide_post(page, COMMENTS[0]["permalink"], log)

        # 3) post both intended comments cleanly
        for i, comment in enumerate(COMMENTS):
            post_comment(page, comment["permalink"], comment["text"], comment["label"], log)
            if i < len(COMMENTS) - 1:
                gap = random.uniform(40, 75)
                print(f"  cooldown {gap:.1f}s")
                time.sleep(gap)
        browser.close()
finally:
    open("bb_dimatty_comments2.json", "w").write(json.dumps({"sessionId": s["id"], "log": log}, indent=2))
    print("=== LOG ===")
    print(json.dumps(log, indent=2))
