#!/bin/bash
# twitter-backend.sh - Twitter pipeline browser bootstrap (harness-only since
# 2026-05-19; the legacy twitter-agent Playwright MCP path was fully ripped
# out; the shared machinery lives in skill/lib/harness-common.sh since
# 2026-07-14).
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
#     port 9555 and launches it idempotently if down (wedge-aware, two-strike
#     tolerant, focus-safe; see harness-common.sh), restores the X session if
#     logged out, then cleans leftover tabs from prior runs.
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

# Default harness URL - used to decide whether we own this Chrome (and should
# launch/clean it) or whether it is externally managed (AppMaker, BYO).
_BH_DEFAULT_URL="http://127.0.0.1:9555"

# Shared engine: _BH_REPO_DIR, hc_ensure_browser, hc_cleanup_tabs,
# _resolve_chrome_bin, wedge detection, focus-safe launch.
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness-common.sh"

# DEPRECATED 2026-06-26: this block is NO LONGER injected into any model prompt.
# run-twitter-cycle.sh now sets TW_ENGINE_PREFIX="" — Phase 1 (query) and Phase 2b
# (prep) are tool-free; the model drafts from inlined candidate context only, and all
# browser work is the shell's deterministic CDP scan + Phase 2b-post's
# twitter_browser.py. Kept only so ensure_twitter_browser_for_backend's existing
# assignment doesn't break; safe to delete once confirmed unreferenced. Do NOT
# reintroduce the "logged in as m13v_" hardcode or a model-facing bh_run contract.
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
    hc_cleanup_tabs 9555 twitter-harness
}

# Back-compat alias: older sourced contexts referenced _bh_cdp_ready directly.
_bh_cdp_ready() {
    hc_cdp_ready "${1:-$_BH_DEFAULT_URL}"
}

_tw_restore_session() {
    # Re-inject the stored X session if the harness Chrome is logged out - e.g.
    # a keychain re-lock wiped Chrome's encrypted Cookies SQLite on this launch
    # (Gap B, 2026-06-02), or an AppMaker Hobby-tier sandbox substitution
    # reseeded /root and wiped the profile. restore_twitter_session.py reads
    # the keychain-independent local cookie mirror (written by connect_x) and
    # injects via CDP. No-op when already logged in; never blocks the cycle on
    # failure. Runs on both the freshly-launched and already-up paths so a
    # mid-life logout heals.
    python3 "$_BH_REPO_DIR/scripts/restore_twitter_session.py" 2>&1 \
        | sed 's/^/[restore] /' >&2 || true
}

ensure_twitter_browser_for_backend() {
    # --password-store=basic + --use-mock-keychain: encrypt the cookie store
    # with Chrome's fixed obfuscation key instead of the macOS Keychain
    # ("Chrome Safe Storage"). Without this, a keychain lock/re-lock leaves
    # Chrome unable to decrypt its Cookies SQLite on the next launch, so it
    # discards the session and the harness comes up logged out. (Root-cause
    # persistence fix, 2026-06-02; _tw_restore_session remains the safety net.)
    #
    # HC_HEALTH_FILE keeps the legacy cdp-health.json name that
    # memory_snapshot.py reads; HC_WEDGE_MARKER resolves to twitter_cdp_wedge,
    # the stderr marker bin/server.js greps. Do not rename either.
    HC_PLATFORM=twitter \
    HC_PORT=9555 \
    HC_PROFILE_DIR="$HOME/.claude/browser-profiles/browser-harness" \
    HC_DEFAULT_URL="$_BH_DEFAULT_URL" \
    HC_CDP_URL="${TWITTER_CDP_URL:-$_BH_DEFAULT_URL}" \
    HC_LAUNCH_URL="${BH_LAUNCH_URL:-https://x.com}" \
    HC_WINDOW_POS="${BH_WINDOW_POS:-3042,-1032}" \
    HC_WINDOW_SIZE="${BH_WINDOW_SIZE:-1024,1013}" \
    HC_EXTRA_FLAGS="--password-store=basic --use-mock-keychain" \
    HC_HEALTH_FILE="$_BH_REPO_DIR/skill/logs/cdp-health.json" \
    HC_POST_LAUNCH_HOOK=_tw_restore_session \
    HC_EXTERNAL_OK_HOOK=_tw_restore_session \
    hc_ensure_browser
}

defer_if_foreign_for_backend() {
    hc_defer_if_foreign
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
    if declare -F log >/dev/null 2>&1; then log "$*"; else echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; fi
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
