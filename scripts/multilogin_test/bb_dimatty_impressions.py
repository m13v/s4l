"""Check Reddit's built-in 'views/impressions' panel on our own profile.
On the logged-in user's own profile, Reddit shows view counts next to each
post/comment (the user-visible 'Insights' / views surface). Navigate to
the profile and dump everything that looks like a view metric per item."""
import json, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
SESSION_ID = "fa694c5a-2764-43cd-a9d2-8f7b78a4b5e3"
PROJECT_ID = "a95115ba-3653-4376-bff7-c6744d4ea18c"
CONTEXT_ID = "6274b94a-66cd-4735-8c19-0e45d313637f"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}


def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers=HDR)
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=40) as r:
        return json.load(r)


def ensure_session():
    try:
        s = api("GET", f"/sessions/{SESSION_ID}")
        if s.get("status") == "RUNNING":
            return s
    except Exception:
        pass
    s = api("POST", "/sessions", {
        "projectId": PROJECT_ID,
        "browserSettings": {"context": {"id": CONTEXT_ID, "persist": True}},
        "proxies": [{"type": "browserbase",
                     "geolocation": {"country": "US", "state": "CA", "city": "San Francisco"}}],
        "keepAlive": True,
        "timeout": 1800,
    })
    return s


s = ensure_session()
print("session:", s["id"], "status:", s.get("status"))

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    page = browser.contexts[0].pages[0]

    # 1) Try the OVERVIEW tab (default) which shows posts + comments with view metrics
    for url in [
        "https://www.reddit.com/user/Negative_Spell899/",
        "https://www.reddit.com/user/Negative_Spell899/comments/",
    ]:
        print(f"\n--- {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)
        # scroll a bit so all rows hydrate
        for _ in range(3):
            page.mouse.wheel(0, 700); time.sleep(0.7)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1.5)

        # Dump every visible text that mentions 'view' / 'views' / 'impression'
        # plus per-card metrics
        info = page.evaluate(r"""
() => {
  const hits = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('*').forEach(n => {
      const t = (n.innerText || '').trim();
      if (!t || t.length > 200) return;
      if (/views?$|impressions?|insight/i.test(t)) {
        const r = n.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          hits.push({tag: n.tagName.toLowerCase(), text: t.slice(0, 120), y: Math.round(r.top), x: Math.round(r.left)});
        }
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return hits;
}
""")
        print("  'view' / 'impression' text hits:")
        for h in info[:30]:
            print(f"    {h}")

        # Also dump attributes that look like view counts on each shreddit-profile-comment
        attrs = page.evaluate(r"""
() => {
  const out = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('shreddit-profile-comment, shreddit-profile-post, shreddit-post, faceplate-tracker[noun*="profile"]').forEach(el => {
      const dump = {};
      for (const a of el.attributes) dump[a.name] = a.value;
      out.push({tag: el.tagName.toLowerCase(), attrs: dump, snippet: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 100)});
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
""")
        print("\n  profile-card attribute dumps:")
        for a in attrs[:6]:
            # filter to interesting attrs only
            interesting = {k: v for k, v in a["attrs"].items() if any(s in k for s in ['view', 'impression', 'score', 'count', 'permalink', 'title', 'subreddit'])}
            print(f"    {a['tag']}: {interesting}")
            print(f"      snippet: {a['snippet']}")

    page.screenshot(path="bb_dimatty_impressions_view.png", full_page=False)
    browser.close()
