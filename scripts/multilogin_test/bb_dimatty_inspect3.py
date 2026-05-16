"""Take a fresh screenshot + dump element shapes around visible vote area.
Recurse into all shadow roots to find them."""
import json, subprocess, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
SESSION_ID = "a442c5c1-ae41-4d5f-a9ec-d0bd0b9d0268"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}


def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers=HDR)
    with urllib.request.urlopen(req, None, timeout=40) as r:
        return json.load(r)


s = api("GET", f"/sessions/{SESSION_ID}")
print("status:", s.get("status"))

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    c = browser.contexts[0]
    page = c.pages[0] if c.pages else c.new_page()
    print("current url:", page.url, "title:", page.title())
    page.screenshot(path="bb_dimatty_now.png", full_page=False)

    # walk shadow DOMs
    walk = page.evaluate("""
() => {
  const seenVote = [];
  function walk(root, depth) {
    if (depth > 8) return;
    const nodes = root.querySelectorAll ? root.querySelectorAll('*') : [];
    nodes.forEach(n => {
      const t = n.tagName?.toLowerCase() || '';
      if (t.includes('vote') || t.includes('upvote') || t.includes('downvote')) {
        seenVote.push({tag: t, id: n.id, cls: (n.className||'').slice(0,80), depth});
      }
      const aria = (n.getAttribute && n.getAttribute('aria-label')) || '';
      if (/upvote|downvote/i.test(aria)) {
        seenVote.push({tag: t, aria, cls: (n.className||'').slice(0,80), depth, tag2: 'aria'});
      }
      if (n.shadowRoot) walk(n.shadowRoot, depth + 1);
    });
  }
  walk(document, 0);
  return {voteCount: seenVote.length, sample: seenVote.slice(0, 20)};
}
""")
    print("voteCount:", walk.get("voteCount"))
    print(json.dumps(walk.get("sample"), indent=2))
    browser.close()
