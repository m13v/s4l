#!/bin/bash
# twitter-backend.sh - Twitter pipeline browser bootstrap (harness-only since
# 2026-05-19; the legacy twitter-agent Playwright MCP path was fully ripped out).
#
# Source this AFTER lock.sh, BEFORE any acquire_lock / browser pre-flight /
# claude -p subprocess calls. Sets these for the caller:
#
#   MCP_CONFIG_FILE        - claude -p --mcp-config path (twitter-harness MCP)
#   BROWSER_INSTRUCTIONS   - prompt block describing the harness backend +
#                            its bh_run tool surface (inject at the TOP of any
#                            prompt that mentions browser_* tools)
#
# And exports (so Python subprocesses like twitter_browser.py inherit them):
#
#   TWITTER_CDP_URL        - http://127.0.0.1:9555 (forces direct CDP attach,
#                            skipping ps-based agent-profile discovery)
#
# Provides these functions (names preserved for back-compat with existing
# callers in engage-twitter.sh, run-twitter-cycle.sh, run-twitter-threads.sh,
# dm-outreach-twitter.sh, scan-twitter-followups.sh):
#
#   ensure_twitter_browser_for_backend
#     Call AFTER acquire_lock "twitter-browser". Probes harness Chrome on
#     port 9555 and launches it idempotently if down, then cleans leftover
#     tabs from prior runs.
#
#   defer_if_foreign_for_backend [log_file]
#     No-op. Harness CDP supports multiple concurrent clients on the same
#     Chrome (no SingletonLock fight), so foreign MCP wrappers never block
#     us. Kept as a function only so callers don't have to change.

MCP_CONFIG_FILE="$HOME/.claude/browser-agent-configs/twitter-harness-mcp.json"

# Per-host env override (written by bin/cli.js when installing on an AppMaker
# VM, where the canonical browser is Chromium on port 9222 behind the SOAX
# residential proxy at 127.0.0.1:3003, NOT the harness Chrome on 9555). On a
# Mac dev box this file does not exist, so the default below kicks in.
if [ -f "$HOME/.social-autoposter-env" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.social-autoposter-env"
fi

# Tell twitter_browser.py (and any other Python helper that honors this env
# var) to skip ps-based discovery and connect directly to the configured CDP
# endpoint. Default 9555 (Mac harness Chrome). AppMaker VMs pre-set this to
# http://127.0.0.1:9222 via ~/.social-autoposter-env above.
export TWITTER_CDP_URL="${TWITTER_CDP_URL:-http://127.0.0.1:9555}"

# Default harness URL — used by ensure_twitter_browser_for_backend +
# cleanup_harness_tabs to decide whether we own this Chrome (and should
# launch/clean it) or whether it is externally managed (AppMaker, BYO).
_BH_DEFAULT_URL="http://127.0.0.1:9555"
BROWSER_INSTRUCTIONS=$(cat <<'BROWSER_HARNESS_EOF'
BROWSER BACKEND: twitter-harness (browser-harness MCP, CDP-driven REAL Google Chrome on
port 9555, profile ~/.claude/browser-profiles/browser-harness). The Chrome is already
logged in as m13v_; cookies persist on disk.

You have ONE tool: mcp__twitter-harness__bh_run(script). It runs arbitrary Python with
these helpers pre-imported:
  new_tab(url), goto_url(url), wait_for_load(), page_info(),
  capture_screenshot(),                     # returns path to PNG; Read it to see the page
  click_at_xy(x, y),                        # coordinate click (viewport pixels)
  js(expression),                           # page.evaluate-style; returns the result
  type_text(text),                          # types into currently-focused element
  press_key(key),                           # e.g. "Enter", "Tab", "Escape"
  scroll(direction, amount), cdp(method, **params)

TAB HYGIENE (IMPORTANT): A placeholder tab ALWAYS already exists when you start
(pre-flight leaves exactly one tab open). REUSE IT: use goto_url() for your VERY FIRST
navigation as well as every subsequent one, so the existing tab is navigated in place.
Call new_tab() ONLY as a fallback when no usable tab exists (goto_url errors because
there is no active page) OR when you genuinely need a second tab open in parallel.
Opening a fresh tab on first navigation orphans the placeholder and leaks a tab every
cycle, which exhausts per-process Chrome resources.

TRANSLATION TABLE - wherever this prompt mentions a Playwright-style tool, do the
following with bh_run instead:

  browser_navigate(url)           ->  Reuse the existing tab (default, incl. first nav):
                                       bh_run('goto_url("URL"); wait_for_load()')
                                       Fallback only if no tab exists / parallel tab needed:
                                       bh_run('new_tab("URL"); wait_for_load()')
  browser_snapshot                ->  bh_run('print(js("""..."""))') to read DOM as structured data,
                                       OR bh_run('print(capture_screenshot())') + Read the PNG
  browser_run_code(js)            ->  bh_run('print(js("""<the JS expression>"""))')
  browser_click(ref=...)          ->  Find the element via selector, compute center coords from
                                       getBoundingClientRect, then bh_run('click_at_xy(X, Y)')
  browser_type(ref=..., text=...) ->  Click the textbox first (click_at_xy), then bh_run('type_text("TEXT")')
  browser_take_screenshot         ->  bh_run('print(capture_screenshot())') then Read the path
  browser_press_key("Enter")      ->  bh_run('press_key("Enter")')

EXAMPLE - click the reply submit button:
  bh_run('''
  pt = js("""
    const el = document.querySelector('[data-testid="tweetButtonInline"]');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {x: r.x + r.width/2, y: r.y + r.height/2};
  """)
  print(pt)
  ''')
  # Then in a follow-up call (substituting the x/y from above):
  bh_run('click_at_xy(123, 456)')

VERIFY AFTER EVERY MUTATION by capturing a screenshot and reading the PNG, coordinate
clicks can miss; visual verification is the only reliable confirmation that the action took.
BROWSER_HARNESS_EOF
)

cleanup_harness_tabs() {
    # Close every CDP "page" tab except one. Delegated to a standalone Python
    # script because bash 3.2 (what launchd uses) cannot parse a nested heredoc
    # inside a function body inside a sourced file. Inline form here broke every
    # launchd-fired twitter script on 2026-05-14 until this refactor.
    #
    # Health-check gate: 2026-05-16 the original `--max-time 2` was too strict.
    # When harness Chrome is busy (long scans, lock backups, CPU-pinned),
    # the /json/version probe times out, cleanup is silently skipped, and the
    # next scan's new_tab() leaks an orphan tab. Symptom: occasional
    # "closed 14/14 extra page tabs" cycles after several skips piled up.
    # Now: 10s timeout + ONE retry; log skips so they are not silent.
    local _probe="curl -sf --max-time 10 -o /dev/null http://127.0.0.1:9555/json/version"
    if ! $_probe 2>/dev/null; then
        sleep 1
        if ! $_probe 2>/dev/null; then
            echo "[$(date +%H:%M:%S)] cleanup_harness_tabs: SKIPPED (harness CDP /json/version unreachable after 10s+retry)" >&2
            return 0
        fi
    fi
    python3 "$HOME/social-autoposter/scripts/cleanup_harness_tabs.py" 2>/dev/null || true
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

ensure_twitter_browser_for_backend() {
    # AppMaker / BYO Chrome: TWITTER_CDP_URL points at something other than our
    # default harness URL. Don't touch that browser; just probe it and bail.
    # The AppMaker bootstrap (and any future BYO setup) is responsible for
    # keeping the externally-managed Chrome alive.
    if [ "${TWITTER_CDP_URL:-$_BH_DEFAULT_URL}" != "$_BH_DEFAULT_URL" ]; then
        local _ext_url="${TWITTER_CDP_URL}"
        if curl -sf --max-time 2 -o /dev/null "${_ext_url}/json/version" 2>/dev/null; then
            echo "[$(date +%H:%M:%S)] Using externally-managed Chrome at ${_ext_url} (skipping harness launch + tab cleanup)" >&2
            # Restore the Twitter login if the sandbox was substituted. AppMaker
            # Hobby-tier sandboxes have a 1h TTL; on substitution /root is reseeded
            # from /etc/skel-root and the harness profile (cookies) is wiped. This
            # re-injects the stored session from social_accounts via the HTTP API.
            # No-op when already logged in. Never blocks the cycle on failure.
            python3 "$HOME/social-autoposter/scripts/restore_twitter_session.py" 2>&1 | sed 's/^/[restore] /' >&2 || true
            return 0
        fi
        echo "[$(date +%H:%M:%S)] ERROR: TWITTER_CDP_URL=${_ext_url} not reachable. External Chrome must be managed by host (AppMaker /opt/startup.sh, etc)." >&2
        return 1
    fi
    # Probe + launch harness Chrome on port 9555 if needed.
    if ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9555/json/version 2>/dev/null; then
        echo "[$(date +%H:%M:%S)] Harness Chrome down on port 9555, launching..." >&2
        local _chrome_bin
        _chrome_bin=$(_resolve_chrome_bin)
        if [ -z "$_chrome_bin" ]; then
            echo "[$(date +%H:%M:%S)] ERROR: no Chrome/Chromium binary found. Set BH_CHROME_BIN." >&2
            return 1
        fi
        # On Linux + no display, run headless. On root, add --no-sandbox.
        # Window-position/size only meaningful on macOS multi-monitor; skip
        # elsewhere so we don't hide the window off-screen on single-display
        # Linux VMs.
        local _extra=()
        case "$(uname -s)" in
            Linux)
                _extra+=(--no-sandbox --disable-dev-shm-usage)
                if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
                    _extra+=(--headless=new --disable-gpu)
                fi
                ;;
            Darwin)
                _extra+=(--window-position="${BH_WINDOW_POS:-3042,-1032}")
                _extra+=(--window-size="${BH_WINDOW_SIZE:-1024,1013}")
                ;;
        esac
        # --password-store=basic + --use-mock-keychain: encrypt the cookie store
        # with Chrome's fixed obfuscation key instead of the macOS Keychain
        # ("Chrome Safe Storage"). Without this, a keychain lock/re-lock leaves
        # Chrome unable to decrypt its Cookies SQLite on the next launch, so it
        # discards the session and the harness comes up logged out. With it, the
        # x.com cookies persist + decrypt across restarts natively, no
        # re-injection needed. Matches the flags the Playwright browser agents
        # already use. (Root-cause persistence fix, 2026-06-02; the cookie
        # mirror + restore_twitter_session.py remain as the safety net.)
        "$_chrome_bin" \
            --remote-debugging-port=9555 \
            --user-data-dir="$HOME/.claude/browser-profiles/browser-harness" \
            --no-first-run --no-default-browser-check \
            --password-store=basic --use-mock-keychain \
            --disable-features=ChromeWhatsNewUI \
            "${_extra[@]}" \
            about:blank >/dev/null 2>&1 &
        disown
        for _i in 1 2 3 4 5 6 7 8 9 10 11 12; do
            curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9555/json/version 2>/dev/null && break
            sleep 1
        done
        if ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9555/json/version 2>/dev/null; then
            echo "[$(date +%H:%M:%S)] ERROR: harness Chrome failed to start within 12s" >&2
            return 1
        fi
        echo "[$(date +%H:%M:%S)] Harness Chrome up on port 9555" >&2
    fi
    # Re-inject the stored X session if the harness Chrome is logged out — e.g. a
    # keychain re-lock wiped Chrome's encrypted Cookies SQLite on this launch
    # (Gap B, 2026-06-02). restore_twitter_session.py reads the keychain-
    # independent local cookie mirror (written by connect_x) and injects via CDP.
    # No-op when already logged in; never blocks the cycle on failure. Runs on
    # both the freshly-launched and already-up paths so a mid-life logout heals.
    TWITTER_CDP_URL="http://127.0.0.1:9555" \
        python3 "$HOME/social-autoposter/scripts/restore_twitter_session.py" 2>&1 \
        | sed 's/^/[restore] /' >&2 || true
    # Always close leftover tabs from prior runs. Safe under acquire_lock
    # "twitter-browser" serialization (every caller of this function holds
    # that lock), so we will not race with another active twitter run.
    cleanup_harness_tabs
}

defer_if_foreign_for_backend() {
    # Harness Chrome accepts multiple concurrent CDP clients on the same
    # browser-harness profile, so a foreign MCP wrapper (Fazm Dev / IDE)
    # cannot cause the SingletonLock contention that historically blocked
    # the twitter-agent profile. Always return 1 (do not defer).
    return 1
}

# --- browser-harness `-c` capability self-heal (added 2026-06-02) -----------
# A stale ~/Developer/browser-harness checkout that PREDATES the `-c` interface
# makes `browser-harness -c "<script>"` print its usage string instead of
# running the script. The Phase 1 scan loop in run-twitter-cycle.sh then yields
# zero tweets with no obvious cause. cli.js documents the same failure for the
# bh_run MCP path. When this bit the testing machine, the debugging agent saw
# the `-c` flag, WRONGLY assumed it was unsupported, and proposed rewriting the
# call to a nonexistent "stdin form" (browser-harness has no stdin mode — `-c`
# is the only interface; see run.py). This runs at source-time, before any
# `-c` call, so all twitter harness scripts (cycle/threads/engage/dm/followups)
# get auto-repair. Static probe is one grep when fresh (zero steady-state cost);
# the git+uv refresh only fires when the checkout is actually stale.
_sa_harness_log() {
    # Use the caller's log() FUNCTION when present; `declare -F` matches only a
    # shell function, never the macOS /usr/bin/log binary (command -v would).
    if declare -F log >/dev/null 2>&1; then log "$*"; else echo "[$(date +%H:%M:%S)] $*" >&2; fi
}
_sa_resolve_uv() {
    local c
    c="$(command -v uv 2>/dev/null)" && { echo "$c"; return 0; }
    for c in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
        [ -x "$c" ] && { echo "$c"; return 0; }
    done
    return 1
}
ensure_harness_c_support() {
    # Retired 2026-06-02. Upstream browser-harness removed `-c` in favor of
    # stdin-heredoc (commits after merge-base 0e679e2); our server.py wrapper
    # now passes scripts via stdin (input=script) so the CLI shape doesn't
    # need any pre-flight probing. The old gate grepped run.py for `"-c"`
    # which always fails against current upstream, and its "self-heal" was a
    # `git reset --hard FETCH_HEAD` on ~/Developer/browser-harness that
    # would clobber local commits AND not actually re-add `-c`. Keep the
    # name + no-op return so older sourced contexts that call it don't break.
    return 0
}
ensure_harness_c_support || true
