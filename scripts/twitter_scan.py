#!/usr/bin/env python3
"""
twitter_scan.py — deterministic X/Twitter search scrape.

Runs inside the browser-harness CLI process (BU_NAME=twitter-harness,
BU_CDP_URL=http://127.0.0.1:9555) and drives the live managed Chrome on 9555.
Called once per drafted query by run-twitter-cycle.sh's Phase 1 lean loop:

    BU_NAME=twitter-harness BU_CDP_URL=http://127.0.0.1:9555 \
        browser-harness -c "
    import sys; sys.path.insert(0, '/Users/matthewdi/social-autoposter/scripts')
    from twitter_scan import scan
    for q in <queries list>:
        scan(query=q['query'], project=q['project'],
             search_topic=q['search_topic'],
             freshness_hours=<env FRESHNESS_HOURS_DISCOVER>,
             skip_ids=<env ENGAGED_TWEET_IDS>)
    "

The cycle shell drafts queries via a small Claude call (no tools), then loops
this function per query. Result tweets go to SCAN_TWEETS_FILE (env-set), which
the cycle reads directly into $RAW_FILE + $QUERIES_FILE for the scorer.

What scan() does:
- Strips any since/until/since_time/until_time from the query so the
  freshness window is operator-controlled, not caller-controlled.
- Builds an x.com/search URL with `&f=live` (Latest tab forced) and appends
  `since_time:<now - freshness_hours*3600>` to the query.
- Reuses an existing real tab or opens one on the first call.
- Scrapes the first ~8 article cards.
- Applies a deterministic Python age gate behind the URL since_time
  (belt-and-suspenders against cached / lazy-loaded stale viewports).
- Drops skip_ids (recently-engaged tweets).
- Stamps search_topic / matched_project / query on every kept tweet.
- Appends a sidecar JSONL record to
  ~/social-autoposter/skill/logs/twitter-scan-attempts.jsonl for operator
  visibility, and a per-attempt record to SCAN_TWEETS_FILE for the shell.
- Returns the kept tweet list.

Standalone test (no cycle shell):

    ~/.local/bin/browser-harness -c '
    import sys; sys.path.insert(0, "/Users/matthewdi/social-autoposter/scripts")
    from twitter_scan import scan
    scan(query="AI agent min_faves:10",
         project="WhatsApp MCP",
         search_topic="AI agent",
         freshness_hours=6)
    '
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse

# Pin the daemon socket BEFORE importing helpers — helpers.py reads BU_NAME
# at module-load time (helpers.py:37). setdefault is a no-op when the bh_run
# wrapper or the cycle shell already set these; required when invoked from a
# bare `browser-harness -c` test invocation where the env happens to be empty.
os.environ.setdefault("BU_NAME", "twitter-harness")
os.environ.setdefault("BU_CDP_URL", "http://127.0.0.1:9555")

from browser_harness.helpers import (  # noqa: E402  (env must be set first)
    goto_url,
    js,
    list_tabs,
    new_tab,
    wait_for_load,
)

_SIDECAR = (
    pathlib.Path.home()
    / "social-autoposter"
    / "skill"
    / "logs"
    / "twitter-scan-attempts.jsonl"
)

# Derived from skill/run-twitter-cycle.sh:666-708 (the legacy inline scan JS).
# twitter_candidates fields (handle, text, tweetUrl, datetime, engagement
# counters) land in the same shape the scorer and dashboard already consume.
# 2026-06-04 DIVERGENCE: this copy adds repost awareness on top of the legacy
# JS — `handle` is now taken from the status URL (authoritative original author,
# since on a repost the first profile link is the REPOSTER), plus `is_repost`
# and `reposted_by` from the "<X> reposted" socialContext banner. This is the
# live data path (the cycle reads SCAN_TWEETS_FILE written here); the locked
# shell's inline JS is the inert fallback and simply omits the two new fields.
_SCRAPE_JS = r"""
(() => {
  const SNOWFLAKE = /\/status\/(\d{15,19})(?:[\/?#]|$)/;
  const FAKE_TAIL = /0{6,}$/;
  const results = [];
  for (const article of [...document.querySelectorAll('article[data-testid="tweet"]')].slice(0, 8)) {
    try {
      let handle = '';
      for (const link of article.querySelectorAll('a[role="link"]')) {
        const href = link.getAttribute('href');
        if (href && href.startsWith('/') && !href.includes('/status/') && !href.includes('/search') && href.length > 1 && href.split('/').length === 2) {
          handle = href.replace('/', ''); break;
        }
      }
      const tweetText = article.querySelector('[data-testid="tweetText"]');
      const text = tweetText ? tweetText.textContent : '';
      const timeEl = article.querySelector('time');
      const timeParent = timeEl ? timeEl.closest('a') : null;
      const tweetUrl = timeParent ? 'https://x.com' + timeParent.getAttribute('href') : '';
      const datetime = timeEl ? timeEl.getAttribute('datetime') : '';
      const sm = tweetUrl.match(SNOWFLAKE);
      if (!sm || FAKE_TAIL.test(sm[1])) continue;
      if (!datetime || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(datetime)) continue;
      // Author = first path segment of the status URL. This is authoritative:
      // on a repost the displayed/first-link handle above is the REPOSTER, not
      // the original author, so prefer the URL author and keep the link-scan
      // handle only as a fallback when the URL can't be parsed.
      const authorM = tweetUrl.match(/x\.com\/([^\/]+)\/status\//);
      if (authorM && authorM[1]) handle = authorM[1];
      // Repost detection: a "<X> reposted" banner lives in socialContext. The
      // SAME testid is reused for "Pinned", so match the text, not presence.
      // reposted_by = the account whose profile link wraps the banner.
      let is_repost = false, reposted_by = '';
      const sc = article.querySelector('[data-testid="socialContext"]');
      if (sc && /\breposted\b/i.test(sc.textContent || '')) {
        is_repost = true;
        const a = sc.closest('a');
        const rh = a ? (a.getAttribute('href') || '') : '';
        if (rh.startsWith('/') && rh.split('/').length === 2) reposted_by = rh.replace('/', '');
      }
      let replies=0, retweets=0, likes=0, views=0, bookmarks=0;
      for (const btn of article.querySelectorAll('[role="group"] button')) {
        const al = btn.getAttribute('aria-label') || '';
        let m;
        if (m=al.match(/([\d,]+)\s*repl/i)) replies=parseInt(m[1].replace(/,/g,''));
        if (m=al.match(/([\d,]+)\s*repost/i)) retweets=parseInt(m[1].replace(/,/g,''));
        if (m=al.match(/([\d,]+)\s*like/i)) likes=parseInt(m[1].replace(/,/g,''));
        if (m=al.match(/([\d,]+)\s*view/i)) views=parseInt(m[1].replace(/,/g,''));
        if (m=al.match(/([\d,]+)\s*bookmark/i)) bookmarks=parseInt(m[1].replace(/,/g,''));
      }
      results.push({handle, text, tweetUrl, datetime, replies, retweets, likes, views, bookmarks, is_repost, reposted_by});
    } catch(e) {}
  }
  return results;
})()
"""

# 2026-05-28: also matches the bash-arithmetic form
# `since_time:$(( $(date +%s) - FRESHNESS_HOURS_DISCOVER * 3600 ))` that was
# accidentally taught to the model when an escaping bug in the prompt sent
# the literal template text instead of an evaluated epoch. \S+ alone stops
# at the first space and leaves the tail (`$(date +%s) - ... ))`) behind as
# keyword garbage that X searches for literally. The non-greedy `.*?` inside
# `$((...))` matches up to the first `))` which is the template's own close.
_DATE_OPS_RE = re.compile(
    r"\b(since|until|since_time|until_time):(?:\$\(\(.*?\)\)|\S+)",
    re.IGNORECASE,
)
# Belt + suspenders: even after _DATE_OPS_RE, residual orphan fragments could
# remain if the model invents some other broken template. Strip common ones.
_BASH_GARBAGE_RE = re.compile(
    r"\$\(\(|\$\([^)]*\)|\bFRESHNESS_HOURS_DISCOVER\s*\*\s*\d+\b|\)\)"
)
_STATUS_ID_RE = re.compile(r"/status/(\d+)")


def _build_url(query: str, freshness_hours: int) -> str:
    """Force-build the Latest-tab URL with since_time pinned `freshness_hours` ago.

    Stripping the model's date operators first is what closes the dodge: a
    rogue `since:2020-01-01` in the model's query string can no longer widen
    the window. `f=live` is what closes the Top-tab dodge: without it X may
    serve the Top tab where the time operator is advisory."""
    cleaned = _DATE_OPS_RE.sub("", query)
    cleaned = _BASH_GARBAGE_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cap_epoch = int(time.time()) - int(freshness_hours) * 3600
    full = f"{cleaned} since_time:{cap_epoch}".strip()
    return "https://x.com/search?q=" + urllib.parse.quote(full) + "&src=typed_query&f=live"


def _parse_dt_epoch(ds: str):
    if not ds:
        return None
    try:
        return int(
            datetime.datetime.fromisoformat(ds.replace("Z", "+00:00")).timestamp()
        )
    except (ValueError, TypeError):
        return None


def _status_id(url: str):
    m = _STATUS_ID_RE.search(url or "")
    return m.group(1) if m else None


def _write_sidecar(rec: dict) -> None:
    try:
        _SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        with _SIDECAR.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # fail-open; sidecar is operator visibility only, not on the data path


def _write_scan_tweets_record(rec: dict) -> None:
    """Append one JSONL record per scan() call to the path in SCAN_TWEETS_FILE.

    2026-05-28: shell-side data path. When the cycle exports SCAN_TWEETS_FILE,
    run-twitter-cycle.sh reads this file after the scan claude session ends
    and uses it as the source of truth for both $RAW_FILE (tweets fed to the
    scorer) and $QUERIES_FILE (attempts fed to log_twitter_search_attempts.py),
    skipping the model's structured_output relay entirely. This cuts the
    relay-tokens bill (model no longer has to copy the tweets/queries_used
    arrays from bh_run stdout into structured_output).

    Inert when SCAN_TWEETS_FILE is unset; the model's structured_output path
    remains the fallback so existing standalone test invocations (no cycle
    env) and any session where the file write fails still produce candidates."""
    path = os.environ.get("SCAN_TWEETS_FILE")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # fail-open; shell falls back to structured_output if file is missing


def _revive_current_tab(url: str) -> bool:
    """Side-door revive of a wedged tab: attach a FRESH CDP session to the
    SAME target and stop+navigate it there, so the one-tab-reused-forever
    invariant survives a hung renderer.

    When goto_url times out, what died is (almost always) the tab session or
    the renderer, not the daemon's browser-level connection — the daemon just
    REPORTED the timeout over that very connection. Page.stopLoading and
    Page.navigate are handled by the BROWSER process, and navigate is allowed
    to replace a crashed/hung renderer, so a fresh session to the same target
    recovers the tab in place. Finish by re-pointing the daemon at the fresh
    session (set_session, deliberately WITHOUT Target.activateTarget — that
    call raises/focuses the Chrome window). Returns True when the navigate
    was acknowledged; False routes the caller to the new-tab last resort
    (only justified when the browser's handling of the target itself is
    dead). Outcome is logged either way: every real wedge doubles as an
    experiment on which recovery door works."""
    from browser_harness import helpers as _bh

    try:
        tid = None
        try:
            tid = (_bh.current_tab() or {}).get("targetId")
        except Exception:
            pass
        if not tid:
            infos = _bh.cdp("Target.getTargets").get("targetInfos", [])
            pages = [t for t in infos if t.get("type") == "page"]
            pref = [t for t in pages if (t.get("url") or "").startswith("http")]
            tid = ((pref or pages) or [{}])[0].get("targetId")
        if not tid:
            print("[twitter_scan] revive: no page target found", file=sys.stderr)
            return False
        sid = _bh.cdp("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
        try:
            _bh.cdp("Page.stopLoading", session_id=sid)
        except Exception:
            pass  # stop is best-effort; navigate is the recovery
        _bh.cdp("Page.navigate", session_id=sid, url=url)
        _bh._send({"meta": "set_session", "session_id": sid, "target_id": tid})
        print(
            f"[twitter_scan] revived wedged tab in place (target={tid[:12]})",
            file=sys.stderr,
        )
        return True
    except Exception as e:
        print(f"[twitter_scan] revive failed ({e})", file=sys.stderr)
        return False


def _navigate(url: str) -> None:
    """ALWAYS reuse the daemon's current tab; escalate through revive; create
    a tab only when both fail — and then in background, so it cannot pop.

    Recovery ladder (2026-07-15, after the Chrome 150 wedge storm):
      1. goto_url — plain reuse; also revives an "Aw, Snap" tab whose session
         still answers. The only path 99% of navigations take.
      2. _revive_current_tab — fresh CDP session to the SAME target
         (stop+navigate); recovers a wedged session/renderer in place.
      3. new_tab(background=True) — last resort for a dead target.
         Target.createTarget/activateTarget are PROVEN to focus-steal (7/7
         same-second harness_browser_foregrounded correlation), hence
         background; the TypeError fallback covers customer boxes whose
         installed harness predates the background parameter."""
    try:
        goto_url(url)
        return
    except Exception as e:
        print(
            f"[twitter_scan] goto_url failed ({e}); trying in-place revive",
            file=sys.stderr,
        )
    if _revive_current_tab(url):
        return
    try:
        new_tab(url, background=True)
    except TypeError:
        new_tab(url)


# Per-query stall guard: shared implementation (scripts/stall_guard.py) used
# by every browser-touching pipeline step — do NOT re-implement per step.
# S4L_SCAN_QUERY_DEADLINE_S kept as a scan-specific override for continuity
# with rc.36-38; unset, the shared default applies.
from stall_guard import stall_guard  # noqa: E402

_SCAN_DEADLINE_OVERRIDE = os.environ.get("S4L_SCAN_QUERY_DEADLINE_S")


def scan(
    query: str,
    project: str,
    search_topic: str,
    freshness_hours: int = 6,
    skip_ids=None,
    settle_seconds: float = 4.0,
) -> list:
    """Deterministic scrape + age gate. Prints JSON between
    ###TWEETS_BEGIN###/###TWEETS_END### sentinels for the scan model to relay
    into StructuredOutput; also returns the kept list so direct callers (tests,
    future shell-driven invocations) can consume it without parsing stdout."""
    with stall_guard(
        "scan_query",
        query,
        deadline_s=float(_SCAN_DEADLINE_OVERRIDE) if _SCAN_DEADLINE_OVERRIDE else None,
    ):
        return _scan_inner(query, project, search_topic, freshness_hours, skip_ids, settle_seconds)


def _scan_inner(
    query: str,
    project: str,
    search_topic: str,
    freshness_hours: int = 6,
    skip_ids=None,
    settle_seconds: float = 4.0,
) -> list:
    skip = {str(s) for s in (skip_ids or [])}
    url = _build_url(query, int(freshness_hours))
    _navigate(url)
    wait_for_load(timeout=15.0)
    # X lazy-loads the result list; settle briefly before scraping. Matches
    # the legacy template's `time.sleep(4)`.
    time.sleep(float(settle_seconds))

    raw = js(_SCRAPE_JS)
    tweets = raw if isinstance(raw, list) else []
    pre_count = len(tweets)

    cap_epoch = int(time.time()) - int(freshness_hours) * 3600
    fresh = []
    for t in tweets:
        ep = _parse_dt_epoch(t.get("datetime", ""))
        if ep is not None and ep >= cap_epoch:
            fresh.append(t)
    dropped_age = pre_count - len(fresh)

    kept = [t for t in fresh if _status_id(t.get("tweetUrl", "")) not in skip]
    dropped_skip = len(fresh) - len(kept)

    for t in kept:
        t["search_topic"] = search_topic
        t["matched_project"] = project
        t["query"] = query

    _write_sidecar(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ts_epoch": int(time.time()),
            "query": query,
            "project": project,
            "search_topic": search_topic,
            "freshness_hours": int(freshness_hours),
            "url": url,
            "pre_count": pre_count,
            "kept_after_age": len(fresh),
            "dropped_age": dropped_age,
            "kept_after_skip": len(kept),
            "dropped_skip": dropped_skip,
            "batch_id": os.environ.get("BATCH_ID"),
            "cycle_variant": os.environ.get("TWITTER_CYCLE_VARIANT"),
        }
    )

    # Shell-side data path. The cycle (when it exports SCAN_TWEETS_FILE) reads
    # this file directly instead of asking the scan model to relay tweets via
    # structured_output, saving relay tokens. One JSONL record per scan() call;
    # the cycle aggregates across all calls in one Phase 1 attempt.
    _write_scan_tweets_record(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "query": query,
            "project": project,
            "search_topic": search_topic,
            "tweets": kept,
        }
    )

    # 2026-05-28 cleanup: sentinel-print removed. The cycle reads SCAN_TWEETS_FILE
    # directly via _write_scan_tweets_record() above; the bh_run stdout relay path
    # is no longer wired. scan() still returns `kept` so direct callers can use it.
    return kept
