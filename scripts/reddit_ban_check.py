#!/usr/bin/env python3
"""Deterministic Reddit community-ban detection.

When a post is discovered removed, "was that one post moderated, or is our
account BANNED from the whole community?" is not answerable from the removal
object itself: a mod-removed comment scrubs to author=[deleted]/body=[removed]
with no reason, and a ban does NOT retroactively delete our older comments, so
"our comments are still visible there" is NOT evidence we are un-banned.

The authoritative signal is the logged-in view of `/r/<sub>/about.json`, whose
`user_is_banned` field is populated per the requesting account's session. We
already hold a logged-in Reddit browser (the reddit-agent / reddit-harness
Chrome); this module CDP-attaches to it read-only (one navigation, N same-origin
fetches, no clicks/posts) and reads that field. Mirrors the read-only carve-out
pattern used elsewhere (linkedin_browser sidebar pre-check).

Graceful degradation: if no Reddit session is reachable (customer install with
no browser, wrong account, session logged out), every checker returns None and
callers MUST treat None as "unknown, do nothing" — never as "not banned".

Usage:
    python3 scripts/reddit_ban_check.py ClaudeAI theravada rust      # ad-hoc
    python3 scripts/reddit_ban_check.py --record --project podlog r/X # detect+persist

Public API:
    banned_state(subs)        -> {sub_lower: True|False|None}
    is_banned(sub)            -> True|False|None
    record_confirmed_bans(subs, project=None)
        -> {"banned": [...], "recorded": [...], "upgraded": [...], "unknown": [...]}
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# about.json JS: fetch each sub's logged-in metadata, return the ban flags.
_ABOUT_JS = r"""
async (subs) => {
  const out = {};
  for (const s of subs) {
    try {
      const r = await fetch(`https://old.reddit.com/r/${s}/about.json`,
                            {headers:{'Accept':'application/json'}, credentials:'include'});
      if (!r.ok) { out[s] = {http: r.status}; continue; }
      const d = (await r.json()).data || {};
      out[s] = { banned: d.user_is_banned === true,
                 muted: d.user_is_muted === true,
                 has_flag: ('user_is_banned' in d) };
    } catch (e) { out[s] = {err: String(e).slice(0,80)}; }
    await new Promise(x => setTimeout(x, 250));
  }
  return out;
}
"""


def _norm(sub: str) -> str:
    s = (sub or "").strip()
    m = re.search(r"/r/([^/]+)", s)
    if m:
        s = m.group(1)
    return s.strip().strip("/").lower()


def _reddit_page(pw):
    """Attach to the running logged-in Reddit Chrome and return (browser, page).

    Prefers REDDIT_CDP_URL, else discovers the reddit-agent port via
    reddit_browser.find_reddit_cdp_port. Opens a DEDICATED page so we never
    fight the live harness tab. Returns (None, None) if unreachable."""
    import urllib.request

    ws = None
    cdp_url = (os.environ.get("REDDIT_CDP_URL") or "").strip()
    if cdp_url:
        ws = cdp_url
    else:
        try:
            from reddit_browser import find_reddit_cdp_port
            port = find_reddit_cdp_port()
            if not port:
                return None, None
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=3
            ) as r:
                ws = json.load(r)["webSocketDebuggerUrl"]
        except Exception:
            return None, None
    try:
        browser = pw.chromium.connect_over_cdp(ws)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(20000)
        return browser, page
    except Exception:
        return None, None


def banned_state(subs) -> dict:
    """Return {sub_lower: True|False|None} for each sub. None = unknown
    (no session / fetch failed): callers MUST NOT treat None as un-banned."""
    norm = []
    for s in subs:
        n = _norm(s)
        if n and n not in norm:
            norm.append(n)
    result = {n: None for n in norm}
    if not norm:
        return result
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return result
    with sync_playwright() as pw:
        browser, page = _reddit_page(pw)
        if page is None:
            return result
        try:
            page.goto("https://old.reddit.com/", wait_until="domcontentloaded",
                      timeout=20000)
            # confirm we actually have a logged-in session; a logged-out view
            # reports user_is_banned=false for everything, a false negative.
            me = page.evaluate(
                "async () => { try { const r = await fetch("
                "'https://old.reddit.com/api/me.json', {credentials:'include'}); "
                "const d = await r.json(); return (d.data && d.data.name) || null; } "
                "catch(e){ return null; } }"
            )
            if not me:
                return result  # not logged in -> all unknown
            raw = page.evaluate(_ABOUT_JS, norm)
        except Exception:
            return result
        finally:
            try:
                page.close()
            except Exception:
                pass
    for n in norm:
        v = (raw or {}).get(n) or {}
        if v.get("has_flag"):
            result[n] = bool(v.get("banned"))
        else:
            result[n] = None  # http error / missing field -> unknown
    return result


def is_banned(sub) -> "bool|None":
    return banned_state([sub]).get(_norm(sub))


def record_confirmed_bans(subs, project=None) -> dict:
    """Check each sub and persist newly-confirmed community bans into
    config.json subreddit_bans.comment_blocked with reason
    'account_blocked_in_sub' (the reason platform_strike_events reads to emit
    platform_banned learning events). Upgrades pre-existing weak entries
    (reason=None) to the confirmed reason. Never records on None (unknown)."""
    from config import config_path
    from post_reddit import _make_ban_entry, _ban_entry_sub

    state = banned_state(subs)
    banned = [s for s, v in state.items() if v is True]
    unknown = [s for s, v in state.items() if v is None]
    recorded, upgraded = [], []
    if not banned:
        return {"banned": [], "recorded": [], "upgraded": [], "unknown": unknown}

    path = config_path()
    with open(path) as f:
        cfg = json.load(f)
    bans = cfg.setdefault("subreddit_bans", {})
    blocked = bans.setdefault("comment_blocked", [])
    by_sub = {(_ban_entry_sub(e) or "").lower(): e for e in blocked}

    for sub in banned:
        entry = by_sub.get(sub)
        if entry is None:
            blocked.append(_make_ban_entry(sub, "account_blocked_in_sub", project))
            recorded.append(sub)
        elif entry.get("reason") != "account_blocked_in_sub":
            # upgrade a weak/manual entry so the digest ban event can fire
            from datetime import datetime, timezone
            entry["reason"] = "account_blocked_in_sub"
            if not entry.get("added_at"):
                entry["added_at"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            entry.setdefault("noticed_by_project", project)
            upgraded.append(sub)

    if recorded or upgraded:
        blocked.sort(key=lambda e: (_ban_entry_sub(e) or ""))
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
    return {"banned": banned, "recorded": recorded, "upgraded": upgraded,
            "unknown": unknown}


# Removal-context JS: fetch each permalink's .json and extract the moderation
# artifacts a removal leaves behind: removed_by_category, the (possibly
# mod-rewritten) link flair, and any AutoModerator / distinguished-mod / sticky
# comments explaining the removal. All of this is public data; unlike
# banned_state no logged-in session is required, the CDP browser just gets us
# past Reddit's non-browser 403 wall.
_CONTEXT_JS = r"""
async (urls) => {
  const out = {};
  for (const u of urls) {
    try {
      const r = await fetch(u + '/.json?raw_json=1&limit=100',
                            {headers:{'Accept':'application/json'}, credentials:'include'});
      if (!r.ok) { out[u] = {http: r.status}; continue; }
      const d = await r.json();
      const s = d[0].data.children[0].data;
      const comments = ((d[1] && d[1].data.children) || [])
        .map(c => c.data).filter(c => c && c.author);
      const modish = comments.filter(c =>
        c.author === 'AutoModerator' || c.distinguished === 'moderator' || c.stickied);
      out[u] = {
        removed_by: s.removed_by_category || null,
        flair: s.link_flair_text || null,
        mod_notes: modish.slice(0, 2).map(c =>
          (c.author + ': ' + (c.body || '').replace(/\s+/g, ' ')).slice(0, 400)),
      };
    } catch (e) { out[u] = {err: String(e).slice(0, 80)}; }
    await new Promise(x => setTimeout(x, 400));
  }
  return out;
}
"""


def removal_context(urls) -> dict:
    """Fetch moderation context for removed reddit posts.

    urls: iterable of reddit permalinks (old. or www., thread or comment).
    Returns {original_url: {"removed_by": str|None, "flair": str|None,
    "mod_notes": [str, ...]} | None}. None = unknown (no browser reachable /
    fetch failed); callers MUST degrade gracefully, the strike event is still
    valid without context."""
    cleaned = {}  # original -> normalized fetch url
    for u in urls:
        u = str(u or "").strip()
        if not u or "reddit.com" not in u:
            continue
        cleaned[u] = u.replace("www.reddit.com", "old.reddit.com").rstrip("/")
    result = {u: None for u in cleaned}
    if not cleaned:
        return result
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return result
    with sync_playwright() as pw:
        browser, page = _reddit_page(pw)
        if page is None:
            return result
        try:
            page.goto("https://old.reddit.com/", wait_until="domcontentloaded",
                      timeout=20000)
            raw = page.evaluate(_CONTEXT_JS, list(cleaned.values()))
        except Exception:
            return result
        finally:
            try:
                page.close()
            except Exception:
                pass
    for orig, norm in cleaned.items():
        v = (raw or {}).get(norm) or {}
        if "removed_by" in v or "flair" in v or "mod_notes" in v:
            result[orig] = {"removed_by": v.get("removed_by"),
                            "flair": v.get("flair"),
                            "mod_notes": v.get("mod_notes") or []}
    return result


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("subs", nargs="+", help="subreddit names or /r/x URLs")
    ap.add_argument("--record", action="store_true",
                    help="persist confirmed bans to config.json")
    ap.add_argument("--project", default=None, help="audit breadcrumb")
    args = ap.parse_args()
    if args.record:
        print(json.dumps(record_confirmed_bans(args.subs, args.project), indent=2))
    else:
        print(json.dumps(banned_state(args.subs), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
