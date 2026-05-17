"""Inspect exactly what's inside shreddit-vote-animations shadow root."""
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
    info = page.evaluate("""
() => {
  // First find all shreddit-vote-animations (need to walk shadow)
  const animEls = [];
  function walk(root, parentPath) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('*').forEach(n => {
      if (n.tagName && n.tagName.toLowerCase() === 'shreddit-vote-animations') {
        animEls.push(n);
      }
      if (n.shadowRoot) walk(n.shadowRoot, parentPath + '>' + n.tagName.toLowerCase());
    });
  }
  walk(document, '');
  if (animEls.length === 0) return {error: 'no animations found'};

  const first = animEls[0];
  const outer = first.outerHTML.slice(0, 800);
  const innerHTML = first.innerHTML.slice(0, 800);
  const hasShadow = !!first.shadowRoot;
  const shadowHTML = hasShadow ? first.shadowRoot.innerHTML.slice(0, 1200) : null;

  // What's the PARENT of this animations element?
  const parent = first.parentElement;
  const parentInfo = parent ? {
    tag: parent.tagName.toLowerCase(),
    cls: (parent.className||'').slice(0,80),
    hasShadow: !!parent.shadowRoot,
    outerHTMLstart: parent.outerHTML.slice(0, 400),
  } : null;

  return {
    totalAnims: animEls.length,
    firstOuter: outer,
    firstInner: innerHTML,
    firstHasShadow: hasShadow,
    firstShadowInner: shadowHTML,
    firstParent: parentInfo,
  };
}
""")
    print(json.dumps(info, indent=2))
    browser.close()
