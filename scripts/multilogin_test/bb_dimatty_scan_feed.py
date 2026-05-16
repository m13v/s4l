"""Reconnect to the dimatty keepAlive session and scan the visible feed:
post title, subreddit, score, comment count, body excerpt, permalink. Print
JSON so we can pick 2 good targets for value-add comments."""
import json, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
SESSION_ID = "aaf81f9f-72b7-4809-a657-57836ff8b1c1"
PROJECT_ID = "a95115ba-3653-4376-bff7-c6744d4ea18c"
CONTEXT_ID = "6274b94a-66cd-4735-8c19-0e45d313637f"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}


def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers=HDR)
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=40) as r:
        return json.load(r)


s = api("GET", f"/sessions/{SESSION_ID}")
print("session status:", s.get("status"))

# If completed, start a fresh one.
if s.get("status") != "RUNNING":
    s = api("POST", "/sessions", {
        "projectId": PROJECT_ID,
        "browserSettings": {"context": {"id": CONTEXT_ID, "persist": True}},
        "proxies": [{"type": "browserbase",
                     "geolocation": {"country": "US", "state": "CA", "city": "San Francisco"}}],
        "keepAlive": True,
        "timeout": 1800,
    })
    print("new session:", s["id"])
    dbg = api("GET", f"/sessions/{s['id']}/debug")
    live = dbg.get("debuggerFullscreenUrl")
    print("LIVE:", live)
    new_session_id = s["id"]
else:
    new_session_id = SESSION_ID

# Use shadow-aware walk to find post cards and pull title / subreddit /
# permalink / body excerpt. The cards are inside shreddit-post elements;
# the post title is in <a> with the permalink, subreddit in
# shreddit-post[subreddit-prefixed-name], score / comments via shreddit-post
# attrs (score, comment-count).
SCAN_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('shreddit-post').forEach(post => {
      const pid = post.getAttribute('id');
      if (!pid || seen.has(pid)) return;
      seen.add(pid);
      const title = post.getAttribute('post-title') || post.querySelector('a[slot="title"], a.title')?.innerText || '';
      const subreddit = post.getAttribute('subreddit-prefixed-name') || post.getAttribute('subreddit-name') || '';
      const author = post.getAttribute('author') || '';
      const score = post.getAttribute('score');
      const cc = post.getAttribute('comment-count');
      const permalink = post.getAttribute('permalink') || '';
      const flair = post.getAttribute('post-type') || '';
      const promoted = post.hasAttribute('promoted');
      // body excerpt: shreddit-post has a slotted text-body or post-rtjson-content
      let body = '';
      const bodyEl = post.querySelector('[slot="text-body"]') || post.querySelector('shreddit-post-content') || post.querySelector('p, .text-neutral-content');
      if (bodyEl) body = (bodyEl.innerText || '').slice(0, 600);
      out.push({pid, title: title.slice(0, 200), subreddit, author, score, comment_count: cc, permalink, post_type: flair, promoted, body});
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
"""


with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    ctx = browser.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    print("url:", page.url)
    if "reddit.com" not in page.url:
        page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
    # scroll a bit to load posts
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1.2)
    for _ in range(6):
        page.mouse.wheel(0, 800)
        time.sleep(0.8)

    posts = page.evaluate(SCAN_JS)
    print(f"found {len(posts)} posts")
    safe = [p for p in posts if not p["promoted"]]
    # surface posts in low-friction subs (text bodies, AskReddit, CasualConversation,
    # CleaningTips, advice subs, hobby subs)
    LOW_FRICTION_SUBS = {
        'r/AskReddit', 'r/CasualConversation', 'r/CleaningTips', 'r/NoStupidQuestions',
        'r/explainlikeimfive', 'r/lifeprotips', 'r/AskWomen', 'r/AskMen',
        'r/PersonalFinance', 'r/cooking', 'r/houseplants', 'r/Pets',
        'r/Cats', 'r/dogs', 'r/Wellthatsucks', 'r/mildlyinfuriating',
        'r/CookingForBeginners', 'r/budgetfood', 'r/Frugal',
        'r/Random_Acts_Of_Kindness', 'r/TalesFromYourServer',
        'r/legaladvice', 'r/college', 'r/socialskills',
    }
    favored = [p for p in safe if p["subreddit"] in LOW_FRICTION_SUBS]
    print("\n=== FAVORED (low-friction) ===")
    for p in favored[:8]:
        print(json.dumps({k: v for k, v in p.items() if k in ('pid','title','subreddit','score','comment_count','permalink','body')}, indent=2))
    print("\n=== ALL (top 20) ===")
    for p in safe[:20]:
        print(f"  {p['subreddit']:35s} score={p['score']:>5} cc={p['comment_count']:>4}  {p['title'][:90]}")
    out_data = {"sessionId": new_session_id, "posts": safe[:30]}
    open("bb_dimatty_scan_feed.json", "w").write(json.dumps(out_data, indent=2))
    browser.close()
