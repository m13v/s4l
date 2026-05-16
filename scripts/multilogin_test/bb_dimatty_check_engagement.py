"""Check engagement on u/Negative_Spell899's 2 recent value-add replies.
Reddit's web UI doesn't expose true 'impressions' to non-mods, but we can
read:
  - score (upvotes - downvotes)
  - number of replies on our comment
  - visibility (still live / not removed)
  - removal indicators (comment showing [removed] or [deleted])

Pulls data from BOTH the profile page (shows score per comment) AND the
actual post pages (shows replies + visibility in context)."""
import json, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
SESSION_ID = "730b7ab3-7d3c-4fd7-8e19-91cccd277b79"
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

result = {"sessionId": s["id"], "profile": [], "in_context": []}

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    page = browser.contexts[0].pages[0]

    # --- 1) Profile view: score + permalink for each comment ---
    page.goto("https://www.reddit.com/user/Negative_Spell899/comments/",
              wait_until="domcontentloaded", timeout=45000)
    time.sleep(4.5)
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1.0)

    profile = page.evaluate(r"""
() => {
  const out = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('shreddit-profile-comment').forEach(el => {
      // attributes the profile-comment element exposes
      const attrs = {};
      ['score','comment-count','reply-count','permalink','post-permalink','content-href','subreddit-prefixed-name','created-timestamp','post-title'].forEach(a => attrs[a] = el.getAttribute(a));
      const permalinkA = el.querySelector('a[href*="/comments/"][href*="/comment/"]');
      const text = (el.innerText || '').replace(/\s+/g, ' ').slice(0, 180);
      // also look for ANY "X point" / "X points" / number visible in card
      const scoreText = (el.innerText || '').match(/(\d+)\s*point/i);
      const replyText = (el.innerText || '').match(/(\d+)\s*repl/i);
      out.push({
        attrs,
        permalinkFromAnchor: permalinkA?.getAttribute('href'),
        textSnippet: text,
        derivedScore: scoreText ? scoreText[1] : null,
        derivedReplies: replyText ? replyText[1] : null,
      });
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return out;
}
""")
    print("\n=== profile view ===")
    print(json.dumps(profile, indent=2))
    result["profile"] = profile

    # --- 2) For each comment, open the comment in context and read the
    # rendered score + reply count + check whether it's visible/removed ---
    for c in profile:
        href = c.get("permalinkFromAnchor")
        if not href:
            continue
        url = "https://www.reddit.com" + href if href.startswith("/") else href
        print(f"\n--- {c['attrs'].get('subreddit-prefixed-name')} -> {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(4.0)

        info = page.evaluate(r"""
() => {
  // Find the specific shreddit-comment authored by Negative_Spell899
  let mine = null;
  function walk(root) {
    if (!root || !root.querySelectorAll || mine) return;
    root.querySelectorAll('shreddit-comment').forEach(el => {
      const author = el.getAttribute('author') || '';
      if (author.toLowerCase() !== 'negative_spell899') return;
      if (mine) return;
      // pull attrs
      const attrs = {};
      ['score','reply-count','depth','collapsed','permalink','thingid'].forEach(a => attrs[a] = el.getAttribute(a));
      const text = (el.innerText || '').replace(/\s+/g, ' ').slice(0, 220);
      // is the comment text actually rendered (not [removed] / [deleted]) ?
      const removed = /\[removed\]|\[deleted\]|removed by moderator/i.test(el.innerText || '');
      // count direct child shreddit-comment (= top-level replies to mine)
      const childReplies = Array.from(el.querySelectorAll('shreddit-comment')).filter(c => c.parentElement === el || c.parentElement?.parentElement === el).length;
      // pull all <faceplate-number> readouts inside (often hold score + reply count)
      const numbers = Array.from(el.querySelectorAll('faceplate-number')).map(n => ({number: n.getAttribute('number'), pretty: n.innerText.trim()}));
      mine = {attrs, text, removed, childReplies, numbers};
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && !mine) walk(n.shadowRoot); });
  }
  walk(document);
  return mine;
}
""")
        print(json.dumps(info, indent=2))
        result["in_context"].append({"permalink": href, "info": info})

    page.screenshot(path="bb_dimatty_engagement_final.png", full_page=False)
    browser.close()

open("bb_dimatty_engagement.json", "w").write(json.dumps(result, indent=2))
print("\n=== SUMMARY ===")
for c in result["in_context"]:
    info = c["info"]
    if not info:
        print(f"  {c['permalink']}: NOT FOUND on page")
        continue
    a = info["attrs"]
    print(f"  {c['permalink']}")
    print(f"    score={a.get('score')}  replies={a.get('reply-count')}  removed={info['removed']}  child_replies={info['childReplies']}")
