#!/usr/bin/env python3
"""LinkedIn SERP discovery: read-only Phase A search-page scrape.

Usage:
    python3 discover_linkedin_candidates.py <vertical> <query>
        # vertical = people | content | companies

Attaches to the already-running LinkedIn Chrome via CDP and reuses its
existing BrowserContext. Same Chrome process, same cookies, same UA, same
fingerprint as whatever LinkedIn already trusts from the live session.
Opens our own page in that context, navigates to the SERP, runs ONE
page.evaluate() against the rendered DOM, closes our page, disconnects.

CDP endpoint resolution (see _resolve_cdp_url, 2026-05-29):
    Lane 1 (preferred): LINKEDIN_CDP_URL — the browser-harness Chrome on
        :9556, exported by skill/lib/linkedin-backend.sh. The main
        run-linkedin.sh pipeline now drives that harness Chrome
        (mcp__linkedin-harness__bh_run) instead of the retired
        linkedin-agent MCP, so this is the live, logged-in session.
    Lane 2 (legacy): http://localhost:<port> read from the linkedin-agent
        MCP's DevToolsActivePort. Kept for running outside the harness.

Read-only DOM scrape: NO Voyager API, NO scroll-and-expand loops, NO
permalink fan-out, NO clicks/typing, NO programmatic login.

Pre-conditions for this to work:
    1. A LinkedIn Chrome is running and reachable via one of the two CDP
       lanes above (normally the harness Chrome on :9556 launched by
       ensure_linkedin_browser_for_backend).
    2. The user is logged in inside that browser. We do NOT log in.

Why CDP attach rather than launch_persistent_context: the previous version
launched its own Chrome against the shared profile dir. When LinkedIn
redirected the SERP request (UA mismatch / fresh-launch fingerprint) the
homepage response contained Set-Cookie headers that cleared li_at. On
context.close() Chrome flushed the cleared cookies to disk, logging the
shared profile out and breaking unread-dms + the linkedin-agent MCP.
Attaching to the MCP's running Chrome eliminates the launch fingerprint,
removes the cookie-flush risk (we never close the context), and keeps the
profile fully owned by one process at a time.

Per CLAUDE.md "LinkedIn: flagged patterns" carve-out (2026-04-29): the
read-only DOM read is permitted because the request runs inside the same
Chrome the MCP already drives. The 2026-04-17 restriction was caused by
Voyager calls + permalink scroll loops, neither of which appear here.

Rate-limited against linkedin_browser_searches per the 2026-04-29 research:
~30s min gap, ~40/day, ~150/month soft cap leaves headroom under LinkedIn's
~300/month commercial-use wall on free accounts. Fails CLOSED on DB errors:
if we cannot enforce the budget we do not perform the search.

Output (stdout, JSON):
    {
        "ok": true,
        "url": "https://www.linkedin.com/search/results/people/?keywords=...",
        "vertical": "people",
        "query": "founder rag retrieval",
        "result_count": 10,
        "results": [...],
        "rate_budget": {"daily_used": N, "daily_cap": null,
                        "monthly_used": N, "monthly_cap": null},
    }

Failure shapes:
    {"ok": false, "error": "session_invalid", "url": "..."}
    {"ok": false, "error": "serp_redirected", "url": "..."}
    {"ok": false, "error": "mcp_not_running", "detail": "..."}
    {"ok": false, "error": "cdp_attach_failed", "detail": "..."}
    {"ok": false, "error": "navigation_failed", "detail": "..."}
    {"ok": false, "error": "bad_vertical", "detail": "..."}
    {"ok": false, "error": "empty_query", "detail": ""}

Note: rate_limited and db_unavailable are no longer raised. All caps were
removed 2026-05-01; the script logs to linkedin_browser_searches for
visibility but never refuses based on volume or recency.

Exits 0 on success, 1 on failure.
"""

import json
import os
import random
import sys
import time
import urllib.parse
from typing import Optional


def _bh_activity_log(action: str, cdp_url: str) -> None:
    """Append to the universal browser-activity.log (Python-CDP path coverage)."""
    try:
        import time as _t
        import os as _o
        from pathlib import Path as _P
        _p = _P(_o.environ.get(
            "BH_ACTIVITY_LOG",
            str(_P.home() / ".claude" / "browser-profiles" / "browser-activity.log"),
        ))
        _port = (cdp_url or "").rsplit(":", 1)[-1].split("/")[0] or "-"
        _p.parent.mkdir(parents=True, exist_ok=True)
        with _p.open("a") as _f:
            _f.write(
                f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] pycdp "
                f"script={_o.path.basename(__file__)} action={action} "
                f"pid={_o.getpid()} ppid={_o.getppid()} cdp={cdp_url or '-'} "
                f"port={_port}\n"
            )
    except Exception:
        pass

# Reuse the lock helper + login-URL detector from linkedin_browser. We share
# the lock so concurrent Python helpers (search vs unread-dms) serialize on
# the same ~/.claude/linkedin-agent-lock.json. PROFILE_DIR also points at
# the directory where the linkedin-agent MCP writes DevToolsActivePort.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from linkedin_browser import (  # noqa: E402
    PROFILE_DIR,
    _acquire_browser_lock,
    _is_login_or_checkpoint,
)
from score_linkedin_candidates import calculate_velocity_score  # noqa: E402

DEVTOOLS_ACTIVE_PORT = os.path.join(PROFILE_DIR, "DevToolsActivePort")

# Virality (velocity * reach_mult * age_decay * (1 + disc_bonus); see
# score_linkedin_candidates.calculate_velocity_score) is a RANKING signal,
# not a cutoff. Aligned with the Twitter model (2026-05-29): score every
# SERP card, sort by velocity_score DESC so the Phase A picker sees the
# strongest candidates first, and NEVER drop a card on virality. The picker
# prompt already steers toward the top of the sorted list and leans toward
# posting, so the old hard floor only caused zero-post cycles on quiet
# topics where every card scored low (e.g. niche backend-dev SERPs). Twitter
# (score_twitter_candidates.py) keeps every candidate the same way: "no cap,
# no cutoff: this only ever raises a score, never removes a candidate."

# Search rate-limit budget removed 2026-05-01 per user instruction. The
# linkedin_browser_searches table is kept so daily/monthly volumes remain
# observable, but no min-gap, daily, or monthly cap is enforced. Caller is
# responsible for cadence. The 2026-04-17 LinkedIn restriction (see CLAUDE.md
# "LinkedIn: flagged patterns") came from behavioral fingerprinting, not raw
# volume, so volume caps weren't the load-bearing protection anyway — but
# back-to-back machine-cadence search hits are now structurally possible
# from this script.
SEARCH_VERTICALS = ("people", "content", "companies")
SEARCH_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS linkedin_browser_searches (
    id        SERIAL PRIMARY KEY,
    query     TEXT NOT NULL,
    vertical  TEXT NOT NULL,
    ok        BOOLEAN NOT NULL,
    error     TEXT,
    ran_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lbs_ran_at
    ON linkedin_browser_searches(ran_at DESC);
"""


def _open_db():
    """Lazy-import scripts.db; raises ImportError on failure."""
    import db as dbmod  # type: ignore  # noqa: WPS433
    dbmod.load_env()
    return dbmod.get_conn()


def _check_rate_limit() -> dict:
    """Always returns ok=True. Caps removed 2026-05-01 per user instruction.

    Still touches the DB to (a) create the linkedin_browser_searches table on
    first use and (b) read current daily/monthly volume so the response shape
    keeps the rate_budget block populated for the dashboard. A DB error here
    is non-fatal — the search proceeds anyway since there's no cap to enforce.
    """
    daily = monthly = 0
    try:
        conn = _open_db()
        try:
            for stmt in [s.strip() for s in SEARCH_TABLE_DDL.split(";") if s.strip()]:
                conn.execute(stmt)
            conn.commit()
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM linkedin_browser_searches "
                "WHERE ran_at >= NOW() - INTERVAL '24 hours'"
            )
            daily = cur.fetchone()["n"]
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM linkedin_browser_searches "
                "WHERE ran_at >= date_trunc('month', NOW())"
            )
            monthly = cur.fetchone()["n"]
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        # DB down? Not our problem — caps are off, search proceeds.
        pass
    return {"ok": True, "daily_used": daily, "monthly_used": monthly}


def _log_search(query: str, vertical: str, ok: bool, error: Optional[str]) -> None:
    """Best-effort write of one row to linkedin_browser_searches.

    Never raises: a failed log must not turn a successful search into a
    failure. Note that if logging fails the next rate-limit check will
    under-count. Acceptable: the monthly cap has 50% headroom under the
    actual wall.
    """
    try:
        conn = _open_db()
    except Exception as e:
        print(
            f"[discover_linkedin_candidates] _log_search: db open failed: {e}",
            file=sys.stderr,
        )
        return
    try:
        conn.execute(
            "INSERT INTO linkedin_browser_searches "
            "(query, vertical, ok, error) VALUES (%s, %s, %s, %s)",
            [query, vertical, ok, error],
        )
        conn.commit()
    except Exception as e:
        print(
            f"[discover_linkedin_candidates] _log_search: insert failed: {e}",
            file=sys.stderr,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


# DOM extractors per vertical. Each is a single querySelectorAll + map, with
# multiple selector fallbacks because LinkedIn's class names rotate. Returns
# JSON.stringify(...) so the Python side can json.loads regardless of how the
# evaluate channel marshals nested objects. Limit to the first 25 cards on the
# page — anything beyond that requires scrolling, which we explicitly do not
# do.
#
# Provenance:
#   _SEARCH_JS_CONTENT  — lifted from skill/run-linkedin.sh (production-tested).
#   _SEARCH_JS_PEOPLE   — UNVERIFIED. Selectors based on widely-documented
#                         LinkedIn patterns + multiple fallbacks. Smoke-test
#                         against a real SERP before relying on the output.
#   _SEARCH_JS_COMPANIES — UNVERIFIED. Same caveat as people.
#
# Reconciliation procedure for the UNVERIFIED extractors (do this once,
# then update this block to mark them VERIFIED with the date):
#   Preconditions:
#     - linkedin-agent has been idle >= 1 hour. Check
#       ~/.playwright-mcp/linkedin-agent/page-*.yml mtimes.
#     - The persistent profile is logged in. Do NOT trigger a probe like
#       "navigate to LinkedIn and tell me what you see" — that prompt itself
#       is the high-risk behavior that invalidated cookies on 2026-04-29.
#       If the session is dead, wait for the next normal pipeline cycle to
#       re-auth, then resume reconciliation in a fresh hour.
#   Steps (use the linkedin-agent MCP, NOT this script — the script logs to
#   linkedin_browser_searches and burns the rate budget for nothing):
#     1. mcp__linkedin-agent__browser_navigate to
#        https://www.linkedin.com/search/results/people/?keywords=founder%20ai
#     2. mcp__linkedin-agent__browser_evaluate, paste _SEARCH_JS_PEOPLE
#        verbatim (including the JSON.stringify wrap). JSON.parse the
#        returned string.
#        Accept criterion: >= 5 entries with non-empty name AND profile_url.
#        Reject: [] or rows with all-empty fields → snapshot the page,
#        find the live card class names, patch the querySelectorAll lists.
#        Keep existing fallback selectors at the END of each list to stay
#        compatible with the older layout.
#     3. Repeat for /search/results/companies/?keywords=founder%20ai with
#        _SEARCH_JS_COMPANIES. Same accept criterion (>= 5 cards with
#        company AND company_url).
#   Hard limits during reconciliation: 2 navigations total, no
#   close-and-reopen of the agent, no scroll, no clicks. Anything more is
#   the same fingerprint pattern that triggered the 2026-04-29 lockouts.
_SEARCH_JS_PEOPLE = r"""
() => {
  const out = [];
  const cards = document.querySelectorAll(
    "div.search-results-container li div.entity-result, "
    + "li.reusable-search__result-container, "
    + "[data-chameleon-result-urn]"
  );
  for (const c of Array.from(cards).slice(0, 25)) {
    const link = c.querySelector(
      "a[href*='/in/'].app-aware-link, a[href*='/in/']"
    );
    const profileUrl = link
      ? (link.href || link.getAttribute("href") || "")
      : "";
    const nameEl = c.querySelector(
      ".entity-result__title-text, .entity-result__title-line, "
      + "span[aria-hidden='true']"
    );
    const name = nameEl ? (nameEl.textContent || "").trim() : "";
    const headlineEl = c.querySelector(
      ".entity-result__primary-subtitle, .t-14.t-black.t-normal"
    );
    const headline = headlineEl
      ? (headlineEl.textContent || "").trim() : "";
    const locEl = c.querySelector(
      ".entity-result__secondary-subtitle, .t-14.t-normal"
    );
    const location = locEl ? (locEl.textContent || "").trim() : "";
    if (!name && !profileUrl) continue;
    out.push({
      name: name.replace(/\s+/g, " "),
      headline: headline.replace(/\s+/g, " "),
      location: location.replace(/\s+/g, " "),
      profile_url: profileUrl.split("?")[0],
    });
  }
  return JSON.stringify(out);
}
"""

# Content-search extractor. Two layouts coexist in the wild:
#
#   New SDUI layout (post 2026-04-30 reconciliation): obfuscated class names,
#   results wrapped in [data-sdui-screen*="SearchResultsContent"], each card
#   [role="listitem"][componentkey]. The activity URN is GONE from the DOM
#   for most cards: only cards that embed a quoted/reposted share keep a
#   visible /feed/update/<urn> link. So post_url/activity_id can legitimately
#   be null on the new layout — callers must dedupe by
#   (author_profile_url, post_text hash) when activity_id is missing.
#
#   Legacy class layout (pre-rollout, may still appear): div.feed-shared-update-v2
#   / div[data-urn=...] cards with full URNs.
#
# Tries the new layout first, falls back to legacy, returns the same shape
# either way. Verified 2026-04-30 against
# /search/results/content/?keywords=ai%20agent%20founder
# 8/8 cards extracted (author_name + author_profile_url + post_text + age_text);
# 1/8 had activity_id (the only embedded-share case).
_SEARCH_JS_CONTENT = r"""
() => {
  const out = [];

  function parseRelativeAge(txt) {
    if (!txt) return null;
    const m = txt.match(/(\d+)\s*(s|min|m|hr|h|d|w|mo|y)\b/i);
    if (!m) return null;
    const n = parseInt(m[1], 10);
    let u = m[2].toLowerCase();
    if (u === 'hr') u = 'h';
    if (u === 'min') u = 'm';
    const map = { s: 1/3600, m: 1/60, h: 1, d: 24, w: 24*7, mo: 24*30, y: 24*365 };
    return n * (map[u] || 0);
  }
  function parseCount(txt) {
    if (!txt) return 0;
    const t = String(txt).replace(/,/g, '').trim();
    const m = t.match(/([\d.]+)\s*([KkMm]?)/);
    if (!m) return 0;
    const n = parseFloat(m[1]);
    const u = (m[2] || '').toLowerCase();
    return Math.round(n * (u === 'k' ? 1000 : u === 'm' ? 1_000_000 : 1));
  }

  // 1. New SDUI layout.
  let items = [];
  const screen = document.querySelector('[data-sdui-screen*="SearchResultsContent"]');
  if (screen) {
    items = Array.from(screen.querySelectorAll('[role="listitem"][componentkey]'));
  }
  // 2. Legacy fallback.
  if (items.length === 0) {
    items = Array.from(document.querySelectorAll(
      'div.feed-shared-update-v2, '
      + 'div[data-urn*="urn:li:activity"], '
      + 'div[data-urn*="urn:li:share"], '
      + 'div[data-urn*="urn:li:ugcPost"]'
    ));
  }

  const seen = new Set();
  const urnRe = /urn:li:(activity|share|ugcPost):(\d{16,19})/;
  const urnReG = /urn:li:(activity|share|ugcPost):(\d{16,19})/g;

  for (const item of items.slice(0, 25)) {
    let urnType = null, activityId = null;
    const allUrns = new Set();

    const updateLink = item.querySelector('a[href*="/feed/update/"]');
    if (updateLink) {
      const m = (updateLink.href || '').match(urnRe);
      if (m) { urnType = m[1]; activityId = m[2]; allUrns.add(m[2]); }
    }
    if (!activityId) {
      const dataUrn = item.getAttribute('data-urn') || '';
      const m = dataUrn.match(urnRe);
      if (m) { urnType = m[1]; activityId = m[2]; allUrns.add(m[2]); }
    }
    if (!activityId) {
      const html = item.outerHTML || '';
      let mm;
      urnReG.lastIndex = 0;
      while ((mm = urnReG.exec(html)) !== null) {
        allUrns.add(mm[2]);
        if (!activityId) { urnType = mm[1]; activityId = mm[2]; }
      }
    }
    if (activityId) {
      if (seen.has(activityId)) continue;
      seen.add(activityId);
    }

    const authorLink = item.querySelector('a[aria-label*="profile" i][href*="/in/"]')
      || item.querySelector('a[href*="/in/"]');
    const authorUrl = authorLink ? (authorLink.href || '').split('?')[0] : null;
    let authorName = null;
    if (authorLink) {
      const al = authorLink.getAttribute('aria-label') || '';
      const m = al.match(/View\s+(.+?)['’]s\s+profile/i);
      if (m) authorName = m[1].trim();
    }
    // The new SDUI layout puts the View-profile aria on an inner <svg>, not
    // the <a>. Probe descendants of the link too before falling back.
    if (!authorName && authorLink) {
      const inner = authorLink.querySelector('[aria-label*="profile" i]');
      if (inner) {
        const m = (inner.getAttribute('aria-label') || '').match(/View\s+(.+?)['’]s\s+profile/i);
        if (m) authorName = m[1].trim();
      }
    }
    if (!authorName) {
      const followBtn = item.querySelector('button[aria-label^="Follow "]');
      if (followBtn) {
        const m = (followBtn.getAttribute('aria-label') || '').match(/^Follow\s+(.+)$/i);
        if (m) authorName = m[1].trim();
      }
    }
    if (!authorName) {
      const nameEl = item.querySelector(
        '.update-components-actor__name, span.feed-shared-actor__name'
      );
      if (nameEl) authorName = (nameEl.textContent || '').trim();
    }

    let authorFollowers = null;
    const supplementary = item.querySelector(
      '.update-components-actor__supplementary-actor-info, '
      + '.feed-shared-actor__sub-description'
    );
    if (supplementary) {
      const fm = (supplementary.textContent || '').match(/([\d.,]+[KkMm]?)\s*follower/);
      if (fm) authorFollowers = parseCount(fm[1]);
    }

    // Actor block = the prefix of the listitem text before "• Follow". On the
    // new SDUI layout it has the shape "Feed post<NAME> • <CONNECTION><HEADLINE><AGE>".
    const fullItemText = (item.textContent || '').replace(/\s+/g, ' ').trim();
    const followIdx0 = fullItemText.indexOf('• Follow');
    const actorBlock = followIdx0 >= 0 ? fullItemText.slice(0, followIdx0) : fullItemText.slice(0, 300);

    // Author headline: strip "Feed post" prefix, the name, the connection
    // marker, and the trailing age. Best-effort; for company pages or
    // non-standard layouts (no • <connection>) we still return whatever's
    // left after the name.
    let authorHeadline = null;
    {
      let h = actorBlock.replace(/^Feed post/, '').trim();
      if (authorName && h.startsWith(authorName)) h = h.slice(authorName.length);
      h = h.replace(/^\s*•\s*(1st|2nd|3rd\+?|Out of network|Following)\s*/i, '');
      h = h.replace(/\s*(?:•\s*)?\d+\s*(?:s|min|m|hr|h|d|w|mo|y)\s*$/i, '');
      h = h.trim();
      if (h) authorHeadline = h;
    }

    // Post body. Legacy: prefer the dedicated text element. New SDUI: take
    // text after "• Follow", then strip trailing CTA / count noise.
    let postText = '';
    const textEl = item.querySelector(
      '.update-components-text, .feed-shared-update-v2__description, span.break-words'
    );
    if (textEl) {
      postText = (textEl.textContent || '').replace(/\s+/g, ' ').trim();
    } else {
      let s = fullItemText.replace(/^Feed post/, '').trim();
      const idx = s.indexOf('• Follow');
      if (idx >= 0) s = s.slice(idx + '• Follow'.length).trim();
      // Strip trailing "… more" / "...more" the new layout appends.
      s = s.replace(/\s*[…\.]+\s*more\s*$/i, '').trim();
      // Strip trailing count noise like "+132 comments23 reactions",
      // "1 comment1", "+811 reaction", "23 reactions".
      // Count widgets concatenate without delimiters; consume runs greedily.
      for (let i = 0; i < 6; i++) {
        const before = s;
        s = s.replace(
          /\s*\+?\s*\d+\s*(?:reactions?|comments?|reposts?)\s*\d*\s*$/i,
          ''
        ).trim();
        if (s === before) break;
      }
      // Strip a stray trailing digit (artifact of glued-in count widgets).
      s = s.replace(/\s+\d+\s*$/, '').trim();
      postText = s;
    }

    let ageText = '';
    const timeEl = item.querySelector(
      'time, .update-components-actor__sub-description, '
      + 'span.feed-shared-actor__sub-description'
    );
    if (timeEl) ageText = (timeEl.textContent || '').trim();
    if (!ageText) {
      const ageM = fullItemText.match(/(\d+\s*(?:s|min|m|hr|h|d|w|mo|y))\b/i);
      if (ageM) ageText = ageM[1];
    }
    const ageHours = parseRelativeAge(ageText);

    // Counts. New SDUI hides counts from button aria-labels and embeds them
    // as plain leaf-divs ("1 comment", "23 reactions", "+811 reaction").
    // We walk every leaf div/span and match the strict shape; we keep the
    // max in case the same widget is mirrored across nested wrappers.
    let reactions = 0, comments = 0, reposts = 0;
    item.querySelectorAll('div, span').forEach(el => {
      if (el.children.length > 0) return;
      const t = (el.textContent || '').trim();
      if (!t || t.length > 30) return;
      let m;
      if ((m = t.match(/^[+]?\s*([\d.,]+\s*[KkMm]?)\s+reactions?$/i))) {
        const v = parseCount(m[1]);
        if (v > reactions) reactions = v;
      }
      if ((m = t.match(/^[+]?\s*([\d.,]+\s*[KkMm]?)\s+comments?$/i))) {
        const v = parseCount(m[1]);
        if (v > comments) comments = v;
      }
      if ((m = t.match(/^[+]?\s*([\d.,]+\s*[KkMm]?)\s+reposts?$/i))) {
        const v = parseCount(m[1]);
        if (v > reposts) reposts = v;
      }
    });
    // Legacy fallbacks (unchanged): aria-label-based counts on the old layout.
    if (reactions === 0) {
      const reactEl = item.querySelector(
        '[aria-label*=" reaction" i], '
        + '.social-details-social-counts__reactions-count'
      );
      if (reactEl) {
        const m = (reactEl.getAttribute('aria-label') || reactEl.textContent || '')
          .match(/([\d.,]+\s*[KkMm]?)\s*reaction/i);
        if (m) reactions = parseCount(m[1]);
      }
    }
    if (comments === 0) {
      const commentEl = item.querySelector(
        '[aria-label*=" comment" i], '
        + 'li.social-details-social-counts__comments'
      );
      if (commentEl) {
        const m = (commentEl.getAttribute('aria-label') || commentEl.textContent || '')
          .match(/([\d.,]+\s*[KkMm]?)\s*comment/i);
        if (m) comments = parseCount(m[1]);
      }
    }
    if (reposts === 0) {
      const repostEl = item.querySelector(
        '[aria-label*=" repost" i], '
        + 'li.social-details-social-counts__item--right-aligned'
      );
      if (repostEl) {
        const m = (repostEl.getAttribute('aria-label') || repostEl.textContent || '')
          .match(/([\d.,]+\s*[KkMm]?)\s*repost/i);
        if (m) reposts = parseCount(m[1]);
      }
    }

    if (!authorName && !authorUrl && !postText) continue;

    out.push({
      post_url: activityId
        ? ('https://www.linkedin.com/feed/update/urn:li:' + urnType + ':' + activityId + '/')
        : null,
      activity_id: activityId,
      all_urns: Array.from(allUrns),
      author_name: authorName || null,
      author_headline: authorHeadline,
      author_profile_url: authorUrl,
      author_followers: authorFollowers,
      post_text: postText,
      age_hours: ageHours,
      reactions: reactions,
      comments: comments,
      reposts: reposts,
      age_text: ageText
    });
  }
  return JSON.stringify(out);
}
"""

_SEARCH_JS_COMPANIES = r"""
() => {
  const out = [];
  const cards = document.querySelectorAll(
    "div.search-results-container li div.entity-result, "
    + "li.reusable-search__result-container, "
    + "[data-chameleon-result-urn]"
  );
  for (const c of Array.from(cards).slice(0, 25)) {
    const link = c.querySelector(
      "a[href*='/company/'].app-aware-link, a[href*='/company/']"
    );
    const url = link ? (link.href || link.getAttribute("href") || "") : "";
    const nameEl = c.querySelector(
      ".entity-result__title-text, .entity-result__title-line, "
      + "span[aria-hidden='true']"
    );
    const name = nameEl ? (nameEl.textContent || "").trim() : "";
    const taglineEl = c.querySelector(
      ".entity-result__primary-subtitle, .t-14.t-black.t-normal"
    );
    const tagline = taglineEl ? (taglineEl.textContent || "").trim() : "";
    if (!name && !url) continue;
    out.push({
      company: name.replace(/\s+/g, " "),
      tagline: tagline.replace(/\s+/g, " "),
      company_url: url.split("?")[0],
    });
  }
  return JSON.stringify(out);
}
"""

_SEARCH_JS_BY_VERTICAL = {
    "people": _SEARCH_JS_PEOPLE,
    "content": _SEARCH_JS_CONTENT,
    "companies": _SEARCH_JS_COMPANIES,
}


def _read_devtools_port() -> Optional[int]:
    """Return the CDP port the linkedin-agent MCP's Chrome is listening on,
    or None if the file is missing/unreadable/stale. Chrome writes the port
    on line 1 of DevToolsActivePort when launched with --remote-debugging-port.

    Chrome SHOULD remove the file when it exits, but doesn't always — a
    crashed/killed Chrome leaves a stale file pointing at a port nothing's
    listening on. We probe the port with a non-blocking TCP connect; if the
    connection is refused, we treat the file as stale and return None so
    callers report the cleaner mcp_not_running error rather than dragging
    out to a noisy cdp_attach_failed."""
    try:
        with open(DEVTOOLS_ACTIVE_PORT) as f:
            port = int(f.readline().strip())
        if port <= 0:
            return None
    except (OSError, ValueError):
        return None
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return port
    except (OSError, socket.timeout):
        return None


def _resolve_cdp_url() -> Optional[str]:
    """Resolve the CDP endpoint to attach the SERP read to.

    Lane 1 (preferred, 2026-05-29): LINKEDIN_CDP_URL, exported by
    skill/lib/linkedin-backend.sh to point at the browser-harness Chrome on
    :9556. The main run-linkedin.sh pipeline now drives that harness Chrome
    (mcp__linkedin-harness__bh_run) instead of the retired linkedin-agent
    MCP, so this is the live session whose cookies/fingerprint we want. We
    probe /json/version with a 1s GET so a stale/unset env falls through
    cleanly rather than dragging into a noisy connect failure.

    Lane 2 (legacy DevToolsActivePort attach to the linkedin-agent profile
    under PROFILE_DIR) was REMOVED 2026-05-31. It silently sent the SERP read
    to a SECOND Chrome (the retired linkedin-agent MCP browser) whenever the
    harness was momentarily unreachable — the "two LinkedIn browsers in
    parallel" bug. The harness Chrome on :9556 is now the ONLY allowed target.

    Returns the harness CDP base URL (e.g. "http://127.0.0.1:9556") or None
    when LINKEDIN_CDP_URL is unset or the harness is unreachable.
    """
    harness = os.environ.get("LINKEDIN_CDP_URL", "").strip()
    if not harness:
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"{harness.rstrip('/')}/json/version", timeout=1.0
        ):
            return harness.rstrip("/")
    except Exception:
        return None


def search(vertical: str, query: str) -> dict:
    """Attach to the linkedin-agent MCP's Chrome via CDP and read one SERP.

    ONE goto, ONE evaluate. No own-Chrome launch, no context.close(),
    so we never write cookies back to disk. Rate-limited against
    linkedin_browser_searches; fails closed if the DB budget is exhausted.
    """
    if vertical not in SEARCH_VERTICALS:
        return {
            "ok": False,
            "error": "bad_vertical",
            "detail": f"got {vertical!r}; want one of {SEARCH_VERTICALS}",
        }
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "empty_query", "detail": ""}

    rate = _check_rate_limit()
    if not rate.get("ok"):
        return rate

    cdp_url = _resolve_cdp_url()
    if cdp_url is None:
        return {
            "ok": False,
            "error": "mcp_not_running",
            "detail": (
                "No LinkedIn CDP endpoint reachable. Set LINKEDIN_CDP_URL "
                "(the browser-harness Chrome on :9556, exported by "
                "skill/lib/linkedin-backend.sh) and make sure that Chrome is "
                f"running; or, for the legacy lane, {DEVTOOLS_ACTIVE_PORT} "
                "must point at a live linkedin-agent MCP Chrome launched with "
                "--remote-debugging-port."
            ),
        }

    from playwright.sync_api import sync_playwright

    _acquire_browser_lock()

    encoded = urllib.parse.quote(query)
    # Content searches sort by date_posted to match skill/run-linkedin.sh
    # Phase A behavior — fresh posts > stale ones for engagement work.
    suffix = "&sortBy=date_posted" if vertical == "content" else ""
    search_url = (
        f"https://www.linkedin.com/search/results/{vertical}/"
        f"?keywords={encoded}{suffix}"
    )
    serp_prefix = f"https://www.linkedin.com/search/results/{vertical}/"

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            _log_search(query, vertical, ok=False, error="cdp_attach_failed")
            return {
                "ok": False,
                "error": "cdp_attach_failed",
                "detail": f"connect_over_cdp({cdp_url}) failed: {e}",
            }

        _bh_activity_log("attach", cdp_url)

        # Reuse the existing context (cookies / UA / fingerprint already set
        # by the MCP launch). Never close it — that would kill the MCP's
        # pages too. We only own the page we create below.
        if not browser.contexts:
            browser.disconnect()
            _log_search(query, vertical, ok=False, error="cdp_attach_failed")
            return {
                "ok": False,
                "error": "cdp_attach_failed",
                "detail": "browser.contexts is empty; MCP has no open context",
            }
        context = browser.contexts[0]

        page = None
        _reused_page = False
        try:
            # Reuse an existing harness tab instead of spawning a throwaway one
            # (mirrors reddit_browser / linkedin_browser). Prefer a tab already
            # on linkedin.com (not login/checkpoint), else the first open page;
            # only new_page() when the context has no usable tab. A reused tab
            # is left open in the finally below so the next consumer reuses it.
            for pg in context.pages:
                u = pg.url or ""
                if "linkedin.com" in u and "login" not in u and "checkpoint" not in u:
                    page, _reused_page = pg, True
                    break
            if page is None and context.pages:
                page, _reused_page = context.pages[0], True
            if page is None:
                page = context.new_page()
            try:
                page.goto(
                    search_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                _log_search(query, vertical, ok=False, error="navigation_failed")
                return {
                    "ok": False,
                    "error": "navigation_failed",
                    "detail": str(e),
                }

            # Settle: search results lazy-render after DOMContentLoaded.
            # Selectors cover the new SDUI layout (post 2026-04 rollout) AND
            # the legacy class layout, in that order.
            try:
                page.wait_for_selector(
                    "[data-sdui-screen*='SearchResultsContent'], "
                    "div.search-results-container, "
                    "main[aria-label*='Search'], "
                    "div.feed-shared-update-v2",
                    timeout=10000,
                )
            except Exception:
                pass  # extractor will return [] if nothing rendered

            # Random 2-4s human-pacing delay before reading the DOM. The new
            # SDUI layout streams cards in after the screen container exists;
            # 1-3s sometimes returned 6/8 cards. 2-4s reliably gets 8/8.
            page.wait_for_timeout(random.randint(2000, 4000))

            cur_url = page.url
            if _is_login_or_checkpoint(cur_url):
                _log_search(query, vertical, ok=False, error="session_invalid")
                return {
                    "ok": False,
                    "error": "session_invalid",
                    "url": cur_url,
                }
            # LinkedIn's anti-automation likes to redirect a refused SERP to
            # https://www.linkedin.com/ (no /login marker). Without this
            # check the extractor would run on the homepage, find nothing,
            # and we'd return ok:true with result_count:0 — masking failure
            # as an empty query. Require landing on the SERP path.
            if not cur_url.startswith(serp_prefix):
                _log_search(query, vertical, ok=False, error="serp_redirected")
                return {
                    "ok": False,
                    "error": "serp_redirected",
                    "url": cur_url,
                }

            raw = page.evaluate(_SEARCH_JS_BY_VERTICAL[vertical])
            try:
                results = json.loads(raw or "[]")
            except json.JSONDecodeError:
                results = []

            if vertical == "content":
                for r in results:
                    velocity, virality, age_clamped = calculate_velocity_score(r)
                    r["engagement_velocity"] = velocity
                    r["velocity_score"] = virality
                    r["age_hours_clamped"] = age_clamped
                # Twitter model: rank, never drop. Sort by velocity_score DESC
                # so the Phase A picker sees the strongest candidates first and
                # takes from the top; weak cards stay eligible as fallback so
                # quiet topics still yield a comment instead of zero-posting.
                results.sort(key=lambda x: x.get("velocity_score") or 0, reverse=True)

            _log_search(query, vertical, ok=True, error=None)
            return {
                "ok": True,
                "url": cur_url,
                "vertical": vertical,
                "query": query,
                "result_count": len(results),
                "dropped_below_virality_floor": 0,
                "virality_floor": None,
                "results": results,
                "rate_budget": {
                    "daily_used": rate.get("daily_used"),
                    "daily_cap": None,
                    "monthly_used": rate.get("monthly_used"),
                    "monthly_cap": None,
                },
            }

        finally:
            # Close ONLY a page WE created, never the context or the browser.
            # The MCP keeps owning the Chrome instance and its existing pages.
            # A reused tab is left open so the next consumer can reuse it.
            try:
                if page is not None and not _reused_page:
                    page.close()
            except Exception:
                pass
            try:
                browser.disconnect()
            except Exception:
                pass


def search_with_retry(vertical: str, query: str, max_attempts: int = 2) -> dict:
    """One retry on transient browser-target failures only. Do NOT retry on
    session_invalid / mcp_not_running / serp_redirected."""
    last_result: dict = {"ok": False, "error": "no_attempts"}
    for attempt in range(1, max_attempts + 1):
        try:
            result = search(vertical, query)
        except Exception as e:
            result = {
                "ok": False,
                "error": "exception",
                "detail": f"{type(e).__name__}: {e}",
                "attempt": attempt,
            }
        last_result = result
        err = (result.get("error") or "").lower()
        detail = (result.get("detail") or "").lower()
        transient = (
            "targetclosed" in detail
            or "target page" in detail
            or "browser has been closed" in detail
            or err == "navigation_failed"
        )
        if result.get("ok") or not transient or attempt >= max_attempts:
            if attempt > 1:
                result["retry_attempt"] = attempt
            return result
        print(
            f"[discover_linkedin_candidates] transient failure attempt "
            f"{attempt}: {result.get('detail') or result.get('error')}; "
            f"retrying...",
            file=sys.stderr,
        )
        time.sleep(2)
    return last_result


def main():
    # Guard: only authorized pipelines may invoke this helper. Other Claude
    # subprocess planners auto-load CLAUDE.md as system context, see this
    # helper documented there, and have wandered off-task to "smoke test"
    # it — racing the linkedin profile's SingletonLock and triggering
    # server-side session invalidation. The legitimate caller sets the
    # matching env var immediately before invoking; nothing else does.
    if os.environ.get("SOCIAL_AUTOPOSTER_LINKEDIN_SEARCH") != "1":
        print(
            json.dumps({
                "ok": False,
                "error": "unauthorized_caller",
                "detail": (
                    "discover_linkedin_candidates.py is invoked only by the "
                    "run-linkedin Phase A discovery pipeline. Set "
                    "SOCIAL_AUTOPOSTER_LINKEDIN_SEARCH=1 from the caller if "
                    "this invocation is legitimate."
                ),
            }),
            file=sys.stderr,
        )
        sys.exit(2)
    if len(sys.argv) < 3:
        print(
            "Usage: discover_linkedin_candidates.py "
            "<people|content|companies> <query>",
            file=sys.stderr,
        )
        sys.exit(2)
    vertical = sys.argv[1]
    query = " ".join(sys.argv[2:])
    result = search_with_retry(vertical, query)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
