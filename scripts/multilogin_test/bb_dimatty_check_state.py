"""Take screenshots and dump current state of each target post page to
understand what's actually rendered. Helps debug why placeholder search failed."""
import json, subprocess, time, urllib.request
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


s = api("GET", f"/sessions/{SESSION_ID}")
print("status:", s.get("status"))

URLS = [
    ("askreddit", "https://www.reddit.com/r/AskReddit/comments/1tau246/whos_an_actor_that_nailed_a_role_so_hard_that/"),
    ("casualconv", "https://www.reddit.com/r/CasualConversation/comments/1te9lt2/i_build_small_instant_web_games_for_a_living_if/"),
    ("profile", "https://www.reddit.com/user/Negative_Spell899/comments/"),
]

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    page = browser.contexts[0].pages[0]
    for label, url in URLS:
        print(f"\n--- {label} ---")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(4.5)
        # scroll down a bit
        page.mouse.wheel(0, 500)
        time.sleep(1.5)
        page.screenshot(path=f"bb_dimatty_state_{label}.png", full_page=False)
        # find any text related to placeholder / banner
        info = page.evaluate(r"""
() => {
  const hits = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('*').forEach(n => {
      const t = (n.innerText || '').trim().slice(0, 120);
      if (!t) return;
      const matchers = [
        'Join the conversation', 'Add a comment', 'What are your thoughts',
        'Post hidden', 'You must be a community member', 'unable to comment',
        'restricted', 'banned', 'verify your email', 'low karma',
      ];
      for (const m of matchers) {
        if (t.includes(m)) {
          const r = n.getBoundingClientRect();
          hits.push({m, tag: n.tagName?.toLowerCase(), y: Math.round(r.top), w: Math.round(r.width), text: t});
          break;
        }
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return hits;
}
""")
        print(f"  matches: {json.dumps(info, indent=2)}")
        # also dump count of shreddit-comment-composer / shreddit-comment elements
        counts = page.evaluate(r"""
() => {
  const counts = {};
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    ['shreddit-comment-composer','shreddit-comment','shreddit-async-loader','faceplate-form-helper','comment-composer-host'].forEach(t => {
      const c = root.querySelectorAll(t).length;
      counts[t] = (counts[t] || 0) + c;
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return counts;
}
""")
        print(f"  counts: {counts}")
    browser.close()
