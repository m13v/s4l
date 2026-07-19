#!/bin/bash
# reddit-backend.sh - Reddit pipeline browser bootstrap (reddit-harness,
# mirrors twitter-backend.sh / linkedin-backend.sh; the shared machinery
# lives in skill/lib/harness-common.sh since 2026-07-14).
#
# 2026-05-29 migration: Reddit's discovery path (reddit_tools.py) fetched
# Reddit's *.json via Python urllib, which Reddit began 403ing from residential
# IPs on 2026-05-28 (TLS-fingerprint + no-cookies block). Fetching the same
# JSON from inside a logged-in real-Chrome page returns 200. So the entire
# Reddit pipeline (discovery + posting) now rides a dedicated browser-harness
# Chrome on port 9557, profile ~/.claude/browser-profiles/reddit-harness
# (seeded from the existing logged-in ~/.claude/browser-profiles/reddit).
#
# Source this AFTER lock.sh, BEFORE any acquire_lock / browser pre-flight /
# claude -p subprocess calls. Sets these for the caller:
#
#   MCP_CONFIG_FILE        - claude -p --mcp-config path (reddit-harness MCP)
#   BROWSER_INSTRUCTIONS   - prompt block describing the harness backend +
#                            its bh_run tool surface (inject at the TOP of any
#                            prompt that mentions browser_* tools)
#
# And exports (so Python subprocesses like reddit_browser.py / reddit_tools.py
# inherit them):
#
#   REDDIT_CDP_URL         - http://127.0.0.1:9557 (forces direct CDP attach,
#                            skipping ps-based agent-profile discovery; also
#                            tells reddit_tools.py to fetch JSON via the browser)
#
# Provides these functions (names mirror twitter/linkedin-backend for the
# existing call shape in run-reddit-search.sh, run-reddit-threads.sh,
# engage-reddit.sh, dm-outreach-reddit.sh, link-edit-reddit.sh, etc.):
#
#   ensure_reddit_browser_for_backend
#     Call AFTER acquire_lock "reddit-browser". Probes harness Chrome on
#     port 9557 and launches it idempotently if down (wedge-aware, focus-safe;
#     see harness-common.sh), then cleans leftover tabs from prior runs.
#
#   defer_if_foreign_for_backend [log_file]
#     No-op. Harness CDP supports multiple concurrent clients on the same
#     Chrome (no SingletonLock fight), so foreign MCP wrappers never block us.

MCP_CONFIG_FILE="$HOME/.claude/browser-agent-configs/reddit-harness-mcp.json"

# Per-host env override (written by bin/cli.js when installing on an AppMaker
# VM). On a Mac dev box this file does not exist, so the default below kicks in.
if [ -f "$HOME/.social-autoposter-env" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.social-autoposter-env"
fi

# Tell reddit_browser.py + reddit_tools.py (and any other Python helper that
# honors this env var) to skip ps-based discovery and connect directly to the
# configured CDP endpoint. Default 9557 (Mac harness Chrome, separate port from
# Twitter's 9555 and LinkedIn's 9556).
export REDDIT_CDP_URL="${REDDIT_CDP_URL:-http://127.0.0.1:9557}"

# Default harness URL - used to decide whether we own this Chrome (and should
# launch/clean it) or whether it is externally managed (AppMaker, BYO).
_BH_REDDIT_DEFAULT_URL="http://127.0.0.1:9557"

# Shared engine: _BH_REPO_DIR, hc_ensure_browser, hc_cleanup_tabs,
# _resolve_chrome_bin, wedge detection, focus-safe launch.
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness-common.sh"

BROWSER_INSTRUCTIONS=$(cat <<'BROWSER_HARNESS_EOF'
BROWSER BACKEND: reddit-harness (browser-harness MCP, CDP-driven REAL Google Chrome on
port 9557, profile ~/.claude/browser-profiles/reddit-harness). The Chrome is already
logged in to Reddit; cookies persist on disk.

You have ONE tool: mcp__reddit-harness__bh_run(script). It runs arbitrary Python with
these helpers pre-imported:
  new_tab(url), goto_url(url), wait_for_load(), page_info(),
  capture_screenshot(),                     # returns path to PNG; Read it to see the page
  click_at_xy(x, y),                        # coordinate click (viewport pixels)
  js(expression),                           # page.evaluate-style; returns the result
  type_text(text),                          # types into currently-focused element
  press_key(key),                           # e.g. "Enter", "Tab", "Escape"
  scroll(direction, amount), cdp(method, **params)

TAB HYGIENE (IMPORTANT): Reuse the SAME tab for sequential same-domain navigation.
Use new_tab() ONLY for the very first navigation OR when you need to keep an old tab
open in parallel. For each subsequent query / page / scan, use goto_url() so the
existing tab is reused. Opening a fresh tab for every query leaks tabs over time and
exhausts per-process Chrome resources.

REDDIT JSON FETCH (the whole point of this backend): Reddit 403s urllib/curl on
*.json from this IP, but same-origin fetch() from inside a logged-in reddit.com page
returns 200. To read any Reddit JSON endpoint:
  bh_run('''
  goto_url("https://www.reddit.com/")
  wait_for_load()
  body = js("""
    return (async () => {
      const r = await fetch("https://www.reddit.com/search.json?q=...&limit=25",
                            {credentials:"include", headers:{"Accept":"application/json"}});
      return JSON.stringify({status:r.status, body: await r.text()});
    })();
  """)
  print(body)
  ''')

TRANSLATION TABLE - wherever this prompt mentions a Playwright-style tool, do the
following with bh_run instead:

  browser_navigate(url)           ->  First navigation: bh_run('new_tab("URL"); wait_for_load()')
                                       Subsequent navigations (same session): bh_run('goto_url("URL"); wait_for_load()')
  browser_snapshot                ->  bh_run('print(js("""..."""))') to read DOM as structured data,
                                       OR bh_run('print(capture_screenshot())') + Read the PNG
  browser_run_code(js)            ->  bh_run('print(js("""<the JS expression>"""))')
  browser_click(ref=...)          ->  Find the element via selector, compute center coords from
                                       getBoundingClientRect, then bh_run('click_at_xy(X, Y)')
  browser_type(ref=..., text=...) ->  Click the textbox first (click_at_xy), then bh_run('type_text("TEXT")')
  browser_take_screenshot         ->  bh_run('print(capture_screenshot())') then Read the path
  browser_press_key("Enter")      ->  bh_run('press_key("Enter")')

VERIFY AFTER EVERY MUTATION by capturing a screenshot and reading the PNG, coordinate
clicks can miss; visual verification is the only reliable confirmation that the action took.
BROWSER_HARNESS_EOF
)

cleanup_harness_tabs() {
    hc_cleanup_tabs 9557 reddit-harness
}

_reddit_defer_if_posting() {
    # Posting-active defer (2026-07-14, mirrors twitter's posting-active flag):
    # a poster mid-drain owns the ONE shared harness tab; a pipeline that
    # grabs it mid-post navigates the tab out from under the poster and the
    # comment-form check false-positives as account_blocked_in_sub. When the
    # flag is fresh (heartbeat < 120s; post_reddit.py re-stamps per row), skip
    # this fire via the reserved code 78 — the launchd job re-fires on its
    # next cadence. A stale flag never blocks (a killed poster must not wedge
    # every reddit pipeline).
    local _f="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}/reddit-posting-active.json"
    [ -f "$_f" ] || return 0
    local _age
    _age=$(( $(date +%s) - $(stat -f %m "$_f" 2>/dev/null || stat -c %Y "$_f" 2>/dev/null || echo 0) ))
    if [ "$_age" -lt 120 ]; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] reddit posting-active (age ${_age}s); skipping this fire (rc=78) so the poster keeps the tab" >&2
        return 78
    fi
    return 0
}

ensure_reddit_browser_for_backend() {
    HC_PLATFORM=reddit \
    HC_PORT=9557 \
    HC_PROFILE_DIR="$HOME/.claude/browser-profiles/reddit-harness" \
    HC_DEFAULT_URL="$_BH_REDDIT_DEFAULT_URL" \
    HC_CDP_URL="${REDDIT_CDP_URL:-$_BH_REDDIT_DEFAULT_URL}" \
    HC_LAUNCH_URL="about:blank" \
    HC_WINDOW_POS="${BH_REDDIT_WINDOW_POS:-2131,-1032}" \
    HC_WINDOW_SIZE="${BH_REDDIT_WINDOW_SIZE:-911,1016}" \
    HC_PRE_LAUNCH_HOOK=_reddit_defer_if_posting \
    HC_EXTRA_FLAGS="${BH_REDDIT_PROXY:+--proxy-server=$BH_REDDIT_PROXY}" \
    hc_ensure_browser
}

defer_if_foreign_for_backend() {
    hc_defer_if_foreign
}
