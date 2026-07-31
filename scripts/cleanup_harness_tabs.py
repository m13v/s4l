#!/usr/bin/env python3
"""Close every CDP "page" tab in harness Chrome except one, and keep that one
tab REAL (http/https).

Called from skill/lib/harness-common.sh::hc_cleanup_tabs (all three platform
backends) as part of pre-flight. Safe to call any time: exits 0 silently when
harness Chrome is down. Workers and iframe targets are left alone; they
auto-clean when their parent page closes.

Revive (2026-07-31): when BH_REVIVE_URL is set to an http(s) url, a Chrome
whose only tab is about:blank gets that tab navigated to the revive url, and
a completely tabless Chrome (--no-startup-window launch) gets ONE background
tab created there. This matters because the harness daemon's is_real_page()
refuses about:/chrome: tabs — a blank-only browser used to trap it in a
"stale session -> foreground createTarget(about:blank) -> still not real"
loop, one macOS focus steal per iteration (30/day on reddit-harness).
Both revive paths need websocket-client (present in the owned runtime venv,
S4L_PYTHON); without it the script degrades to close-only, as before.

The standalone-script form (vs an inline heredoc) is required because bash
3.2 on macOS cannot parse a nested heredoc inside a function body inside a
sourced file. See git history around 2026-05-14 for the prior inline form
that broke every launchd-fired twitter script.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

# Port can be overridden via BH_CLEANUP_PORT so the LinkedIn backend
# (skill/lib/linkedin-backend.sh) can reuse this same cleanup script against
# its own harness Chrome on 9556. Default 9555 keeps Twitter callers unchanged.
CDP_PORT = int(os.environ.get("BH_CLEANUP_PORT", "9555"))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
REVIVE_URL = os.environ.get("BH_REVIVE_URL", "")


def _is_real(t) -> bool:
    return (t.get("url") or "").startswith(("http://", "https://"))


def _ws_call(ws_url: str, method: str, timeout: float = 8.0, **params):
    # suppress_origin: Chrome 111+ rejects ws clients whose Origin header is
    # not in --remote-allow-origins (same pattern as cdp_ready_check.py).
    import websocket  # websocket-client; ImportError handled by callers

    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg
        raise TimeoutError(f"no reply to {method}")
    finally:
        ws.close()


def _revive(keep) -> None:
    """Ensure one REAL tab exists: navigate a lone non-real tab (keep) to
    REVIVE_URL, or create a background tab there when keep is None (tabless
    Chrome). Best-effort; cleanup's exit code never depends on it."""
    if not REVIVE_URL.startswith(("http://", "https://")):
        return
    try:
        if keep is None:
            # background=True: a foreground Target.createTarget raises the
            # Chrome window and steals macOS app focus; revive must be silent.
            info = json.loads(
                urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2).read()
            )
            _ws_call(
                info["webSocketDebuggerUrl"], "Target.createTarget",
                url=REVIVE_URL, background=True,
            )
            print(f"[cleanup_harness_tabs] created background tab on {REVIVE_URL}")
        elif not _is_real(keep) and keep.get("webSocketDebuggerUrl"):
            _ws_call(keep["webSocketDebuggerUrl"], "Page.navigate", url=REVIVE_URL)
            print(f"[cleanup_harness_tabs] revived blank tab -> {REVIVE_URL}")
    except Exception as e:
        print(f"[cleanup_harness_tabs] revive skipped ({type(e).__name__}: {e})")


def main() -> int:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json", timeout=2) as r:
            tabs = json.loads(r.read())
    except Exception:
        return 0
    pages = [t for t in tabs if t.get("type") == "page"]
    if not pages:
        print("[cleanup_harness_tabs] 0 page tab(s)")
        _revive(None)
        return 0
    if len(pages) == 1:
        print("[cleanup_harness_tabs] 1 page tab(s), no cleanup needed")
        _revive(pages[0])
        return 0
    # Keep a REAL (http/https) tab when one exists, not blindly pages[0]. The
    # /json order is roughly most-recently-active first, so a freshly-spawned
    # about:blank can sit at index 0 and the old code would keep the blank and
    # close the live x.com tab the harness daemon is attached to. Closing the
    # daemon's tab forces it to re-attach and re-spawn another about:blank, which
    # is exactly the orphan-tab churn this script is meant to clean up. Falling
    # back to pages[0] preserves the prior behavior when every tab is blank.
    keep = next((t for t in pages if _is_real(t)), pages[0])
    closed = 0
    for t in pages:
        if t is keep:
            continue
        tid = t.get("id")
        if not tid:
            continue
        try:
            urllib.request.urlopen(f"{CDP_URL}/json/close/{tid}", timeout=2).read()
            closed += 1
        except Exception:
            pass
    kept_kind = "1 real" if _is_real(keep) else "1"
    print(f"[cleanup_harness_tabs] closed {closed}/{len(pages) - 1} extra page tabs (kept {kept_kind})")
    _revive(keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
