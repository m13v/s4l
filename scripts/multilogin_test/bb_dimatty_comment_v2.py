"""Comment v2: target the visible faceplate-textarea-input (composer entry
point). The previous script picked the 0x0 shadow-DOM copy. Filter to
elements with real dimensions and y > 200 (below the page header)."""
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


def trusted_click(page, cx, cy):
    page.mouse.move(cx - 6, cy - 3)
    time.sleep(random.uniform(0.15, 0.3))
    page.mouse.move(cx, cy)
    time.sleep(random.uniform(0.08, 0.18))
    page.mouse.down()
    time.sleep(random.uniform(0.05, 0.1))
    page.mouse.up()


# Find the VISIBLE composer entry point: faceplate-textarea-input with
# placeholder="Join the conversation", real width > 200, y between 100 and
# (viewport - 100).
FIND_VISIBLE_COMPOSER_JS = r"""
() => {
  const out = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('faceplate-textarea-input').forEach(el => {
      const ph = el.getAttribute('placeholder') || '';
      if (!/conversation|comment|thoughts/i.test(ph)) return;
      const r = el.getBoundingClientRect();
      if (r.width < 200 || r.height < 16) return;
      if (r.top < 100 || r.top > (window.innerHeight - 40)) return;
      out.push({cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height, ph});
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
"""

# After clicking the composer, an expanded contenteditable / lexical editor
# appears. Find it (real dimensions, not in header).
FIND_ACTIVE_EDITOR_JS = r"""
() => {
  const out = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('[contenteditable="true"]').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 200 || r.height < 24) return;
      if (r.top < 100) return;
      out.push({cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height,
                aria: el.getAttribute('aria-label'), placeholder: el.getAttribute('data-placeholder')});
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
"""

# Find the Comment submit button (after editor has content)
FIND_SUBMIT_JS = r"""
() => {
  const out = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('button').forEach(b => {
      const t = (b.innerText || '').trim();
      if (t === 'Comment') {
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && r.top > 100) {
          out.push({cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height, disabled: b.disabled});
        }
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
"""


def post_comment(page, permalink, text, label, log):
    full = "https://www.reddit.com" + permalink
    print(f"\n[{label}] -> {full}")
    page.goto(full, wait_until="domcontentloaded", timeout=45000)
    time.sleep(random.uniform(4.0, 5.5))
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1.5)

    # Scroll just enough to get the composer into the middle of the viewport.
    # We don't know its y yet, so probe first.
    composers = page.evaluate(FIND_VISIBLE_COMPOSER_JS)
    if not composers:
        # try a small scroll, the composer might be slightly off-viewport
        page.mouse.wheel(0, 300); time.sleep(1.0)
        composers = page.evaluate(FIND_VISIBLE_COMPOSER_JS)
    print(f"  composers visible: {composers}")
    if not composers:
        log.append({"label": label, "ok": False, "reason": "no visible composer"})
        return False

    c = composers[0]
    # nudge composer to center-ish of viewport for a clean trusted click
    delta = c["cy"] - 400
    if abs(delta) > 80:
        page.mouse.wheel(0, int(delta))
        time.sleep(1.0)
        composers = page.evaluate(FIND_VISIBLE_COMPOSER_JS)
        if not composers:
            log.append({"label": label, "ok": False, "reason": "composer scrolled away"})
            return False
        c = composers[0]

    print(f"  clicking composer at ({c['cx']}, {c['cy']})")
    trusted_click(page, c["cx"], c["cy"])
    time.sleep(random.uniform(1.5, 2.5))

    # Now an editor should be active. Find it.
    editors = page.evaluate(FIND_ACTIVE_EDITOR_JS)
    print(f"  editors: {editors}")
    if not editors:
        # composer may still be the entry point; click again to ensure
        trusted_click(page, c["cx"], c["cy"])
        time.sleep(2.0)
        editors = page.evaluate(FIND_ACTIVE_EDITOR_JS)
        print(f"  editors retry: {editors}")
    if editors:
        e = editors[0]
        # click the editor explicitly to ensure focus
        trusted_click(page, e["cx"], e["cy"])
        time.sleep(0.8)
    # If still no editor, just type at current focus (composer entry may
    # already have focus from the click)

    human_type(page, text)
    time.sleep(random.uniform(1.2, 2.0))

    # Find + click Comment submit
    submits = page.evaluate(FIND_SUBMIT_JS)
    print(f"  submits: {submits}")
    enabled = [s for s in submits if not s.get("disabled")]
    if enabled:
        s2 = enabled[0]
        print(f"  clicking submit at ({s2['cx']}, {s2['cy']})")
        trusted_click(page, s2["cx"], s2["cy"])
        time.sleep(random.uniform(3.5, 5.0))
    else:
        print("  no enabled submit, trying Ctrl+Enter")
        page.keyboard.press("Control+Enter")
        time.sleep(3.5)

    # Verify
    snippet = text[:40]
    found = page.evaluate("(s) => document.body.innerText.includes(s)", snippet)
    print(f"  comment present: {found}")
    page.screenshot(path=f"bb_dimatty_v2_{label}.png", full_page=False)
    log.append({"label": label, "ok": found, "url": page.url, "snippet": snippet})
    return found


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
        for i, c in enumerate(COMMENTS):
            post_comment(page, c["permalink"], c["text"], c["label"], log)
            if i < len(COMMENTS) - 1:
                gap = random.uniform(45, 80)
                print(f"  cooldown {gap:.1f}s")
                time.sleep(gap)
        browser.close()
finally:
    open("bb_dimatty_comments_v2.json", "w").write(json.dumps({"sessionId": s["id"], "log": log}, indent=2))
    print("=== LOG ===")
    print(json.dumps(log, indent=2))
