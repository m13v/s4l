#!/usr/bin/env python3
"""Backfill missing run_monitor.log summary lines from per-run reddit-search logs.

Why: SIGTERM hits run-reddit-search.sh between the Post phase (where the
post_reddit.py CDP step has already minted a row in `posts` and called
log_post()) and the final `python3 scripts/log_run.py ...` summary write at
the bottom of the script. The DB therefore knows about the post; the
operator dashboard does not, because it only reads run_monitor.log.

This script reconstructs the missing summary lines by scanning
skill/logs/run-reddit-search-*.log files, parsing the per-iteration
`[post_reddit] phase=post project=X posted=N failed=N` lines, and
appending a post_reddit summary line for any run whose log file does NOT
already have a corresponding entry within ~5 min of the file's mtime.

The synthesized line uses the file's mtime as the completion timestamp,
counts posted/failed/skipped/salvaged across all iterations seen, and
otherwise mimics scripts/log_run.py output exactly so bin/server.js's
RUN_LINE_RE parses it.

Usage:
    python3 scripts/backfill_run_monitor.py            # dry-run (default)
    python3 scripts/backfill_run_monitor.py --apply    # actually append
    python3 scripts/backfill_run_monitor.py --since 4h # only consider recent

The --since window applies to the per-run log file's mtime (so an old
backfill won't touch ancient runs by accident). Default 24h.
"""
from __future__ import annotations  # PEP 604 unions (str | None) for Python 3.9 launchd

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_DIR = Path(os.path.expanduser("~/social-autoposter"))
LOG_DIR = REPO_DIR / "skill" / "logs"
RUN_MONITOR = LOG_DIR / "run_monitor.log"
RUN_LOG_GLOB = "run-reddit-search-*.log"
RUN_LOG_NAME_RE = re.compile(
    r"run-reddit-search-(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})\.log$"
)
PHASE_ROLLUP_RE = re.compile(
    r"\[post_reddit\] phase=post .*? posted=(\d+) failed=(\d+)"
)
SALVAGED_ITER_RE = re.compile(r"\[post_reddit\] SALVAGED candidate")
SKIPPED_ITER_RES = [
    re.compile(r"Ripen phase: 0 survivors"),
    re.compile(r"Discover phase: no candidates found"),
    re.compile(r"Draft phase: no drafted decisions"),
]
DRAFT_FAIL_RE = re.compile(r"Draft phase: Claude failed")
DISCOVER_FAIL_RE = re.compile(r"Discover phase: Claude failed")
CDP_FAIL_RE = re.compile(r"\[post_reddit\] CDP FAILED: ([a-z_]+)")
PHASE0_SALVAGED_RE = re.compile(r"salvaged=(\d+)")
RIPEN_INPUT_RE = re.compile(r"\[ripen\] summary input=(\d+) survivors=(\d+)")

CDP_REASON_MAP = {
    "thread_locked": "reddit_locked",
    "thread_archived": "reddit_archived",
    "thread_not_found": "reddit_deleted",
    "account_blocked_in_sub": "account_blocked",
    "not_logged_in": "reddit_logged_out",
    "all_attempts_failed": "cdp_no_response",
    "comment_box_not_found": "comment_box_missing",
}

# Existing run_monitor.log line shape we need to detect. Same anchor used by
# bin/server.js's RUN_LINE_RE, scoped to post_reddit.
EXISTING_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s*\|\s*post_reddit\s*\|"
)


def parse_window(since: str) -> timedelta:
    """Parse a value like '4h', '90m', '2d' into a timedelta."""
    m = re.fullmatch(r"(\d+)([hmd])", since.strip())
    if not m:
        raise ValueError(f"--since must look like '24h', '90m', '2d'; got {since!r}")
    n, unit = int(m.group(1)), m.group(2)
    return {"h": timedelta(hours=n), "m": timedelta(minutes=n), "d": timedelta(days=n)}[unit]


def existing_post_reddit_timestamps() -> list[datetime]:
    """Pull every post_reddit timestamp already in run_monitor.log."""
    if not RUN_MONITOR.exists():
        return []
    out: list[datetime] = []
    with RUN_MONITOR.open("r", errors="replace") as f:
        for line in f:
            m = EXISTING_LINE_RE.match(line)
            if not m:
                continue
            try:
                out.append(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                continue
    return out


def parse_run_log(path: Path) -> dict:
    """Walk one per-run log and accumulate counters the way the shell does."""
    posted = failed = skipped = 0
    salvaged_iters = 0  # iterations that replayed a salvaged row
    candidates_seen = 0  # mirrors TOTAL_CANDIDATES in the shell
    failure_reasons: dict[str, int] = {}

    text = path.read_text(errors="replace")

    for line in text.splitlines():
        m = PHASE_ROLLUP_RE.search(line)
        if m:
            posted += int(m.group(1))
            failed += int(m.group(2))
            continue
        if DRAFT_FAIL_RE.search(line) or DISCOVER_FAIL_RE.search(line):
            failed += 1
        for sk in SKIPPED_ITER_RES:
            if sk.search(line):
                skipped += 1
                break
        if SALVAGED_ITER_RE.search(line):
            salvaged_iters += 1
            candidates_seen += 1
        m = CDP_FAIL_RE.search(line)
        if m:
            key = CDP_REASON_MAP.get(m.group(1), f"cdp_{m.group(1)}")
            failure_reasons[key] = failure_reasons.get(key, 0) + 1

    return {
        "posted": posted,
        "failed": failed,
        "skipped": skipped,
        "salvaged": salvaged_iters,
        "candidates": candidates_seen,
        "failure_reasons": failure_reasons,
    }


def synth_line(ts: datetime, counters: dict, elapsed_s: int) -> str:
    """Format a run_monitor.log line that matches scripts/log_run.py output."""
    parts = [
        f"posted={counters['posted']}",
        f"skipped={counters['skipped']}",
        f"failed={counters['failed']}",
    ]
    if counters["salvaged"]:
        parts.append(f"salvaged={counters['salvaged']}")
    discover_segments = []
    if counters["candidates"]:
        discover_segments.append(f"candidates={counters['candidates']}")
    discover_str = (" discover=" + ",".join(discover_segments)) if discover_segments else ""
    fr = counters["failure_reasons"]
    fr_str = ""
    if fr:
        # Stable ordering: by count desc then key asc, like the dashboard sort.
        items = sorted(fr.items(), key=lambda kv: (-kv[1], kv[0]))
        fr_str = " failure_reasons=" + ",".join(f"{k}:{v}" for k, v in items)
    head = " ".join(parts)
    return (
        f"{ts.strftime('%Y-%m-%dT%H:%M:%S')} | post_reddit | "
        f"{head}{discover_str} cost=$0.00 elapsed={elapsed_s}s"
        f"{fr_str}  # backfilled"
    )


def synth_warning(ts: datetime, counters: dict) -> str | None:
    """Mirror log_run.py's silent-failure warning emit."""
    if counters["posted"] == 0 and counters["failed"] > 0:
        return (
            f"{ts.strftime('%Y-%m-%dT%H:%M:%S')} | "
            f"WARNING: post_reddit posted=0 failed={counters['failed']} "
            f"-- possible silent failure  # backfilled"
        )
    return None


def discover_candidates(window: timedelta) -> list[Path]:
    cutoff = time.time() - window.total_seconds()
    out = []
    for p in sorted(LOG_DIR.glob(RUN_LOG_GLOB)):
        if not RUN_LOG_NAME_RE.search(p.name):
            continue
        try:
            if p.stat().st_mtime < cutoff:
                continue
        except FileNotFoundError:
            continue
        out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually append synthesized lines to run_monitor.log "
                         "(default: dry-run, print only).")
    ap.add_argument("--since", default="24h",
                    help="Only consider per-run logs whose mtime is within this "
                         "window. Format: 4h | 90m | 2d. Default: 24h.")
    ap.add_argument("--match-window-sec", type=int, default=90,
                    help="If an existing post_reddit run_monitor line lies within "
                         "this many seconds of a per-run log's mtime, treat the "
                         "run as already logged and skip it. Default: 90 (the "
                         "summary write happens ~1-2s after the per-run log's "
                         "last write, so the window only needs to absorb "
                         "python startup variance; making it any wider risks "
                         "collapsing two adjacent runs into one match).")
    args = ap.parse_args()

    try:
        window = parse_window(args.since)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    existing = existing_post_reddit_timestamps()
    candidates = discover_candidates(window)
    if not candidates:
        print(f"No per-run logs in last {args.since}; nothing to do.")
        return

    to_append: list[str] = []
    summary_rows = []
    for path in candidates:
        m = RUN_LOG_NAME_RE.search(path.name)
        if not m:
            continue
        try:
            start_ts = datetime.strptime(
                f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                "%Y-%m-%d %H:%M:%S",
            )
        except ValueError:
            continue
        end_ts = datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0)
        elapsed_s = max(0, int((end_ts - start_ts).total_seconds()))

        # Already-logged check: any existing post_reddit timestamp within the
        # match window of this run's end-time means scripts/log_run.py did
        # write the line, no backfill needed.
        already_logged = any(
            abs((end_ts - et).total_seconds()) <= args.match_window_sec
            for et in existing
        )
        if already_logged:
            summary_rows.append((path.name, "skip-already-logged", 0, 0))
            continue

        counters = parse_run_log(path)
        if (counters["posted"] == 0 and counters["failed"] == 0
                and counters["skipped"] == 0 and counters["salvaged"] == 0):
            # Nothing meaningful to backfill (e.g. SIGTERM in the very first
            # phase before any iteration registered anything). Skip.
            summary_rows.append((path.name, "skip-no-counters", 0, 0))
            continue

        line = synth_line(end_ts, counters, elapsed_s)
        warn = synth_warning(end_ts, counters)
        to_append.append(line)
        if warn:
            to_append.append(warn)
        summary_rows.append(
            (path.name, "BACKFILL", counters["posted"], counters["failed"])
        )

    print(f"Window: last {args.since}.  Logs scanned: {len(candidates)}.")
    print(f"{'file':50s}  action  posted  failed")
    for name, action, posted, failed in summary_rows:
        print(f"{name:50s}  {action:20s}  {posted:>6d}  {failed:>6d}")

    if not to_append:
        print("\nNothing to backfill. run_monitor.log is already in sync.")
        return

    print("\nLines to append to run_monitor.log:")
    for ln in to_append:
        print("  " + ln)

    if not args.apply:
        print("\n(dry-run; pass --apply to write)")
        return

    # The existing log_run.py uses `with open(...,"a")` so we mirror that.
    # We don't try to insert in chronological order; the dashboard parses
    # line-by-line and sorts by timestamp anyway.
    with RUN_MONITOR.open("a") as f:
        for ln in to_append:
            f.write(ln + "\n")
    print(f"\nAppended {len(to_append)} line(s) to {RUN_MONITOR}.")


if __name__ == "__main__":
    main()
