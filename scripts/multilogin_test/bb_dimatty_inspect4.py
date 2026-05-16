"""Dump the actual outer HTML of one shreddit-post post-card to see what
elements actually render the up/down arrow icons."""
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

    html = page.evaluate("""
() => {
  // Pick the FIRST shreddit-post and serialize all anchors, faceplate elements,
  // and any element whose visible text mentions upvote/downvote OR icon names.
  const post = document.querySelector('shreddit-post');
  if (!post) return {error: 'no post'};
  const tagSet = new Set();
  post.querySelectorAll('*').forEach(n => tagSet.add(n.tagName.toLowerCase()));
  // Collect anchor + faceplate-tracker + faceplate-number + svg + img children
  const anchors = Array.from(post.querySelectorAll('a, faceplate-tracker, faceplate-number, [data-post-click-location]'))
    .slice(0, 25)
    .map(n => ({
      tag: n.tagName.toLowerCase(),
      cls: (n.className || '').slice(0, 60),
      ariaLabel: n.getAttribute('aria-label'),
      dataLoc: n.getAttribute('data-post-click-location'),
      dataAction: n.getAttribute('data-action'),
      noun: n.getAttribute('noun'),
      href: n.getAttribute('href'),
      text: (n.innerText || '').slice(0, 40),
    }));
  return {
    postId: post.id,
    permalink: post.getAttribute('permalink'),
    descendantCount: post.querySelectorAll('*').length,
    tags: Array.from(tagSet).sort(),
    anchorsSample: anchors,
  };
}
""")
    print(json.dumps(html, indent=2))
    browser.close()
