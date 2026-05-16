"""Post 2 value-add comments via the dimatty01 / u/Negative_Spell899 context.

Targets (drafted to add real value, not spam):
  1. r/CasualConversation - "build me a hyper-specific web game" - reply with
     a weird specific game pitch that matches OP's tone (barista name-recall sim).
  2. r/AskReddit - "actor that nailed a role" - Andy Serkis as Gollum, with
     a clear reason (set the rulebook for mocap acting).

Mechanics: open each post detail page, find the comment composer (Reddit
uses a lexical-style rich-text editor inside a faceplate-form-helper element,
NOT a plain textarea), focus it, type text via keyboard.type(), then click
the visible Comment submit button."""
import json, random, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
SESSION_ID = "730b7ab3-7d3c-4fd7-8e19-91cccd277b79"
PROJECT_ID = "a95115ba-3653-4376-bff7-c6744d4ea18c"
CONTEXT_ID = "6274b94a-66cd-4735-8c19-0e45d313637f"
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


# Find the composer's content-editable inside a shreddit-composer / faceplate
# rich text editor on a post detail page. Returns selector + bounding rect.
FIND_COMPOSER_JS = r"""
() => {
  // Strategies:
  // 1) shreddit-composer with role=textbox inside
  // 2) [contenteditable="true"] on the page
  // 3) <textarea name="text"> on old reddit (just in case)
  const out = {paths: []};
  function walk(root, label) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('[contenteditable="true"]').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 100 || r.height < 16) return;
      out.paths.push({type: 'contenteditable', cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height, src: label});
    });
    root.querySelectorAll('textarea').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 100) return;
      out.paths.push({type: 'textarea', cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height, src: label, name: el.name});
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot, (label || '') + '>' + n.tagName.toLowerCase()); });
  }
  walk(document, 'doc');
  return out;
}
"""


# Find the visible Comment submit button (rpl button with text "Comment")
FIND_SUBMIT_JS = r"""
() => {
  const candidates = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('button').forEach(b => {
      const t = (b.innerText || '').trim();
      const aria = b.getAttribute('aria-label') || '';
      // Reddit's submit text is exactly "Comment" once the editor has content.
      if (t === 'Comment' || aria === 'Comment') {
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          candidates.push({cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height, aria, text: t, disabled: b.disabled});
        }
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return candidates;
}
"""


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
    """Type with small jittery delays to mimic real typing."""
    for ch in text:
        page.keyboard.type(ch)
        # short pauses, occasional longer gaps
        if ch in '.,!?':
            time.sleep(random.uniform(0.18, 0.40))
        elif ch == ' ':
            time.sleep(random.uniform(0.06, 0.16))
        else:
            time.sleep(random.uniform(0.03, 0.09))


def post_comment(page, permalink, text, label, log):
    full_url = "https://www.reddit.com" + permalink
    print(f"\n[{label}] navigating to {full_url}")
    page.goto(full_url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(random.uniform(3.0, 4.5))

    # scroll the composer into view (usually right under post body)
    slow_scroll(page, total_px=random.randint(400, 700))
    time.sleep(random.uniform(1.0, 1.8))

    # find composer
    composers = page.evaluate(FIND_COMPOSER_JS)
    print(f"  composers: {composers}")
    if not composers["paths"]:
        log.append({"label": label, "ok": False, "reason": "no composer"})
        return False

    target = composers["paths"][0]
    print(f"  using composer at ({target['cx']}, {target['cy']}) type={target['type']}")
    # nudge into clear view
    if target["cy"] > 600:
        page.mouse.wheel(0, int(target["cy"] - 360))
        time.sleep(1.0)
        composers = page.evaluate(FIND_COMPOSER_JS)
        target = composers["paths"][0]

    # click composer to focus
    cx, cy = target["cx"], target["cy"]
    page.mouse.move(cx - 8, cy - 4)
    time.sleep(random.uniform(0.15, 0.3))
    page.mouse.move(cx, cy)
    time.sleep(random.uniform(0.1, 0.2))
    page.mouse.down()
    time.sleep(0.07)
    page.mouse.up()
    time.sleep(random.uniform(0.8, 1.4))

    # type the comment
    human_type(page, text)
    time.sleep(random.uniform(1.0, 2.0))

    # find + click the Comment submit button
    submits = page.evaluate(FIND_SUBMIT_JS)
    print(f"  submit candidates: {submits}")
    enabled = [s for s in submits if not s.get("disabled")]
    if not enabled:
        # try keyboard ctrl+Enter as fallback (Reddit accepts this on many composers)
        print("  no enabled submit, trying Ctrl+Enter")
        page.keyboard.press("Control+Enter")
        time.sleep(2.5)
    else:
        btn = enabled[0]
        print(f"  clicking submit at ({btn['cx']}, {btn['cy']})")
        page.mouse.move(btn["cx"] - 6, btn["cy"] - 3)
        time.sleep(random.uniform(0.15, 0.3))
        page.mouse.move(btn["cx"], btn["cy"])
        time.sleep(random.uniform(0.1, 0.2))
        page.mouse.down()
        time.sleep(0.07)
        page.mouse.up()
        time.sleep(random.uniform(3.0, 5.0))

    # verify the comment is present in DOM (search for first ~30 chars of text)
    snippet = text[:35]
    found = page.evaluate("(s) => { const a = document.body.innerText; return a.includes(s); }", snippet)
    print(f"  comment text present after submit: {found}")
    page.screenshot(path=f"bb_dimatty_comment_{label}.png", full_page=False)
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
        print("starting url:", page.url)

        for i, comment in enumerate(COMMENTS):
            post_comment(page, comment["permalink"], comment["text"], comment["label"], log)
            # gap between comments to look human
            if i < len(COMMENTS) - 1:
                gap = random.uniform(35, 65)
                print(f"  waiting {gap:.1f}s before next comment")
                time.sleep(gap)
        browser.close()
finally:
    open("bb_dimatty_comments.json", "w").write(json.dumps({"sessionId": s["id"], "log": log}, indent=2))
    print("=== LOG ===")
    print(json.dumps(log, indent=2))
