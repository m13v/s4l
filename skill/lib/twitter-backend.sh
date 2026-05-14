#!/bin/bash
# twitter-backend.sh - Backend selector for the Twitter pipeline (2026-05-13).
#
# Source this AFTER lock.sh, BEFORE any acquire_lock / browser pre-flight /
# claude -p subprocess calls. Sets these for the caller:
#
#   TWITTER_BACKEND        - "agent" (default) or "harness"
#   MCP_CONFIG_FILE        - claude -p --mcp-config path for the chosen backend
#   BROWSER_INSTRUCTIONS   - prompt block describing the backend + tool translation
#                            table (inject at the TOP of any prompt that mentions
#                            browser_* tools)
#
# And exports (so Python subprocesses like twitter_browser.py inherit them):
#
#   TWITTER_CDP_URL        - http://127.0.0.1:9555 (harness only; unset for agent)
#
# Provides these functions:
#
#   ensure_twitter_browser_for_backend
#     Call AFTER acquire_lock "twitter-browser". For agent: cleans singleton
#     symlinks + ensure_browser_healthy. For harness: probes Chrome on port
#     9555, launches it (idempotently) if down.
#
#   defer_if_foreign_for_backend [log_file]
#     Returns 0 (defer) only when TWITTER_BACKEND=agent and a foreign
#     twitter-agent MCP wrapper has a live Chrome under it. Harness CDP
#     supports multiple concurrent clients on the same Chrome (no SingletonLock
#     fight), so the harness path never defers.

TWITTER_BACKEND="${TWITTER_BACKEND:-agent}"

case "$TWITTER_BACKEND" in
    agent)
        MCP_CONFIG_FILE="$HOME/.claude/browser-agent-configs/twitter-agent-mcp.json"
        unset TWITTER_CDP_URL
        BROWSER_INSTRUCTIONS=$(cat <<'BROWSER_AGENT_EOF'
BROWSER BACKEND: twitter-agent (Playwright MCP, headed Chromium at ~/.claude/browser-profiles/twitter).
Tools available: mcp__twitter-agent__browser_navigate, browser_snapshot, browser_run_code,
browser_click, browser_type, browser_take_screenshot, browser_wait_for, browser_press_key,
browser_resize, browser_console_messages, browser_network_requests. The MCP holds the
browser open across calls; tool calls are session-stateful.
BROWSER_AGENT_EOF
        )
        ;;
    harness)
        MCP_CONFIG_FILE="$HOME/.claude/browser-agent-configs/twitter-harness-mcp.json"
        # Tell twitter_browser.py (and any other Python helper that honors
        # this env var) to skip ps-based discovery and connect directly to
        # the harness Chrome on port 9555.
        export TWITTER_CDP_URL="http://127.0.0.1:9555"
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

TRANSLATION TABLE — wherever this prompt mentions a Playwright-style tool, do the
following with bh_run instead:

  browser_navigate(url)           ->  bh_run('new_tab("URL")') or bh_run('goto_url("URL"); wait_for_load()')
  browser_snapshot                ->  bh_run('print(js("""..."""))') to read DOM as structured data,
                                       OR bh_run('print(capture_screenshot())') + Read the PNG
  browser_run_code(js)            ->  bh_run('print(js("""<the JS expression>"""))')
  browser_click(ref=...)          ->  Find the element via selector, compute center coords from
                                       getBoundingClientRect, then bh_run('click_at_xy(X, Y)')
  browser_type(ref=..., text=...) ->  Click the textbox first (click_at_xy), then bh_run('type_text("TEXT")')
  browser_take_screenshot         ->  bh_run('print(capture_screenshot())') then Read the path
  browser_press_key("Enter")      ->  bh_run('press_key("Enter")')

EXAMPLE — click the reply submit button:
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

VERIFY AFTER EVERY MUTATION by capturing a screenshot and reading the PNG — coordinate
clicks can miss; visual verification is the only reliable confirmation that the action took.
BROWSER_HARNESS_EOF
        )
        ;;
    *)
        echo "ERROR: unknown TWITTER_BACKEND='$TWITTER_BACKEND' (expected: agent, harness)" >&2
        exit 2
        ;;
esac

ensure_twitter_browser_for_backend() {
    if [ "$TWITTER_BACKEND" = "agent" ]; then
        bash "$HOME/social-autoposter/scripts/clean_stale_singleton.sh" "$HOME/.claude/browser-profiles/twitter" 2>&1 || true
        ensure_browser_healthy "twitter"
    else
        # harness path: probe + launch Chrome on port 9555 if needed.
        if ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9555/json/version 2>/dev/null; then
            echo "[$(date +%H:%M:%S)] Harness Chrome down on port 9555 — launching..." >&2
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
                --remote-debugging-port=9555 \
                --user-data-dir="$HOME/.claude/browser-profiles/browser-harness" \
                --no-first-run --no-default-browser-check \
                --disable-features=ChromeWhatsNewUI \
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
    fi
}

defer_if_foreign_for_backend() {
    local log_file="${1:-}"
    if [ "$TWITTER_BACKEND" = "agent" ]; then
        defer_if_foreign_browser_mcp_active "twitter" "$log_file"
    else
        return 1  # harness never defers on foreign MCP processes
    fi
}
