#!/bin/bash
# reddit-backend.sh - Reddit pipeline browser bootstrap (reddit-harness,
# mirrors twitter-backend.sh / linkedin-backend.sh).
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
#     port 9557 and launches it idempotently if down, then cleans leftover
#     tabs from prior runs.
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

# Default harness URL - used by ensure_reddit_browser_for_backend +
# cleanup_harness_tabs to decide whether we own this Chrome (and should
# launch/clean it) or whether it is externally managed (AppMaker, BYO).
_BH_REDDIT_DEFAULT_URL="http://127.0.0.1:9557"

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
    # Close every CDP "page" tab except one. Same pattern as twitter/linkedin
    # backend, scoped to the Reddit harness Chrome on port 9557.
    #
    # Health-check gate: 10s timeout + ONE retry; log skips so they are not silent.
    local _probe="curl -sf --max-time 10 -o /dev/null http://127.0.0.1:9557/json/version"
    if ! $_probe 2>/dev/null; then
        sleep 1
        if ! $_probe 2>/dev/null; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cleanup_harness_tabs: SKIPPED (reddit-harness CDP /json/version unreachable after 10s+retry)" >&2
            return 0
        fi
    fi
    BH_CLEANUP_PORT=9557 python3 "$HOME/social-autoposter/scripts/cleanup_harness_tabs.py" 2>/dev/null || true
}

_resolve_chrome_bin() {
    # Auto-detect Chrome/Chromium so the same script launches the harness on
    # macOS dev boxes AND Linux VMs. Override with BH_CHROME_BIN.
    if [ -n "${BH_CHROME_BIN:-}" ] && [ -x "$BH_CHROME_BIN" ]; then
        echo "$BH_CHROME_BIN"; return 0
    fi
    for _p in \
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        "/Applications/Chromium.app/Contents/MacOS/Chromium" \
        "/usr/bin/google-chrome" "/usr/bin/google-chrome-stable" \
        "/usr/bin/chromium" "/usr/bin/chromium-browser" "/snap/bin/chromium"
    do
        if [ -x "$_p" ]; then echo "$_p"; return 0; fi
    done
    for _n in google-chrome google-chrome-stable chromium chromium-browser; do
        _which=$(command -v "$_n" 2>/dev/null) && [ -n "$_which" ] && { echo "$_which"; return 0; }
    done
    echo ""; return 1
}

ensure_reddit_browser_for_backend() {
    # AppMaker / BYO Chrome: REDDIT_CDP_URL points at something other than our
    # default harness URL. Don't touch that browser; just probe it and bail.
    if [ "${REDDIT_CDP_URL:-$_BH_REDDIT_DEFAULT_URL}" != "$_BH_REDDIT_DEFAULT_URL" ]; then
        local _ext_url="${REDDIT_CDP_URL}"
        if curl -sf --max-time 2 -o /dev/null "${_ext_url}/json/version" 2>/dev/null; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Using externally-managed Chrome at ${_ext_url} (skipping harness launch + tab cleanup)" >&2
            return 0
        fi
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: REDDIT_CDP_URL=${_ext_url} not reachable. External Chrome must be managed by host." >&2
        return 1
    fi
    # Probe + launch harness Chrome on port 9557 if needed.
    if ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9557/json/version 2>/dev/null; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Reddit harness Chrome down on port 9557, launching..." >&2
        local _chrome_bin
        _chrome_bin=$(_resolve_chrome_bin)
        if [ -z "$_chrome_bin" ]; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: no Chrome/Chromium binary found. Set BH_CHROME_BIN." >&2
            return 1
        fi
        # On Linux + no display, run headless. On root, add --no-sandbox.
        local _extra=()
        case "$(uname -s)" in
            Linux)
                _extra+=(--no-sandbox --disable-dev-shm-usage)
                if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
                    _extra+=(--headless=new --disable-gpu)
                fi
                ;;
            Darwin)
                # Default position = the Reddit browser's current off-screen
                # spot (captured 2026-05-29); overridable via BH_REDDIT_WINDOW_POS.
                _extra+=(--window-position="${BH_REDDIT_WINDOW_POS:-2131,-1032}")
                _extra+=(--window-size="${BH_REDDIT_WINDOW_SIZE:-911,1016}")
                ;;
        esac
        # Self-heal (2026-06-03): reap any stale Chrome holding THIS profile dir
        # but not answering CDP on our port, else the relaunch hands off via the
        # SingletonLock and loops "failed to start within 12s". Exact-dir match
        # (trailing space) keeps this scoped to reddit-harness only. See
        # twitter-backend.sh for the regression that motivated this.
        local _prof_dir="$HOME/.claude/browser-profiles/reddit-harness"
        local _stale_pids
        _stale_pids=$(pgrep -f -- "--user-data-dir=$_prof_dir " 2>/dev/null || true)
        if [ -n "$_stale_pids" ] && ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9557/json/version 2>/dev/null; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CDP down but Chrome still holds $_prof_dir (pids: $(echo $_stale_pids | tr '\n' ' ')); reaping stale profile owner before relaunch" >&2
            kill $_stale_pids 2>/dev/null || true
            sleep 2
            _stale_pids=$(pgrep -f -- "--user-data-dir=$_prof_dir " 2>/dev/null || true)
            [ -n "$_stale_pids" ] && { kill -9 $_stale_pids 2>/dev/null || true; sleep 1; }
            rm -f "$_prof_dir/SingletonLock" "$_prof_dir/SingletonSocket" "$_prof_dir/SingletonCookie" 2>/dev/null || true
        fi
        # Spawn via the SHARED launcher (skill/lib/browser-launch.sh): clean-
        # exit stamp (no Aw-Snap restore), `open -n -g` on macOS (no focus
        # steal, out of this job's pgroup), setsid exec fallback elsewhere.
        # This backend previously used a bare exec and had NONE of those
        # protections (2026-07-13 consolidation).
        # shellcheck disable=SC1091
        source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/browser-launch.sh"
        launch_harness_chrome "$_chrome_bin" "$_prof_dir" \
            --remote-debugging-port=9557 \
            --user-data-dir="$HOME/.claude/browser-profiles/reddit-harness" \
            --no-first-run --no-default-browser-check \
            --disable-features=ChromeWhatsNewUI \
            "${_extra[@]}" \
            about:blank
        for _i in 1 2 3 4 5 6 7 8 9 10 11 12; do
            curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9557/json/version 2>/dev/null && break
            sleep 1
        done
        if ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9557/json/version 2>/dev/null; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: Reddit harness Chrome failed to start within 12s" >&2
            return 1
        fi
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Reddit harness Chrome up on port 9557" >&2
    fi
    # Always close leftover tabs from prior runs. Safe under acquire_lock
    # "reddit-browser" serialization.
    cleanup_harness_tabs
}

defer_if_foreign_for_backend() {
    # Harness Chrome accepts multiple concurrent CDP clients on the same
    # reddit-harness profile, so a foreign MCP wrapper cannot cause
    # SingletonLock contention. Always return 1 (do not defer).
    return 1
}
