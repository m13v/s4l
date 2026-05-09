#!/bin/bash
# Portable file locking (no flock needed)
# Usage: source lock.sh; acquire_lock "platform-name" [timeout_seconds]
#
# Multiple acquire_lock calls stack: all held locks are cleaned up on exit by
# a single trap. Acquire platform-browser locks BEFORE pipeline-specific locks
# to avoid deadlock across pipelines that share a browser profile.

# shellcheck source=lib/platform.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/platform.sh"

# Stack of currently-held lock directories AND outstanding queue tickets,
# both cleaned up on exit. Declared at source time so they survive across
# acquire_lock calls.
if [ -z "${_SA_LOCK_DIRS+x}" ]; then
  declare -a _SA_LOCK_DIRS=()
  declare -a _SA_LOCK_TICKETS=()
  _sa_release_locks() {
    local d t
    # Safe for bash 3.2: ${arr[@]+"${arr[@]}"} expands to nothing when arr is
    # unset or empty, avoiding the "unbound variable" error with set -u.
    # The earlier if+for guard was insufficient because bash 3.2 treats even
    # ${#unset_arr[@]} as an "unbound variable" error in some exit-trap contexts.
    for d in ${_SA_LOCK_DIRS[@]+"${_SA_LOCK_DIRS[@]}"}; do
      rm -rf "$d"
    done
    for t in ${_SA_LOCK_TICKETS[@]+"${_SA_LOCK_TICKETS[@]}"}; do
      rm -f "$t"
    done
  }
  trap _sa_release_locks EXIT INT TERM HUP
fi

acquire_lock() {
  local name="$1"
  local timeout="${2:-3600}"
  local lock_dir="/tmp/social-autoposter-${name}.lock"
  local queue_dir="${lock_dir}.queue"
  local waited=0

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
        break
      fi

      # We're head-of-queue but lock_dir exists. Either the holder is alive
      # and active (normal — wait), or they died uncleanly. Apply the same
      # stale-detection used pre-FIFO.
      local should_remove=false
      if [ ! -f "$lock_dir/pid" ]; then
        should_remove=true
      else
        local holder_pid
        holder_pid=$(cat "$lock_dir/pid" 2>/dev/null || echo "")
        if [ -z "$holder_pid" ] || ! kill -0 "$holder_pid" 2>/dev/null; then
          should_remove=true
        fi
      fi
      # Safety net: remove any lock older than 3 hours regardless. Watchdog's
      # per-script caps (45m default, 120m for stats_reddit/github-engage) will
      # SIGTERM a hung holder long before this fires.
      if [ -d "$lock_dir" ]; then
        local lock_age
        lock_age=$(( $(date +%s) - $(stat_mtime "$lock_dir") ))
        if [ "$lock_age" -gt 10800 ]; then
          should_remove=true
        fi
      fi
      if $should_remove; then
        echo "Removing stale $name lock"
        rm -rf "$lock_dir"
        continue
      fi
    fi

    if [ "$waited" -ge "$timeout" ]; then
      echo "Previous $name run still active after $((timeout/60))min, skipping"
      rm -f "$ticket_file"
      exit 0
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
        index($0,p)>0 && index($0,"--type=")==0 {
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
        index($0,p)>0 && index($0,"--type=")==0 {found=1; exit}
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
            index($0,p)>0 && index($0,"--type=")==0 {found=1; exit}
            END {print (found ? "yes" : "no")}' \
        || echo "no")
      if [ "$has_chrome" = "no" ]; then
        echo "[ensure_browser_healthy] ${platform}: Chrome exited cleanly after ${waited}s; safe to launch fresh."
        break
      fi
    done

    # Still here after the wait? Treat as a wedged orphan and force-kill.
    if [ "$has_chrome" = "yes" ]; then
      echo "[ensure_browser_healthy] ${platform}: Chrome still alive after ${browser_wait_sec}s — force-killing as wedged orphan."
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

  return 0
}

# Explicit early release. Use this when a long-running script only needs the
# browser for part of its run (e.g. run-twitter-cycle.sh holds the lock for
# Phase 1 scrape, releases during the 5-min T1 sleep + Phase 2a HTTP poll, then
# re-acquires before Phase 2b posting). Without this, sibling pipelines waiting
# on the same profile lock block for the full cycle even when the holder is
# only sleeping.
release_lock() {
  local name="$1"
  local lock_dir="/tmp/social-autoposter-${name}.lock"
  rm -rf "$lock_dir"
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
