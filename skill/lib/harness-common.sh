#!/bin/bash
# harness-common.sh - the shared engine behind every per-platform browser
# backend (twitter-backend.sh, reddit-backend.sh, linkedin-backend.sh).
#
# LOCAL OVERRIDES: ~/.social-autoposter-mcp/harness-local.env is sourced (when
# present) before any launch decision. It is machine-local (never shipped),
# for per-box experiments without a release: e.g. pointing one operator box's
# harness at Chrome Beta (BH_CHROME_BIN=...) or adding launch flags
# (HC_EXTRA_FLAGS="--no-startup-window") while every other install keeps
# stock behavior. Values here are plain shell assignments; keep it tiny.
# shellcheck disable=SC1090
[ -f "$HOME/.social-autoposter-mcp/harness-local.env" ] && . "$HOME/.social-autoposter-mcp/harness-local.env" 2>/dev/null || true
#
# Born 2026-07-14 from the three near-duplicate backends. Every hard-won fix
# used to land in ONE backend and drift: the CDP wedge detector, the two-strike
# tolerance, the focus-safe launcher, the occlusion flags, and the repo-relative
# script paths all existed only on the twitter side for weeks. This file owns
# the machinery once; a backend supplies configuration plus platform hooks.
#
# Contract: a backend sources this file, then defines its public
# ensure_<platform>_browser_for_backend() as a thin wrapper that calls
# hc_ensure_browser with HC_* set as env-prefixed assignments on the call
# (NOT as file-level globals, so sourcing two backends in one process can
# never cross-wire their configs):
#
#     ensure_reddit_browser_for_backend() {
#         HC_PLATFORM=reddit HC_PORT=9557 ... hc_ensure_browser
#     }
#
# HC_* knobs (required unless a default is shown):
#   HC_PLATFORM      - twitter|reddit|linkedin; log labels + wedge marker prefix
#   HC_PORT          - CDP port (9555/9556/9557)
#   HC_PROFILE_DIR   - Chrome profile dir; also the exact-dir pgrep reap match
#   HC_DEFAULT_URL   - http://127.0.0.1:<port>; ownership test vs external/BYO
#   HC_CDP_URL       - actual CDP url (backend passes its <PLATFORM>_CDP_URL)
#   HC_LAUNCH_URL    - first-tab url (default about:blank)
#   HC_WINDOW_POS    - macOS offscreen window position (x,y)
#   HC_WINDOW_SIZE   - macOS window size (w,h)
#   HC_EXTRA_FLAGS   - extra platform Chrome flags, ONE STRING, split on
#                      whitespace on purpose (none of our flags carry spaces)
#   HC_HEALTH_FILE   - readiness-verdict sink (default
#                      skill/logs/cdp-health-<platform>.json; twitter passes
#                      the legacy cdp-health.json that memory_snapshot.py reads)
#   HC_WEDGE_MARKER  - stderr marker prefix (default <platform>_cdp_wedge;
#                      twitter's resolves to the load-bearing twitter_cdp_wedge
#                      that bin/server.js greps - do not rename)
# Hooks (optional FUNCTION NAMES, invoked only when declared):
#   HC_PRE_LAUNCH_HOOK   - after the external-Chrome bail, before probe/launch.
#                          Nonzero rc propagates (linkedin's whole-run lock
#                          returns the reserved skip code 78 through here).
#   HC_POST_LAUNCH_HOOK  - after the browser is confirmed up (freshly launched
#                          OR already up), before tab cleanup. Best effort;
#                          failures never fail the ensure (twitter session
#                          restore).
#   HC_EXTERNAL_OK_HOOK  - on the externally-managed-Chrome happy path, before
#                          returning (twitter session restore on AppMaker).
#   HC_POST_CLEANUP_HOOK - after tab cleanup; its rc IS the ensure rc
#                          (linkedin logout detect-gate aborts the fire).
#
# Everything here must stay bash-3.2-safe (launchd's shell): no arrays that
# might expand empty under set -u, no ${var,,}, no associative arrays.

# Repo root for the helper scripts this file shells out to, resolved from this
# file's own location (skill/lib/ -> two up), honoring S4L_REPO_DIR when the
# caller sets it. $HOME/social-autoposter hardcodes ran code OUTSIDE the
# managed package on customer boxes and silently no-op'd where that directory
# didn't exist (S4L-4H triage 2026-07-12).
_BH_REPO_DIR="${_BH_REPO_DIR:-${S4L_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}}"

_hc_ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

_hc_run_hook() {
    # $1 = hook function name (may be empty/undeclared -> no-op success).
    [ -n "${1:-}" ] || return 0
    declare -F "$1" >/dev/null 2>&1 || return 0
    "$1"
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

_hc_capture_wedge_diagnostics() {
    # Photograph a wedged Chrome BEFORE the reap kills it (2026-07-16): the
    # ws-handshake wedge survived the Chrome 150→151 jump and the 8s→20s
    # probe widening, hits all three harness ports ~hourly, and correlates
    # with zero renderer crashes — so stop theorizing and capture (a) a 3s
    # thread-stack sample of the browser process (what is the DevTools accept
    # path stuck on?) and (b) an open-connection census of the CDP port
    # (client/socket pile-up?). Written to skill/logs/wedge-diag/, pruned to
    # the last 10 wedges. Read-only, best-effort, ~4s added to a restart that
    # was already happening. sample(1) is Darwin-only.
    local _wd_port="${1:?port}" _wd_prof="${2:?profile dir}"
    [ "$(uname -s)" = "Darwin" ] || return 0
    local _wd_dir="$_BH_REPO_DIR/skill/logs/wedge-diag"
    mkdir -p "$_wd_dir" 2>/dev/null || return 0
    local _wd_ts _wd_pid _wd_out
    _wd_ts=$(date -u +%Y%m%dT%H%M%SZ)
    _wd_out="$_wd_dir/wedge-${_wd_port}-${_wd_ts}.txt"
    _wd_pid=$(lsof -nP -iTCP:"$_wd_port" -sTCP:LISTEN -t 2>/dev/null | head -1)
    {
        echo "=== wedge diagnostics port=${_wd_port} pid=${_wd_pid:-none} ts=${_wd_ts} profile=${_wd_prof} ==="
        echo "--- open TCP connections on ${_wd_port} ---"
        lsof -nP -iTCP:"$_wd_port" 2>/dev/null
        echo "--- system load ---"
        uptime
    } > "$_wd_out" 2>&1 || true
    if [ -n "$_wd_pid" ]; then
        # Sample WHILE a third probe runs (v2, 2026-07-16). The first capture
        # photographed a perfectly healthy Chrome ~40s after the failed
        # handshakes — post-seizure. Sampling concurrently with a live
        # handshake catches the DevTools thread in the failing act, and the
        # third probe's own verdict is the transient-vs-dead discriminator:
        # READY here means the "wedge" clears within ~a minute and the right
        # fix is a wider strike window, not a kill.
        sample "$_wd_pid" 10 -mayDie -file "${_wd_out%.txt}.sample.txt" >/dev/null 2>&1 &
        local _wd_sampler=$!
        local _wd_v3=""
        if _wd_v3=$(hc_cdp_ready "http://127.0.0.1:${_wd_port}"); then
            echo "--- third probe DURING sample: READY (wedge is TRANSIENT): ${_wd_v3}" >> "$_wd_out"
        else
            echo "--- third probe DURING sample: STILL FAILING: ${_wd_v3}" >> "$_wd_out"
        fi
        wait "$_wd_sampler" 2>/dev/null || true
    fi
    echo "[$(_hc_ts)] wedge diagnostics captured: $_wd_out" >&2
    # Prune: keep the 10 newest captures (2 files each).
    ls -t "$_wd_dir"/wedge-* 2>/dev/null | tail -n +21 | xargs rm -f 2>/dev/null || true
}

hc_cdp_ready() {
    # Real CDP readiness: complete an actual connect_over_cdp handshake against
    # the harness. /json/version alone is a liveness probe that a WEDGED Chrome
    # still passes (S4L-4H); see scripts/cdp_ready_check.py (which degrades to
    # an http-only probe when playwright is not importable, so a bare-python3
    # platform never false-wedges on a ModuleNotFoundError). Prints the probe's
    # one-line JSON verdict on stdout; exit status is the verdict.
    #
    # Timeout history: born at 8000ms on 2026-07-12 as "fail fast instead of
    # Playwright's 180s default hang" during the Chrome 150 crash storm — a
    # direction, not a calibration. On a loaded-but-healthy machine (load 10-20
    # is normal on the operator Mac) an 8s handshake can time out twice and
    # false-wedge a live browser (2026-07-16 02:27, zero crashes on Beta 151).
    # 20s keeps the fail-fast intent (9x under the old hang) with headroom;
    # HC_CDP_READY_TIMEOUT_MS overrides per box via harness-local.env.
    "${S4L_PYTHON:-python3}" "$_BH_REPO_DIR/scripts/cdp_ready_check.py" \
        "${1:?cdp url}" "${HC_CDP_READY_TIMEOUT_MS:-20000}" 2>/dev/null
}

hc_record_health() {
    # Persist the latest readiness verdict where memory_snapshot.py picks it up
    # (cdp_health block on the per-minute heartbeat sample). $1 = action tag,
    # $2 = the probe's JSON verdict (may be empty). Best effort, never fails
    # the caller.
    local _f="${HC_HEALTH_FILE:-$_BH_REPO_DIR/skill/logs/cdp-health-${HC_PLATFORM:-unknown}.json}"
    printf '{"ts":"%s","url":"%s","action":"%s","verdict":%s}\n' \
        "$(_hc_ts)" \
        "${HC_CDP_URL:-${HC_DEFAULT_URL:-}}" \
        "$1" \
        "${2:-null}" \
        > "$_f" 2>/dev/null || true
}

hc_cleanup_tabs() {
    # Close every CDP "page" tab except one. $1 = port, $2 = log label.
    # Delegated to a standalone Python script because bash 3.2 (what launchd
    # uses) cannot parse a nested heredoc inside a function body inside a
    # sourced file (broke every launchd-fired twitter script 2026-05-14).
    #
    # Health-check gate: 10s timeout + ONE retry (a busy Chrome blows a 2s
    # probe, cleanup silently skips, and orphan tabs pile up; 2026-05-16).
    # Log skips so they are not silent.
    local _port="${1:?port}" _label="${2:-harness}"
    local _probe="curl -sf --max-time 10 -o /dev/null http://127.0.0.1:${_port}/json/version"
    if ! $_probe 2>/dev/null; then
        sleep 1
        if ! $_probe 2>/dev/null; then
            echo "[$(_hc_ts)] cleanup_harness_tabs: SKIPPED (${_label} CDP /json/version unreachable after 10s+retry)" >&2
            return 0
        fi
    fi
    BH_CLEANUP_PORT="$_port" python3 "$_BH_REPO_DIR/scripts/cleanup_harness_tabs.py" 2>/dev/null || true
}

# Shared interpreter for the reserved skip code 78 (2026-07-15). Every
# ensure_<platform>_browser_for_backend() caller MUST check its own exit
# status at ITS OWN call site, not rely on the hook exiting for them: an
# `exit` inside a HC_PRE_LAUNCH_HOOK (or inside hc_ensure_browser itself)
# only terminates the immediate shell, so a caller that wraps the call in a
# subshell (`( source ...; ensure_x_browser_for_backend )`, several LinkedIn
# callers do this) would see it silently swallowed -- exactly the 2026-07-06
# incident (two live LinkedIn Chrome tabs from two pipelines) that first
# established this convention. This helper only standardizes the CHECK so
# every caller gets it right without re-deriving it; it does not (cannot)
# replace calling it at the right scope.
#
# Usage, immediately after the ensure_* call, in the CALLER's own shell (not
# a subshell, not inside a function you'd `|| _rc=$?` from two frames away):
#   ensure_reddit_browser_for_backend 2>&1 | tee -a "$LOG_FILE" || true
#   _rc="${PIPESTATUS[0]}"
#   hc_exit_if_deferred "$_rc" "reddit-harness"
#   if [ "$_rc" != "0" ]; then ...caller's own genuine-failure handling...; fi
#
# Exits the CALLING SCRIPT with 0 when $1 is exactly 78 (a peer pipeline is
# actively driving the shared harness Chrome; the launchd job re-fires on its
# next cadence, nothing lost). Returns (does not exit) for every other value,
# INCLUDING 0 and genuine failures, so callers keep full control of their own
# success/failure/fallback handling -- this does not replace the elif branch
# above, only the code every caller was duplicating (or getting wrong) for
# the defer case.
hc_exit_if_deferred() {
    local _rc="${1:?rc}" _label="${2:-browser}"
    if [ "$_rc" = "78" ]; then
        echo "[$(_hc_ts)] ${_label} bootstrap deferred (rc=78, peer pipeline active on the shared tab); skipping this fire." >&2
        exit 0
    fi
}

_hc_reap_profile_owners() {
    # Kill any Chrome holding EXACTLY this profile dir (trailing space in the
    # pattern keeps browser-harness from matching browser-harness-linkedin),
    # then clear the Singleton* files so the next launch binds our port instead
    # of handing off via Chrome's SingletonLock and looping "failed to start
    # within 12s" (2026-06-03; 8h twitter outage root cause).
    local _prof="${1:?profile dir}"
    local _pids
    _pids=$(pgrep -f -- "--user-data-dir=$_prof " 2>/dev/null || true)
    if [ -n "$_pids" ]; then
        kill $_pids 2>/dev/null || true
        sleep 2
        _pids=$(pgrep -f -- "--user-data-dir=$_prof " 2>/dev/null || true)
        [ -n "$_pids" ] && { kill -9 $_pids 2>/dev/null || true; sleep 1; }
    fi
    rm -f "$_prof/SingletonLock" "$_prof/SingletonSocket" "$_prof/SingletonCookie" 2>/dev/null || true
}

hc_ensure_browser() {
    local _plat="${HC_PLATFORM:?HC_PLATFORM}" _port="${HC_PORT:?HC_PORT}"
    local _prof="${HC_PROFILE_DIR:?HC_PROFILE_DIR}"
    local _def_url="${HC_DEFAULT_URL:?HC_DEFAULT_URL}"
    local _cdp_url="${HC_CDP_URL:-$_def_url}"
    local _marker="${HC_WEDGE_MARKER:-${_plat}_cdp_wedge}"

    # AppMaker / BYO Chrome: the platform CDP URL points at something other
    # than our default harness URL. Don't touch that browser (it is the host's
    # to restart); probe it, verify the handshake, and bail.
    if [ "$_cdp_url" != "$_def_url" ]; then
        if curl -sf --max-time 2 -o /dev/null "${_cdp_url}/json/version" 2>/dev/null; then
            local _ext_verdict
            if ! _ext_verdict=$(hc_cdp_ready "$_cdp_url"); then
                echo "[$(_hc_ts)] ERROR: external Chrome at ${_cdp_url} is WEDGED (/json/version answers but the CDP handshake never completes). Host must restart it (AppMaker /opt/startup.sh, etc)." >&2
                echo "${_marker}: detected url=${_cdp_url} action=none-external" >&2
                hc_record_health external_wedged "$_ext_verdict"
                return 1
            fi
            hc_record_health ok-external "$_ext_verdict"
            echo "[$(_hc_ts)] Using externally-managed Chrome at ${_cdp_url} (skipping harness launch + tab cleanup)" >&2
            _hc_run_hook "${HC_EXTERNAL_OK_HOOK:-}" || true
            return 0
        fi
        echo "[$(_hc_ts)] ERROR: ${_plat} CDP url ${_cdp_url} not reachable. External Chrome must be managed by host." >&2
        return 1
    fi

    # Platform pre-launch gate (linkedin's one-driver-per-Chrome lock). A
    # nonzero rc propagates untouched so the reserved skip code 78 reaches the
    # caller's parent shell.
    _hc_run_hook "${HC_PRE_LAUNCH_HOOK:-}" || return $?

    # Probe + launch harness Chrome if needed. Two-stage probe: /json/version
    # (liveness) then a real CDP handshake (readiness). A wedged Chrome passes
    # the first and fails the second; handing it downstream made every attach
    # eat Playwright's 180s default while holding the browser lock (S4L-4H).
    local _need_launch=0 _launch_reason="" _ready_verdict=""
    if ! curl -sf --max-time 2 -o /dev/null "${_def_url}/json/version" 2>/dev/null; then
        _need_launch=1; _launch_reason="http_down"
        echo "[$(_hc_ts)] ${_plat} harness Chrome down on port ${_port}, launching..." >&2
    else
        # TWO-STRIKE gate (2026-07-13): one failed handshake is NOT proof of a
        # wedge. Under heavy system load a perfectly healthy Chrome blows the
        # 8s attach budget, and a single-failure kill produced 3 false
        # wedge-kills in one day. First failure: stamp a strike file and
        # proceed WITHOUT killing (a truly wedged Chrome makes the cycle's own
        # pre-flight fail loudly right after). Second consecutive failure
        # within 30 minutes: genuinely wedged, kill + relaunch. A passing
        # check clears any stale strike.
        local _strike="/tmp/s4l_cdp_wedge_strike_${_port}"
        if ! _ready_verdict=$(hc_cdp_ready "$_def_url"); then
            if [ -z "$(find "$_strike" -mmin -30 2>/dev/null)" ]; then
                touch "$_strike"
                echo "[$(_hc_ts)] ${_plat} harness Chrome failed the CDP handshake check (${_ready_verdict:-no verdict}); first strike, NOT killing (loaded-machine tolerance). Second consecutive failure within 30m triggers the wedge heal." >&2
                echo "${_marker}: detected url=${_def_url} action=first_strike" >&2
                hc_record_health wedge_first_strike "$_ready_verdict"
                # Fall through WITHOUT killing: post-launch hook + tab cleanup
                # below still run.
            else
                rm -f "$_strike" 2>/dev/null || true
                _need_launch=1; _launch_reason="cdp_wedge"
                echo "[$(_hc_ts)] ${_plat} harness Chrome WEDGED on port ${_port} (second consecutive handshake failure: ${_ready_verdict:-no verdict}); killing and relaunching..." >&2
                # Machine-greppable marker (same stderr-marker convention as
                # twitter_access_gate; bin/server.js parses the twitter one).
                echo "${_marker}: detected url=${_def_url} action=restart" >&2
                hc_record_health wedge_restart "$_ready_verdict"
                _hc_capture_wedge_diagnostics "$_port" "$_prof"
                _hc_reap_profile_owners "$_prof"
            fi
        else
            # Healthy handshake: clear any earlier strike so isolated blips
            # never accumulate into a kill.
            rm -f "$_strike" 2>/dev/null || true
        fi
    fi

    if [ "$_need_launch" = 1 ]; then
        local _chrome_bin
        _chrome_bin=$(_resolve_chrome_bin)
        if [ -z "$_chrome_bin" ]; then
            echo "[$(_hc_ts)] ERROR: no Chrome/Chromium binary found. Set BH_CHROME_BIN." >&2
            return 1
        fi
        # Dated relaunch stamp for central observability: memory_snapshot.py
        # counts these (chrome_relaunches block) so a kill-respawn loop is
        # visible per-install without depending on a foreground observer.
        # chrome= carries the binary version at launch time: a version change
        # between consecutive lines marks exactly when a silent auto-update
        # went live (how the 2026-07-12 Chrome 150 crash-storm onset was
        # dated: update on disk 07-10, active only at the 07-12 reboot).
        local _chrome_ver
        _chrome_ver=$("$_chrome_bin" --version 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+)+' | head -1)
        echo "$(_hc_ts) port=${_port} reason=${_launch_reason} chrome=${_chrome_ver:-unknown}" \
            >> "$_BH_REPO_DIR/skill/logs/chrome-relaunch-events.log" 2>/dev/null || true
        # On Linux + no display, run headless. On root, add --no-sandbox.
        # Window-position/size only meaningful on macOS multi-monitor; skip
        # elsewhere so we don't hide the window off-screen on single-display
        # Linux VMs. Built as a plain string (word-split on purpose) because
        # an empty array expansion trips bash 3.2 under set -u.
        local _os_extra=""
        case "$(uname -s)" in
            Linux)
                _os_extra="--no-sandbox --disable-dev-shm-usage"
                if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
                    _os_extra="$_os_extra --headless=new --disable-gpu"
                fi
                ;;
            Darwin)
                _os_extra="--window-position=${HC_WINDOW_POS:-2000,-1032} --window-size=${HC_WINDOW_SIZE:-1024,1013}"
                ;;
        esac
        # Self-heal: reap any stale Chrome holding THIS profile dir but not
        # answering CDP on our port, else the relaunch hands off via the
        # SingletonLock and loops "failed to start within 12s".
        local _stale_pids
        _stale_pids=$(pgrep -f -- "--user-data-dir=$_prof " 2>/dev/null || true)
        if [ -n "$_stale_pids" ] && ! curl -sf --max-time 2 -o /dev/null "${_def_url}/json/version" 2>/dev/null; then
            echo "[$(_hc_ts)] CDP down but Chrome still holds $_prof (pids: $(echo $_stale_pids | tr '\n' ' ')); reaping stale profile owner before relaunch" >&2
            _hc_reap_profile_owners "$_prof"
        fi
        # Spawn via the SHARED launcher (skill/lib/browser-launch.sh): clean-
        # exit stamp (no Aw-Snap restore), `open -n -g` on macOS (no focus
        # steal, out of this job's pgroup), setsid exec fallback elsewhere.
        # The occlusion/backgrounding flags matter for every platform: the
        # window sits offscreen, and without them Chrome stops laying out
        # SPA-rendered content, so elements measure 0x0 and coordinate clicks
        # become impossible (2026-07-03).
        # shellcheck disable=SC1091
        source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/browser-launch.sh"
        # shellcheck disable=SC2086
        # --disable-hang-monitor: standard automation flag. Without it Chrome's
        # hang monitor kills renderers that stop responding while CDP-driven
        # scans hammer them under system load — the leading explanation for the
        # 2026-07-14/15 EXC_BREAKPOINT (deliberate internal abort) crash dumps
        # arriving ~once per cycle with zero jetsam/OOM evidence. Playwright
        # and Puppeteer both launch with it for exactly this reason.
        # With --no-startup-window (focus-steal prevention: Chrome creates NO
        # window at launch, so macOS has nothing to activate; the first
        # pipeline op mints its tab via the background Target.createTarget
        # path) a positional URL argument would force a window and defeat the
        # flag — drop the launch URL in that mode.
        local _launch_url="${HC_LAUNCH_URL:-about:blank}"
        case " ${HC_EXTRA_FLAGS:-} " in
            *" --no-startup-window "*) _launch_url="" ;;
        esac
        launch_harness_chrome "$_chrome_bin" "$_prof" \
            --remote-debugging-port="$_port" \
            --user-data-dir="$_prof" \
            --no-first-run --no-default-browser-check \
            --disable-hang-monitor \
            --disable-features=ChromeWhatsNewUI,CalculateNativeWinOcclusion \
            --disable-backgrounding-occluded-windows \
            ${HC_EXTRA_FLAGS:-} \
            ${_os_extra} \
            ${_launch_url}
        for _i in 1 2 3 4 5 6 7 8 9 10 11 12; do
            curl -sf --max-time 2 -o /dev/null "${_def_url}/json/version" 2>/dev/null && break
            sleep 1
        done
        if ! curl -sf --max-time 2 -o /dev/null "${_def_url}/json/version" 2>/dev/null; then
            echo "[$(_hc_ts)] ERROR: ${_plat} harness Chrome failed to start within 12s" >&2
            hc_record_health launch_failed null
            return 1
        fi
        # Verify the fresh Chrome actually completes a CDP handshake before
        # declaring victory; a relaunch that comes up wedged again should fail
        # the cycle loudly, not feed 180s hangs downstream.
        local _post_verdict
        if ! _post_verdict=$(hc_cdp_ready "$_def_url"); then
            echo "[$(_hc_ts)] ERROR: ${_plat} harness Chrome answers HTTP after relaunch but the CDP handshake is STILL failing: ${_post_verdict:-no verdict}" >&2
            echo "${_marker}: detected url=${_def_url} action=relaunch_failed" >&2
            hc_record_health relaunch_failed "$_post_verdict"
            return 1
        fi
        hc_record_health relaunched "$_post_verdict"
        echo "[$(_hc_ts)] ${_plat} harness Chrome up on port ${_port}" >&2
    else
        hc_record_health ok "${_ready_verdict:-null}"
    fi

    # Platform post-launch hook (twitter session restore). Runs on both the
    # freshly-launched and already-up paths so a mid-life logout heals. Best
    # effort; never blocks the cycle on failure.
    _hc_run_hook "${HC_POST_LAUNCH_HOOK:-}" || true

    # Always close leftover tabs from prior runs. Safe under the caller's
    # browser-lock serialization.
    hc_cleanup_tabs "$_port" "${_plat}-harness"

    # Platform post-cleanup gate (linkedin logout detect-gate). Its rc IS the
    # ensure rc, so a confirmed logout aborts this fire.
    _hc_run_hook "${HC_POST_CLEANUP_HOOK:-}"
}

hc_defer_if_foreign() {
    # Harness Chrome accepts multiple concurrent CDP clients on the same
    # profile, so a foreign MCP wrapper cannot cause SingletonLock contention.
    # Always return 1 (do not defer).
    return 1
}
