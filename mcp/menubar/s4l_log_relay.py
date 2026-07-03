"""Tee the menu bar's stderr to the S4L Cloud Run log relay.

The .mcpb server already streams the verbatim stdout/stderr of every pipeline
subprocess to POST /api/v1/installations/logs (mcp/src/telemetry.ts), where the
Cloud Run relay console.log()s each line into Cloud Logging. The menu bar is a
separate launchd agent, so its stderr (including every [s4l-card] surface
lifecycle line) only ever reached the local menubar.err.log; in the 2026-07-02
unseen-card incident that meant zero central signal. This module closes the
gap: sys.stderr is wrapped so every line still lands in the local log file AND
ships to the same relay endpoint under the same X-Installation identity, with
context "menubar" so Log Explorer can split it from pipeline output.

Mirrors telemetry.ts semantics: batch flushes on a short cadence, blank and
base64-blob noise filtering (the X-Installation echo loop guard), bounded
buffer with drop-oldest overflow, and strictly best-effort: nothing in here may
ever raise into the menu bar or write to stderr itself (that would loop).
Disable with SAPS_LOG_STREAM=0, point elsewhere with AUTOPOSTER_LOG_BASE.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.request

LOG_BASE = (os.environ.get("AUTOPOSTER_LOG_BASE") or "https://app.s4l.ai").rstrip("/")
ENABLED = os.environ.get("SAPS_LOG_STREAM", "1") != "0"
MAX_LINE_LEN = 8192  # relay cap
MAX_BUFFER = 1000  # drop oldest beyond this
MAX_PER_POST = 200  # relay accepts 1-200 lines per request
FLUSH_SECONDS = 3.0

# Lines that are nothing but a long base64 run are the identity-header echo
# that once flooded Cloud Logging via the pipeline lane; same guard here.
_BASE64_BLOB_RE = re.compile(r"^[A-Za-z0-9+/=_-]{120,}$")

_buf = []
_lock = threading.Lock()
_header = None


def _install_header():
    global _header
    if _header:
        return _header
    # Lane 1: the shared helper, present in current repos.
    try:
        # scripts/ is on sys.path (SAPS_REPO_DIR insertion at menubar boot).
        from http_api import get_identity_header

        _header = (get_identity_header() or "").strip() or None
    except Exception:
        _header = None
    if _header:
        return _header
    # Lane 2: older deployed repos lack get_identity_header; mint the header
    # the way telemetry.ts does, by shelling scripts/identity.py.
    try:
        import subprocess

        script = os.path.join(
            os.environ.get("SAPS_REPO_DIR") or "", "scripts", "identity.py"
        )
        if os.path.isfile(script):
            out = subprocess.run(
                [sys.executable, script, "header"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                _header = (out.stdout or "").strip() or None
    except Exception:
        _header = None
    return _header


def _now_iso():
    try:
        import datetime

        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    except Exception:
        return ""


class _Tee:
    """File-like wrapper: pass every write through to the real stderr (launchd's
    menubar.err.log redirect), and buffer non-noise lines for the relay."""

    def __init__(self, real):
        self._real = real

    def write(self, s):
        try:
            n = self._real.write(s)
        except Exception:
            n = len(s) if isinstance(s, str) else 0
        try:
            for ln in str(s).splitlines():
                t = ln.strip()
                if not t or _BASE64_BLOB_RE.match(t):
                    continue
                with _lock:
                    _buf.append(
                        {
                            "ts": _now_iso(),
                            "stream": "stderr",
                            "line": ln[:MAX_LINE_LEN],
                            "context": "menubar",
                        }
                    )
                    if len(_buf) > MAX_BUFFER:
                        del _buf[: len(_buf) - MAX_BUFFER]
        except Exception:
            pass
        return n

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._real, name)


def _flush_once():
    header = _install_header()
    if not header:
        return  # identity not mintable yet; keep buffering
    while True:
        with _lock:
            if not _buf:
                return
            batch = _buf[:MAX_PER_POST]
            del _buf[:MAX_PER_POST]
        try:
            req = urllib.request.Request(
                LOG_BASE + "/api/v1/installations/logs",
                data=json.dumps({"lines": batch}).encode("utf-8"),
                headers={
                    "X-Installation": header,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            # Network blip or relay down: drop this batch (a persistent failure
            # must not grow the buffer unbounded) and stop draining; the cadence
            # loop retries with newer lines. Never log from in here.
            return


def _loop():
    while True:
        time.sleep(FLUSH_SECONDS)
        try:
            _flush_once()
        except Exception:
            pass


def install():
    """Wrap sys.stderr with the relay tee and start the background flusher.
    Call once at menubar boot, after the SAPS_REPO_DIR sys.path insertion."""
    if not ENABLED:
        return
    try:
        if isinstance(sys.stderr, _Tee):
            return
        sys.stderr = _Tee(sys.stderr)
        threading.Thread(target=_loop, daemon=True, name="s4l-log-relay").start()
    except Exception:
        pass
