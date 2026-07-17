#!/usr/bin/env python3
"""Regression test: the autopilot stall watchdog's batch-progression check must
read the last N twitter_batches from the API envelope's data.batches, NOT the
(nonexistent) top-level resp["batches"].

Background (Nhat ae18af69, 07-15): http_api.api_get returns the RAW envelope
{"ok":..,"data":{"batches":[..],"owner_host":..}} and does not unwrap "data".
_recent_batches_not_progressing() read resp.get("batches") -> always None ->
batches=[] -> len<min -> returned False on EVERY call. batches_stuck was thus
hardwired False and the "drafting is not actually happening" stall cause could
never be selected, so the watchdog mislabeled a real 30h phase2b-prep pileup as
"orphaned routines / account change?". This locks in the correct key + the
True/False logic so it cannot silently regress again.

Run:  python3 scripts/test_stall_watch_batch_progression.py
Exit 0 = all pass; non-zero with FAIL lines otherwise.
"""
from __future__ import annotations

import importlib
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
# Source-tree test: pin S4L_REPO_DIR to THIS checkout so the function resolves
# http_api from here, not an inherited installed-package path
# (see gotcha_s4l_repo_dir_env_inherited_installed_package).
os.environ["S4L_REPO_DIR"] = REPO
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

asw = importlib.import_module("autopilot_stall_watch")
MIN = asw.BATCH_PROGRESSION_MIN_BATCHES

failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(name)


def _fake_api(canned):
    """Swap http_api for one whose api_get returns `canned` (or raises it, if a
    BaseException). The function under test does `import http_api` lazily, so a
    module planted in sys.modules wins."""
    fake = types.ModuleType("http_api")

    def api_get(path, query=None, ok_on_404=False):
        if isinstance(canned, BaseException):
            raise canned
        return canned

    fake.api_get = api_get
    sys.modules["http_api"] = fake


def envelope(phases):
    """The real /api/v1/twitter-batches shape: batches nested under data."""
    return {"ok": True, "data": {"owner_host": "test-host",
                                 "batches": [{"current_phase": p} for p in phases]}}


# 1. Last N batches ALL stuck at prep -> stall detected (the case that was missed).
_fake_api(envelope(["phase2b-prep"] * MIN))
check("all phase2b-prep -> batches_not_progressing True",
      asw._recent_batches_not_progressing(), True)

# 2. One of the last N reached real drafting -> not a stall.
_fake_api(envelope(["phase2b-prep"] * (MIN - 1) + ["phase2b-gen"]))
check("one phase2b-gen among last N -> False",
      asw._recent_batches_not_progressing(), False)

# 3. Fewer than N batches -> inconclusive, never page on thin history.
_fake_api(envelope(["phase2b-prep"] * (MIN - 1)))
check("fewer than MIN batches -> False",
      asw._recent_batches_not_progressing(), False)

# 4. THE REGRESSION GUARD: batches present ONLY at the wrong top-level key must be
#    ignored. Correct code reads data.batches -> sees [] -> False. If someone
#    "simplifies" back to resp.get("batches"), this flips to True and fails.
_fake_api({"ok": True, "batches": [{"current_phase": "phase2b-prep"}] * MIN})
check("top-level resp['batches'] ignored (must read data.batches)",
      asw._recent_batches_not_progressing(), False)

# 5. A terminal API failure (http_api raises SystemExit, a BaseException) must be
#    swallowed to False, never crash this best-effort watchdog.
_fake_api(SystemExit("terminal 5xx"))
check("api SystemExit swallowed -> False",
      asw._recent_batches_not_progressing(), False)

if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("\nALL PASS")
