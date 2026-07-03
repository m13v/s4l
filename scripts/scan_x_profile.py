#!/usr/bin/env python3
"""Scan a logged-in user's X/Twitter profile to build a "grounding truth" corpus
for the setup wizard.

WHERE THIS FITS: right after setup's connect_x detects the real @handle, we
already have an authenticated CDP session on the autoposter's managed Chrome
(port 9555). This script reuses that session to read three surfaces of the
user's own profile:

  1. profile header  -> name, bio, location, url, follower/following, pinned
  2. posts tab       -> up to ~20 original posts (their authentic voice)
  3. /with_replies   -> up to ~50 of their own replies/comments (how they talk
                        TO people, which is what the autoposter actually does)

It does NOT synthesize anything. It returns one JSON blob (the corpus) plus a
`grounding_instructions` block. The setup *conversation* (the host agent already
interviewing the user) reads that and drafts the config fields (voice,
differentiator, icp, search_topics) in the user's own register, then confirms
with the user before writing config.json. Keeping synthesis in the conversation
(not a nested `claude -p`) is deliberate: it stays conversational and lets the
user correct the read before anything is saved.

Read-only. Never posts, never clicks, never writes config. Attaches to the
EXISTING managed Chrome; never launches a login flow.

Usage:
  python3 scripts/scan_x_profile.py [--handle m13v_] [--posts 20] [--comments 50]
  # --handle optional: if omitted, reads the live logged-in handle from the DOM.

Output (stdout, last line is JSON):
  {"ok": true, "handle": "...", "profile": {...}, "posts": [...],
   "comments": [...], "counts": {...}, "grounding_instructions": "..."}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

try:
    from websocket import create_connection  # websocket-client
except Exception:  # pragma: no cover
    create_connection = None  # type: ignore[assignment]

CDP = os.environ.get(
    "S4L_TWITTER_CDP_URL",
    os.environ.get("TWITTER_CDP_URL", "http://127.0.0.1:9555"),
).rstrip("/")


# --------------------------------------------------------------------------- #
# CDP attach (mirrors setup_twitter_auth.py::_attach so behavior is identical).
# --------------------------------------------------------------------------- #
def _attach():
    targets = json.load(urllib.request.urlopen(f"{CDP}/json", timeout=10))
    page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        page = json.load(
            urllib.request.urlopen(
                urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT"),
                timeout=10,
            )
        )
    ws = create_connection(page["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
    state = {"id": 0}

    def send(method, params=None):
        state["id"] += 1
        ws.send(json.dumps({"id": state["id"], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == state["id"]:
                return msg

    return ws, send


def _eval(send, expr: str):
    r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return (r.get("result", {}).get("result", {}) or {}).get("value")


def _current_url(send) -> str:
    return _eval(send, "location.href") or ""


def _navigate(send, url: str, settle: float = 3.5, expect: "str | None" = None,
              attempts: int = 3) -> bool:
    """Navigate and (optionally) assert we actually landed on `expect` (a substring
    of the URL). The managed Chrome is shared with the posting cycle, so another
    process can yank the page mid-load; retry instead of scraping the wrong page.
    Returns True if the expected URL was reached (or no expectation given)."""
    send("Page.enable")
    for _ in range(attempts):
        send("Page.navigate", {"url": url})
        time.sleep(settle)
        if not expect:
            return True
        for _ in range(6):
            if expect in (_current_url(send) or ""):
                return True
            time.sleep(1.5)
    return expect in (_current_url(send) or "")


# --------------------------------------------------------------------------- #
# Live handle (when --handle not passed). Same selectors as setup_twitter_auth.
# --------------------------------------------------------------------------- #
_HANDLE_JS = r"""(function(){
  function fromHref(sel){var a=document.querySelector(sel);if(a){var h=a.getAttribute('href')||'';var m=h.match(/^\/([A-Za-z0-9_]{1,15})$/);if(m)return m[1];}return '';}
  var h=fromHref('a[data-testid="AppTabBar_Profile_Link"]');
  if(h)return h;
  var b=document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
  if(b){var m=(b.textContent||'').match(/@([A-Za-z0-9_]{1,15})/);if(m)return m[1];}
  return '';
})()"""


def _resolve_live_handle(send) -> "str | None":
    u = _current_url(send)
    if "x.com" not in u and "twitter.com" not in u:
        _navigate(send, "https://x.com/home")
    for _ in range(8):
        v = (_eval(send, _HANDLE_JS) or "").strip().lstrip("@")
        if v:
            return v
        time.sleep(1)
    return None


# --------------------------------------------------------------------------- #
# Profile header scrape.
# --------------------------------------------------------------------------- #
_PROFILE_JS = r"""(function(){
  function txt(sel){var e=document.querySelector(sel);return e?(e.innerText||'').trim():'';}
  var name=txt('[data-testid="UserName"] span');
  var bio=txt('[data-testid="UserDescription"]');
  var loc=txt('[data-testid="UserLocation"]');
  var url=txt('[data-testid="UserUrl"]');
  var join=txt('[data-testid="UserJoinDate"]');
  // follower / following counts (anchors ending in /verified_followers, /followers, /following)
  function count(suffix){
    var a=document.querySelector('a[href$="/'+suffix+'"]');
    if(!a)return '';
    var s=(a.innerText||'').trim();
    var m=s.match(/([\d.,]+[KMB]?)/);
    return m?m[1]:s;
  }
  var following=count('following');
  var followers=count('verified_followers')||count('followers');
  // pinned tweet text (first article carrying a "Pinned" socialContext)
  var pinned='';
  var arts=document.querySelectorAll('article');
  for(var i=0;i<arts.length;i++){
    var sc=arts[i].querySelector('[data-testid="socialContext"]');
    if(sc && /pinned/i.test(sc.innerText||'')){
      var t=arts[i].querySelector('[data-testid="tweetText"]');
      pinned=t?(t.innerText||'').trim():'';
      break;
    }
  }
  return JSON.stringify({name:name,bio:bio,location:loc,url:url,join:join,followers:followers,following:following,pinned:pinned});
})()"""


def scrape_profile(send) -> dict:
    raw = _eval(send, _PROFILE_JS) or "{}"
    try:
        return json.loads(raw)
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Timeline scrape (posts tab OR /with_replies). Scrolls, dedupes by tweet URL,
# classifies each article as authored-post vs reply, keeps only the user's OWN
# articles (drops reposts/quotes of other accounts that show on the main tab).
# --------------------------------------------------------------------------- #
_TIMELINE_JS_TMPL = r"""(function(){
  var ME=%s; // lowercase handle without @
  var out=[];
  var arts=document.querySelectorAll('article');
  for(var i=0;i<arts.length;i++){
    var art=arts[i];
    // author handle for THIS article
    var authorHandle='';
    var links=art.querySelectorAll('a[href^="/"]');
    for(var j=0;j<links.length;j++){
      var hh=links[j].getAttribute('href')||'';
      var mm=hh.match(/^\/([A-Za-z0-9_]{1,15})$/);
      if(mm){authorHandle=mm[1].toLowerCase();break;}
    }
    if(authorHandle && authorHandle!==ME) continue; // skip others' posts (reposts/quotes/threads)
    var tEl=art.querySelector('[data-testid="tweetText"]');
    var text=tEl?(tEl.innerText||'').trim():'';
    if(!text) continue;
    // permalink + id
    var url='';var id='';
    var statusLinks=art.querySelectorAll('a[href*="/status/"]');
    for(var k=0;k<statusLinks.length;k++){
      var sh=statusLinks[k].getAttribute('href')||'';
      var sm=sh.match(/\/status\/(\d+)/);
      if(sm){id=sm[1];url='https://x.com'+sh.split('?')[0];break;}
    }
    // reply? presence of a "Replying to" header in the cell
    var isReply=false, replyTo='';
    var spans=art.querySelectorAll('span,div');
    for(var s=0;s<spans.length;s++){
      var st=(spans[s].innerText||'');
      if(/^Replying to/i.test(st)){
        isReply=true;
        var rm=st.match(/@([A-Za-z0-9_]{1,15})/);
        if(rm)replyTo='@'+rm[1];
        break;
      }
    }
    // engagement (best-effort from aria-labels)
    function metric(name){
      var b=art.querySelector('[data-testid="'+name+'"]');
      if(!b)return 0;
      var al=b.getAttribute('aria-label')||'';
      var m=al.match(/([\d,]+)/);
      return m?parseInt(m[1].replace(/,/g,''),10):0;
    }
    out.push({text:text,url:url,id:id,is_reply:isReply,reply_to:replyTo,
              likes:metric('like'),replies:metric('reply'),retweets:metric('retweet')});
  }
  return JSON.stringify(out);
})()"""


def scrape_timeline(send, me: str, want: int, max_scrolls: int = 30,
                    exclude_ids: "set | None" = None) -> list:
    """Scroll the current timeline, collecting up to `want` of the user's OWN
    authored articles (in DOM order = newest first). `exclude_ids` drops items
    already captured elsewhere — that's how the comments pass (/with_replies)
    subtracts the original posts to leave just replies. We do NOT rely on a
    'Replying to' header: the profile /with_replies timeline doesn't render one
    per article, so post-vs-reply is decided by set subtraction, not DOM text.

    End-of-feed is detected by COLLECTED-COUNT STALL, not scrollHeight: x.com
    virtualizes the timeline (unloads off-screen articles and keeps total height
    ~constant while swapping content), so scrollHeight plateaus even mid-feed and
    would false-trigger an early stop. We instead stop when no NEW item has been
    captured for `STALL_LIMIT` consecutive scrolls (after a min number of scrolls),
    scrolling to the bottom each step to force the next lazy-load batch."""
    seen: dict[str, dict] = {}
    exclude_ids = exclude_ids or set()
    expr = _TIMELINE_JS_TMPL % json.dumps(me.lower())
    STALL_LIMIT = 4
    stall = 0
    for n in range(max_scrolls):
        raw = _eval(send, expr) or "[]"
        try:
            batch = json.loads(raw)
        except Exception:
            batch = []
        before = len(seen)
        for item in batch:
            key = item.get("id") or item.get("url") or item.get("text", "")[:80]
            if not key or key in seen or key in exclude_ids:
                continue
            seen[key] = item
        if len(seen) >= want:
            break
        # No new items this pass? Count it as a stall. Give the feed a few
        # consecutive empty scrolls (lazy-load can lag) before declaring the end.
        if len(seen) == before and n > 0:
            stall += 1
            if stall >= STALL_LIMIT:
                break
        else:
            stall = 0
        # Scroll to the bottom of currently-loaded content to trigger the next
        # batch, then wait for it to render before the next read.
        _eval(send, "window.scrollTo(0, document.documentElement.scrollHeight);")
        time.sleep(2.0)
    items = list(seen.values())
    return items[:want]


GROUNDING_INSTRUCTIONS = (
    "You now have this user's real X profile (bio, original posts, and their own "
    "replies). Use it as GROUND TRUTH to draft their autoposter config fields in "
    "THEIR register, not a generic marketing voice. Specifically:\n"
    "1. PROFESSION & IDENTITY: from the bio + what they post about, state who they "
    "are and what they do. This anchors `description`/`differentiator`.\n"
    "2. VOICE & VIBE: read the actual posts/replies and capture HOW they talk, the "
    "tone (dry, hype, technical, warm, terse, profane, formal), sentence length, "
    "capitalization habits, emoji/punctuation usage, and recurring phrases or tics. "
    "Write the `voice` field so a reply drafted with it would be indistinguishable "
    "from something they'd actually type.\n"
    "3. GOLDEN-RULE EXAMPLES: pick 2-4 of their strongest real replies/posts "
    "verbatim and keep them as exemplars (these become few-shot anchors). Choose "
    "ones that show the target reply behavior: helpful, specific, in-voice.\n"
    "4. PHRASE BANK: list the kinds of phrases / openers / sign-offs they reuse, "
    "and any words/claims they clearly AVOID (for `content_guardrails`).\n"
    "5. ICP: infer who they engage with (who they reply to, what communities) to "
    "draft `icp`.\n"
    "6. SEARCH TOPICS: pull the recurring themes/keywords from their posts into "
    "`search_topics` (these literally seed the X searches the cycle runs).\n"
    "Then SHOW the user your read ('here's the voice/topics I picked up from your "
    "profile, does this sound like you?') and only call setup to save after they "
    "confirm or correct it. Never invent traits the corpus doesn't support."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", default=None, help="@handle to scan (default: live logged-in handle)")
    ap.add_argument("--posts", type=int, default=20, help="max original posts to collect")
    ap.add_argument("--comments", type=int, default=50, help="max replies/comments to collect")
    args = ap.parse_args()

    if create_connection is None:
        print(json.dumps({"ok": False, "state": "error",
                          "error": "websocket-client not installed (needed for CDP)."}))
        return 1

    try:
        ws, send = _attach()
    except Exception as e:
        print(json.dumps({"ok": False, "state": "browser_not_running",
                          "error": f"Could not attach to managed Chrome on {CDP}: {e}. "
                                   "Run setup action:'connect_x' first."}))
        return 1

    try:
        send("Page.enable")
        send("Runtime.enable")

        handle = (args.handle or "").strip().lstrip("@") or _resolve_live_handle(send)
        if not handle:
            print(json.dumps({"ok": False, "state": "no_handle",
                              "error": "Could not determine the logged-in X handle. "
                                       "Confirm X is connected (setup action:'connect_x')."}))
            return 1

        # 1. Profile header (also lands us on the posts tab).
        on_profile = _navigate(send, f"https://x.com/{handle}", settle=4.0,
                               expect=f"/{handle}")
        profile = scrape_profile(send) if on_profile else {}

        # 2. Original posts (current page = posts tab).
        posts = scrape_timeline(send, handle, args.posts) if on_profile else []
        post_ids = {p.get("id") for p in posts if p.get("id")}

        # 3. Replies / comments = the user's own articles on /with_replies that
        #    are NOT among the original posts (set subtraction, not DOM text).
        on_replies = _navigate(send, f"https://x.com/{handle}/with_replies",
                               settle=4.0, expect=f"/{handle}/with_replies")
        comments = (
            scrape_timeline(send, handle, args.comments, exclude_ids=post_ids)
            if on_replies else []
        )

        result = {
            "ok": True,
            "state": "scanned",
            "handle": handle,
            "profile": profile,
            "posts": posts,
            "comments": comments,
            "counts": {"posts": len(posts), "comments": len(comments)},
            "grounding_instructions": GROUNDING_INSTRUCTIONS,
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "state": "error", "error": str(e)}))
        return 1
    finally:
        try:
            ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
