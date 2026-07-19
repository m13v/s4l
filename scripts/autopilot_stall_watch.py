#!/usr/bin/env python3
"""Box-side autopilot stall watchdog (fleet backstop).

Fires a Sentry event when the draft autopilot's scheduled-task routines stop
draining the local job queue. The most common cause is the user logging Claude
Desktop into a DIFFERENT account, which leaves the two queue-worker routines
(saps-phase1-query / saps-phase2b-draft) registered only under the OLD account's
session, so nothing claims the jobs the pipeline enqueues. The routines' SKILL.md
files live in a GLOBAL dir and survive the switch, so the old "is the SKILL.md on
disk?" check stayed falsely green while drafting silently died for hours.

The menu bar already surfaces this to the user (title -> "S4L ⚠" + a "Re-arm
autopilot" item). This watcher is the part the user can't see: a fleet-side alert
so a sustained stall pages us even when nobody is looking at the menu bar.

Design mirrors the stall signal in mcp/menubar/s4l_menubar.py (_autopilot_stalled)
and mcp/src/index.ts (autopilotStalled); the thresholds are shared via
schedule_state.py (index.ts mirrors by hand):
  stalled = the autopilot is configured (a complete worker set's SKILL.md
            files present — see WORKER_TASK_SETS)
            AND a draft job has sat unclaimed in pending/ past STALL_SECONDS.
False-positive free: an idle queue (no candidates) has no pending job at all, so
a quiet pipeline never trips this.

Idempotency: only ONE Sentry event per stall episode, and only after the stall
has persisted ALERT_AFTER consecutive checks (so a single slow claim during a
restart doesn't page). State lives in <queue>/stall-watch.json; reset when the
stall clears.

Runs as launchd com.m13v.social-autopilot-stall-watch (StartInterval 120) off the
owned venv (needs sentry-sdk + scripts/ on sys.path via S4L_REPO_DIR). Stdlib
otherwise. Best-effort: never raises into launchd.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time

# SAPS_->S4L_ env mirror (brand rename 2026-07-03): old launchd plists and
# scheduled-task prompts still export SAPS_*; this process reads S4L_*.
import s4l_env  # noqa: E402  (lives next to this file in scripts/)
import identity  # noqa: E402  (lives next to this file in scripts/)

s4l_env.mirror()

# Stall thresholds live in schedule_state.py (HEALTHY_DRAIN_MAX_SECONDS and the
# names derived from it) — retune there, not here. STALL_SECONDS = pending job
# unclaimed; RUNNING_STALL_SECONDS = claimed but wedged (see _oldest_running_age).
from schedule_state import STALL_SECONDS, RUNNING_STALL_SECONDS  # noqa: E402
# Require the stall to persist this many consecutive checks before paging, so a
# transient slow claim (e.g. right after a Claude restart) doesn't false-alarm.
# At StartInterval 120 that is ~6 min of continuous stall.
ALERT_AFTER = 3

# --- Host-sleep awareness (2026-07-13, Sentry S4L-4B) ------------------------
# launchd does NOT fire StartInterval jobs while the host is suspended, so the
# wall-clock gap between consecutive ticks of THIS watchdog is a reliable sleep
# detector: a gap of 3+ intervals means the box was asleep (or powered off),
# not stalled. S4L-4B (Nhat's MacBook Air, 2026-07-11): laptop slept mid-cycle,
# one producer enqueue timeout latched, no cycle ran to clear it, and the page
# fired with pending=0/running=0 and a bogus "account change?" cause while the
# telemetry showed 9 samples in a 2h window (40-min gaps). Laptops that sleep
# between cycles would re-trigger that page forever.
TICK_INTERVAL_SECONDS = 120  # keep in sync with launchd StartInterval
SLEEP_GAP_SECONDS = TICK_INTERVAL_SECONDS * 3  # missed >=3 ticks -> host slept
# For this long after a detected sleep gap, pre-existing latch/age signals are
# treated as sleep-tainted: they must be corroborated by actual queued work
# (pending/running > 0) or the outcome-level batches_stuck backstop to page.
SLEEP_GAP_RECENT_SECONDS = 1800
# A worker-task transcript modified this recently proves the routines are
# firing, which rules out the "orphaned routines / account change" cause.
WORKER_RECENT_SECONDS = 1800

# A box counts as configured when ANY complete worker set has its SKILL.md on
# disk: the universal type-blind worker, its short-lived staging predecessor, or
# the legacy per-type pair. Keep in sync with WORKER_TASK_SETS in
# scripts/schedule_state.py. The old flat all()-over-four-ids check could never
# pass on a universal install (no box has all four dirs), which silently killed
# the fleet watchdog for every post-2026-07-02 install — Karol's 13-hour stall
# on 2026-07-06 paged nobody (found during that investigation).
WORKER_TASK_SETS = (
    ("s4l-worker",),
    ("saps-worker",),
    ("saps-phase1-query", "saps-phase2b-draft"),
)
WORKER_TASK_IDS = ("s4l-worker", "saps-worker", "saps-phase1-query", "saps-phase2b-draft")


def _state_dir() -> str:
    return os.environ.get("S4L_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def _queue_root() -> str:
    return os.path.join(_state_dir(), "claude-queue")


def _watch_state_path() -> str:
    return os.path.join(_queue_root(), "stall-watch.json")


def _claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )


def _autopilot_configured() -> bool:
    """A complete worker set has its SKILL.md on disk = the autopilot was set up
    here (so 'no drafts draining' is a real stall, not just unfinished setup)."""
    base = os.path.join(_claude_config_dir(), "scheduled-tasks")
    return any(
        all(os.path.exists(os.path.join(base, tid, "SKILL.md")) for tid in task_set)
        for task_set in WORKER_TASK_SETS
    )


def _consecutive_timeouts() -> int:
    """The producer's LATCHED stall count: consecutive enqueue->timeout cycles with
    no drain since. Persists across the between-cycle gap, so it's the durable
    signal (the pending file is gone between cycles). Cleared on any successful
    drain. See claude_job.py::drain_status_path."""
    try:
        with open(os.path.join(_queue_root(), "drain-status.json")) as f:
            return int((json.load(f) or {}).get("consecutive_timeouts", 0) or 0)
    except Exception:
        return 0


def _recent_rate_limit(window: int = 1200) -> bool:
    """True if a worker run in the last `window` seconds hit the Claude weekly/usage
    limit. That stall is EXPECTED and auto-resets, so it must NOT page Sentry —
    paging would be pure noise. Reads the ~/.s4l-worker transcript bucket."""
    try:
        now = time.time()
        files = glob.glob(
            os.path.expanduser("~/.claude/projects/*s4l-worker*/*.jsonl")
        )
        recent = sorted(
            (f for f in files if (now - os.path.getmtime(f)) <= window),
            key=os.path.getmtime,
            reverse=True,
        )[:5]
        for f in recent:
            try:
                low = open(f).read().lower()
            except Exception:
                continue
            if "weekly limit" in low or "usage limit" in low or "hit your limit" in low:
                return True
    except Exception:
        pass
    return False


def _worker_ran_recently(window: int = WORKER_RECENT_SECONDS) -> bool:
    """True if any s4l-worker scheduled-task transcript was written in the last
    `window` seconds — direct on-disk proof the worker routines are firing, so
    a stall can NOT be the orphaned-routines / account-change shape. Same
    transcript bucket _recent_rate_limit reads."""
    try:
        now = time.time()
        for f in glob.glob(os.path.expanduser("~/.claude/projects/*s4l-worker*/*.jsonl")):
            try:
                if (now - os.path.getmtime(f)) <= window:
                    return True
            except OSError:
                continue
    except Exception:
        pass
    return False


def _api_reachable(timeout: float = 3.0) -> bool:
    """True if the S4L API host answers HTTP at all. ANY HTTP status counts as
    reachable (a 4xx/5xx still proves the network path is up); only a socket /
    URL error means offline. Used to hold the Sentry page while the box has no
    network — an offline box is a different, self-healing condition, and the
    page would be misleading (plus the event can't ship anyway). The stall
    episode keeps counting, so if it's still stalled when connectivity returns,
    the page fires then."""
    import urllib.error
    import urllib.request

    base = os.environ.get("AUTOPOSTER_API_BASE", "https://s4l.ai").rstrip("/")
    try:
        req = urllib.request.Request(base, method="HEAD")
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # server answered; network is up
    except Exception:
        return False


BATCH_PROGRESSION_MIN_BATCHES = 5
BATCH_PROGRESSED_PHASES = {"phase2b-gen", "phase2b-post"}


def _recent_batches_not_progressing(min_batches: int = BATCH_PROGRESSION_MIN_BATCHES) -> bool:
    """True if the last `min_batches` twitter_batches for THIS install ALL failed
    to reach phase2b-gen (real draft generation) or later. A second, independent
    detection layer: the queue's consecutive_timeouts counter is one level
    removed from the actual product outcome (did a batch progress). During the
    Karol 2026-07-07 investigation the DB showed the stall the whole time — 46
    consecutive batches dying at phase2b-prep — while this watchdog's other
    signals took hours longer to latch. Requires network (via http_api); any
    failure (offline, endpoint missing, not enough batch history yet) returns
    False so this signal can only ever ADD confidence, never false-positive on
    its own from a transient API hiccup.

    NOTE: catches BaseException, not just Exception — scripts/http_api.py's
    _request() deliberately raises SystemExit (not a plain Exception; it
    inherits from BaseException) on a terminal 4xx/5xx, which is correct for
    most one-shot pipeline callers but would silently kill this entire
    best-effort watchdog process if left uncaught here. Caught by testing this
    against a not-yet-deployed endpoint version during development."""
    try:
        repo = os.environ.get("S4L_REPO_DIR")
        if repo:
            scripts_dir = os.path.join(repo, "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
        import http_api  # noqa: E402

        resp = http_api.api_get("/api/v1/twitter-batches", {"list": "1", "limit": str(min_batches)})
        # http_api.api_get returns the RAW envelope {"ok":..,"data":{"batches":[..],
        # "owner_host":..}} — it does NOT unwrap "data". The batches live at
        # resp["data"]["batches"], never at resp["batches"]. Reading the wrong key
        # made this return [] -> len<min -> False on EVERY call, so batches_stuck was
        # hardwired False and the "drafting is not actually happening" stall cause
        # could never be selected (missed Nhat's 07-15 phase2b-prep pileup entirely).
        # Do NOT collapse back to resp.get("batches").
        batches = ((resp or {}).get("data") or {}).get("batches") or []
        if len(batches) < min_batches:
            return False  # not enough history yet to conclude a stall
        return all((b or {}).get("current_phase") not in BATCH_PROGRESSED_PHASES for b in batches)
    except BaseException:
        return False


def _oldest_pending_age() -> float | None:
    """Seconds since the oldest unclaimed pending draft job was written, or None
    if nothing is pending (idle queue). The FAST signal: catches a fresh stall
    before the first full producer timeout has latched."""
    pend_root = os.path.join(_queue_root(), "pending")
    oldest = None
    for sub in glob.glob(os.path.join(pend_root, "*")):
        for jf in glob.glob(os.path.join(sub, "*.json")):
            if jf.endswith(".tmp"):
                continue
            try:
                m = os.path.getmtime(jf)
            except OSError:
                continue
            if oldest is None or m < oldest:
                oldest = m
    if oldest is None:
        return None
    return time.time() - oldest


def _pending_count() -> int:
    """Number of unclaimed pending draft jobs across all types. Cheap: same glob
    shape as _oldest_pending_age, just counted instead of min'd. Feeds the
    queue_health_samples row (see _report_queue_health_sample)."""
    pend_root = os.path.join(_queue_root(), "pending")
    n = 0
    for sub in glob.glob(os.path.join(pend_root, "*")):
        for jf in glob.glob(os.path.join(sub, "*.json")):
            if not jf.endswith(".tmp"):
                n += 1
    return n


def _running_count() -> int:
    """Number of claimed-but-unfinished jobs. See _pending_count."""
    run_root = os.path.join(_queue_root(), "running")
    return sum(1 for jf in glob.glob(os.path.join(run_root, "*.json")) if not jf.endswith(".tmp"))


def _oldest_running_age() -> float | None:
    """Seconds since the oldest CLAIMED-but-unfinished job was written, or None if
    nothing is in flight. A worker claims by moving a job pending/ -> running/ and
    only removes it on result, so a job lingering in running/ far past any real
    drafting turn means the worker claimed it and then wedged mid-run.
    This is the ONLY signal for that case: pending-age is
    silent (the job left pending/) and the producer's drain latch hasn't fired yet
    (it's still inside its own timeout). running/ is flat (see claude_job.py)."""
    run_root = os.path.join(_queue_root(), "running")
    oldest = None
    for jf in glob.glob(os.path.join(run_root, "*.json")):
        if jf.endswith(".tmp"):
            continue
        try:
            m = os.path.getmtime(jf)
        except OSError:
            continue
        if oldest is None or m < oldest:
            oldest = m
    if oldest is None:
        return None
    return time.time() - oldest


def _read_state() -> dict:
    try:
        with open(_watch_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(obj: dict) -> None:
    try:
        os.makedirs(_queue_root(), exist_ok=True)
        tmp = f"{_watch_state_path()}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, _watch_state_path())
    except Exception:
        pass


def _sentry():
    """Import the pipeline's Sentry helper (S4L_REPO_DIR/scripts on path)."""
    repo = os.environ.get("S4L_REPO_DIR")
    if repo:
        scripts = os.path.join(repo, "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
    import sentry_init  # noqa: E402

    return sentry_init


def _report_recovery(total_duration_s: float, paged_duration_s: float | None) -> None:
    """Log a recovery milestone (with duration) once per stall episode that
    actually paged. Previously this had to be hand-reconstructed per incident by
    diffing DB/log timestamps after the fact (see the Karol 2026-07-07
    update-orphan investigation); this makes it a first-class, queryable Sentry
    event so incident severity is trackable fleet-wide over time instead of
    per-investigation. Only fires for episodes that crossed ALERT_AFTER — a
    stall that self-clears before paging is normal jitter, not an incident."""
    try:
        sentry = _sentry()
        sentry.init()
        total_min = total_duration_s / 60.0
        paged_str = f"{paged_duration_s / 60.0:.1f} min" if paged_duration_s is not None else "n/a"
        sentry.capture_message(
            "social-autoposter autopilot recovered: draft jobs draining again "
            f"after {total_min:.1f} min stalled ({paged_str} since it paged).",
            level="info",
            tags={"component": "autopilot", "issue": "stall_recovered"},
            extra={
                "stall_total_duration_seconds": round(total_duration_s, 1),
                "stall_paged_duration_seconds": (
                    round(paged_duration_s, 1) if paged_duration_s is not None else None
                ),
            },
        )
        sentry.flush()
    except Exception:
        sys.stderr.write(f"[stall-watch] recovered after {total_duration_s:.0f}s but Sentry report failed\n")


def _report_queue_health_sample(
    pending: int,
    running: int,
    timeouts: int,
    age: float | None,
    run_age: float | None,
    stalled: bool,
    batches_stuck: bool,
) -> None:
    """Best-effort POST of one queue_health_samples row, on EVERY tick (not
    just while stalled) — a continuous baseline is the point: it's what makes
    "was this box actually healthy at time T" a one-line SQL query instead of
    absence-of-evidence. See migrations/2026-07-07-queue-health-samples.sql.

    Catches BaseException, not just Exception — see _recent_batches_not_progressing
    for why (http_api._request raises SystemExit on a terminal 4xx/5xx, which
    must never be allowed to kill this watchdog process)."""
    try:
        repo = os.environ.get("S4L_REPO_DIR")
        if repo:
            scripts_dir = os.path.join(repo, "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
        import http_api  # noqa: E402

        http_api.api_post(
            "/api/v1/queue-health-samples",
            {
                "pending": pending,
                "running": running,
                "consecutive_timeouts": timeouts,
                "oldest_pending_age_s": int(age) if age is not None else None,
                "oldest_running_age_s": int(run_age) if run_age is not None else None,
                "stalled": stalled,
                "batches_stuck": batches_stuck,
            },
        )
    except BaseException:
        pass


def main() -> int:
    now = time.time()
    st = _read_state()

    # Sleep detection: launchd skips ticks while the host is suspended, so a
    # wall-clock gap between this tick and the previous one of >= 3 intervals
    # means the box slept (see the S4L-4B block by the constants above).
    last_tick_at = st.get("last_tick_at")
    last_sleep_gap_at = st.get("last_sleep_gap_at")
    if last_tick_at is not None:
        tick_gap = now - float(last_tick_at)
        if tick_gap > SLEEP_GAP_SECONDS:
            last_sleep_gap_at = now
            sys.stderr.write(
                f"[stall-watch] tick gap {int(tick_gap)}s (> {SLEEP_GAP_SECONDS}s) — "
                "host was asleep/off; treating stall signals as sleep-tainted for "
                f"{SLEEP_GAP_RECENT_SECONDS}s\n"
            )
    sleep_gap_recent = bool(
        last_sleep_gap_at is not None
        and (now - float(last_sleep_gap_at)) < SLEEP_GAP_RECENT_SECONDS
    )

    pending = _pending_count()
    running = _running_count()
    age = _oldest_pending_age()
    run_age = _oldest_running_age()
    timeouts = _consecutive_timeouts()
    configured = _autopilot_configured()
    # batches_stuck is deliberately computed unconditionally (not short-circuited
    # by the other signals) — it's a genuinely independent, outcome-level
    # backstop, not a cheaper proxy for the same thing. It's what would have
    # caught the Karol 2026-07-07 stall immediately instead of hours later.
    batches_stuck = configured and _recent_batches_not_progressing()
    # Four complementary signals, OR'd; all gated on the autopilot actually being
    # configured here. (1) durable producer drain latch, (2) fast pending-age (job
    # never claimed), (3) running-age (job claimed then wedged mid-run) — (3) is
    # the only one of the first three that catches a worker dying after it picked
    # up the job — (4) batches_stuck, the outcome-level backstop above.
    # Latch sanity gate (S4L-4B): consecutive_timeouts is durable and only
    # clears on a successful drain, so ONE timed-out enqueue keeps a box
    # looking stalled for as long as no cycle runs — even with a provably idle
    # queue (pending=0, running=0). A single timeout only counts when there is
    # actual queued work to corroborate it; an idle-queue latch needs >= 2.
    # NOTE: deliberately stricter than the menubar/_index.ts stall HINT
    # (timeouts >= 1) — a UI hint may over-trigger, a fleet page must not.
    timeouts_signal = timeouts >= 2 or (timeouts >= 1 and (pending > 0 or running > 0))
    stalled = configured and (
        timeouts_signal
        or (age is not None and age > STALL_SECONDS)
        or (run_age is not None and run_age > RUNNING_STALL_SECONDS)
        or batches_stuck
    )
    # A rate-limit stall is expected and self-heals at the quota reset — never page
    # for it (and re-arm can't fix it). Treat it as "not an actionable stall" so the
    # episode resets and a LATER real stall (orphaned routines) still alerts.
    if stalled and _recent_rate_limit():
        stalled = False
    # Sleep suppression: right after a wake, the latch (and any pre-nap ages)
    # predate the sleep, not a worker failure. Require corroboration by real
    # queued work or the outcome-level backstop before calling it a stall.
    if stalled and sleep_gap_recent and not (pending > 0 or running > 0 or batches_stuck):
        stalled = False

    # Record this tick regardless of outcome — see _report_queue_health_sample.
    _report_queue_health_sample(
        pending, running, timeouts, age, run_age, stalled, batches_stuck
    )

    consecutive = int(st.get("consecutive", 0))
    alerted = bool(st.get("alerted", False))
    # first_seen_at: first check this episode looked stalled at all (predates
    # paging by ALERT_AFTER checks). alerted_at: the single moment we paged.
    # Both are stamped once and carried forward untouched for the rest of the
    # episode — NOT refreshed every check — so they measure the episode's
    # start, not "last time we saw it stalled".
    first_seen_at = st.get("first_seen_at")
    alerted_at = st.get("alerted_at")

    # Tick bookkeeping persisted on EVERY exit path — the sleep detector needs
    # an unbroken last_tick_at chain even (especially) while healthy.
    tick_state = {"last_tick_at": now, "last_sleep_gap_at": last_sleep_gap_at}

    if not stalled:
        # Recovered (or never stalled) -> reset the episode so the next stall pages.
        if consecutive or alerted:
            if alerted and first_seen_at:
                total_duration = time.time() - float(first_seen_at)
                paged_duration = (time.time() - float(alerted_at)) if alerted_at else None
                _report_recovery(total_duration, paged_duration)
        _write_state({**tick_state, "consecutive": 0, "alerted": False})
        return 0

    consecutive += 1
    if first_seen_at is None:
        first_seen_at = time.time()
    age_str = f"{int(age)}s" if age is not None else "n/a (between cycles)"
    run_age_str = f"{int(run_age)}s" if run_age is not None else "n/a (none in flight)"
    # Distinguish the shapes so the alert points at the right cause: a claimed-
    # but-wedged job (running-age) is a mid-run worker death, batches_stuck is
    # the outcome-level backstop firing independently of the queue signals, and
    # the fallback is the classic orphaned-routine case.
    wedged_inflight = run_age is not None and run_age > RUNNING_STALL_SECONDS
    if consecutive >= ALERT_AFTER and not alerted:
        if not _api_reachable():
            # No network: an offline box is a different, self-healing condition,
            # the page would misattribute it to the worker (and the event can't
            # ship anyway). Keep the episode counting so a stall that survives
            # the outage still pages the tick connectivity returns.
            sys.stderr.write(
                f"[stall-watch] stall persisted {consecutive} checks but the API is "
                "unreachable (box offline?); deferring the page until network returns\n"
            )
        else:
            try:
                sentry = _sentry()
                sentry.init()
                if wedged_inflight:
                    cause = "a worker claimed a draft job and then died mid-run"
                    stall_shape = "inflight_wedged"
                elif batches_stuck:
                    cause = (
                        f"last {BATCH_PROGRESSION_MIN_BATCHES} twitter_batches all failed to "
                        "reach phase2b-gen — drafting is not actually happening even if the "
                        "queue-level counters look ambiguous"
                    )
                    stall_shape = "batches_not_progressing"
                elif _worker_ran_recently():
                    # On-disk transcripts prove the worker routines ARE firing, so
                    # this cannot be the orphaned-routines shape (S4L-4B paged with
                    # exactly that bogus cause while the registry sample showed the
                    # task had run seconds earlier).
                    cause = (
                        "producer timeout latch, but worker-task transcripts show recent "
                        "runs — routines are firing; suspect a transient enqueue timeout"
                        + (" or a host sleep gap" if sleep_gap_recent else "")
                        + ", NOT orphaned routines"
                    )
                    stall_shape = "latch_worker_alive"
                else:
                    cause = "scheduled-task routines likely orphaned — Claude Desktop account change?"
                    stall_shape = "not_draining"
                # Our own staging/QA/dev boxes (identity.is_internal_install) get set
                # up and rebuilt with nobody actively feeding the queue, which looks
                # identical to a real stall on the signals above. Downgrade those to
                # warning instead of error so they don't page as a customer incident
                # (the digest only scans error/fatal) while still leaving a Sentry
                # record if we ever need to look one up by hand.
                is_internal = identity.is_internal_install()
                sentry.capture_message(
                    "social-autoposter autopilot stalled: draft jobs are not being "
                    f"drained ({cause}). producer consecutive timeouts={timeouts}, "
                    f"oldest pending job age={age_str}, oldest in-flight (running) job "
                    f"age={run_age_str}, sustained {consecutive} checks.",
                    level=("warning" if is_internal else "error"),
                    tags={
                        "component": "autopilot",
                        "issue": "stall",
                        "stall_shape": stall_shape,
                        "consecutive_timeouts": str(timeouts),
                        "oldest_pending_age_s": str(int(age)) if age is not None else "",
                        "oldest_running_age_s": str(int(run_age)) if run_age is not None else "",
                        "batches_stuck": str(batches_stuck),
                        "internal_install": str(is_internal),
                        "pending": str(pending),
                        "running": str(running),
                        "recent_sleep_gap": str(sleep_gap_recent),
                    },
                )
                sentry.flush()
            except Exception:
                # No Sentry (helper/SDK missing) -> at least leave a local breadcrumb.
                sys.stderr.write(
                    f"[stall-watch] autopilot stalled (timeouts={timeouts}, "
                    f"pending_age={age_str}, running_age={run_age_str}) but Sentry report failed\n"
                )
            alerted = True
            alerted_at = time.time()

    _write_state({
        **tick_state,
        "consecutive": consecutive,
        "alerted": alerted,
        "first_seen_at": first_seen_at,
        "alerted_at": alerted_at,
        "at": time.time(),
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never let launchd see a non-zero/crash loop
        sys.stderr.write(f"[stall-watch] unexpected error: {e}\n")
        sys.exit(0)
