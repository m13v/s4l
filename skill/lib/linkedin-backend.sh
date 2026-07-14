#!/bin/bash
# linkedin-backend.sh - LinkedIn pipeline browser bootstrap (linkedin-harness,
# mirrors twitter-backend.sh post the 2026-05-19 Twitter harness migration).
#
# Source this AFTER lock.sh, BEFORE any acquire_lock / browser pre-flight /
# claude -p subprocess calls. Sets these for the caller:
#
#   MCP_CONFIG_FILE        - claude -p --mcp-config path (linkedin-harness MCP)
#   BROWSER_INSTRUCTIONS   - prompt block describing the harness backend +
#                            its bh_run tool surface (inject at the TOP of any
#                            prompt that mentions browser_* tools)
#
# And exports (so Python subprocesses like linkedin_browser.py inherit them):
#
#   LINKEDIN_CDP_URL       - http://127.0.0.1:9556 (forces direct CDP attach,
#                            skipping ps-based agent-profile discovery)
#
# Provides these functions (names mirror twitter-backend for back-compat with
# the existing call shape used in run-linkedin.sh, stats-linkedin.sh,
# scan-linkedin-mentions.sh, dm-outreach-linkedin.sh, etc.):
#
#   ensure_linkedin_browser_for_backend
#     Call AFTER acquire_lock "linkedin-browser". Probes harness Chrome on
#     port 9556 and launches it idempotently if down, then cleans leftover
#     tabs from prior runs.
#
#   defer_if_foreign_for_backend [log_file]
#     No-op. Harness CDP supports multiple concurrent clients on the same
#     Chrome (no SingletonLock fight), so foreign MCP wrappers never block
#     us. Kept as a function only so callers don't have to change.
#
# IMPORTANT — LinkedIn anti-bot considerations (per CLAUDE.md):
# The 2026-04-17 ban was caused by Voyager API calls + permalink scrape loops
# (behavioral fingerprinting), NOT by the CDP-attach mechanism itself. The
# existing discover_linkedin_candidates.py and scrape_linkedin_comment_stats.py
# already CDP-attach without triggering bans, so the harness substrate is safe.
# What MUST stay forbidden inside any bh_run script targeting LinkedIn:
#   - /voyager/api/* calls (Python, fetch(), page.evaluate())
#   - Loops that open each post permalink to scrape reactions/comments
#   - scrollBy combined with "Show more comments" / "Load earlier replies" clicks
#   - Programmatic login flows (passive checks only; on checkpoint return early)

MCP_CONFIG_FILE="$HOME/.claude/browser-agent-configs/linkedin-harness-mcp.json"

# Per-host env override (written by bin/cli.js when installing on an AppMaker
# VM). On a Mac dev box this file does not exist, so the default below kicks in.
if [ -f "$HOME/.social-autoposter-env" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.social-autoposter-env"
fi

# Tell linkedin_browser.py (and any other Python helper that honors this env
# var) to skip ps-based discovery and connect directly to the configured CDP
# endpoint. Default 9556 (Mac harness Chrome, separate port from Twitter's 9555).
export LINKEDIN_CDP_URL="${LINKEDIN_CDP_URL:-http://127.0.0.1:9556}"

# Repo root for helper scripts. Honors S4L_REPO_DIR (managed installs run from
# ~/.social-autoposter-mcp/repo/package, NOT ~/social-autoposter); a $HOME
# hardcode here silently no-ops on customer boxes (same bug twitter-backend.sh
# fixed; see its header comment).
_BH_REPO_DIR="${S4L_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

# Resolve a Playwright-capable Python for the browser-path SERP search
# (discover_linkedin_candidates.py CDP-attaches to the harness Chrome via
# playwright.sync_api). The agent's bare `python3` resolves to whatever is
# first on PATH, which on this Mac is /opt/homebrew/bin/python3 (3.14) where
# Playwright is NOT installed -> ModuleNotFoundError. Playwright lives under
# /opt/homebrew/bin/python3.11 and /usr/bin/python3 (3.9). Pick the first
# interpreter that can actually import playwright.sync_api and export it so the
# Phase A browser prompt can shell out via "$LINKEDIN_DISCOVER_PYTHON" instead
# of the ambiguous bare "python3". Only the browser backend needs this; the
# unipile path uses the REST API and never imports Playwright.
if [ -z "${LINKEDIN_DISCOVER_PYTHON:-}" ]; then
    for _li_py in /opt/homebrew/bin/python3.11 /usr/bin/python3 /opt/homebrew/bin/python3 python3; do
        if command -v "$_li_py" >/dev/null 2>&1 && \
           "$_li_py" -c 'from playwright.sync_api import sync_playwright' >/dev/null 2>&1; then
            export LINKEDIN_DISCOVER_PYTHON="$_li_py"
            break
        fi
    done
    # Fallback: if none resolved, keep bare python3 so the failure is loud and
    # obvious in the run log rather than silently substituting a wrong path.
    export LINKEDIN_DISCOVER_PYTHON="${LINKEDIN_DISCOVER_PYTHON:-python3}"
fi

# Default harness URL - used by ensure_linkedin_browser_for_backend +
# cleanup_harness_tabs to decide whether we own this Chrome (and should
# launch/clean it) or whether it is externally managed (AppMaker, BYO).
_BH_LINKEDIN_DEFAULT_URL="http://127.0.0.1:9556"

BROWSER_INSTRUCTIONS=$(cat <<'BROWSER_HARNESS_EOF'
BROWSER BACKEND: linkedin-harness (browser-harness MCP, CDP-driven REAL Google Chrome on
port 9556, profile ~/.claude/browser-profiles/browser-harness-linkedin). The Chrome is
already logged in as Matthew Diakonov (i@m13v.com); cookies persist on disk.

You have ONE tool: mcp__linkedin-harness__bh_run(script). It runs arbitrary Python with
these helpers pre-imported:
  new_tab(url), goto_url(url), wait_for_load(), page_info(),
  capture_screenshot(),                     # returns path to PNG; Read it to see the page
  click_at_xy(x, y),                        # coordinate click (viewport pixels)
  js(expression),                           # page.evaluate-style; returns the result
  type_text(text),                          # types into currently-focused element
  press_key(key),                           # e.g. "Enter", "Tab", "Escape"
  scroll(x, y, dy=-300, dx=0), cdp(method, **params)

TAB HYGIENE (IMPORTANT): A placeholder tab ALWAYS already exists when you start
(pre-flight leaves exactly one tab open). REUSE IT: use goto_url() for your VERY FIRST
navigation as well as every subsequent one, so the existing tab is navigated in place.
Call new_tab() ONLY as a fallback when no usable tab exists (goto_url errors because
there is no active page) OR when you genuinely need a second tab open in parallel.
Opening a fresh tab on first navigation orphans the placeholder and leaks a tab every
cycle, which exhausts per-process Chrome resources.

LINKEDIN SAFETY (HARD RULES):
- NEVER call /voyager/api/* endpoints (Python, fetch(), js()). That is the internal
  web-client backend and tripped the 2026-04-17 restriction.
- NEVER loop opening individual post permalinks to scrape reactions/comments.
- NEVER combine scrollBy() with clicks on "Show more comments" or "Load earlier replies".
- If a checkpoint / login / verify-you-are-human page appears, return SESSION_INVALID
  immediately and stop. Do not attempt programmatic login.

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
  browser_scroll(down)            ->  bh_run('scroll(590, 450, dy=600)')

EXAMPLE - read recent activity comment count:
  bh_run('''
  goto_url("https://www.linkedin.com/in/me/recent-activity/comments/")
  wait_for_load()
  count = js("""
    return document.querySelectorAll('[data-id^="urn:li:comment:"]').length;
  """)
  print(count)
  ''')

VERIFY AFTER EVERY MUTATION by capturing a screenshot and reading the PNG, coordinate
clicks can miss; visual verification is the only reliable confirmation that the action took.
BROWSER_HARNESS_EOF
)

cleanup_harness_tabs() {
    # Close every CDP "page" tab except one. Same pattern as twitter-backend,
    # but scoped to the LinkedIn harness Chrome on port 9556.
    #
    # Health-check gate: 10s timeout + ONE retry; log skips so they are not silent.
    local _probe="curl -sf --max-time 10 -o /dev/null http://127.0.0.1:9556/json/version"
    if ! $_probe 2>/dev/null; then
        sleep 1
        if ! $_probe 2>/dev/null; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cleanup_harness_tabs: SKIPPED (linkedin-harness CDP /json/version unreachable after 10s+retry)" >&2
            return 0
        fi
    fi
    # Reuse the same cleanup script as Twitter; it just iterates /json on the
    # default port. Pass the port via env so a single script can serve both.
    BH_CLEANUP_PORT=9556 python3 "$_BH_REPO_DIR/scripts/cleanup_harness_tabs.py" 2>/dev/null || true
}

# ===== Cross-pipeline whole-run lock (2026-05-30) =====
# Only ONE LinkedIn browser pipeline may drive the single linkedin-harness
# Chrome (port 9556) at a time: run-linkedin, engage-linkedin,
# dm-outreach-linkedin, audit-linkedin, engage-dm-replies-linkedin,
# stats-linkedin. Without this, two launchd-fired pipelines interleave (each
# releases the per-phase `linkedin-browser` FIFO lock between phases), so e.g.
# run-linkedin Phase B posts a comment while engage drives a SERP, yanking the
# same window back and forth and leaking tabs between reactive sweeps.
#
# Every browser pipeline funnels through ensure_linkedin_browser_for_backend
# before it touches Chrome, so acquiring here covers ALL of them without
# editing the (chflags-locked) top-level scripts. Semantics mirror
# run-linkedin.sh's existing singleton guard:
#   - try once (mkdir), reclaim if the holder PID is dead
#   - if a DIFFERENT live pipeline holds it -> return 78 (reserved skip code).
#     Every call site converts 78 into a clean `exit 0` in the PARENT shell
#     (skip this fire; the launchd job retries on its next cadence). No
#     indefinite wait, so the ordering vs the per-phase FIFO
#     `linkedin-browser` lock can't deadlock.
#     WHY return-78 and not exit-0 here (2026-07-06 incident): most call
#     sites invoke ensure_linkedin_browser_for_backend inside a subshell,
#     either `( source ...; ensure... )` or `ensure... | tee`, so an `exit 0`
#     in this function only killed the subshell and every pipeline kept
#     driving Chrome anyway (observed: engage-linkedin Phase B and
#     engage-dm-replies-linkedin with two live LinkedIn tabs at once, and
#     linkedin-presence scrolling mid run-linkedin). `kill -TERM $$` is NOT a
#     usable alternative: lock.sh traps TERM with _sa_release_locks, which
#     cleans up locks but does not exit, so the pipeline would continue
#     running lockless. Only an explicit status check in the parent works.
#   - idempotent within a process via _LI_PIPELINE_LOCK_HELD, AND via a
#     holder-pid==$$ check (the env flag is lost when the acquiring call ran
#     in a subshell/pipe, but $$ is the top-level script pid even inside
#     subshells), so the SECOND phase-call (e.g. run-linkedin Phase B) does
#     not skip a lock this same process already owns.
# No release trap on purpose: a finished pipeline's lock dir is reclaimed by
# the next pipeline's dead-PID check, exactly like the singleton guard. This
# avoids clobbering the parent scripts' EXIT/INT/TERM/HUP run_monitor traps.
# Env-overridable ONLY so tests can exercise the lock against a scratch dir;
# real pipelines never set this and share the one /tmp path.
_LI_PIPELINE_LOCK_DIR="${_LI_PIPELINE_LOCK_DIR:-/tmp/s4l-linkedin-pipeline.lock}"
_acquire_linkedin_pipeline_lock() {
    # Already held by THIS process (re-entry across phases) -> proceed.
    if [ "${_LI_PIPELINE_LOCK_HELD:-0}" = "1" ]; then
        return 0
    fi
    local _who="${S4L_PIPELINE_NAME:-$(basename "${0:-linkedin-pipeline}")}"
    # BAIL, don't wait. Reverted 2026-06-05 to the original behavior: if another
    # LinkedIn pipeline already drives the 9556 Chrome, this fire exits 0 and
    # launchd re-fires on its next cadence. The 2026-06-04/05 "wait + drop/retake
    # browser lock" experiment starved the 15-min run-linkedin comment poster by
    # making it queue (and risk a lock-ordering deadlock) behind stats/dm jobs.
    # No indefinite wait, so the per-phase FIFO linkedin-browser lock can't
    # deadlock against this coarse one-driver-per-Chrome lock.
    while : ; do
        if mkdir "$_LI_PIPELINE_LOCK_DIR" 2>/dev/null; then
            echo "$$" > "$_LI_PIPELINE_LOCK_DIR/pid"
            echo "$_who" > "$_LI_PIPELINE_LOCK_DIR/holder"
            export _LI_PIPELINE_LOCK_HELD=1
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] linkedin-pipeline lock ACQUIRED by $_who (pid $$)" >&2
            return 0
        fi
        local _h_pid _h_who
        _h_pid="$(cat "$_LI_PIPELINE_LOCK_DIR/pid" 2>/dev/null || echo "")"
        _h_who="$(cat "$_LI_PIPELINE_LOCK_DIR/holder" 2>/dev/null || echo "?")"
        # Re-entry when the acquiring call ran in a subshell/pipe: the
        # _LI_PIPELINE_LOCK_HELD export was lost with that subshell, but the
        # recorded holder pid is still OURS ($$ is the top-level script pid
        # even inside subshells). Treat as already held.
        if [ "$_h_pid" = "$$" ]; then
            export _LI_PIPELINE_LOCK_HELD=1
            return 0
        fi
        if [ -z "$_h_pid" ] || ! kill -0 "$_h_pid" 2>/dev/null; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] linkedin-pipeline lock: reclaiming stale lock (dead holder ${_h_who} pid ${_h_pid:-unknown})" >&2
            rm -rf "$_LI_PIPELINE_LOCK_DIR"
            continue
        fi
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] linkedin-pipeline lock: held by ${_h_who} (pid ${_h_pid}); ${_who} skipping this fire (rc=78) to avoid two drivers on the 9556 Chrome" >&2
        return 78
    done
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

ensure_linkedin_browser_for_backend() {
    # AppMaker / BYO Chrome: LINKEDIN_CDP_URL points at something other than our
    # default harness URL. Don't touch that browser; just probe it and bail.
    if [ "${LINKEDIN_CDP_URL:-$_BH_LINKEDIN_DEFAULT_URL}" != "$_BH_LINKEDIN_DEFAULT_URL" ]; then
        local _ext_url="${LINKEDIN_CDP_URL}"
        if curl -sf --max-time 2 -o /dev/null "${_ext_url}/json/version" 2>/dev/null; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Using externally-managed Chrome at ${_ext_url} (skipping harness launch + tab cleanup)" >&2
            return 0
        fi
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: LINKEDIN_CDP_URL=${_ext_url} not reachable. External Chrome must be managed by host." >&2
        return 1
    fi
    # Cross-pipeline whole-run lock: only one LinkedIn browser pipeline drives
    # the 9556 harness Chrome at a time. Acquired here (the single chokepoint
    # every browser pipeline calls). Skipped above for externally-managed
    # (AppMaker/BYO) Chrome, which is not ours to serialize. A held-by-peer
    # lock returns the reserved skip code 78; propagate it so the CALLER's
    # parent shell can exit 0 (an exit here would only kill the subshell most
    # call sites wrap around this function; see the 2026-07-06 incident note
    # above the lock function).
    _acquire_linkedin_pipeline_lock || return $?
    # Probe + launch harness Chrome on port 9556 if needed.
    if ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9556/json/version 2>/dev/null; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] LinkedIn harness Chrome down on port 9556, launching..." >&2
        # Dated relaunch stamp for central observability (see twitter-backend.sh).
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) port=9556" \
            >> "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/logs/chrome-relaunch-events.log" 2>/dev/null || true
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
                # Default position captured 2026-05-26 from the user's
                # secondary monitor; overridable via BH_LINKEDIN_WINDOW_POS.
                _extra+=(--window-position="${BH_LINKEDIN_WINDOW_POS:-3814,-1050}")
                _extra+=(--window-size="${BH_LINKEDIN_WINDOW_SIZE:-1024,1013}")
                ;;
        esac
        # Self-heal (2026-06-03): reap any stale Chrome holding THIS profile dir
        # but not answering CDP on our port, else the relaunch hands off via the
        # SingletonLock and loops "failed to start within 12s". Exact-dir match
        # (trailing space) so this never touches the twitter browser-harness
        # profile. See twitter-backend.sh for the regression that motivated this.
        local _prof_dir="$HOME/.claude/browser-profiles/browser-harness-linkedin"
        local _stale_pids
        _stale_pids=$(pgrep -f -- "--user-data-dir=$_prof_dir " 2>/dev/null || true)
        if [ -n "$_stale_pids" ] && ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9556/json/version 2>/dev/null; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CDP down but Chrome still holds $_prof_dir (pids: $(echo $_stale_pids | tr '\n' ' ')); reaping stale profile owner before relaunch" >&2
            kill $_stale_pids 2>/dev/null || true
            sleep 2
            _stale_pids=$(pgrep -f -- "--user-data-dir=$_prof_dir " 2>/dev/null || true)
            [ -n "$_stale_pids" ] && { kill -9 $_stale_pids 2>/dev/null || true; sleep 1; }
            rm -f "$_prof_dir/SingletonLock" "$_prof_dir/SingletonSocket" "$_prof_dir/SingletonCookie" 2>/dev/null || true
        fi
        # The occlusion/backgrounding flags matter: the window sits offscreen,
        # and without them Chrome stops laying out SPA-rendered content, so
        # every element measures 0x0 and clicks become impossible (2026-07-03).
        # Spawn via the SHARED launcher (skill/lib/browser-launch.sh) — see
        # that file for the clean-exit stamp + no-focus-steal + detach
        # rationale. Do NOT hand-roll launch logic per backend.
        # shellcheck disable=SC1091
        source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/browser-launch.sh"
        launch_harness_chrome "$_chrome_bin" "$_prof_dir" \
            --remote-debugging-port=9556 \
            --user-data-dir="$HOME/.claude/browser-profiles/browser-harness-linkedin" \
            --no-first-run --no-default-browser-check \
            --disable-features=ChromeWhatsNewUI,CalculateNativeWinOcclusion \
            --disable-backgrounding-occluded-windows \
            --disable-renderer-backgrounding \
            --disable-background-timer-throttling \
            "${_extra[@]}" \
            about:blank
        for _i in 1 2 3 4 5 6 7 8 9 10 11 12; do
            curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9556/json/version 2>/dev/null && break
            sleep 1
        done
        if ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9556/json/version 2>/dev/null; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: LinkedIn harness Chrome failed to start within 12s" >&2
            return 1
        fi
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] LinkedIn harness Chrome up on port 9556" >&2
    fi
    # Always close leftover tabs from prior runs. Safe under acquire_lock
    # "linkedin-browser" serialization.
    cleanup_harness_tabs

    # Per-run logout detection (2026-06-03). Every browser pipeline funnels
    # through here before it touches LinkedIn, so this single call makes ANY
    # pipeline trip the killswitch on its natural next fire if the harness
    # Chrome has been logged out (999 / authwall / checkpoint), without editing
    # the chflags-locked top-level scripts. detect-gate is a no-op when the
    # killswitch is already active, and only ENGAGES on a CONCLUSIVE /feed/
    # redirect to auth (infra hiccups -> proceed, so a flaky render never
    # strands the pipeline). On a confirmed logout it engages the flag (which
    # pauses every pipeline on its next fire + starts the 24h recovery clock)
    # and returns 2, so we abort this fire instead of burning a Claude session
    # on a dead session.
    _linkedin_session_detect_gate
}

# Once-per-process guard mirrors _LI_PIPELINE_LOCK_HELD: run-linkedin.sh calls
# ensure_linkedin_browser_for_backend in both Phase A and Phase B, and we do not
# want two /feed/ probes per fire.
_linkedin_session_detect_gate() {
    if [ "${_LI_SESSION_PROBED:-0}" = "1" ]; then
        return 0
    fi
    export _LI_SESSION_PROBED=1
    local _py="${LINKEDIN_DISCOVER_PYTHON:-python3}"
    # `|| _rc=$?` so a nonzero exit (e.g. 2 = logged out) is "handled" and does
    # not trip a caller's `set -e` before we inspect the code ourselves.
    local _rc=0
    "$_py" "$_BH_REPO_DIR/scripts/linkedin_killswitch.py" detect-gate \
        --cdp-url "${LINKEDIN_CDP_URL:-$_BH_LINKEDIN_DEFAULT_URL}" >&2 || _rc=$?
    if [ "$_rc" = "2" ]; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] detect-gate tripped the LinkedIn killswitch; aborting this fire" >&2
        return 1
    fi
    return 0
}

defer_if_foreign_for_backend() {
    # Harness Chrome accepts multiple concurrent CDP clients on the same
    # browser-harness-linkedin profile, so a foreign MCP wrapper cannot cause
    # SingletonLock contention. Always return 1 (do not defer).
    return 1
}
