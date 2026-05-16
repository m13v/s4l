"""Walk OUTSIDE the post element to find the sibling vote action row."""
import json, subprocess, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
SESSION_ID = "a442c5c1-ae41-4d5f-a9ec-d0bd0b9d0268"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}


def api(method, path):
    req = urllib.request.Request(BASE + path, headers=HDR)
    with urllib.request.urlopen(req, None, timeout=40) as r:
        return json.load(r)


s = api("GET", f"/sessions/{SESSION_ID}")

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    page = browser.contexts[0].pages[0]
    page.evaluate("window.scrollTo(0, 0)")

    info = page.evaluate("""
() => {
  const post = document.querySelectorAll('shreddit-post')[1];  // skip first; pick 2nd
  if (!post) return {error: 'no post'};
  const parent = post.parentElement;
  const siblings = Array.from(parent.children).map(c => ({
    tag: c.tagName.toLowerCase(),
    id: c.id,
    cls: (c.className||'').slice(0,80),
    descendantCount: c.querySelectorAll('*').length,
    hasShadow: !!c.shadowRoot,
  }));
  // Probe the full parent for vote-anything
  const voteTags = new Set();
  const probe = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('*').forEach(n => {
      const t = n.tagName?.toLowerCase() || '';
      if (t.includes('vote') || t.includes('shreddit-vote') || t.includes('action-row') || t.includes('faceplate-tracker')) {
        voteTags.add(t + (n.id ? '#' + n.id : ''));
      }
      if (n.shadowRoot) probe(n.shadowRoot);
    });
  };
  probe(parent);
  // Also see what's right after this post (likely the action row)
  const after = post.nextElementSibling ? {
    tag: post.nextElementSibling.tagName.toLowerCase(),
    cls: (post.nextElementSibling.className||'').slice(0,100),
    html: post.nextElementSibling.outerHTML.slice(0, 600),
  } : null;
  return {
    postId: post.id,
    parentTag: parent.tagName.toLowerCase(),
    parentCls: (parent.className||'').slice(0,80),
    siblingCount: parent.children.length,
    siblings: siblings.slice(0, 10),
    voteTagsNearby: Array.from(voteTags).slice(0, 20),
    afterPost: after,
  };
}
""")
    print(json.dumps(info, indent=2))
    browser.close()
