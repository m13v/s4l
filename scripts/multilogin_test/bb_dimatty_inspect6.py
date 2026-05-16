"""Walk every shadow root and list ALL buttons with their aria-labels."""
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
    page.evaluate("window.scrollTo(0, 400)")

    info = page.evaluate("""
() => {
  const buttons = [];
  function walk(root, depth) {
    if (!root || !root.querySelectorAll || depth > 10) return;
    root.querySelectorAll('*').forEach(n => {
      if (n.tagName === 'BUTTON') {
        buttons.push({
          depth,
          aria: n.getAttribute('aria-label'),
          cls: (n.className||'').slice(0,40),
          rootKind: root === document ? 'doc' : (root.host ? root.host.tagName.toLowerCase() : 'unknown'),
        });
      }
      if (n.shadowRoot) walk(n.shadowRoot, depth + 1);
    });
  }
  walk(document, 0);
  return {totalButtons: buttons.length, sample: buttons.slice(0, 80)};
}
""")
    print(f"total buttons: {info['totalButtons']}")
    for b in info["sample"]:
        if b["aria"]:
            print(f"  d={b['depth']} host={b['rootKind']:30s} aria={b['aria']}")
    browser.close()
