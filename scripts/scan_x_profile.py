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
   "comments": [...], "top_posts": [...], "top_replies": [...],
   "counts": {...}, "grounding_instructions": "..."}

top_posts / top_replies are the same items ranked by real engagement
(likes*3 + retweets*5 + replies*2), each stamped with rank + engagement_score.
On /with_replies each reply also carries `parent` = {author, text, url} (the
tweet it replied to, best-effort DOM-adjacent pairing), and the top posts get
their thread continuation expanded (`thread`: [tweet texts]) by visiting the
permalink. scripts/voice_exemplars.py turns these into voice.examples +
persona_corpus.txt exemplars.
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
  var WITH_PARENTS=%s; // true only on /with_replies: pair each reply with the
                       // other-author article directly above it (same
                       // conversation cell renders parent then our reply)
  var out=[];
  var lastOther=null;
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
    var tEl=art.querySelector('[data-testid="tweetText"]');
    var text=tEl?(tEl.innerText||'').trim():'';
    // permalink + id
    var url='';var id='';
    var statusLinks=art.querySelectorAll('a[href*="/status/"]');
    for(var k=0;k<statusLinks.length;k++){
      var sh=statusLinks[k].getAttribute('href')||'';
      var sm=sh.match(/\/status\/(\d+)/);
      if(sm){id=sm[1];url='https://x.com'+sh.split('?')[0];break;}
    }
    if(authorHandle && authorHandle!==ME){
      // Someone else's article. On /with_replies it is the parent of our next
      // reply in the same conversation cell; remember it. On the posts tab it
      // is a repost/quote we simply skip (WITH_PARENTS=false there).
      if(WITH_PARENTS && text){
        lastOther={author:'@'+authorHandle,text:text.slice(0,500),url:url};
      }
      continue;
    }
    if(!text) continue;
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
    var item={text:text,url:url,id:id,is_reply:isReply,reply_to:replyTo,
              likes:metric('like'),replies:metric('reply'),retweets:metric('retweet')};
    if(WITH_PARENTS){
      item.parent=lastOther; // may be null (their own standalone post)
      lastOther=null;        // consume: never pair one parent with two replies
    }
    out.push(item);
  }
  return JSON.stringify(out);
})()"""


def scrape_timeline(send, me: str, want: int, max_scrolls: int = 30,
                    exclude_ids: "set | None" = None,
                    capture_parents: bool = False) -> list:
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
    expr = _TIMELINE_JS_TMPL % (json.dumps(me.lower()),
                                "true" if capture_parents else "false")
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


def _own_posted_ids() -> set:
    """Status ids of everything S4L itself has posted from this install, via
    /api/v1/posts (install-scoped by the X-Installation header). Used to keep
    the bot's own output OUT of the author-voice exemplar pool: on accounts
    where S4L has been active, a recency scan is dominated by S4L drafts, and
    feeding those back as 'the author's voice' is a feedback loop. Best-effort:
    any failure (offline, fresh install, no API) returns an empty set and the
    scan proceeds unfiltered."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get  # noqa: PLC0415
        import re
        ids: set = set()
        rows = api_get("/posts", {"platform": "twitter", "has_our_url": "true",
                                  "limit": 2000}) or []
        if isinstance(rows, dict):
            rows = rows.get("posts") or rows.get("rows") or []
        for r in rows:
            m = re.search(r"/status/(\d+)", str(r.get("our_url") or ""))
            if m:
                ids.add(m.group(1))
        return ids
    except Exception as e:
        print(f"[scan_x_profile] own-posts exclusion unavailable: {e}", file=sys.stderr)
        return set()


# --------------------------------------------------------------------------- #
# Engagement ranking + thread expansion for exemplar extraction.
# --------------------------------------------------------------------------- #
def _engagement_score(item: dict) -> int:
    """Same weighting everywhere exemplars are ranked (voice_exemplars.py
    re-derives it if absent): a retweet is a stronger signal than a like,
    a like stronger than a reply-back."""
    return (int(item.get("likes") or 0) * 3
            + int(item.get("retweets") or 0) * 5
            + int(item.get("replies") or 0) * 2)


def rank_top(items: list, n: int = 5) -> list:
    """Top n items by engagement, stamped with rank + engagement_score.
    Zero-engagement items still rank (small accounts often have nothing else);
    the score on each entry lets downstream decide what to keep."""
    ranked = sorted(items, key=_engagement_score, reverse=True)[:n]
    out = []
    for i, it in enumerate(ranked, 1):
        e = dict(it)
        e["rank"] = i
        e["engagement_score"] = _engagement_score(it)
        out.append(e)
    return out


_THREAD_JS_TMPL = r"""(function(){
  var ME=%s;
  var out=[];var started=false;
  var arts=document.querySelectorAll('article');
  for(var i=0;i<arts.length;i++){
    var art=arts[i];
    var authorHandle='';
    var links=art.querySelectorAll('a[href^="/"]');
    for(var j=0;j<links.length;j++){
      var hh=links[j].getAttribute('href')||'';
      var mm=hh.match(/^\/([A-Za-z0-9_]{1,15})$/);
      if(mm){authorHandle=mm[1].toLowerCase();break;}
    }
    var tEl=art.querySelector('[data-testid="tweetText"]');
    var text=tEl?(tEl.innerText||'').trim():'';
    if(authorHandle===ME && text){out.push(text);started=true;}
    else if(started){break;} // first non-ME article after the run = end of thread
  }
  return JSON.stringify(out);
})()"""


def expand_thread(send, me: str, url: str) -> list:
    """Visit a post's permalink and return the consecutive run of the user's own
    tweets starting at the focal one ([focal, continuation, ...]). Length 1 =
    not a thread. Bonus: the permalink shows full text, so this also untruncates
    posts the timeline cut with 'Show more'. Best-effort; [] on failure."""
    if not url or not _navigate(send, url, settle=3.5, expect="/status/"):
        return []
    raw = _eval(send, _THREAD_JS_TMPL % json.dumps(me.lower())) or "[]"
    try:
        parts = json.loads(raw)
    except Exception:
        parts = []
    return parts if isinstance(parts, list) else []


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
    "3. GOLDEN-RULE EXAMPLES: `top_replies` and `top_posts` are already ranked by "
    "REAL engagement (each entry carries likes/retweets/replies, engagement_score, "
    "the parent tweet it replied to, and thread continuations). From them keep up "
    "to 5 replies verbatim as exemplars, skipping throwaway one-liners that show "
    "no voice; on small accounts engagement is sparse, so judge voice quality too. "
    "STORE them in the project's `voice.examples` (every drafter on every platform "
    "mirrors that field), or run "
    "`voice_exemplars.py apply --scan <scan.json> --project <name>` to write "
    "voice.examples + the persona_corpus.txt exemplar section deterministically.\n"
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
    ap.add_argument("--posts", type=int, default=60, help="max original posts to collect")
    ap.add_argument("--comments", type=int, default=150, help="max replies/comments to collect")
    ap.add_argument("--top", type=int, default=5, help="how many top posts/replies to rank")
    ap.add_argument("--expand-threads", type=int, default=3,
                    help="visit this many top posts' permalinks to capture thread "
                         "continuations + full text (0 = off)")
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

        # 2. Everything S4L itself posted from this install gets excluded from
        #    BOTH surfaces (posts and replies): the exemplars must be the
        #    human's writing, not the bot's own output echoed back.
        s4l_ids = _own_posted_ids()
        if s4l_ids:
            print(f"[scan_x_profile] excluding {len(s4l_ids)} s4l-posted statuses "
                  "from the exemplar pool", file=sys.stderr)

        # 3. Original posts (current page = posts tab). max_scrolls tracks the
        #    requested depth: the scan is programmatic, so scrolling deeper
        #    costs only time, and end-of-feed stall detection stops it early
        #    on small accounts.
        posts = (scrape_timeline(send, handle, args.posts,
                                 max_scrolls=max(30, args.posts),
                                 exclude_ids=s4l_ids)
                 if on_profile else [])
        post_ids = {p.get("id") for p in posts if p.get("id")}

        # 4. Replies / comments = the user's own articles on /with_replies that
        #    are NOT among the original posts (set subtraction, not DOM text).
        on_replies = _navigate(send, f"https://x.com/{handle}/with_replies",
                               settle=4.0, expect=f"/{handle}/with_replies")
        comments = (
            scrape_timeline(send, handle, args.comments,
                            max_scrolls=max(30, args.comments),
                            exclude_ids=post_ids | s4l_ids,
                            capture_parents=True)
            if on_replies else []
        )

        # 5. Rank both surfaces by real engagement, then expand the top posts'
        #    permalinks to capture thread continuations (and untruncated text).
        top_posts = rank_top(posts, args.top)
        top_replies = rank_top(comments, args.top)
        for tp in top_posts[:max(args.expand_threads, 0)]:
            parts = expand_thread(send, handle, tp.get("url") or "")
            if parts:
                if len(parts[0]) > len(tp.get("text") or ""):
                    tp["text"] = parts[0]  # permalink text is never truncated
                if len(parts) > 1:
                    tp["thread"] = parts

        result = {
            "ok": True,
            "state": "scanned",
            "handle": handle,
            "profile": profile,
            "posts": posts,
            "comments": comments,
            "top_posts": top_posts,
            "top_replies": top_replies,
            "counts": {"posts": len(posts), "comments": len(comments),
                       "s4l_posted_excluded": len(s4l_ids)},
            "grounding_instructions": GROUNDING_INSTRUCTIONS,
        }

        # Persist the corpus beside config.json so the later project-save step
        # (setup writes the project, then voice_exemplars.py auto-applies the
        # exemplars) works without a re-scan. The scan can run BEFORE config.json
        # exists on a fresh onboarding, hence mkdir. Best-effort: the scan is
        # still fully usable from stdout if the write fails.
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import s4l_mode
            sidecar = s4l_mode.config_path().parent / "last_profile_scan.json"
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps(result, ensure_ascii=False))
            result["scan_file"] = str(sidecar)
        except Exception:
            result["scan_file"] = None

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
