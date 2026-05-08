#!/usr/bin/env python3
"""mcp_lock_proxy.py — heartbeat wrapper for browser-MCP servers.

Spawns a real MCP server (e.g. `npx @playwright/mcp@latest`) as a stdio
subprocess and proxies JSON-RPC traffic between Claude and that server.
Whenever a `tools/call` request crosses the wire, the wrapper pushes the
matching `reddit_browser_lock.py` lease forward by `--ttl` seconds.

Why this exists
---------------
Pre-2026-05-08 the reddit-browser lock was held for the full duration the
agent decided to "keep" it. If the agent forgot to call `release`, crashed,
or did 5+ minutes of non-browser work (page-gen, sleeps, DB updates), the
lock leaked and every peer reddit pipeline blocked behind a Chrome that
nobody was using. The fix has two halves:

  1. The lock now has a `expires_at` lease field (see `reddit_browser_lock.py`).
     If `now() > expires_at`, peers steal it. Default lease = 90s (≫ p99 of
     real reddit-agent MCP call durations, which is 30s).
  2. This wrapper renews the lease on every actual MCP browser call. So as
     long as real browser work is happening the lease stays alive; the moment
     it stops, the lease expires within 90s and peers proceed automatically.

Heartbeat strategy
------------------
- On every JSON-RPC `tools/call` we see, fire `reddit_browser_lock.py heartbeat`
  in a background thread (so we never block the request).
- On every response that matches a pending request id, fire heartbeat again.
- A daemon thread also fires a heartbeat every 30s while at least one request
  is in flight. This covers the rare 5-min `browser_close` / `browser_tabs`
  outliers without bloating the lease window for the common case.

Failure modes
-------------
- If the lock is currently held by a different owner, heartbeat returns
  `HELD_BY_OTHER` and silently no-ops. We don't try to "fix" it; the actual
  acquire/release logic is the source of truth.
- If the lock isn't held at all, heartbeat returns `NOT_HELD` and silently
  no-ops. (Browser activity outside the lock is a separate prompt-discipline
  bug; this wrapper isn't where we enforce it.)
- Heartbeat shells out to a short-lived python3 process. If it hangs or fails,
  the timeout is 5s and we just drop the heartbeat. Worst case: a single MCP
  call doesn't extend the lease — usually fine because subsequent calls
  re-extend it; if it really IS the only call in a window, lease expires
  exactly as the design intends.

Args
----
    mcp_lock_proxy.py [--lock-name reddit-browser] [--ttl 90] -- <real mcp cmd...>

Or via env:
    BROWSER_LOCK_NAME=reddit-browser
    BROWSER_LOCK_TTL=90
    BROWSER_LOCK_SCRIPT=/path/to/reddit_browser_lock.py

Notes
-----
The proxy must be transparent to the MCP protocol. We never modify, drop,
or reorder messages. We only inspect them to decide whether to fire a
heartbeat side effect.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_DIR = Path("/Users/matthewdi/social-autoposter")
DEFAULT_LOCK_SCRIPT = REPO_DIR / "scripts" / "reddit_browser_lock.py"
DEFAULT_LOCK_NAME = "reddit-browser"
DEFAULT_TTL = 90
HEARTBEAT_PULSE_INTERVAL = 30  # while a request is in flight

# Tunable via env so other browser agents can re-use this exact wrapper.
LOCK_NAME = os.environ.get("BROWSER_LOCK_NAME", DEFAULT_LOCK_NAME)
LOCK_TTL = int(os.environ.get("BROWSER_LOCK_TTL", str(DEFAULT_TTL)))
LOCK_SCRIPT = Path(os.environ.get("BROWSER_LOCK_SCRIPT", str(DEFAULT_LOCK_SCRIPT)))

# Optional debug log for proxy-internal events. Default off.
DEBUG_LOG_PATH = os.environ.get("BROWSER_LOCK_PROXY_LOG", "")
DEBUG_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    if not DEBUG_LOG_PATH:
        return
    try:
        with DEBUG_LOG_LOCK, open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.time():.3f}] {msg}\n")
    except Exception:
        pass


# ---- Heartbeat plumbing -----------------------------------------------------

_pending_lock = threading.Lock()
_pending_request_ids: set = set()  # JSON-RPC ids we sent and haven't seen a response for
_last_heartbeat_at = 0.0
_heartbeat_min_interval = 1.0  # Don't fire more than once per second


def _fire_heartbeat() -> None:
    """Shell out to `reddit_browser_lock.py heartbeat`. Always non-blocking."""
    global _last_heartbeat_at
    now = time.time()
    # Cheap throttle: avoid stampedes when a burst of calls fires.
    if now - _last_heartbeat_at < _heartbeat_min_interval:
        return
    _last_heartbeat_at = now
    try:
        subprocess.run(
            [
                sys.executable,
                str(LOCK_SCRIPT),
                "heartbeat",
                "--name",
                LOCK_NAME,
                "--ttl",
                str(LOCK_TTL),
            ],
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        _log(f"heartbeat fired ttl={LOCK_TTL}")
    except Exception as e:
        _log(f"heartbeat failed: {e}")


def _heartbeat_async() -> None:
    threading.Thread(target=_fire_heartbeat, daemon=True).start()


def _pulse_loop() -> None:
    """Periodic heartbeat while any request is in flight.

    This handles the rare case where a single MCP call legitimately runs
    longer than the lease TTL (observed max: ~5.6 min on browser_close /
    browser_tabs). Without this loop, that one call would let the lease
    expire mid-flight and a peer would steal the browser from under us.
    """
    while True:
        time.sleep(HEARTBEAT_PULSE_INTERVAL)
        with _pending_lock:
            has_pending = bool(_pending_request_ids)
        if has_pending:
            _heartbeat_async()


# ---- JSON-RPC stream proxy --------------------------------------------------


def _try_parse(line: bytes) -> dict | None:
    if not line:
        return None
    s = line.strip()
    if not s:
        return None
    try:
        msg = json.loads(s.decode("utf-8", errors="replace"))
        if isinstance(msg, dict):
            return msg
    except Exception:
        pass
    return None


def _proxy_stdin_to_proc(proc: subprocess.Popen) -> None:
    """Read JSON-RPC from our stdin (Claude → us), forward to the MCP server.

    Inspect for `tools/call` requests; record id and fire heartbeat.
    """
    while True:
        try:
            line = sys.stdin.buffer.readline()
        except Exception as e:
            _log(f"stdin read error: {e}")
            break
        if not line:
            break
        msg = _try_parse(line)
        if msg is not None:
            method = msg.get("method")
            req_id = msg.get("id")
            if method == "tools/call" and req_id is not None:
                with _pending_lock:
                    _pending_request_ids.add(req_id)
                _heartbeat_async()
                _log(f"req in id={req_id}")
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            break
        except Exception as e:
            _log(f"forward to subprocess failed: {e}")
            break
    try:
        proc.stdin.close()
    except Exception:
        pass


def _proxy_proc_to_stdout(proc: subprocess.Popen) -> None:
    """Read MCP server stdout, forward to our stdout (us → Claude).

    Inspect for response objects matching pending request ids; on match,
    drop the id from the pending set and fire one final heartbeat (so the
    lease covers the moment the call resolved).
    """
    while True:
        try:
            line = proc.stdout.readline()
        except Exception as e:
            _log(f"subprocess stdout read error: {e}")
            break
        if not line:
            break
        msg = _try_parse(line)
        if msg is not None:
            resp_id = msg.get("id")
            if resp_id is not None and "method" not in msg:
                with _pending_lock:
                    if resp_id in _pending_request_ids:
                        _pending_request_ids.discard(resp_id)
                        _log(f"resp out id={resp_id}")
                        _heartbeat_async()
        try:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError):
            break
        except Exception as e:
            _log(f"forward to claude failed: {e}")
            break


# ---- Subprocess lifecycle ---------------------------------------------------

_subprocess_handle: subprocess.Popen | None = None


def _cleanup_subprocess() -> None:
    """Make sure the wrapped MCP server dies if our proxy goes away.

    Without this, killing the proxy (e.g. when claude exits abnormally) would
    leave `npx @playwright/mcp@latest` and its Chrome child running, which
    permanently holds the reddit browser profile lock.
    """
    p = _subprocess_handle
    if p is None:
        return
    try:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
    except Exception:
        pass


def _signal_exit(signum, _frame) -> None:
    _log(f"received signal {signum}, exiting")
    _cleanup_subprocess()
    # Use os._exit to skip atexit (already ran cleanup) and avoid stuck threads.
    os._exit(0)


# ---- Entrypoint -------------------------------------------------------------


def main() -> int:
    # Hoist `global` declarations to the very top of main() so argparse
    # defaults below can reference module-level values without Python's
    # "used prior to global declaration" SyntaxError.
    global LOCK_NAME, LOCK_TTL, LOCK_SCRIPT

    p = argparse.ArgumentParser(
        description="Heartbeat wrapper for a browser-MCP stdio server.",
        allow_abbrev=False,
    )
    p.add_argument("--lock-name", default=LOCK_NAME)
    p.add_argument("--ttl", type=int, default=LOCK_TTL)
    p.add_argument(
        "--lock-script", default=str(LOCK_SCRIPT),
        help="Path to reddit_browser_lock.py (or compatible).",
    )
    p.add_argument(
        "real_cmd", nargs=argparse.REMAINDER,
        help="The real MCP server command (everything after `--`).",
    )
    args = p.parse_args()

    # Apply CLI overrides (env was already read at import; CLI wins).
    LOCK_NAME = args.lock_name
    LOCK_TTL = args.ttl
    LOCK_SCRIPT = Path(args.lock_script)

    # Strip a leading `--` separator if argparse left it in REMAINDER.
    real_cmd = list(args.real_cmd)
    if real_cmd and real_cmd[0] == "--":
        real_cmd = real_cmd[1:]
    if not real_cmd:
        print(
            "mcp_lock_proxy: missing real MCP server command. "
            "Pass it after `--`, e.g. `mcp_lock_proxy.py -- npx @playwright/mcp@latest ...`",
            file=sys.stderr,
        )
        return 2

    _log(f"starting wrapper lock_name={LOCK_NAME} ttl={LOCK_TTL} cmd={real_cmd}")

    # Install lifecycle hooks BEFORE spawning the child, so any spawn-time
    # crash still triggers cleanup.
    atexit.register(_cleanup_subprocess)
    try:
        signal.signal(signal.SIGTERM, _signal_exit)
        signal.signal(signal.SIGINT, _signal_exit)
        signal.signal(signal.SIGHUP, _signal_exit)
    except (ValueError, OSError):
        # Not all platforms allow handler installation in non-main threads.
        pass

    global _subprocess_handle
    try:
        _subprocess_handle = subprocess.Popen(
            real_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            bufsize=0,
        )
        proc = _subprocess_handle
    except FileNotFoundError as e:
        print(f"mcp_lock_proxy: failed to spawn real MCP server: {e}", file=sys.stderr)
        return 127

    t_in = threading.Thread(target=_proxy_stdin_to_proc, args=(proc,), daemon=True)
    t_out = threading.Thread(target=_proxy_proc_to_stdout, args=(proc,), daemon=True)
    t_pulse = threading.Thread(target=_pulse_loop, daemon=True)
    t_in.start()
    t_out.start()
    t_pulse.start()

    rc = proc.wait()
    # Give the output thread a moment to flush any tail.
    t_out.join(timeout=1.0)
    _log(f"wrapper exiting rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
