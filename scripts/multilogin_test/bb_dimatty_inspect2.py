"""Probe the whole page for vote button elements; they're not inside <shreddit-post>."""
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

    info = page.evaluate("""
() => {
  // Count globally
  const allBtn = document.querySelectorAll('button');
  const tag = name => document.querySelectorAll(name).length;
  const labeled = (re) => Array.from(allBtn)
      .filter(b => re.test((b.getAttribute('aria-label')||'') + ' ' + (b.getAttribute('data-action')||'') + ' ' + b.className))
      .slice(0, 5)
      .map(b => ({
        aria: b.getAttribute('aria-label'),
        cls: b.className.slice(0, 100),
        dataAction: b.getAttribute('data-action'),
        rpl: b.getAttribute('rpl'),
        text: b.innerText?.trim().slice(0, 30),
        closestPost: b.closest('shreddit-post')?.getAttribute('id') || null,
        parent: b.parentElement?.tagName + '.' + (b.parentElement?.className||'').slice(0,40),
      }));
  return {
    totalButtons: allBtn.length,
    shredditPostVoteEl: tag('shreddit-post-vote'),
    voteSlotEl: tag('vote-slot'),
    upvoteButtons: labeled(/upvote/i),
    downvoteButtons: labeled(/downvote/i),
    dataActionButtons: labeled(/^upvote|^downvote/i),
    // sample of post-card-vote class
    voteEls: Array.from(document.querySelectorAll('[data-post-click-location*=vote i], [class*=vote i]'))
        .slice(0, 6).map(e => ({tag: e.tagName, cls: (e.className||'').slice(0,80), dataPostClick: e.getAttribute('data-post-click-location'), id: e.id})),
  };
}
""")
    print(json.dumps(info, indent=2))
    browser.close()
