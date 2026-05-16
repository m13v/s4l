"""Get more detail: the actual reply text on our CasualConversation comment,
each comment's rank in the post (Best sort), and the parent post's total
comment count (= denominator for 'how much noise we're competing with')."""
import json, subprocess, time, urllib.request
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


s = api("GET", f"/sessions/{SESSION_ID}")
print("session status:", s.get("status"))

TARGETS = [
    {
        "label": "casualconversation",
        "post_url": "https://www.reddit.com/r/CasualConversation/comments/1te9lt2/i_build_small_instant_web_games_for_a_living_if/",
        "comment_url": "https://www.reddit.com/r/CasualConversation/comments/1te9lt2/comment/om2edny/?context=3",
        "thingid": "t1_om2edny",
    },
    {
        "label": "askreddit",
        "post_url": "https://www.reddit.com/r/AskReddit/comments/1tau246/whos_an_actor_that_nailed_a_role_so_hard_that/",
        "comment_url": "https://www.reddit.com/r/AskReddit/comments/1tau246/comment/om2eq8j/?context=3",
        "thingid": "t1_om2eq8j",
    },
]

result = []

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    page = browser.contexts[0].pages[0]

    for t in TARGETS:
        print(f"\n--- {t['label']} ---")
        page.goto(t["comment_url"], wait_until="domcontentloaded", timeout=45000)
        time.sleep(4.0)

        # find our comment + any replies + post comment count
        info = page.evaluate(r"""
({thingid}) => {
  let mine = null;
  let post = null;
  const replies = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    if (!post) {
      const sp = root.querySelector('shreddit-post');
      if (sp) {
        post = {
          score: sp.getAttribute('score'),
          comment_count: sp.getAttribute('comment-count'),
          title: sp.getAttribute('post-title'),
        };
      }
    }
    root.querySelectorAll('shreddit-comment').forEach(el => {
      if (el.getAttribute('thingid') === thingid) {
        mine = {
          score: el.getAttribute('score'),
          permalink: el.getAttribute('permalink'),
          depth: el.getAttribute('depth'),
          text: (el.innerText || '').slice(0, 300),
        };
        // collect direct child replies
        el.querySelectorAll(':scope shreddit-comment').forEach(child => {
          if (child === el) return;
          replies.push({
            author: child.getAttribute('author'),
            score: child.getAttribute('score'),
            thingid: child.getAttribute('thingid'),
            depth: child.getAttribute('depth'),
            text: (child.innerText || '').slice(0, 300),
          });
        });
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  return {mine, post, replies};
}
""", {"thingid": t["thingid"]})

        # find rank of our comment in the top-level thread when sorted by Best
        page.goto(t["post_url"], wait_until="domcontentloaded", timeout=45000)
        time.sleep(4.5)
        # scroll a few times to make sure top-level comments are loaded
        for _ in range(4):
            page.mouse.wheel(0, 1000); time.sleep(0.7)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1.0)
        rank_info = page.evaluate(r"""
({thingid}) => {
  // List all depth-0 (top-level) comments in DOM order = the order Reddit
  // is rendering them in for this sort.
  const top = [];
  function walk(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('shreddit-comment').forEach(el => {
      if (el.getAttribute('depth') === '0') {
        top.push({thingid: el.getAttribute('thingid'), author: el.getAttribute('author'), score: el.getAttribute('score')});
      }
    });
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  }
  walk(document);
  const idx = top.findIndex(c => c.thingid === thingid);
  return {totalRenderedTopLevel: top.length, ourIndex: idx, ourScore: idx >= 0 ? top[idx].score : null, top5: top.slice(0, 5)};
}
""", {"thingid": t["thingid"]})

        entry = {"label": t["label"], "in_context": info, "rank": rank_info, "thingid": t["thingid"]}
        print(json.dumps(entry, indent=2))
        result.append(entry)

    browser.close()

open("bb_dimatty_deep_engagement.json", "w").write(json.dumps(result, indent=2))

print("\n=== SUMMARY ===")
for r in result:
    info = r["in_context"]
    mine = info.get("mine") or {}
    post = info.get("post") or {}
    rank = r.get("rank") or {}
    print(f"\n[{r['label']}]")
    print(f"  parent post: {post.get('title')!r:.80}")
    print(f"  parent comment_count: {post.get('comment_count')}, post score: {post.get('score')}")
    print(f"  our comment score: {mine.get('score')}  depth: {mine.get('depth')}")
    print(f"  direct replies: {len(info.get('replies') or [])}")
    for rep in (info.get("replies") or []):
        print(f"    by u/{rep['author']} (score {rep['score']}): {rep['text']!r:.150}")
    print(f"  visible rank in 'Best' sort (rendered): index {rank.get('ourIndex')} of {rank.get('totalRenderedTopLevel')}")
