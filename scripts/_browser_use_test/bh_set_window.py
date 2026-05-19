"""Configure the browser-harness Chrome window: position, size, default zoom.

What this does, one-shot:
  1. Gracefully kills the running browser-harness Chrome (SIGTERM, flushes Preferences/cookies to disk).
  2. Patches Default/Preferences to set default zoom level for ALL sites.
  3. Relaunches Chrome with the exact same flags the daemon expects.
     (Window placement is already persisted in Preferences from any prior CDP setWindowBounds call.)

Subsequent bh_run calls will auto-reconnect the daemon to the new Chrome.

Usage:
    .venv/bin/python bh_set_window.py --zoom 0.67 --left 3042 --top -1032 --width 1024 --height 768

Re-run any time you want to change values.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROFILE_DIR = Path("/Users/matthewdi/.claude/browser-profiles/browser-harness")
PREFS_PATH = PROFILE_DIR / "Default" / "Preferences"
LOCAL_STATE_PATH = PROFILE_DIR / "Local State"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9555
CHROME_FLAGS = [
    f"--remote-debugging-port={CDP_PORT}",
    f"--user-data-dir={PROFILE_DIR}",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=ChromeWhatsNewUI",
    "about:blank",
]


def find_chrome_pid() -> int | None:
    out = subprocess.run(
        ["pgrep", "-f", f"remote-debugging-port={CDP_PORT} --user-data-dir={PROFILE_DIR}"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        return None
    return int(out.splitlines()[0])


def graceful_kill(pid: int, deadline: float = 10.0) -> bool:
    os.kill(pid, signal.SIGTERM)
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
    return False


def patch_zoom(zoom_factor: float) -> None:
    """Write default zoom into Preferences. Chrome reads this on next launch.

    zoom_level = log(zoomFactor) / log(1.2)
      0.67  → -2.196545  (67%, three Ctrl+- steps from 100%)
      0.90  → -0.5778
      1.00  →  0.0
      1.10  →  0.5778
    """
    zoom_level = math.log(zoom_factor) / math.log(1.2)
    prefs = json.loads(PREFS_PATH.read_text())
    prefs.setdefault("partition", {}).setdefault("default_zoom_level", {})["0"] = zoom_level
    # Older form, harmless to also set.
    prefs.setdefault("profile", {})["default_zoom_level"] = zoom_level
    PREFS_PATH.write_text(json.dumps(prefs, separators=(",", ":")))
    print(f"  zoom: factor={zoom_factor} (level={zoom_level:.6f}) written to {PREFS_PATH.name}")


def patch_window(left: int, top: int, width: int, height: int) -> None:
    """Persist window position/size in Preferences so the next Chrome launch restores them.

    Chrome's `browser.window_placement` key controls initial window bounds.
    """
    prefs = json.loads(PREFS_PATH.read_text())
    prefs.setdefault("browser", {})["window_placement"] = {
        "bottom": top + height,
        "left": left,
        "maximized": False,
        "right": left + width,
        "top": top,
        "work_area_bottom": 0,
        "work_area_left": 0,
        "work_area_right": 0,
        "work_area_top": 0,
    }
    PREFS_PATH.write_text(json.dumps(prefs, separators=(",", ":")))
    print(f"  window: position=({left},{top}) size=({width}x{height}) written to {PREFS_PATH.name}")


def relaunch_chrome() -> int:
    p = subprocess.Popen(
        [CHROME_BIN, *CHROME_FLAGS],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Wait until the DevTools port is listening.
    import urllib.request
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1).read()
            return p.pid
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"Chrome did not open CDP port {CDP_PORT} within 15s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom", type=float, default=0.67, help="Default zoom factor (0.67 = 67%)")
    ap.add_argument("--left", type=int, default=3042)
    ap.add_argument("--top", type=int, default=-1032)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=768)
    args = ap.parse_args()

    print(f"Applying: zoom={args.zoom}, window=({args.left},{args.top}) {args.width}x{args.height}")

    pid = find_chrome_pid()
    if pid is None:
        print("  Chrome not running — will patch Preferences and launch.")
    else:
        print(f"  Chrome PID {pid} — sending SIGTERM (graceful)...")
        if not graceful_kill(pid):
            print(f"  Chrome PID {pid} didn't exit in 10s; aborting (avoid stomping on dirty Preferences)", file=sys.stderr)
            return 2
        print("  Chrome exited cleanly.")

    patch_window(args.left, args.top, args.width, args.height)
    patch_zoom(args.zoom)

    print("  Relaunching Chrome...")
    new_pid = relaunch_chrome()
    print(f"  Chrome relaunched, PID {new_pid}, CDP port {CDP_PORT} listening.")
    print("Done. Next bh_run call will auto-reconnect the daemon.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
