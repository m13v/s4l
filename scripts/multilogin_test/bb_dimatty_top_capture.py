"""Navigate to AskReddit post, scroll to TOP, screenshot, and probe for the
composer element with no y-filter so we can see where it actually is."""
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

    page.goto("https://www.reddit.com/r/AskReddit/comments/1tau246/whos_an_actor_that_nailed_a_role_so_hard_that/",
              wait_until="domcontentloaded", timeout=45000)
    time.sleep(5)
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1.5)
    page.screenshot(path="bb_dimatty_top_askreddit.png", full_page=False)

    # Probe everything related to composer / contenteditable / placeholder without
    # filtering by position
    info = page.evaluate(r"""
() => {
  const results = {composer: [], contentEditable: [], placeholders: [], textareas: [], composerHost: []};
  function walk(root, label) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('shreddit-composer, comment-composer-host').forEach(el => {
      const r = el.getBoundingClientRect();
      results.composer.push({tag: el.tagName.toLowerCase(), x: r.left, y: r.top, w: r.width, h: r.height,
                             open: el.hasAttribute('open'), hidden: el.hidden, ariaHidden: el.getAttribute('aria-hidden'),
                             display: getComputedStyle(el).display,
                             innerSnippet: (el.innerText || '').slice(0, 100)});
    });
    root.querySelectorAll('[contenteditable="true"]').forEach(el => {
      const r = el.getBoundingClientRect();
      results.contentEditable.push({tag: el.tagName.toLowerCase(), x: r.left, y: r.top, w: r.width, h: r.height,
                                    placeholder: el.getAttribute('data-placeholder') || el.getAttribute('aria-label'),
                                    display: getComputedStyle(el).display});
    });
    root.querySelectorAll('faceplate-textarea-input').forEach(el => {
      const r = el.getBoundingClientRect();
      results.textareas.push({tag: 'faceplate-textarea-input', x: r.left, y: r.top, w: r.width, h: r.height,
                              placeholder: el.getAttribute('placeholder'),
                              display: getComputedStyle(el).display, name: el.getAttribute('name')});
    });
    root.querySelectorAll('*').forEach(n => {
      const t = (n.innerText || '').trim();
      if (t === 'Join the conversation' || t === 'Add a comment' || t === 'What are your thoughts?') {
        const r = n.getBoundingClientRect();
        results.placeholders.push({tag: n.tagName.toLowerCase(), text: t, x: r.left, y: r.top, w: r.width, h: r.height,
                                   display: getComputedStyle(n).display, visibility: getComputedStyle(n).visibility});
      }
      if (n.shadowRoot) walk(n.shadowRoot, label + '>' + n.tagName.toLowerCase());
    });
  }
  walk(document, 'doc');
  return results;
}
""")
    print(json.dumps(info, indent=2)[:5000])
    browser.close()
