#!/usr/bin/env python3
# Run via:
#   uv run --quiet --with mcp <this-file>
# or:
#   python3 -m pip install --break-system-packages 'mcp>=1.0.0' && python3 <this-file>
"""
browser-harness MCP server.

Wraps the `browser-harness` CLI (https://github.com/browser-use/browser-harness)
behind an MCP stdio server so any Claude Code session can drive direct CDP
browser control without manually managing the daemon.

Architecture:
- Auto-launches a dedicated Chrome instance on port 9555 with a persistent
  profile at ~/.claude/browser-profiles/browser-harness so cookies/sessions
  carry across Claude Code sessions.
- Exposes tools that shell out to the `browser-harness` CLI with
  BU_CDP_URL pointed at our managed Chrome.
- Stays out of the user's normal Chrome (which is what playwright-extension
  uses); this is a separate isolated profile.

Cross-platform: works on macOS and Linux. Chrome binary is auto-detected
(env override: BH_CHROME_BIN). On Linux + root we add --no-sandbox; on Linux
without a display we add --headless=new (override with BH_HEADLESS=0 to force
headed, e.g. when Xvfb is available).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# --- Config ---

PORT = int(os.environ.get("BH_PORT", "9555"))
PROFILE_DIR = Path.home() / ".claude" / "browser-profiles" / "browser-harness"
PID_FILE = Path.home() / ".claude" / "browser-profiles" / "browser-harness.chrome.pid"
LOG_FILE = Path.home() / ".claude" / "browser-profiles" / "browser-harness.chrome.log"
MCP_LOG_FILE = Path.home() / ".claude" / "browser-profiles" / "browser-harness.mcp.log"


def _detect_chrome_bin() -> str:
    """Find the Chrome binary on disk. Env override wins."""
    env = os.environ.get("BH_CHROME_BIN")
    if env and Path(env).exists():
        return env

    candidates = [
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        # Linux (Debian/Ubuntu defaults)
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]
    for p in candidates:
        if Path(p).exists():
            return p

    # Fall back to PATH lookup.
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found

    # Last-resort default (matches the pre-2026-05-20 hardcoded value).
    return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _detect_browser_harness_bin() -> str:
    """Find the browser-harness CLI. Env override wins."""
    env = os.environ.get("BH_HARNESS_BIN")
    if env and Path(env).exists():
        return env

    # uv-tool default install location.
    candidate = Path.home() / ".local" / "bin" / "browser-harness"
    if candidate.exists():
        return str(candidate)

    found = shutil.which("browser-harness")
    if found:
        return found

    return str(candidate)  # report the expected path even if missing


CHROME_BIN = _detect_chrome_bin()
BROWSER_HARNESS_BIN = _detect_browser_harness_bin()
CDP_URL = f"http://127.0.0.1:{PORT}"

# Default exec timeout for a single tool call (seconds). Browser flows can be
# slow; raise the cap so multi-step scripts don't get killed mid-flight.
EXEC_TIMEOUT_SEC = int(os.environ.get("BH_EXEC_TIMEOUT_SEC", "300"))

# Heuristic: are we on Linux without a graphical display? Then we should
# launch Chrome headless unless the operator explicitly says otherwise.
_IS_LINUX = sys.platform.startswith("linux")
_HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
_DEFAULT_HEADLESS = "1" if (_IS_LINUX and not _HAS_DISPLAY) else "0"
HEADLESS = os.environ.get("BH_HEADLESS", _DEFAULT_HEADLESS) == "1"
RUNNING_AS_ROOT = (hasattr(os, "geteuid") and os.geteuid() == 0)


# --- Logging ---

def _log(msg: str) -> None:
    try:
        MCP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with MCP_LOG_FILE.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

# --- Chrome lifecycle ---

def _port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _cdp_alive() -> bool:
    """Return True if the CDP /json/version endpoint responds."""
    if not _port_open(PORT):
        return False
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1.5) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _build_chrome_cmd() -> list[str]:
    """Compose the Chrome launch argv for this platform / environment."""
    cmd = [
        CHROME_BIN,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=ChromeWhatsNewUI",
    ]

    # Headless + sandboxing for Linux/root.
    if HEADLESS:
        cmd.append("--headless=new")
        cmd.append("--disable-gpu")
    if RUNNING_AS_ROOT or _IS_LINUX:
        # --no-sandbox is required when Chrome runs as root (e.g. inside a
        # rootful container/VM). Harmless on Linux non-root too.
        cmd.append("--no-sandbox")
        cmd.append("--disable-dev-shm-usage")

    # 2026-05-13: persistent window placement on macOS multi-monitor setups.
    # Skip on headless / Linux where positioning is meaningless and the
    # off-screen values would just hide the window on a single-monitor setup.
    if not HEADLESS and not _IS_LINUX:
        cmd.append(f"--window-position={os.environ.get('BH_WINDOW_POS', '3042,-1032')}")
        cmd.append(f"--window-size={os.environ.get('BH_WINDOW_SIZE', '1024,1013')}")

    # Open a real tab so CDP has something to attach to immediately.
    cmd.append("about:blank")
    return cmd


def ensure_chrome() -> dict:
    """Make sure our managed Chrome is running on PORT. Idempotent."""
    if _cdp_alive():
        return {"status": "already_running", "pid": _read_pid(), "cdp": CDP_URL}

    if not Path(CHROME_BIN).exists() and not shutil.which(CHROME_BIN):
        return {
            "status": "no_chrome_binary",
            "looked_for": CHROME_BIN,
            "hint": "Set BH_CHROME_BIN to your Chrome/Chromium path, or install google-chrome / chromium.",
        }

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Clean up stale daemon socket so first browser-harness call doesn't
    # try to talk to a dead daemon from a previous Chrome instance.
    for stale in ("/tmp/bu-default.sock", "/tmp/bu-default.pid"):
        try:
            os.unlink(stale)
        except FileNotFoundError:
            pass

    # If a Chrome with our profile is still partially up but not on the port,
    # try to surface that in the log rather than silently double-launching.
    pid = _read_pid()
    if pid and _pid_alive(pid):
        _log(f"stale pid {pid} alive but CDP dead; killing")
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        time.sleep(0.5)

    cmd = _build_chrome_cmd()

    log_fh = LOG_FILE.open("ab")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    _log(f"launched Chrome pid={proc.pid} port={PORT} profile={PROFILE_DIR} headless={HEADLESS}")

    # Wait for CDP to be ready. First launch on a cold/fresh machine has to
    # create the profile and run Chrome's first-run setup, which routinely
    # exceeds 15s on a slow VM; an over-tight deadline returns launch_timeout
    # and the caller runs against a port that was about to come up. 30s is the
    # safe floor (override with BH_LAUNCH_TIMEOUT_SEC).
    launch_timeout = int(os.environ.get("BH_LAUNCH_TIMEOUT_SEC", "30"))
    deadline = time.time() + launch_timeout
    while time.time() < deadline:
        if _cdp_alive():
            return {"status": "started", "pid": proc.pid, "cdp": CDP_URL}
        time.sleep(0.3)

    return {
        "status": "launch_timeout",
        "pid": proc.pid,
        "cdp": CDP_URL,
        "log": str(LOG_FILE),
        "waited_sec": launch_timeout,
        "log_tail": _chrome_log_tail(),
    }


def _port_owner_pids() -> list[int]:
    """PIDs LISTENing on our debug PORT, via lsof. Lets stop_chrome reap a Chrome
    that another launcher (e.g. setup_twitter_auth.py's connect_x) started without
    writing PID_FILE, instead of stranding an un-reapable orphan that makes
    bh_start keep reporting 'already_running'. Returns [] if lsof is unavailable."""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{PORT}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for tok in (out.stdout or "").split():
        try:
            pids.append(int(tok))
        except ValueError:
            pass
    return pids


def _terminate(pid: int, grace: float = 5.0) -> None:
    """SIGTERM, then SIGKILL only if still alive after `grace` seconds. The wait
    gives Chrome time to flush its in-memory cookie store to the on-disk profile,
    so a stop->start restart preserves the X session instead of coming back
    logged out (the failure the setup agent hit after a hard kill)."""
    try:
        os.kill(pid, 15)
    except OSError:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def stop_chrome() -> dict:
    """Gracefully stop the managed Chrome — the tracked process AND any orphan
    still LISTENing on the debug port — so connect_x-launched Chromes can't
    strand the port. SIGTERM-with-grace lets Chrome persist cookies to disk."""
    targets: list[int] = []
    pid = _read_pid()
    if pid and _pid_alive(pid):
        targets.append(pid)
    for owner in _port_owner_pids():
        if owner not in targets and _pid_alive(owner):
            targets.append(owner)

    for t in targets:
        _terminate(t)

    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    for stale in ("/tmp/bu-default.sock", "/tmp/bu-default.pid"):
        try:
            os.unlink(stale)
        except FileNotFoundError:
            pass
    return {"status": "stopped", "reaped": targets, "tracked_pid": pid}


# --- browser-harness exec wrapper ---

def _chrome_log_tail(lines: int = 25) -> str:
    """Last `lines` of the managed-Chrome log, for surfacing in CDP errors."""
    try:
        text = LOG_FILE.read_text(errors="replace")
    except (FileNotFoundError, OSError):
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _ensure_cdp_ready() -> dict | None:
    """Guarantee CDP is actually answering on PORT before we shell out to the
    harness CLI. Returns None when CDP is live; otherwise returns a structured,
    actionable error dict (and leaves the chrome log tail attached).

    Without this gate, ensure_chrome() failures (no_chrome_binary,
    launch_timeout) were swallowed and the CLI ran against a dead port, so the
    agent saw a cryptic usage banner / connection error instead of the real
    cause. This is the #1 fresh-install failure mode."""
    res = ensure_chrome()
    if _cdp_alive():
        return None

    # One self-heal attempt: a stale Chrome bound to the port but not speaking
    # CDP (crashed renderer, half-dead profile) won't recover on its own.
    _log(f"CDP not alive after ensure_chrome (status={res.get('status')}); attempting stop+relaunch")
    stop_chrome()
    time.sleep(1.0)
    res = ensure_chrome()
    if _cdp_alive():
        return None

    status = res.get("status", "unknown")
    if status == "no_chrome_binary":
        hint = res.get("hint", "Install Chrome/Chromium or set BH_CHROME_BIN.")
    else:
        hint = (
            f"Chrome did not expose CDP on {CDP_URL} (status={status}). "
            "On a headless Linux box ensure BH_HEADLESS=1 and a Chrome binary "
            "are present; on macOS make sure no other Chrome owns the profile. "
            f"See {LOG_FILE}."
        )
    return {
        "ok": False,
        "error": f"browser-harness CDP not connected: {hint}",
        "cdp": CDP_URL,
        "ensure_chrome": res,
        "chrome_log_tail": _chrome_log_tail(),
    }


def _run_harness(script: str, timeout: int = EXEC_TIMEOUT_SEC) -> dict:
    if not shutil.which(BROWSER_HARNESS_BIN) and not Path(BROWSER_HARNESS_BIN).exists():
        return {
            "ok": False,
            "error": (
                f"browser-harness CLI not found at {BROWSER_HARNESS_BIN}. "
                "Install with: cd ~/Developer/browser-harness && uv tool install -e ."
            ),
        }

    cdp_err = _ensure_cdp_ready()
    if cdp_err is not None:
        return cdp_err

    env = os.environ.copy()
    env["BU_CDP_URL"] = CDP_URL
    # Make sure ~/.local/bin is on PATH (uv tools live there).
    env["PATH"] = f"{Path.home()}/.local/bin:" + env.get("PATH", "")

    # Upstream browser-harness dropped the `-c <script>` flag and now reads the
    # script from stdin only (heredoc style). Pass via stdin so we work against
    # current upstream; the old `-c` form returns the usage banner and exits 1,
    # which used to surface as "CDP not connected" on every fresh install.
    try:
        proc = subprocess.run(
            [BROWSER_HARNESS_BIN],
            input=script,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "error": f"browser-harness timed out after {timeout}s",
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "") if isinstance(e.stderr, str) else "",
        }

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


# --- MCP server ---

mcp = FastMCP(
    "browser-harness",
    instructions=(
        "Direct CDP browser control via the browser-use/browser-harness CLI. "
        "Runs in a dedicated Chrome with a persistent profile at "
        "~/.claude/browser-profiles/browser-harness so cookies/sessions persist "
        "across Claude Code sessions. Separate from the user's normal Chrome "
        "(which playwright-extension drives) and from the per-platform agents "
        "(reddit/twitter/linkedin/logged-in-browser/isolated-browser). "
        "Primary tool: bh_run(script) — runs arbitrary Python with the harness "
        "helpers pre-imported (new_tab, goto_url, page_info, capture_screenshot, "
        "click_at_xy, js, type_text, press_key, fill_input, scroll, "
        "wait_for_load, wait_for_element, ensure_real_tab, list_tabs, "
        "switch_tab, http_get, cdp, etc.). "
        "Workflow: capture_screenshot → click_at_xy(x,y) → re-screenshot. "
        "Helpers cheat-sheet lives at ~/Developer/browser-harness/SKILL.md."
    ),
)


@mcp.tool()
def bh_run(script: str, timeout: int = EXEC_TIMEOUT_SEC) -> str:
    """Execute a Python script inside browser-harness.

    The script runs with all browser-harness helpers pre-imported (new_tab,
    goto_url, page_info, capture_screenshot, click_at_xy, js, type_text,
    press_key, fill_input, scroll, wait_for_load, wait_for_element,
    ensure_real_tab, list_tabs, switch_tab, http_get, cdp, etc.).

    The first navigation in a fresh tab should be new_tab(url), not
    goto_url(url) — goto runs in the user's currently-focused tab and clobbers
    whatever is loaded there.

    To inspect / extract data, use print(...) and read it back from the
    "stdout" field of the result.

    Returns a JSON string with: ok, returncode, stdout, stderr.
    """
    result = _run_harness(script, timeout=timeout)
    return json.dumps(result, indent=2)


@mcp.tool()
def bh_status() -> str:
    """Report whether the managed Chrome is alive and where it lives."""
    pid = _read_pid()
    owners = _port_owner_pids()
    return json.dumps(
        {
            "cdp_url": CDP_URL,
            "cdp_alive": _cdp_alive(),
            "chrome_pid": pid,
            "chrome_alive": (pid is not None and _pid_alive(pid)) or _cdp_alive(),
            # Untracked Chromes (e.g. launched by connect_x) show up here even
            # when chrome_pid is null — that's the orphan that bh_stop now reaps.
            "port_owner_pids": owners,
            "profile_dir": str(PROFILE_DIR),
            "log_file": str(LOG_FILE),
            "harness_bin": BROWSER_HARNESS_BIN,
            "chrome_bin": CHROME_BIN,
            "headless": HEADLESS,
            "root": RUNNING_AS_ROOT,
        },
        indent=2,
    )


@mcp.tool()
def bh_start() -> str:
    """Start the managed Chrome (idempotent). Normally bh_run handles this."""
    return json.dumps(ensure_chrome(), indent=2)


@mcp.tool()
def bh_stop() -> str:
    """Kill the managed Chrome instance. Cookies/profile data persist on disk.

    Reaps both the tracked process and any orphan still holding the debug port,
    using a graceful SIGTERM-with-grace so cookies flush to disk first."""
    return json.dumps(stop_chrome(), indent=2)


@mcp.tool()
def bh_restart() -> str:
    """Flush + restart the managed Chrome in one step. Gracefully stops it (so
    Chrome persists the cookie store to disk and any port-orphan is reaped), then
    starts a fresh instance that loads the just-flushed session from disk. Use
    this instead of killing Chrome by hand — a hard kill drops the in-memory X
    session before it is written, which is what forces a re-login."""
    stopped = stop_chrome()
    time.sleep(0.5)
    started = ensure_chrome()
    return json.dumps({"status": "restarted", "stopped": stopped, "started": started}, indent=2)


@mcp.tool()
def bh_seed_cookies(source: str = "chrome:Default", domains: str | None = None) -> str:
    """Import cookies from a local browser profile into the managed Chrome.

    Shells out to ai_browser_profile.cookies (~/ai-browser-profile/.venv).
    Reads cookies from the source profile via macOS Keychain + AES-CBC decrypt,
    then injects via CDP Storage.setCookies into our managed Chrome on PORT.

    macOS-only at present (depends on Keychain). On Linux this returns an
    error pointing to a manual cookie-injection path.

    Args:
        source: 'browser:profile' spec, e.g. 'chrome:Profile 1', 'arc:Default'.
                Browsers: chrome, arc, brave, edge.
        domains: comma-separated host_key substrings to filter
                 (e.g. 'github.com,linear.app'). None = ALL cookies. Highly
                 recommended to filter — cookies are auth secrets and importing
                 everything mirrors your full session into the managed browser.

    Returns JSON with ok, returncode, stdout (cookie counts only — never values), stderr.
    """
    if _IS_LINUX:
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "bh_seed_cookies is macOS-only (depends on Keychain). "
                    "On Linux, log in to the target site once in the managed "
                    "Chrome (the profile at ~/.claude/browser-profiles/browser-harness "
                    "persists), or inject cookies via your own bootstrap."
                ),
            },
            indent=2,
        )
    ensure_chrome()
    abp_python = Path.home() / "ai-browser-profile" / ".venv" / "bin" / "python"
    if not abp_python.exists():
        return json.dumps(
            {"ok": False, "error": f"ai-browser-profile venv not found at {abp_python}"},
            indent=2,
        )

    cmd = [
        str(abp_python),
        "-m",
        "ai_browser_profile.cookies",
        "copy",
        "--from",
        source,
        "--to",
        CDP_URL,
    ]
    if domains:
        cmd += ["--domains", domains]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path.home() / "ai-browser-profile"),
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "seed timed out after 60s"}, indent=2)

    return json.dumps(
        {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        },
        indent=2,
    )


@mcp.tool()
def bh_seed_localstorage(source: str = "chrome:Default", origins: str | None = None) -> str:
    """Import localStorage from a local browser profile into the managed Chrome.

    Sister to bh_seed_cookies. macOS-only (same Keychain dependency).

    Args:
        source: 'browser:profile' spec, e.g. 'chrome:Profile 1'.
        origins: comma-separated host substrings (e.g. 'chatgpt.com,notion.so').

    Returns JSON with ok, returncode, stdout (counts only, no values), stderr.
    """
    if _IS_LINUX:
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "bh_seed_localstorage is macOS-only (depends on Keychain). "
                    "On Linux, log in once in the managed Chrome."
                ),
            },
            indent=2,
        )
    ensure_chrome()
    abp_python = Path.home() / "ai-browser-profile" / ".venv" / "bin" / "python"
    if not abp_python.exists():
        return json.dumps(
            {"ok": False, "error": f"ai-browser-profile venv not found at {abp_python}"},
            indent=2,
        )

    cmd = [
        str(abp_python),
        "-m",
        "ai_browser_profile.localstorage",
        "copy",
        "--from",
        source,
        "--to",
        CDP_URL,
    ]
    if origins:
        cmd += ["--origins", origins]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,  # tab-per-origin can take a while
            cwd=str(Path.home() / "ai-browser-profile"),
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "seed timed out after 180s"}, indent=2)

    return json.dumps(
        {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        },
        indent=2,
    )


@mcp.tool()
def bh_screenshot(quality: int = 50) -> str:
    """Capture a screenshot of the current tab and write it to a temp file.

    Returns JSON with the file path and basic page info. Use bh_run for any
    workflow that needs to keep state across multiple steps.

    The `quality` parameter is accepted for back-compat but is ignored — the
    current upstream `capture_screenshot()` signature is (path, full, max_dim)
    and does not expose a JPEG-quality knob. Older callers (and the MCP tool
    schema) still pass it; we just don't forward it.
    """
    # Avoid the unused-variable lint and keep `quality` part of the MCP
    # contract: a sanity-cap so a bad caller can't pass arbitrary types.
    _ = int(quality)
    script = (
        "import json, time, os\n"
        "ensure_real_tab()\n"
        "info = page_info()\n"
        "path = capture_screenshot()\n"
        "print(json.dumps({\"screenshot\": str(path), \"page\": info}))\n"
    )
    result = _run_harness(script)
    if not result.get("ok"):
        return json.dumps(result, indent=2)
    # Last line of stdout is our JSON.
    out = (result.get("stdout") or "").strip().splitlines()
    payload = out[-1] if out else "{}"
    return payload


@mcp.tool()
def bh_navigate(url: str, new_tab: bool = True) -> str:
    """Open a URL. By default opens in a fresh tab (recommended)."""
    if new_tab:
        nav = f"new_tab({url!r})"
    else:
        nav = f"ensure_real_tab(); goto_url({url!r})"
    script = (
        "import json\n"
        f"{nav}\n"
        "wait_for_load()\n"
        "info = page_info()\n"
        "print(json.dumps(info))\n"
    )
    result = _run_harness(script)
    if not result.get("ok"):
        return json.dumps(result, indent=2)
    out = (result.get("stdout") or "").strip().splitlines()
    return out[-1] if out else "{}"


if __name__ == "__main__":
    _log("server starting")
    try:
        mcp.run()
    except Exception as e:
        _log(f"server crashed: {e!r}")
        raise
