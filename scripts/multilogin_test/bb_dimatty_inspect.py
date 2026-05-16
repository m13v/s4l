"""Reconnect to the live dimatty warmup session and print the actual
selectors that Reddit uses for vote buttons (DOM is shadow-DOM-heavy)."""
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
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=40) as r:
        return json.load(r)


s = api("GET", f"/sessions/{SESSION_ID}")
print("session status:", s.get("status"))
print("connectUrl:", s.get("connectUrl"))

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    c = browser.contexts[0]
    page = c.pages[0] if c.pages else c.new_page()
    print("current url:", page.url)

    # Probe DOM for vote-button shape
    info = page.evaluate("""
() => {
  const posts = document.querySelectorAll('shreddit-post');
  const sample = [];
  posts.forEach((post, idx) => {
    if (idx > 3) return;
    const buttons = post.querySelectorAll('button');
    const btnInfo = [];
    buttons.forEach(b => {
      btnInfo.push({
        cls: b.className.slice(0, 80),
        aria: b.getAttribute('aria-label'),
        ariaPressed: b.getAttribute('aria-pressed'),
        rpl: b.getAttribute('rpl'),
        upvote: b.getAttribute('upvote'),
        downvote: b.getAttribute('downvote'),
        text: b.innerText?.trim().slice(0, 30)
      });
    });
    // also look for shreddit-post-vote and its inner shape
    const v = post.querySelector('shreddit-post-vote');
    let vDesc = null;
    if (v) {
      vDesc = {
        outerHTML_start: v.outerHTML.slice(0, 400),
        hasShadowRoot: !!v.shadowRoot,
      };
    }
    sample.push({
      idx,
      id: post.getAttribute('id'),
      permalink: post.getAttribute('permalink'),
      promoted: post.getAttribute('promoted'),
      buttonCount: buttons.length,
      buttons: btnInfo.slice(0, 6),
      shredditPostVote: vDesc,
    });
  });
  return {totalPosts: posts.length, sample};
}
""")
    print(json.dumps(info, indent=2))
    browser.close()
