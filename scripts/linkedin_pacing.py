#!/usr/bin/env python3
"""LinkedIn action pacing gate.

Single source of truth for "is it safe to post another LinkedIn comment right
now, or should we wait / stop for today". Every LinkedIn write path calls
`check` immediately BEFORE each comment and honours the answer.

Why this exists
---------------
Until 2026-07-29 the LinkedIn write path had NO pacing control at all: no
minimum gap, no jitter, no hourly cap, no per-day cap. The human-looking cadence
on healthy days was an accident of queue availability and lock contention, not a
control. Observed inter-action gaps routinely bottomed out at 0-3 SECONDS.

TWO ACTION STREAMS - counting only one is the trap
--------------------------------------------------
LinkedIn writes land in two different tables and BOTH must be counted:

    replies (replied_at)  engage-linkedin.sh - replying to comments on our posts
    posts   (posted_at)   run-linkedin.sh    - commenting on others' posts,
                                               via log_post.py, status='active'

The first cut of this gate counted only `replies` and therefore saw ~25% of
reality: it read 2026-07-20 as 23 actions when the true figure was 90, and read
2026-07-19 as ZERO when it was 73. Any future edit that narrows this query to a
single table silently disables most of the gate. Only rows with status='active'
count as real writes; log_post.py also records rejected candidates.

What the data does and does NOT support
---------------------------------------
Combined-stream daily figures, 2026-07-06..07-20:

    n/day      65 .. 96 actions
    per hour   2.9 .. 4.4
    CV         0.32 .. 1.26   on EVERY day, healthy or not

An earlier draft of this file claimed CV (sd/mean of inter-action gaps)
separated logout days from healthy ones at ~1.0. That was an artifact of the
replies-only subsample. On the full stream it does NOT separate: 2026-07-14 ran
96 actions at CV 0.72 with no logout, and 2026-07-20 ran 90 at CV 0.78 and was
killed. We therefore do NOT rely on CV as a predictor; it is retained only at a
very low floor to catch a true metronome, which remains bad regardless of
whether it predicts a logout.

Stated plainly: we have NO statistic that reliably separates logout days from
healthy days. What we do know is (a) this account is flagged, in LinkedIn's own
words ("temporary restriction for automated activity" twice, "automation tool
detected" once), (b) every active stretch so far has ended in a session kill,
6 for 6, and (c) 65-96 automated actions a day with sub-second minimum gaps is
indefensible in absolute terms whatever the trigger turns out to be. The
ceilings below are therefore a deliberate ~70% volume cut chosen on judgment,
NOT a proven safe operating point. Revise them as evidence accumulates.

Ceilings (env-overridable, see CONFIG below):
    min gap            120s   hard floor; observed minimum was 0-3s
    per rolling 1h     4      observed average was 2.9-4.4/h with bursts
    per rolling 24h    25     observed 65-96/day
    per rolling 72h    60     observed ~230/3d
    CV floor           0.25   true-metronome catch only; NOT a logout predictor
    min daily spread   4h     actions must not bunch into one short window

CLI
---
    linkedin_pacing.py check            # exit 0 = post now, 75 = wait, 78 = stop
    linkedin_pacing.py check --json     # machine-readable decision
    linkedin_pacing.py status           # current counters, always exit 0

Exit codes are distinct so a shell caller can tell "wait" from "stop for now":
    0  -> allowed, post now
    75 -> not yet; sleep `wait_seconds` then re-check (EX_TEMPFAIL-ish)
    78 -> a ceiling is blown; do not post at all this run (matches the repo's
          existing rc=78 "skip this fire" convention)

FAIL-CLOSED: if the database cannot be read we deny (rc=78). The account is
already flagged; posting blind is worse than skipping a cycle. This mirrors the
existing "db_unavailable -> script already fails closed" rule in run-linkedin.sh.
"""

import argparse
import json
import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone


# --------------------------------------------------------------------------
# CONFIG - every value env-overridable so we can tune without editing a frozen
# file, and so tests can drive it against throwaway numbers.
# --------------------------------------------------------------------------
def _envf(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _envi(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


MIN_GAP_S      = _envi("LI_PACE_MIN_GAP_S", 120)
CV_FLOOR       = _envf("LI_PACE_CV_FLOOR", 0.25)
CV_WINDOW      = _envi("LI_PACE_CV_WINDOW", 10)
MAX_PER_1H     = _envi("LI_PACE_MAX_1H", 4)
MAX_PER_24H    = _envi("LI_PACE_MAX_24H", 25)
MAX_PER_72H    = _envi("LI_PACE_MAX_72H", 60)
MIN_SPREAD_S   = _envi("LI_PACE_MIN_SPREAD_S", 4 * 3600)
# When CV is too low we do not just wait the floor, we inject a long randomized
# pause to actively break the metronome and pull CV back up.
CV_PAUSE_MIN_S = _envi("LI_PACE_CV_PAUSE_MIN_S", 900)
CV_PAUSE_MAX_S = _envi("LI_PACE_CV_PAUSE_MAX_S", 3600)

PLATFORM = "linkedin"

RC_ALLOW = 0
RC_WAIT = 75
RC_STOP = 78


def _database_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url.strip().strip('"').strip("'")
    env_path = os.path.expanduser("~/social-autoposter/.env")
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _recent_timestamps(hours=72):
    """Every LinkedIn write action in the last `hours`, UTC, ascending.

    UNION of BOTH write streams. See the module docstring: counting only one
    table silently disables most of this gate.

    Raises on any failure so callers can fail closed.
    """
    import psycopg2  # imported lazily so `--help` works without the driver

    url = _database_url()
    if not url:
        raise RuntimeError("DATABASE_URL not resolvable")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    conn = psycopg2.connect(url, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select ts from ("
                "  select posted_at as ts from posts"
                "   where platform = %s and status = 'active'"
                "     and posted_at is not null and posted_at >= %s"
                "  union all"
                "  select replied_at as ts from replies"
                "   where platform = %s"
                "     and replied_at is not null and replied_at >= %s"
                ") a order by ts",
                (PLATFORM, since, PLATFORM, since),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _gaps(ts):
    return [
        (b - a).total_seconds()
        for a, b in zip(ts, ts[1:])
        if (b - a).total_seconds() >= 0
    ]


def _cv(gaps):
    """Coefficient of variation. None when undefined (need >= 2 gaps)."""
    if len(gaps) < 2:
        return None
    mean = statistics.fmean(gaps)
    if mean <= 0:
        return 0.0
    return statistics.stdev(gaps) / mean


def evaluate(now=None, timestamps=None):
    """Return a decision dict. Pure given its inputs, so it is testable."""
    now = now or datetime.now(timezone.utc)
    ts = timestamps if timestamps is not None else _recent_timestamps()

    in_1h = [t for t in ts if t >= now - timedelta(hours=1)]
    in_24h = [t for t in ts if t >= now - timedelta(hours=24)]
    in_72h = ts

    counters = {
        "count_1h": len(in_1h),
        "count_24h": len(in_24h),
        "count_72h": len(in_72h),
        "max_1h": MAX_PER_1H,
        "max_24h": MAX_PER_24H,
        "max_72h": MAX_PER_72H,
    }

    def decision(action, rc, reason, wait_seconds=0, **extra):
        d = {
            "action": action,
            "rc": rc,
            "reason": reason,
            "wait_seconds": int(wait_seconds),
            "checked_at": now.isoformat(),
        }
        d.update(counters)
        d.update(extra)
        return d

    # ---- hard ceilings first: these mean "stop", not "wait a bit" ----------
    if len(in_24h) >= MAX_PER_24H:
        return decision("stop", RC_STOP,
                        f"24h cap reached ({len(in_24h)}/{MAX_PER_24H})")
    if len(in_72h) >= MAX_PER_72H:
        return decision("stop", RC_STOP,
                        f"72h cap reached ({len(in_72h)}/{MAX_PER_72H})")

    # ---- rolling hour: a wait, since it clears on its own ------------------
    if len(in_1h) >= MAX_PER_1H:
        oldest = min(in_1h)
        wait = (oldest + timedelta(hours=1) - now).total_seconds()
        return decision("wait", RC_WAIT,
                        f"1h cap reached ({len(in_1h)}/{MAX_PER_1H})",
                        max(wait, 60))

    # ---- minimum gap ------------------------------------------------------
    if ts:
        since_last = (now - max(ts)).total_seconds()
        if since_last < MIN_GAP_S:
            return decision("wait", RC_WAIT,
                            f"min gap not met ({int(since_last)}s < {MIN_GAP_S}s)",
                            MIN_GAP_S - since_last,
                            seconds_since_last=int(since_last))

    # ---- cadence regularity (the 2026-07-20 fingerprint) ------------------
    gaps = _gaps(ts)[-CV_WINDOW:]
    cv = _cv(gaps)
    if cv is not None and cv < CV_FLOOR:
        # Metronomic. Break it with a long randomized pause rather than the floor.
        pause = random.uniform(CV_PAUSE_MIN_S, CV_PAUSE_MAX_S)
        return decision("wait", RC_WAIT,
                        f"cadence too regular (CV={cv:.2f} < {CV_FLOOR:.2f} "
                        f"over last {len(gaps)} gaps)",
                        pause, cv=round(cv, 3))

    # ---- daily spread: do not bunch the day's comments into one window ----
    if len(in_24h) >= 3:
        spread = (max(in_24h) - min(in_24h)).total_seconds()
        # Only bites once we are a meaningful way into the daily allowance.
        if spread < MIN_SPREAD_S and len(in_24h) >= max(3, MAX_PER_24H // 2):
            pause = random.uniform(CV_PAUSE_MIN_S, CV_PAUSE_MAX_S)
            return decision("wait", RC_WAIT,
                            f"daily spread too tight ({int(spread/60)}min over "
                            f"{len(in_24h)} comments; want >= {MIN_SPREAD_S//3600}h)",
                            pause, spread_minutes=int(spread / 60))

    return decision("allow", RC_ALLOW, "within all pacing limits",
                    cv=(round(cv, 3) if cv is not None else None))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["check", "status"])
    ap.add_argument("--json", action="store_true",
                    help="emit the full decision as JSON")
    args = ap.parse_args()

    try:
        d = evaluate()
    except Exception as exc:  # noqa: BLE001 - fail closed on ANY read failure
        d = {
            "action": "stop",
            "rc": RC_STOP,
            "reason": f"pacing state unreadable, failing closed: {exc}",
            "wait_seconds": 0,
        }

    if args.command == "status":
        print(json.dumps(d, indent=2))
        return 0

    if args.json:
        print(json.dumps(d))
    else:
        print(f"{d['action'].upper()}: {d['reason']}"
              + (f" (wait {d['wait_seconds']}s)" if d["wait_seconds"] else ""))
    return d["rc"]


if __name__ == "__main__":
    sys.exit(main())
