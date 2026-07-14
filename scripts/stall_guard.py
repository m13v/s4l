#!/usr/bin/env python3
"""Shared zero-progress deadline for browser-touching pipeline steps.

Any step that relays CDP to the harness Chrome (scan queries, thread media
capture, ...) can hang FOREVER against a half-wedged browser or a dead
renderer: the call never returns, the step holds the browser lock, and the
cycle-entry health checks that could heal Chrome can't run because the next
cycle can't start. 2026-07-13: a scan hung 23 min at zero queries; the same
night media capture hung 34+ min at candidate 24/92 and only the watchdog's
90-min lock-liveness net was ever going to reap it.

One implementation, used by every step (twitter_scan.py Phase-1 queries,
twitter_browser.py media capture). Usage:

    from stall_guard import stall_guard
    with stall_guard("thread_media", url):   # default deadline 240s
        <one bounded unit of browser work>

If the guarded block does not finish inside the deadline the guard:
  1. prints a loud machine-greppable marker to stderr
     ([stall_guard] STALL_ABORT step=<step> ...)
  2. stamps the port's wedge strike file, so the next cycle's two-strike
     gate in skill/lib/twitter-backend.sh converges to a kill+relaunch if
     Chrome is genuinely sick
  3. hard-exits the process with EX_TEMPFAIL (75) so the cycle fails fast
     and releases the browser lock + cycle slot.

A completed block cancels its timer; a healthy pipeline never notices the
guard exists. Deadline override: S4L_BROWSER_OP_DEADLINE_S (seconds).
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import sys
import threading

DEFAULT_DEADLINE_S = float(os.environ.get("S4L_BROWSER_OP_DEADLINE_S", "240"))


def _strike_file(port: int) -> str:
    # Must match the two-strike gate in skill/lib/twitter-backend.sh.
    return f"/tmp/s4l_cdp_wedge_strike_{port}"


@contextlib.contextmanager
def stall_guard(step: str, detail: str = "", deadline_s: float | None = None, port: int = 9555):
    limit = DEFAULT_DEADLINE_S if deadline_s is None else float(deadline_s)

    def _fire():
        sys.stderr.write(
            f"[stall_guard] STALL_ABORT step={step} detail={detail!r} "
            f"deadline={limit:.0f}s — browser call hung (wedged Chrome or dead "
            "renderer); stamping wedge strike and aborting so the lock frees\n"
        )
        sys.stderr.flush()
        try:
            pathlib.Path(_strike_file(port)).touch()
        except OSError:
            pass
        os._exit(75)  # EX_TEMPFAIL

    t = threading.Timer(limit, _fire)
    t.daemon = True
    t.start()
    try:
        yield
    finally:
        t.cancel()
