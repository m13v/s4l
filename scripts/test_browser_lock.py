#!/usr/bin/env python3
"""Regression test for the browser session-lock fix (2026-06-16).

Covers BOTH twitter_browser.py and linkedin_browser.py (same fix, ported). With
NO real browser, it exercises the three session-lock defects:
  (a) dead python:PID holders must be reclaimed immediately (not after 300s)
  (b) [shell-side, verified separately] no `rm -f` of the lockfile in pipelines
  (c) lock acquisition must be atomic (two acquirers cannot both win)

Run:
    /opt/homebrew/bin/python3 scripts/test_browser_lock.py
Exit 0 = all pass; non-zero with FAIL lines otherwise.

Canonical "did the fix survive / still work?" check. See
docs/twitter_browser_lock.md for the full verification playbook.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (HERE, os.path.join(HERE, "..")):
    if os.path.exists(os.path.join(_cand, "twitter_browser.py")):
        sys.path.insert(0, _cand)
        break
import twitter_browser  # noqa: E402
import linkedin_browser  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def write_lock(mod, holder, ts):
    with open(mod.LOCK_FILE, "w") as f:
        json.dump({"session_id": holder, "timestamp": ts}, f)


def read_holder(mod):
    with open(mod.LOCK_FILE) as f:
        return json.load(f)["session_id"]


def reset(mod):
    mod._LOCK_SESSION_ID = f"python:{os.getpid()}"
    mod._LOCK_INHERITED = False
    try:
        os.remove(mod.LOCK_FILE)
    except OSError:
        pass


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    try:
        os.kill(p.pid, 0)
        return None
    except ProcessLookupError:
        return p.pid


def run_suite(mod, giveup_substrings):
    P = mod.__name__  # prefix for check names
    tmpdir = tempfile.mkdtemp(prefix=f"brlock-{P}-")
    mod.LOCK_FILE = os.path.join(tmpdir, "lock.json")
    mod.LOCK_WAIT_MAX = 2
    mod.LOCK_POLL_INTERVAL = 0.2
    print(f"\n# {P}: LOCK_FILE={mod.LOCK_FILE}  LOCK_WAIT_MAX={mod.LOCK_WAIT_MAX}")

    check(f"{P}.fix_present: _is_python_holder_alive", hasattr(mod, "_is_python_holder_alive"))
    check(f"{P}.fix_present: _try_take_lock (atomic)", hasattr(mod, "_try_take_lock"))

    # (c) atomic take
    reset(mod)
    first = mod._try_take_lock()
    second = mod._try_take_lock()
    check(f"{P}.c.atomic_take: first wins, second loses", first is True and second is False,
          f"first={first} second={second}")
    check(f"{P}.c.atomic_take: file holds our id", read_holder(mod) == mod._LOCK_SESSION_ID)

    # (a) dead python:PID holder reclaimed immediately
    reset(mod)
    dp = dead_pid()
    if dp is None:
        check(f"{P}.a.dead_python_reclaim", False, "could not obtain a dead pid")
    else:
        write_lock(mod, f"python:{dp}", int(time.time()))  # RECENT ts
        err = io.StringIO()
        t0 = time.time()
        with redirect_stderr(err):
            mod._acquire_browser_lock()
        elapsed = time.time() - t0
        check(f"{P}.a.dead_python_reclaim: fast (<1s, not LOCK_WAIT_MAX)", elapsed < 1.0,
              f"elapsed={elapsed:.2f}s")
        check(f"{P}.a.dead_python_reclaim: lock now ours", read_holder(mod) == mod._LOCK_SESSION_ID)
        check(f"{P}.a.dead_python_reclaim: marker reason=dead_python",
              "reclaimed" in err.getvalue() and "reason=dead_python" in err.getvalue(),
              err.getvalue().strip())

    # LIVE python peer -> wait then give up
    reset(mod)
    peer = subprocess.Popen(["sleep", "30"])
    try:
        write_lock(mod, f"python:{peer.pid}", int(time.time()))
        out, err = io.StringIO(), io.StringIO()
        t0 = time.time()
        code = None
        try:
            with redirect_stdout(out), redirect_stderr(err):
                mod._acquire_browser_lock()
        except SystemExit as e:
            code = e.code
        elapsed = time.time() - t0
        check(f"{P}.live_peer.giveup: exits 1", code == 1, f"code={code}")
        check(f"{P}.live_peer.giveup: waited ~LOCK_WAIT_MAX", elapsed >= mod.LOCK_WAIT_MAX * 0.8,
              f"elapsed={elapsed:.2f}s")
        payload = out.getvalue()
        for sub in giveup_substrings:
            check(f"{P}.live_peer.giveup: payload has '{sub}'", sub in payload, payload.strip())
    finally:
        peer.terminate()
        peer.wait()

    # re-entrant -> take fast, refresh timestamp
    reset(mod)
    old_ts = int(time.time()) - 120
    write_lock(mod, mod._LOCK_SESSION_ID, old_ts)
    t0 = time.time()
    mod._acquire_browser_lock()
    check(f"{P}.reentrant: fast", time.time() - t0 < 1.0)
    with open(mod.LOCK_FILE) as f:
        new_ts = json.load(f)["timestamp"]
    check(f"{P}.reentrant: timestamp refreshed", new_ts > old_ts, f"old={old_ts} new={new_ts}")

    # dead UUID holder -> reclaim
    reset(mod)
    write_lock(mod, "deadbeef-0000-0000-0000-000000000000", int(time.time()))
    err = io.StringIO()
    with redirect_stderr(err):
        mod._acquire_browser_lock()
    check(f"{P}.dead_uuid_reclaim: lock now ours", read_holder(mod) == mod._LOCK_SESSION_ID)
    check(f"{P}.dead_uuid_reclaim: marker reason=dead_uuid", "reason=dead_uuid" in err.getvalue(),
          err.getvalue().strip())

    # expired holder -> reclaim
    reset(mod)
    write_lock(mod, "weird:holder:form", int(time.time()) - (mod.LOCK_EXPIRY + 50))
    err = io.StringIO()
    with redirect_stderr(err):
        mod._acquire_browser_lock()
    check(f"{P}.expired_reclaim: lock now ours", read_holder(mod) == mod._LOCK_SESSION_ID)
    check(f"{P}.expired_reclaim: marker reason=expired", "reason=expired" in err.getvalue(),
          err.getvalue().strip())

    # cold start -> fast + silent
    reset(mod)
    err = io.StringIO()
    t0 = time.time()
    with redirect_stderr(err):
        mod._acquire_browser_lock()
    check(f"{P}.cold_start: fast + silent", (time.time() - t0) < 1.0 and "reclaim" not in err.getvalue())

    try:
        os.remove(mod.LOCK_FILE)
    except OSError:
        pass
    os.rmdir(tmpdir)


# twitter giveup: {"success": false, "error": "...locked by session ... peer alive..."}
run_suite(twitter_browser, ["locked by session", "peer alive"])
# linkedin giveup: {"ok": false, "error": "profile_locked", "detail": "...peer_alive=1"}
run_suite(linkedin_browser, ["profile_locked", "peer_alive"])

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL PASS")
