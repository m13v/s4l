"""Click 'See More Insights' on each of our 2 comments and dump the full
insights panel (Reddit shows views, share, upvote rate, etc.)."""
import json, random, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
SESSION_ID = "fa694c5a-2764-43cd-a9d2-8f7b78a4b5e3"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}


def api(method, path):
    req = urllib.request.Request(BASE + path, headers=HDR)
    with urllib.request.urlopen(req, None, timeout=40) as r:
        return json.load(r)


def trusted_click(page, cx, cy):
    page.mouse.move(cx - 6, cy - 3)
    time.sleep(random.uniform(0.15, 0.3))
    page.mouse.move(cx, cy)
    time.sleep(random.uniform(0.08, 0.18))
    page.mouse.down()
    time.sleep(random.uniform(0.05, 0.1))
    page.mouse.up()


s = api("GET", f"/sessions/{SESSION_ID}")

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    page = browser.contexts[0].pages[0]
    page.goto("https://www.reddit.com/user/Negative_Spell899/comments/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(5)
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1.5)

    # find each "See More Insights" link, click in turn, capture panel content,
    # then go back. Pair each click to its sibling profile-comment so we know
    # which comment.
    targets = page.evaluate(r"""
() => {
  const out = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('a').forEach(a => {
      const t = (a.innerText || '').trim();
      if (t === 'See More Insights') {
        const r = a.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          // find the comment card this insight link belongs to (walk up)
          let cur = a;
          let card = null;
          while (cur) {
            if (cur.tagName?.toLowerCase() === 'shreddit-profile-comment') { card = cur; break; }
            cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
          }
          const subreddit = card ? (card.innerText.match(/r\/(\w+)/) || [])[1] : null;
          const href = a.getAttribute('href');
          out.push({cx: r.left + r.width/2, cy: r.top + r.height/2, href, subreddit});
        }
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
""")
    print("insights links:", targets)

    results = []
    for tgt in targets:
        print(f"\n--- subreddit={tgt['subreddit']} href={tgt['href']}")
        # if there's an href, navigate directly; otherwise click
        if tgt["href"]:
            full = tgt["href"] if tgt["href"].startswith("http") else "https://www.reddit.com" + tgt["href"]
            page.goto(full, wait_until="domcontentloaded", timeout=45000)
        else:
            trusted_click(page, tgt["cx"], tgt["cy"])
        time.sleep(5)
        page.screenshot(path=f"bb_dimatty_insights_{tgt['subreddit']}.png", full_page=False)

        # dump the insights panel content
        panel = page.evaluate(r"""
() => {
  // Pull every visible text whose nearest heading hints at insights /
  // metrics. Also pull all faceplate-number and faceplate-tracker[noun~="insights"] children.
  const lines = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('*').forEach(n => {
      const t = (n.innerText || '').trim();
      if (!t || t.length > 80) return;
      // keywords commonly used on the Reddit insights drawer
      if (/^\d+[\s\u00a0]*(view|share|upvote|impressions?|comments?)/i.test(t)
          || /^upvote rate$/i.test(t) || /^views$/i.test(t) || /^shares?$/i.test(t)
          || /^total views$/i.test(t) || /^communities$/i.test(t) || /^impressions?$/i.test(t)
          || /^audience$/i.test(t)) {
        const r = n.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) lines.push({text: t, y: Math.round(r.top), x: Math.round(r.left)});
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  // dedupe by text+y
  const seen = new Set();
  const uniq = [];
  for (const l of lines) {
    const k = l.text + '@' + Math.floor(l.y / 30);
    if (!seen.has(k)) { seen.add(k); uniq.push(l); }
  }
  return uniq;
}
""")
        print(f"  insights panel lines: {len(panel)}")
        for ln in panel[:40]:
            print(f"    y={ln['y']:>5}  {ln['text']}")
        results.append({"subreddit": tgt["subreddit"], "panel": panel})

        # back to profile
        page.go_back()
        time.sleep(3)

    browser.close()

open("bb_dimatty_insights_detail.json", "w").write(json.dumps(results, indent=2))
