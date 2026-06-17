#!/usr/bin/env python3
"""Regression test for the twitter_browser.py session-lock fix (2026-06-16).

Exercises the three session-lock defects in isolation, with NO real browser:
  (a) dead python:PID holders must be reclaimed immediately (not after 300s)
  (b) [shell-side, verified separately] no `rm -f` of the lockfile in pipelines
  (c) lock acquisition must be atomic (two acquirers cannot both win)

Run:
    /opt/homebrew/bin/python3 scripts/test_browser_lock.py
Exit 0 = all pass; non-zero with FAIL lines otherwise.

This is the canonical "did the fix survive / still work?" check. See
docs/twitter_browser_lock.md for the full verification playbook (including the
live-log greps and the regression signatures to watch for).
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
# twitter_browser.py lives in scripts/; tolerate being run from scripts/ or scripts/tmp/.
for _cand in (HERE, os.path.join(HERE, "..")):
    if os.path.exists(os.path.join(_cand, "twitter_browser.py")):
        sys.path.insert(0, _cand)
        break
import twitter_browser as tb  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def write_lock(holder, ts):
    with open(tb.LOCK_FILE, "w") as f:
        json.dump({"session_id": holder, "timestamp": ts}, f)


def read_holder():
    with open(tb.LOCK_FILE) as f:
        return json.load(f)["session_id"]


def reset():
    tb._LOCK_SESSION_ID = f"python:{os.getpid()}"
    tb._LOCK_INHERITED = False
    try:
        os.remove(tb.LOCK_FILE)
    except OSError:
        pass


def dead_pid():
    """A PID that is definitely not alive (spawn + reap). None if instantly reused."""
    p = subprocess.Popen(["true"])
    p.wait()
    try:
        os.kill(p.pid, 0)
        return None
    except ProcessLookupError:
        return p.pid


# --- isolate onto a temp lock file + fast timeouts -------------------------
_tmpdir = tempfile.mkdtemp(prefix="brlock-test-")
tb.LOCK_FILE = os.path.join(_tmpdir, "twitter-browser-lock.json")
tb.LOCK_WAIT_MAX = 2
tb.LOCK_POLL_INTERVAL = 0.2
print(f"# LOCK_FILE={tb.LOCK_FILE}  LOCK_WAIT_MAX={tb.LOCK_WAIT_MAX}")

# Guard: confirm the fix is actually present (catches a silent revert).
check("fix_present: _is_python_holder_alive exists", hasattr(tb, "_is_python_holder_alive"))
check("fix_present: _try_take_lock (atomic) exists", hasattr(tb, "_try_take_lock"))

# --- (c) atomic take: two cold-start acquirers cannot both win -------------
reset()
first = tb._try_take_lock()
second = tb._try_take_lock()
check("c.atomic_take: first wins, second loses", first is True and second is False,
      f"first={first} second={second}")
check("c.atomic_take: file holds our id", read_holder() == tb._LOCK_SESSION_ID)

# --- (a) dead python:PID holder is reclaimed immediately (not after 300s) --
reset()
dp = dead_pid()
if dp is None:
    check("a.dead_python_reclaim", False, "could not obtain a dead pid (reused)")
else:
    write_lock(f"python:{dp}", int(time.time()))  # RECENT ts: old code would wait+exit
    err = io.StringIO()
    t0 = time.time()
    with redirect_stderr(err):
        tb._acquire_browser_lock()  # must return, NOT sys.exit
    elapsed = time.time() - t0
    check("a.dead_python_reclaim: returns fast (<1s, not LOCK_WAIT_MAX)", elapsed < 1.0,
          f"elapsed={elapsed:.2f}s")
    check("a.dead_python_reclaim: lock is now ours", read_holder() == tb._LOCK_SESSION_ID)
    check("a.dead_python_reclaim: emits verifiable marker",
          "reclaimed" in err.getvalue() and "reason=dead_python" in err.getvalue(),
          err.getvalue().strip())

# --- LIVE python peer: we wait then give up with structured error ----------
reset()
peer = subprocess.Popen(["sleep", "30"])
try:
    write_lock(f"python:{peer.pid}", int(time.time()))
    out, err = io.StringIO(), io.StringIO()
    t0 = time.time()
    code = None
    try:
        with redirect_stdout(out), redirect_stderr(err):
            tb._acquire_browser_lock()
    except SystemExit as e:
        code = e.code
    elapsed = time.time() - t0
    check("live_peer.giveup: exits 1", code == 1, f"code={code}")
    check("live_peer.giveup: waited ~LOCK_WAIT_MAX", elapsed >= tb.LOCK_WAIT_MAX * 0.8,
          f"elapsed={elapsed:.2f}s")
    payload = out.getvalue()
    check("live_peer.giveup: preserves 'locked by session' substring",
          "locked by session" in payload, payload.strip())
    check("live_peer.giveup: says peer alive", "peer alive" in payload)
finally:
    peer.terminate()
    peer.wait()

# --- re-entrant: holder == us -> take immediately, refresh timestamp --------
reset()
old_ts = int(time.time()) - 120
write_lock(tb._LOCK_SESSION_ID, old_ts)
t0 = time.time()
tb._acquire_browser_lock()
check("reentrant: returns fast", time.time() - t0 < 1.0)
with open(tb.LOCK_FILE) as f:
    new_ts = json.load(f)["timestamp"]
check("reentrant: timestamp refreshed", new_ts > old_ts, f"old={old_ts} new={new_ts}")

# --- dead UUID holder (no live `claude --session-id` proc) -> reclaim -------
reset()
write_lock("deadbeef-0000-0000-0000-000000000000", int(time.time()))
err = io.StringIO()
with redirect_stderr(err):
    tb._acquire_browser_lock()
check("dead_uuid_reclaim: lock now ours", read_holder() == tb._LOCK_SESSION_ID)
check("dead_uuid_reclaim: marker reason=dead_uuid", "reason=dead_uuid" in err.getvalue(),
      err.getvalue().strip())

# --- expired holder (old ts, holder we cannot probe) -> reclaim -------------
reset()
write_lock("weird:holder:form", int(time.time()) - (tb.LOCK_EXPIRY + 50))
err = io.StringIO()
with redirect_stderr(err):
    tb._acquire_browser_lock()
check("expired_reclaim: lock now ours", read_holder() == tb._LOCK_SESSION_ID)
check("expired_reclaim: marker reason=expired", "reason=expired" in err.getvalue(),
      err.getvalue().strip())

# --- no-contention cold start: silent, immediate ----------------------------
reset()
err = io.StringIO()
t0 = time.time()
with redirect_stderr(err):
    tb._acquire_browser_lock()
check("cold_start: fast + silent (no reclaim noise)",
      (time.time() - t0) < 1.0 and "reclaim" not in err.getvalue())

# cleanup
try:
    os.remove(tb.LOCK_FILE)
except OSError:
    pass
os.rmdir(_tmpdir)

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL PASS")
