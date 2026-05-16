"""Audit u/Negative_Spell899's comment history. Lists every comment with
timestamp + snippet so we can see whether the partial broken comment from
attempt 1 is still up or got auto-removed."""
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

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    page = browser.contexts[0].pages[0]
    page.goto("https://www.reddit.com/user/Negative_Spell899/comments/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(4)
    # scroll a bit to load
    for _ in range(3):
        page.mouse.wheel(0, 600); time.sleep(0.8)
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1)
    page.screenshot(path="bb_dimatty_profile_audit.png", full_page=False)

    info = page.evaluate(r"""
() => {
  const out = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('shreddit-profile-comment').forEach(el => {
      const text = (el.innerText || '').replace(/\s+/g, ' ').slice(0, 220);
      out.push({id: el.id, text});
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
""")
    print(json.dumps(info, indent=2))
    browser.close()
