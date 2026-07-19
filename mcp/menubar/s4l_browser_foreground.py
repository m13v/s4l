"""Ground-truth "the harness browser just went foreground" telemetry.

Customers reported the managed Chrome popping over their work (1.7.x made the
launchd kicker keep a harness Chrome alive all day, the screencast reconnect
sends Page.bringToFront, and single-display Macs clamp the off-screen
--window-position back on-screen). None of those moments were recorded
anywhere: the causing sites had no logs and nothing observed the OS z-order.

This module is the cause-agnostic observer. It subscribes to NSWorkspace
activate/launch notifications and, whenever the app coming to the front is a
Chrome/Chromium whose command line carries a MANAGED profile
(~/.claude/browser-profiles/browser-harness*), emits one structured JSON line
via s4l_log_relay.emit(..., context="browser-foreground"). The relay POSTs it
to /api/v1/installations/logs under the install's X-Installation identity, so
in Cloud Logging (project s4l-app-prod) the events are:

    jsonPayload.context="browser-foreground"
    AND jsonPayload.install_id="<uuid>"

Payload fields: cause ("activated" fires on every raise, "launched" only on a
fresh Chrome process = the launch-activation steal), pid, profile + CDP port +
--window-position (parsed from the process command line; window-position
80,80 = setup_twitter_auth's on-screen login, 3042,-1032 = the pipeline
default), interrupted_app (the frontmost app the user lost), and
suppressed_since_last (burst dedupe counter, so a bringToFront storm is
countable without flooding the relay).

Design constraints:
  - The notification handler must never block the main run loop: it only
    enqueues; a daemon worker does the `ps` classification + emit.
  - Only harness-Chrome events are emitted. The user's own Chrome, and every
    other app switch, produce zero relay traffic (we keep the last non-harness
    app name in memory as interrupted_app context, nothing more).
  - Strictly best-effort: install() returning False just means no telemetry.
"""

import json
import os
import queue
import re
import subprocess
import threading
import time

import s4l_log_relay

# A managed harness Chrome is one launched on a profile under this marker
# (browser-harness = twitter 9555, browser-harness-linkedin = 9556, ...).
# Any managed harness profile under browser-profiles/ counts — matching the
# literal "browser-harness" prefix silently EXCLUDED the reddit harness
# (profile "reddit-harness"), so reddit-Chrome activations went unlogged and
# unattributable for days (2026-07-17). The remote-debugging-port requirement
# in the check below keeps the user's own Chrome / MCP-agent profiles (which
# use the debugging PIPE, not a port) out of scope.
_PROFILE_MARKER = os.path.join(".claude", "browser-profiles", "")

# Within this window, repeats of the same (cause, pid) are counted, not emitted.
# A screencast-reconnect storm raises Chrome every few seconds; one line per
# 30s with suppressed_since_last preserves the frequency without the flood.
_DEDUPE_SECONDS = 30.0

_events = queue.Queue()
_started = False


def _cmdline(pid):
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3,
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


class _Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="s4l-browser-foreground")
        self._pid_cache = {}  # pid -> (is_harness, details dict)
        self._prev_app = None  # last non-harness frontmost app name
        self._last_key = None  # (cause, pid) of last emitted event
        self._last_emit_at = 0.0
        self._suppressed = 0

    def _classify(self, pid):
        cached = self._pid_cache.get(pid)
        if cached is not None:
            return cached
        cmd = _cmdline(pid)
        is_harness = _PROFILE_MARKER in cmd and "--remote-debugging-port=" in cmd
        details = {}
        if is_harness:
            m = re.search(r"--user-data-dir=(\S+)", cmd)
            details["profile"] = os.path.basename(m.group(1).rstrip("/")) if m else ""
            m = re.search(r"--remote-debugging-port=(\d+)", cmd)
            details["port"] = int(m.group(1)) if m else None
            m = re.search(r"--window-position=(\S+)", cmd)
            details["window_position"] = m.group(1) if m else None
        result = (is_harness, details)
        # pids recycle rarely; a tiny bounded cache is enough and self-clears.
        if len(self._pid_cache) > 64:
            self._pid_cache.clear()
        self._pid_cache[pid] = result
        return result

    def _handle(self, cause, pid, name, low):
        if "chrome" not in low and "chromium" not in low:
            if cause == "activated" and name:
                self._prev_app = name
            return
        is_harness, details = self._classify(pid)
        if not is_harness:
            # The user's own Chrome counts as their workspace too.
            if cause == "activated" and name:
                self._prev_app = name
            return
        now = time.time()
        key = (cause, pid)
        if key == self._last_key and now - self._last_emit_at < _DEDUPE_SECONDS:
            self._suppressed += 1
            return
        payload = {
            "ev": "harness_browser_foregrounded",
            "cause": cause,  # "activated" | "launched"
            "app": name,
            "pid": pid,
            "interrupted_app": self._prev_app,
            "suppressed_since_last": self._suppressed,
        }
        payload.update(details)
        self._last_key = key
        self._last_emit_at = now
        self._suppressed = 0
        s4l_log_relay.emit(
            "[browser-foreground] " + json.dumps(payload, ensure_ascii=False),
            context="browser-foreground",
        )

    def run(self):
        while True:
            try:
                cause, pid, name, low = _events.get()
                self._handle(cause, pid, name, low)
            except Exception:
                # Never die: a bad event must not end foreground telemetry.
                time.sleep(0.5)


def install():
    """Subscribe to NSWorkspace activate/launch notifications and start the
    classifier worker. Call once at menubar boot (the AppKit run loop rumps
    starts delivers the notifications). Returns True on success; never raises."""
    global _started
    if _started:
        return True
    try:
        from AppKit import NSWorkspace

        nc = NSWorkspace.sharedWorkspace().notificationCenter()

        def _make(cause):
            def _handler(note):
                try:
                    app = note.userInfo().objectForKey_("NSWorkspaceApplicationKey")
                    if app is None:
                        return
                    name = str(app.localizedName() or "")
                    bid = str(app.bundleIdentifier() or "")
                    pid = int(app.processIdentifier())
                    _events.put((cause, pid, name, (name + " " + bid).lower()))
                except Exception:
                    pass
            return _handler

        # Keep the block handlers referenced for the process lifetime: PyObjC
        # does retain the blocks it wraps, but the observer tokens returned
        # here are our only handle if we ever need to removeObserver_.
        tokens = [
            nc.addObserverForName_object_queue_usingBlock_(
                "NSWorkspaceDidActivateApplicationNotification", None, None,
                _make("activated"),
            ),
            nc.addObserverForName_object_queue_usingBlock_(
                "NSWorkspaceDidLaunchApplicationNotification", None, None,
                _make("launched"),
            ),
        ]
        install._tokens = tokens  # noqa: SLF001 (lifetime anchor)
        _Worker().start()
        _started = True
        return True
    except Exception:
        return False
