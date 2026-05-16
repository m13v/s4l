"""Probe the bad comment's permalink page to find the right overflow selector."""
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
    page.goto("https://www.reddit.com/r/CasualConversation/comments/1te9lt2/comment/om2coy3/?context=3",
              wait_until="domcontentloaded", timeout=45000)
    time.sleep(5)
    page.screenshot(path="bb_dimatty_probe_bad.png", full_page=False)

    info = page.evaluate(r"""
() => {
  let result = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || result) return;
    root.querySelectorAll('shreddit-comment').forEach(el => {
      if (result) return;
      const t = (el.innerText || '').toLowerCase();
      if ((t.includes('three years ago and you have to remember') || t.startsWith('der')) && !t.includes('a barista sim')) {
        // List ALL buttons inside this comment + their attrs
        const btns = Array.from(el.querySelectorAll('button')).map(b => ({
          aria: b.getAttribute('aria-label'),
          cls: (b.className || '').slice(0, 80),
          text: (b.innerText || '').slice(0, 30),
          slot: b.getAttribute('slot'),
          x: Math.round(b.getBoundingClientRect().left),
          y: Math.round(b.getBoundingClientRect().top),
          w: Math.round(b.getBoundingClientRect().width),
          icon: b.querySelector('svg')?.getAttribute('icon-name'),
        }));
        // also list nested faceplate-dropdown-menu
        const dds = Array.from(el.querySelectorAll('faceplate-dropdown-menu')).map(d => ({
          slot: d.getAttribute('slot'),
          html: d.outerHTML.slice(0, 300)
        }));
        result = {commentId: el.id, btnCount: btns.length, btns, ddCount: dds.length, dds, snippet: el.innerText.slice(0, 100)};
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !result) walk(n.shadowRoot); });
  }
  walk(document);
  return result;
}
""")
    print(json.dumps(info, indent=2))
    browser.close()
