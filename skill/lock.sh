#!/bin/bash
# Portable file locking (no flock needed)
# Usage: source lock.sh; acquire_lock "platform-name" [timeout_seconds]
#
# Multiple acquire_lock calls stack: all held locks are cleaned up on exit by
# a single trap. Acquire platform-browser locks BEFORE pipeline-specific locks
# to avoid deadlock across pipelines that share a browser profile.

# shellcheck source=lib/platform.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/platform.sh"

# --- Lock-event instrumentation (added 2026-06-16) ---------------------------
# Single shared, DATED log of every lock lifecycle event from EVERY pipeline,
# so cross-pipeline contention is reconstructable from ONE file instead of
# merging undated per-pipeline launchd stderr streams (the exact thing that
# made the 2026-06-15 twitter-browser double-hold so hard to prove). Purely
# additive: best-effort, never fails the caller, changes NO lock behavior.
# The high-value field is `owner=self|OTHER` at every deletion point: if we
# ever delete a lock dir whose recorded pid is NOT ours, that line is the
# red-handed proof of an ownership-blind rm.
_SA_LOCK_EVENT_LOG="${_SA_LOCK_EVENT_LOG:-$(dirname "${BASH_SOURCE[0]}")/logs/lock-events.log}"
mkdir -p "$(dirname "$_SA_LOCK_EVENT_LOG")" 2>/dev/null || true
_sa_lock_event() {
  # usage: _sa_lock_event <event> <lock_name> [extra k=v ...]
  printf '%s pid=%s event=%s lock=%s %s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$$" "$1" "$2" "${*:3}" \
    >> "$_SA_LOCK_EVENT_LOG" 2>/dev/null || true
}
_sa_lock_owner_tag() {
  # echoes "owner=self on_disk=<pid>" or "owner=OTHER on_disk=<pid|none>" for $1=lock_dir.
  # owner=OTHER means the pid recorded in the lock dir is NOT us -> we are about
  # to delete a DIFFERENT holder's lock (the double-hold smoking gun).
  local _odp=""
  if [ -f "$1/pid" ]; then
    _odp="$(head -1 "$1/pid" 2>/dev/null || true)"
  fi
  if [ "$_odp" = "$$" ]; then
    printf 'owner=self on_disk=%s' "$_odp"
  else
    printf 'owner=OTHER on_disk=%s' "${_odp:-none}"
  fi
}
# Ownership guard (added 2026-06-17): returns 0 (true) ONLY if the lock dir $1's
# recorded pid is OURS. Used to gate every rm of a lock dir so we never delete a
# lock a peer currently holds. Proven necessary: 17h of lock-events.log caught 32
# `owner=OTHER` deletions on twitter-browser (trap + release blindly rm-ing a live
# peer's lock), which cascaded into real double-holds (two pipelines on one Chrome).
# Safe failure mode: if this ever wrongly returns false for OUR OWN lock (e.g. a
# transient pid-file read miss), we just skip our own cleanup; the acquire-side
# kill -0 stale path then reclaims it within one cycle. Never deadlocks.
_sa_we_own_lock() {
  local _odp=""
  [ -f "$1/pid" ] && _odp="$(head -1 "$1/pid" 2>/dev/null || true)"
  [ "$_odp" = "$$" ]
}

# Stack of currently-held lock directories AND outstanding queue tickets,
# both cleaned up on exit. Declared at source time so they survive across
# acquire_lock calls.
if [ -z "${_SA_LOCK_DIRS+x}" ]; then
  declare -a _SA_LOCK_DIRS=()
  declare -a _SA_LOCK_TICKETS=()
  _sa_release_locks() {
    local d t
    # Browser-profile cleanup BEFORE releasing locks (added 2026-05-13).
    # Before this, dm-outreach-twitter would exit, _sa_release_locks would
    # rm the twitter-browser lock dir, the next pipeline (engage-twitter,
    # engage-dm-replies-twitter) would acquire the now-free shell lock,
    # then find the Chrome profile's SingletonLock STILL held by the
    # previous pipeline's Chrome (which hadn't fully torn down yet) and
    # crash with "chromium profile locked by another process; waited 45s".
    # Observed 2026-05-13 14:06 (engage-twitter), 14:13 (engage-dm-replies-twitter,
    # spawned dozens of Chrome respawns at 3-5s cadence).
    #
    # Fix: for any held lock that LOOKS like a browser lock (suffix -browser),
    # kill any top-level Chrome on the corresponding profile BEFORE releasing
    # the shell lock, regardless of ppid. We hold the lock so this is safe
    # by construction (no peer can race us, unlike the post-acquire sweep
    # in acquire_lock which restricts to ppid==1 to avoid clobbering peers).
    # The next pipeline then takes a clean profile.
    for d in ${_SA_LOCK_DIRS[@]+"${_SA_LOCK_DIRS[@]}"}; do
      local lock_name="${d##*/}"
      lock_name="${lock_name#social-autoposter-}"
      lock_name="${lock_name%.lock}"
      case "$lock_name" in
        twitter-browser|reddit-browser|linkedin-browser)
          local plat="${lock_name%-browser}"
          local profile_dir="$HOME/.claude/browser-profiles/$plat"
          # Top-level Chromes on this profile (skip --type= subprocesses).
          local chrome_pids
          chrome_pids=$(ps -A -o pid=,command= 2>/dev/null             | awk -v p="user-data-dir=$profile_dir" '
                index($0,p)>0 && index($0,"--type=")==0 && index($0,"awk ")==0 {print $1}'             || true)
          if [ -n "$chrome_pids" ]; then
            # SIGTERM first for graceful close, brief pause, then SIGKILL stragglers.
            echo "$chrome_pids" | xargs kill -TERM 2>/dev/null || true
            sleep 1
            local still_alive
            still_alive=$(ps -A -o pid=,command= 2>/dev/null               | awk -v p="user-data-dir=$profile_dir" '
                  index($0,p)>0 && index($0,"--type=")==0 && index($0,"awk ")==0 {print $1}'               || true)
            if [ -n "$still_alive" ]; then
              echo "$still_alive" | xargs kill -KILL 2>/dev/null || true
            fi
            # Also kill matching MCP wrappers so they can't relaunch Chrome.
            pkill -KILL -f "${plat}-agent.json" 2>/dev/null || true
            # Clear singletons so the next launch_persistent_context starts clean.
            rm -f "$profile_dir/SingletonLock"                   "$profile_dir/SingletonCookie"                   "$profile_dir/SingletonSocket" 2>/dev/null || true
          fi
          ;;
      esac
    done
    # Safe for bash 3.2: ${arr[@]+"${arr[@]}"} expands to nothing when arr is
    # unset or empty, avoiding the "unbound variable" error with set -u.
    # The earlier if+for guard was insufficient because bash 3.2 treats even
    # ${#unset_arr[@]} as an "unbound variable" error in some exit-trap contexts.
    for d in ${_SA_LOCK_DIRS[@]+"${_SA_LOCK_DIRS[@]}"}; do
      local _lname="${d##*/}"
      _lname="${_lname#social-autoposter-}"
      _lname="${_lname%.lock}"
      # Ownership guard: only delete the dir if WE still hold it. A peer may have
      # legitimately re-acquired it after our mid-cycle release; deleting it here
      # is defect "owner=OTHER" (wipes a live peer's lock -> double-hold).
      if _sa_we_own_lock "$d"; then
        _sa_lock_event trap_rm "$_lname" "$(_sa_lock_owner_tag "$d")"
        echo "[lock] trap-released $_lname pid=$$ at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
        rm -rf "$d"
      else
        _sa_lock_event trap_rm_skipped "$_lname" "$(_sa_lock_owner_tag "$d")"
        echo "[lock] trap-release SKIPPED $_lname pid=$$ (not owner) at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
      fi
    done
    for t in ${_SA_LOCK_TICKETS[@]+"${_SA_LOCK_TICKETS[@]}"}; do
      rm -f "$t"
    done
  }
  trap _sa_release_locks EXIT
  # 2026-07-12: INT/TERM/HUP release locks AND EXIT, instead of sharing the
  # plain EXIT handler. Scenario this fixes (observed 2026-07-12 12:02:09,
  # engage-dm-replies.sh pid 72465): a stray signal fired the old combined
  # trap, which released BOTH held locks and then RESUMED the script (bash
  # continues after a trapped signal when the handler doesn't exit). The run
  # kept driving the shared twitter Chrome without its twitter-browser lock,
  # the twitter cycle legitimately acquired the freed lock and navigated the
  # same tab mid-send, and all 3 send-dm attempts died with
  # thread_url_redirected ("shared-tab drift", human_dm_replies id 100045).
  # A signaled run must never continue lock-protected work: log WHICH signal
  # hit (the old single-line event gave no clue), clean up once, exit 128+N.
  # Scripts that install their own combined traps after sourcing this file
  # (e.g. run-twitter-cycle.sh's _sa_combined_exit) still override these.
  _sa_exit_on_signal() {
    local _sig="$1" _code="$2"
    _sa_lock_event signal_exit "-" "signal=$_sig"
    echo "[lock] caught SIG${_sig} pid=$$; releasing locks and exiting $_code at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
    _sa_release_locks
    trap - EXIT
    exit "$_code"
  }
  trap '_sa_exit_on_signal INT 130' INT
  trap '_sa_exit_on_signal TERM 143' TERM
  trap '_sa_exit_on_signal HUP 129' HUP
fi

acquire_lock() {
  local name="$1"
  local timeout="${2:-3600}"
  local lock_dir="/tmp/social-autoposter-${name}.lock"
  local queue_dir="${lock_dir}.queue"
  local waited=0
  # logged_holder: per-acquire flag so we surface "who is holding this lock"
  # exactly once when we start waiting, not on every 2s poll. Added 2026-05-26
  # so the operator can answer "why did Twitter cycle 90245 wait 60s for the
  # browser lock?" by grepping the cycle log for `[lock] waiting for
  # twitter-browser` instead of cross-correlating launchd start times.
  local logged_holder=false

  # Platform-browser locks still get the orphan-Chrome sweep on acquire (after
  # the lock is taken). Peers do NOT force-kill each other: a long-running
  # holder is the watchdog's responsibility (per-script caps in
  # scripts/watchdog_hung_runs.py), not a peer pipeline's. Prior versions
  # killed the holder's whole process group at lock_age > 600s and clobbered
  # unrelated steps (e.g. stats.sh Step 2 was SIGTERMed mid-API-call by a
  # waiting dm-replies-reddit on 2026-04-25).
  local is_browser_lock=false
  case "$name" in
    reddit-browser|linkedin-browser|twitter-browser) is_browser_lock=true ;;
  esac

  # FIFO ticket queue (added 2026-05-01). Without this, mkdir-race acquisition
  # starved long-waiters under parallel cycles: a fresh cycle entering Phase 1
  # would race-win the lock the moment the prior holder released, ahead of a
  # peer's Phase 2b-post that had been waiting 5+ min. Observed live: cycle
  # 90205's Phase 2b-post waited 8+ min while three newer cycles cut in line
  # for their own Phase 1 scrapes.
  #
  # Mechanism: each waiter writes a `<ns_timestamp>-<pid>` ticket into
  # `${lock_dir}.queue/`. ls-sort gives FIFO order. Only the head-of-queue
  # waiter races to mkdir the lock_dir, so post-release acquisition is
  # deterministic by arrival time. Ticket is removed once the lock is held
  # (and on EXIT trap as a safety net for SIGKILLed waiters).
  mkdir -p "$queue_dir"
  local ticket
  ticket="$(python3 -c 'import time; print(time.time_ns())' 2>/dev/null)-$$"
  if [ -z "${ticket%-$$}" ]; then
    # python3 unavailable; fall back to seconds + microsecond approximation.
    # PID disambiguates same-second collisions; loses sub-second FIFO ordering
    # but maintains correctness (waiters in same second arbitrate by PID).
    ticket="$(date +%s)000000000-$$"
  fi
  local ticket_file="$queue_dir/$ticket"
  echo $$ > "$ticket_file"
  _SA_LOCK_TICKETS+=("$ticket_file")

  while true; do
    # GC stale tickets: any ticket whose owning PID is dead. Without this a
    # SIGKILLed waiter (no trap fired) would block all newer waiters forever
    # because its ticket would always be oldest.
    local t tpid
    for t in $(ls -1 "$queue_dir" 2>/dev/null); do
      tpid=$(cat "$queue_dir/$t" 2>/dev/null || echo "")
      if [ -n "$tpid" ] && ! kill -0 "$tpid" 2>/dev/null; then
        rm -f "$queue_dir/$t"
      fi
    done

    # Check our position. ls -1 + sort gives lexicographic (== numeric) order
    # over fixed-width nanosecond timestamps, so head is the oldest waiter.
    local oldest
    oldest=$(ls -1 "$queue_dir" 2>/dev/null | sort | head -1)

    if [ "$oldest" = "$ticket" ]; then
      # We are the head. Try to acquire the lock.
      if mkdir "$lock_dir" 2>/dev/null; then
        # Won the lock. Write PID, register for trap-cleanup, drop our ticket,
        # then break out into the post-acquire (Chrome sweep + return).
        echo $$ > "$lock_dir/pid"
        # Initial 90s lease so watchdog reads lease_remaining instead of
        # "missing" before the first heartbeat fires. Pipelines that go
        # through reddit_browser.py or MCP hooks will bump this on each CDP
        # op; bash-only acquires get the 90s grace window.
        echo $(($(date +%s) + 90)) > "$lock_dir/expires_at" 2>/dev/null || true
        _SA_LOCK_DIRS+=("$lock_dir")
        rm -f "$ticket_file"
        # Remove our ticket from _SA_LOCK_TICKETS so the EXIT trap doesn't
        # try to rm-f it again (harmless, but keeps the array honest).
        local _new_t=()
        local _existing
        for _existing in ${_SA_LOCK_TICKETS[@]+"${_SA_LOCK_TICKETS[@]}"}; do
          [ "$_existing" != "$ticket_file" ] && _new_t+=("$_existing")
        done
        _SA_LOCK_TICKETS=(${_new_t[@]+"${_new_t[@]}"})
        echo "[lock] acquired $name pid=$$ at $(date -u +%Y-%m-%dT%H:%M:%SZ) waited=${waited}s" >&2
        _sa_lock_event acquired "$name" "waited=${waited}s"
        break
      fi

      # We're head-of-queue but lock_dir exists. Either the holder is alive
      # and active (normal — wait), or they died uncleanly. Apply the same
      # stale-detection used pre-FIFO.
      # 2026-07-07 hardening: twice (2026-07-06 20:36, 2026-07-07 11:10) this
      # block reclaimed linkedin-browser from a LIVE run-linkedin holder, and
      # the old single-line event gave no clue WHICH branch fired. Now:
      #   - every reclaim records reason=no_pidfile|dead_pid|age
      #   - stat_mtime's 0-on-failure sentinel no longer counts as ancient
      #     (now-0 made the age check treat a fresh lock as 56 years old)
      #   - a 1s re-verify runs before rm so a one-poll misread (transient
      #     cat/kill -0 flake) cannot yank a live peer's lock
      local should_remove=false
      local stale_reason=""
      if [ ! -f "$lock_dir/pid" ]; then
        should_remove=true
        stale_reason="no_pidfile"
      else
        local holder_pid
        holder_pid=$(cat "$lock_dir/pid" 2>/dev/null || echo "")
        if [ -z "$holder_pid" ] || ! kill -0 "$holder_pid" 2>/dev/null; then
          should_remove=true
          stale_reason="dead_pid:${holder_pid:-empty}"
        fi
      fi
      # Safety net: remove any lock older than 3 hours regardless of holder
      # liveness. Watchdog's per-script caps (45m default, 120m for
      # stats_reddit/github-engage) will SIGTERM a hung holder long before
      # this fires.
      if ! $should_remove && [ -d "$lock_dir" ]; then
        local lock_age lock_mtime
        lock_mtime=$(stat_mtime "$lock_dir")
        if [ "$lock_mtime" -gt 0 ]; then
          lock_age=$(( $(date +%s) - lock_mtime ))
          if [ "$lock_age" -gt 10800 ]; then
            should_remove=true
            stale_reason="age:${lock_age}s"
          fi
        fi
      fi
      if $should_remove; then
        sleep 1
        if [ ! -d "$lock_dir" ]; then
          # Holder (or its trap) removed it during our re-verify window; loop
          # back and race mkdir normally.
          continue
        fi
        local still_stale=false
        if [ ! -f "$lock_dir/pid" ]; then
          still_stale=true
        else
          local recheck_pid
          recheck_pid=$(cat "$lock_dir/pid" 2>/dev/null || echo "")
          if [ -z "$recheck_pid" ] || ! kill -0 "$recheck_pid" 2>/dev/null; then
            still_stale=true
          elif [ "${stale_reason#age:}" != "$stale_reason" ]; then
            # Age-based reclaim is deliberate even with a live (hung) holder.
            still_stale=true
          fi
        fi
        if $still_stale; then
          _sa_lock_event stale_reclaim "$name" "reason=$stale_reason $(_sa_lock_owner_tag "$lock_dir")"
          echo "Removing stale $name lock (reason=$stale_reason)"
          rm -rf "$lock_dir"
          continue
        fi
        _sa_lock_event stale_reclaim_aborted "$name" "reason=$stale_reason $(_sa_lock_owner_tag "$lock_dir")"
        echo "[lock] stale-reclaim ABORTED for $name pid=$$ (initial reason=$stale_reason; holder alive on re-verify)" >&2
        # Not stale after all — fall through to the normal wait path.
      fi
    fi

    if [ "$waited" -ge "$timeout" ]; then
      echo "Previous $name run still active after $((timeout/60))min, skipping"
      rm -f "$ticket_file"
      exit 0
    fi
    # Holder identity: log who is holding the lock the first time we sleep.
    # Read pid file, confirm liveness, then best-effort extract the .sh script
    # name from `ps -o args=`. Without this we only knew lock waits happened
    # (the `waited=Ns` at acquire time); we never knew which peer cycle caused
    # them, which made cross-cycle contention impossible to attribute. We log
    # at most once per acquire_lock call to avoid flooding the cycle log on
    # long waits (a 60s wait would otherwise produce 30 identical lines).
    if ! $logged_holder && [ -d "$lock_dir" ] && [ -f "$lock_dir/pid" ]; then
      local hpid hcmd hscript
      hpid=$(cat "$lock_dir/pid" 2>/dev/null || echo "")
      if [ -n "$hpid" ] && kill -0 "$hpid" 2>/dev/null; then
        hcmd=$(ps -o args= -p "$hpid" 2>/dev/null | head -c 240)
        hscript=$(echo "$hcmd" | grep -oE '[^ /]+\.sh' | head -1)
        [ -z "$hscript" ] && hscript='(non-shell)'
        echo "[lock] waiting for $name pid=$$ held_by=$hpid script=$hscript cmd='${hcmd}'" >&2
        _sa_lock_event waiting "$name" "held_by=$hpid script=$hscript"
        logged_holder=true
      fi
    fi
    # 2s poll keeps head-of-queue snappy after release without burning CPU.
    # Pre-FIFO this was 10s, but FIFO means only the head actually contends —
    # tighter polling here mostly affects the winner, not the racing pack.
    sleep 2
    waited=$((waited + 2))
  done

  # Platform-browser locks: sweep orphan Chromes holding the profile. A prior
  # run may have exited without cleanly closing Chrome (parent playwright-mcp
  # dies, Chrome gets reparented to PID 1, profile stays locked). Since we
  # now hold the exclusive shell lock, any Chrome on this profile is an
  # orphan and safe to kill before the caller launches a fresh MCP session.
  #
  # Also sweep orphan playwright-mcp / node wrappers reparented to PID 1. A
  # live holder's MCP child is parented to its claude process; only true
  # orphans (parent died without running the EXIT trap, e.g. SIGKILL/OOM)
  # end up at ppid=1 and survive. The ppid==1 filter keeps a manually-
  # attached Claude session pointed at the same agent config safe: its MCP
  # child has the live claude as parent, not init. Without this sweep,
  # orphan wrappers accumulate over days and keep launchd from re-firing
  # because launchd treats the slot as still in flight.
  if $is_browser_lock; then
    local platform="${name%-browser}"
    # Chrome sweep: only kill Chromes whose top-level Chromium has been
    # reparented to launchd (ppid==1), i.e. true orphans whose parent
    # playwright-mcp died without cleanup. A LIVE peer's Chromium is parented
    # to its mcp wrapper (alive), so this filter skips it. Without the
    # ppid==1 guard, a peer that managed to acquire the lock concurrently
    # would SIGTERM the legitimate holder's Chrome and trigger crashes like
    # the GPU exit_code=15 we saw on 2026-04-28 14:12 PT.
    local chrome_pids
    chrome_pids=$(ps -A -o pid=,ppid=,command= | awk -v plat="browser-profiles/${platform}" '$2 == "1" && index($0, "user-data-dir=") > 0 && index($0, plat) > 0 {print $1}')
    if [ -n "$chrome_pids" ]; then
      echo "$chrome_pids" | xargs kill -TERM 2>/dev/null || true
      echo "Killed orphan Chrome (ppid=1) holding ${platform} profile: $(echo $chrome_pids | tr '\n' ' ')"
      sleep 1
    fi
    local mcp_pids
    mcp_pids=$(ps -A -o pid=,ppid=,command= | awk -v plat="${platform}-agent.json" '$2 == "1" && index($0, plat) > 0 {print $1}')
    if [ -n "$mcp_pids" ]; then
      echo "$mcp_pids" | xargs kill -TERM 2>/dev/null || true
      echo "Killed orphan MCP wrappers (ppid=1) for ${platform}-agent: $(echo $mcp_pids | tr '\n' ' ')"
      sleep 1
    fi
  fi
}

# Probe + recover a wedged platform browser. Call ONLY after acquire_lock
# "<platform>-browser" — the lock holder has exclusive access to the profile,
# so killing live MCP/Chrome here is safe (peers cannot race us). The 2026-04-25
# stats-mid-API SIGTERM and 2026-04-28 GPU exit_code=15 regressions both came
# from peers killing the holder's processes; this is the inverse and is safe
# by construction.
#
# Detection: find the Chrome whose --user-data-dir matches this platform's
# profile, extract its --remote-debugging-port, GET /json/version with a 2s
# timeout. If port is missing, Chrome isn't there, or HTTP fails, the MCP
# is wedged or absent.
#
# Recovery: SIGTERM (then SIGKILL) any Chrome on the profile + any MCP wrapper
# matching <platform>-agent.json, regardless of ppid. Remove SingletonLock so
# the next caller can launch_persistent_context cleanly. The next claude -p /
# twitter_browser.py / reddit_browser.py invocation cold-starts a fresh MCP.
ensure_browser_healthy() {
  local platform="$1"
  local profile_dir="$HOME/.claude/browser-profiles/$platform"

  # 1. Find Chrome on this profile, extract its remote-debugging-port.
  # Skip renderer/gpu/utility subprocesses (those carry --type=...). They
  # inherit --user-data-dir from the parent but get --remote-debugging-port=0,
  # so without this filter we'd extract "0" from a renderer, fail the
  # localhost:0 CDP probe, and (worse) the awk's `exit` mid-pipeline sends
  # SIGPIPE to ps. With pipefail + set -e that propagates as exit 141 and
  # silently kills the entire calling script before the scraper ever runs.
  # `|| true` is the seatbelt against the SIGPIPE corner case in case ps
  # races awk's exit on a future config change. Observed live 2026-05-06:
  # stats-linkedin-comments fires at 19:56 + 20:15 + 20:17 all died here.
  local cdp_port
  cdp_port=$(ps -A -o command= 2>/dev/null \
    | awk -v p="user-data-dir=$profile_dir" '
        index($0,p)>0 && index($0,"--type=")==0 && index($0,"awk ")==0 {
          if (match($0, /remote-debugging-port=[0-9]+/)) {
            print substr($0, RSTART+22, RLENGTH-22); exit
          }
        }' || true)

  # 2. Probe CDP. Healthy → return immediately.
  if [ -n "$cdp_port" ] \
     && curl -fsS --max-time 2 "http://localhost:${cdp_port}/json/version" >/dev/null 2>&1; then
    return 0
  fi

  # 3. CDP probe failed. Two reasons this can happen:
  #   (a) No Chrome at all on this profile — fall through to the singleton
  #       cleanup so launch_persistent_context starts fresh.
  #   (b) Chrome IS running but isn't reachable via CDP port — most likely
  #       a user-driven MCP session (linkedin-agent, twitter-agent, etc.)
  #       that uses --remote-debugging-pipe instead of a port. KILLING this
  #       Chrome destroys in-memory cookies (the disk copy can be 30-60s
  #       stale) and triggers anti-bot fingerprints, especially on LinkedIn
  #       (observed live 2026-05-06, Mediar account got authwalled).
  #
  # New behavior (was: kill immediately): when Chrome is running on the
  # profile, WAIT up to BROWSER_WAIT_SEC for it to exit on its own. Only
  # kill if it's still there after the wait. The lock is already held, so
  # peer pipelines aren't the source — it's either a user MCP session
  # (will close when they're done) or a stuck orphan (will need killing).
  local has_chrome
  has_chrome=$(ps -A -o command= 2>/dev/null \
    | awk -v p="user-data-dir=$profile_dir" '
        index($0,p)>0 && index($0,"--type=")==0 && index($0,"awk ")==0 {found=1; exit}
        END {print (found ? "yes" : "no")}' \
    || echo "no")

  if [ "$has_chrome" = "yes" ]; then
    local browser_wait_sec="${BROWSER_WAIT_SEC:-60}"
    echo "[ensure_browser_healthy] ${platform}: Chrome alive on profile but no reachable CDP port. Waiting up to ${browser_wait_sec}s for it to exit (likely user MCP session or slow-finishing prior run)."
    local waited=0
    while [ "$waited" -lt "$browser_wait_sec" ]; do
      sleep 5
      waited=$((waited + 5))
      has_chrome=$(ps -A -o command= 2>/dev/null \
        | awk -v p="user-data-dir=$profile_dir" '
            index($0,p)>0 && index($0,"--type=")==0 && index($0,"awk ")==0 {found=1; exit}
            END {print (found ? "yes" : "no")}' \
        || echo "no")
      if [ "$has_chrome" = "no" ]; then
        echo "[ensure_browser_healthy] ${platform}: Chrome exited cleanly after ${waited}s; safe to launch fresh."
        break
      fi
    done

    # Still here after the wait? Two cases:
    #   (a) Foreign MCP wrapper alive on this profile (user's IDE / Fazm Dev /
    #       Claude Code interactive session that has <platform>-agent.json in
    #       its MCP config) — DO NOT force-kill. Killing destroys the user's
    #       Chrome session mid-use. Log + exit cleanly so the next cron cycle
    #       retries when the user is done. Observed live 2026-05-13 14:15:14:
    #       run-twitter-cycle force-killed the user's Fazm Dev twitter-agent
    #       Chrome as "wedged orphan" and trashed an active IDE session.
    #   (b) No foreign MCP wrapper alive — true wedged orphan. Force-kill.
    if [ "$has_chrome" = "yes" ]; then
      if defer_if_foreign_browser_mcp_active "$platform"; then
        echo "[ensure_browser_healthy] ${platform}: Chrome still alive after ${browser_wait_sec}s AND foreign MCP wrapper detected. NOT force-killing — exiting this run cleanly so the user's session is preserved."
        exit 0
      fi
      echo "[ensure_browser_healthy] ${platform}: Chrome still alive after ${browser_wait_sec}s — no foreign MCP wrapper found, treating as wedged orphan and force-killing."
      pkill -TERM -f "${platform}-agent.json"          2>/dev/null || true
      pkill -TERM -f "user-data-dir=${profile_dir}"    2>/dev/null || true
      sleep 1
      pkill -KILL -f "${platform}-agent.json"          2>/dev/null || true
      pkill -KILL -f "user-data-dir=${profile_dir}"    2>/dev/null || true
    fi
  fi

  # 4. Clear singletons so launch_persistent_context can start fresh.
  rm -f "$profile_dir/SingletonLock" \
        "$profile_dir/SingletonCookie" \
        "$profile_dir/SingletonSocket" 2>/dev/null || true

  # 5. Normalize a "Crashed" exit_type left behind when the previous Chrome on
  # this profile died ungracefully (the force-kill above, OOM/jetsam, force-
  # quit, system sleep). Chrome reads profile.exit_type at startup; if it's
  # "Crashed" it pops the "Something went wrong when opening your profile. Some
  # features may be unavailable" modal. That dialog is GUI-only — it never
  # reaches the launchd log — and blocks the headless pipeline until dismissed.
  # Chrome is confirmed not running here (probe failed / we waited it out /
  # force-killed), so editing Preferences is race-free. Mirrors the clean-exit
  # flush Playwright/Selenium do internally but that a killed Chrome never runs.
  local prefs_file="$profile_dir/Default/Preferences"
  if [ -f "$prefs_file" ]; then
    python3 - "$prefs_file" "$platform" <<'PYEOF' || true
import json, sys
prefs_path, platform = sys.argv[1], sys.argv[2]
try:
    with open(prefs_path) as f:
        data = json.load(f)
    prof = data.setdefault("profile", {})
    before = prof.get("exit_type")
    changed = False
    if prof.get("exit_type") != "Normal":
        prof["exit_type"] = "Normal"; changed = True
    if prof.get("exited_cleanly") is not True:
        prof["exited_cleanly"] = True; changed = True
    if changed:
        with open(prefs_path, "w") as f:
            json.dump(data, f)
        print(f"[profile_health] {platform}: normalized exit_type={before!r} -> 'Normal'")
    else:
        print(f"[profile_health] {platform}: exit_type already clean")
except Exception as e:
    print(f"[profile_health] {platform}: normalize skipped ({e})", file=sys.stderr)
PYEOF
  fi

  return 0
}

# Detect if a foreign playwright-mcp wrapper for the given platform agent has
# a LIVE Chrome under its process tree on the platform's profile directory.
# When found, the calling cron pipeline should exit cleanly without launching
# Chrome — racing the foreign MCP's profile crashes Chrome with "chromium
# profile locked by another process; waited 45s" and burns the run.
#
# Why "wrapper + Chrome" instead of "wrapper alone": playwright-mcp wrappers
# spawn at Claude Code session startup (regardless of whether mcp__<platform>-
# agent__* tools have ever been called) and stay alive for the lifetime of
# the IDE/CLI session — hours to days. Chrome only launches lazily on the
# first tool call. Treating a naked wrapper as a conflict permanently starves
# the cron whenever any developer keeps a Claude Code window open with the
# agent in their MCP config (the steady-state in this workflow). Observed
# live 2026-05-13 17:00 / 17:15 / 17:30 / 17:43 / 17:45: four consecutive
# reddit + one twitter cycle bailed with posted=0 cost=$0.00 elapsed=61s
# even though no Chrome was actually open on either profile.
#
# Observed live 2026-05-13 14:29 (still a real conflict): engage-twitter.sh
# fired on schedule while the user's Fazm Dev IDE held a twitter-agent MCP
# wrapper via codex-acp AND that wrapper had a live Chrome. Phase A
# Playwright SIGTRAPed after the 45s SingletonLock wait. THIS case is what
# the defer mechanism exists to prevent.
#
# Usage:
#   defer_if_foreign_browser_mcp_active twitter || exit 0
#   defer_if_foreign_browser_mcp_active reddit  "$LOG_FILE"  # optional log path
#
# Returns 0 (foreign conflict, caller should defer) or 1 (clean, caller proceeds).
defer_if_foreign_browser_mcp_active() {
  local platform="$1"
  local log_file="${2:-}"
  local our_pid=$$
  local cfg_pattern="${platform}-agent.json"
  local profile_dir="$HOME/.claude/browser-profiles/$platform"

  # Step 1. Find every playwright-mcp wrapper or node-playwright-mcp child
  # whose command line references this platform's agent config file. Captures
  # both the npm-exec wrapper layer and the underlying node process so we
  # don't miss either tier of the tree.
  local wrappers
  wrappers=$(ps -A -o pid=,command= 2>/dev/null     | awk -v cfg="$cfg_pattern" '
        index($0,cfg)==0 { next }
        /npm exec @playwright\/mcp/ || /playwright-mcp/ { print $1 }
      ' || true)

  [ -z "$wrappers" ] && return 1

  # Step 2. Partition wrappers into ours (descendants of $$) vs foreign by
  # walking each wrapper's parent chain.
  local wpid cur depth foreign_wrappers=""
  for wpid in $wrappers; do
    cur=$wpid
    depth=0
    local is_ours=false
    # Cap at 20 hops to avoid pathological ancestry walks.
    while [ -n "$cur" ] && [ "$cur" != "1" ] && [ "$depth" -lt 20 ]; do
      if [ "$cur" = "$our_pid" ]; then
        is_ours=true
        break
      fi
      cur=$(ps -p "$cur" -o ppid= 2>/dev/null | tr -d ' ')
      depth=$((depth+1))
    done
    if ! $is_ours; then
      foreign_wrappers="$foreign_wrappers $wpid"
    fi
  done
  foreign_wrappers="${foreign_wrappers# }"

  [ -z "$foreign_wrappers" ] && return 1

  # Step 3. Require that at least one foreign wrapper has a live Chrome
  # child on this platform's profile. Walk every Chrome process whose
  # cmdline references user-data-dir=$profile_dir (this catches both the
  # top-level Chrome and its --type= renderer/utility subprocesses, all of
  # which inherit the cmdline) and check whether any of their ancestors is
  # one of the foreign wrappers. Bottom-up because pgrep -P is not portable
  # to all macOS variants and we already do ancestor walks above.
  local chrome_pids cpid
  chrome_pids=$(ps -A -o pid=,command= 2>/dev/null | awk -v p="user-data-dir=$profile_dir" '
        index($0,p)>0 && index($0,"awk ")==0 { print $1 }' || true)

  local foreign_pid=""
  if [ -n "$chrome_pids" ]; then
    for cpid in $chrome_pids; do
      cur=$cpid
      depth=0
      while [ -n "$cur" ] && [ "$cur" != "1" ] && [ "$depth" -lt 20 ]; do
        for wpid in $foreign_wrappers; do
          if [ "$cur" = "$wpid" ]; then
            foreign_pid=$wpid
            break
          fi
        done
        [ -n "$foreign_pid" ] && break
        cur=$(ps -p "$cur" -o ppid= 2>/dev/null | tr -d ' ')
        depth=$((depth+1))
      done
      [ -n "$foreign_pid" ] && break
    done
  fi

  if [ -z "$foreign_pid" ]; then
    # Foreign wrapper(s) exist but none have a live Chrome on this profile.
    # No collision risk — proceed. This is the steady state when the user
    # has Claude Code open but hasn't invoked an mcp__<platform>-agent__*
    # tool this session (or invoked one and Chrome already closed).
    local first_foreign="${foreign_wrappers%% *}"
    echo "[defer_foreign_mcp] ${platform}: foreign wrapper(s) detected (${foreign_wrappers}) but NO live Chrome on profile ${profile_dir}; proceeding." >&2
    if [ -n "$log_file" ] && [ -w "$(dirname "$log_file")" ]; then
      echo "[defer_foreign_mcp] ${platform}: foreign wrapper(s) detected (${foreign_wrappers}) but NO live Chrome on profile ${profile_dir}; proceeding." >> "$log_file"
    fi
    return 1
  fi

  # Step 4. Identify the root process owning the foreign wrapper so the log
  # is useful (tells the user which IDE / cron session is holding Chrome).
  local foreign_root=$foreign_pid
  cur=$foreign_pid
  while [ -n "$cur" ] && [ "$cur" != "1" ]; do
    foreign_root=$cur
    cur=$(ps -p "$cur" -o ppid= 2>/dev/null | tr -d ' ')
  done
  local foreign_root_cmd
  foreign_root_cmd=$(ps -p "$foreign_root" -o command= 2>/dev/null | head -c 120)

  local msg="[defer_foreign_mcp] ${platform}: foreign ${platform}-agent MCP wrapper PID ${foreign_pid} has a live Chrome on profile ${profile_dir} (root PID ${foreign_root}: ${foreign_root_cmd}). Skipping this run to avoid Chrome profile collision."
  echo "$msg" >&2
  if [ -n "$log_file" ] && [ -w "$(dirname "$log_file")" ]; then
    echo "$msg" >> "$log_file"
  fi
  return 0
}

# Explicit early release. Use this when a long-running script only needs the
# browser for part of its run (e.g. run-twitter-cycle.sh holds the lock for
# Phase 1 scrape, releases between Phase 1 and Phase 2b posting, then re-acquires
# before Phase 2b). Without this, sibling pipelines waiting on the same profile
# lock block for the full cycle even when the holder is not using the browser.
release_lock() {
  local name="$1"
  local lock_dir="/tmp/social-autoposter-${name}.lock"
  # Ownership guard: only delete the dir if WE still hold it. If a peer re-acquired
  # it after our mid-cycle release (or it was already cleared), do NOT rm — that is
  # exactly the defect that wiped live peers' locks. The stack rebuild below still
  # runs so we stop tracking it either way.
  if _sa_we_own_lock "$lock_dir"; then
    _sa_lock_event release "$name" "$(_sa_lock_owner_tag "$lock_dir")"
    rm -rf "$lock_dir"
    echo "[lock] released $name pid=$$ at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
  else
    _sa_lock_event release_skipped "$name" "$(_sa_lock_owner_tag "$lock_dir")"
    echo "[lock] release SKIPPED $name pid=$$ (not owner) at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
  fi
  # Rebuild the lock stack without this entry so the EXIT trap doesn't try to
  # rm it again (harmless, but keeps the stack honest if release_lock is paired
  # with a later re-acquire of the same name).
  local new_stack=()
  local d
  for d in ${_SA_LOCK_DIRS[@]+"${_SA_LOCK_DIRS[@]}"}; do
    [ "$d" != "$lock_dir" ] && new_stack+=("$d")
  done
  _SA_LOCK_DIRS=(${new_stack[@]+"${new_stack[@]}"})
}

# ---- whole-run pipeline singleton (generalized 2026-07-14) ------------------
# acquire_pipeline_singleton NAME
#
# One-driver-per-resource lock for an ENTIRE pipeline run, generalized from
# linkedin-backend.sh's _acquire_linkedin_pipeline_lock (2026-05-30; read the
# incident comments there for why every semantic below is load-bearing):
#   - try once (mkdir /tmp/s4l-<NAME>-pipeline.lock); NO waiting/queueing (the
#     2026-06-04/05 wait experiment starved the comment poster).
#   - reclaim when the recorded holder pid is dead.
#   - re-entrant per top-level script: an env flag survives same-process
#     re-calls, and a holder-pid==$$ check survives subshell/pipe call sites
#     (where the env export is lost but $$ is still the script pid).
#   - a DIFFERENT live pipeline holds it -> return the RESERVED SKIP CODE 78.
#     Callers MUST check for 78 in the PARENT shell and exit 0 (skip this
#     fire; launchd re-fires on cadence). `exit` inside this function only
#     kills the caller's subshell (2026-07-06 incident), and `kill -TERM $$`
#     is neutered by lock.sh's TERM trap, so the rc contract is the ONLY
#     mechanism that works.
#   - NO release trap on purpose: the next pipeline's dead-pid check reclaims
#     a finished run's dir, and adding a trap would clobber the parent
#     scripts' EXIT/INT/TERM/HUP run_monitor traps.
acquire_pipeline_singleton() {
  local _ps_name="${1:?pipeline singleton name}"
  local _ps_dir="/tmp/s4l-${_ps_name}-pipeline.lock"
  local _ps_flag_var="_S4L_PIPE_LOCK_HELD_$(printf '%s' "$_ps_name" | tr -c 'A-Za-z0-9' '_')"
  # Already held by THIS process (re-entry across phases) -> proceed.
  if [ "$(eval "printf '%s' \"\${${_ps_flag_var}:-0}\"")" = "1" ]; then
    return 0
  fi
  local _ps_who="${S4L_PIPELINE_NAME:-$(basename "${0:-${_ps_name}-pipeline}")}"
  while : ; do
    if mkdir "$_ps_dir" 2>/dev/null; then
      echo "$$" > "$_ps_dir/pid"
      echo "$_ps_who" > "$_ps_dir/holder"
      eval "export ${_ps_flag_var}=1"
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ${_ps_name}-pipeline lock ACQUIRED by $_ps_who (pid $$)" >&2
      return 0
    fi
    local _ps_h_pid _ps_h_who
    _ps_h_pid="$(cat "$_ps_dir/pid" 2>/dev/null || echo "")"
    _ps_h_who="$(cat "$_ps_dir/holder" 2>/dev/null || echo "?")"
    # Re-entry when the acquiring call ran in a subshell/pipe: the env flag
    # was lost with that subshell, but the recorded holder pid is still OURS.
    if [ "$_ps_h_pid" = "$$" ]; then
      eval "export ${_ps_flag_var}=1"
      return 0
    fi
    if [ -z "$_ps_h_pid" ] || ! kill -0 "$_ps_h_pid" 2>/dev/null; then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ${_ps_name}-pipeline lock: reclaiming stale lock (dead holder ${_ps_h_who} pid ${_ps_h_pid:-unknown})" >&2
      rm -rf "$_ps_dir"
      continue
    fi
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ${_ps_name}-pipeline lock: held by ${_ps_h_who} (pid ${_ps_h_pid}); ${_ps_who} skipping this fire (rc=78)" >&2
    return 78
  done
}
