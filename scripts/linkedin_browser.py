#!/usr/bin/env python3
"""LinkedIn browser automation: read-only sidebar pre-check.

Usage:
    python3 linkedin_browser.py unread-dms

Read-only DOM scrape: NO Voyager API, NO scroll-and-expand loops, NO
permalink fan-out, NO clicks/typing, NO programmatic login. Each
invocation does ONE navigation + ONE page.evaluate() then closes the context.

Per CLAUDE.md "LinkedIn: flagged patterns" carve-out (2026-04-29): a
read-only DOM read is permitted because its fingerprint is indistinguishable
from the existing mcp__linkedin-agent__ sessions (same profile, same cookies,
same headed Chrome binary). The 2026-04-17 restriction was caused by Voyager
calls + permalink scroll loops, neither of which appear here.

Connects to the running linkedin-agent's persistent profile at
~/.claude/browser-profiles/linkedin. Launches HEADED Chromium (per the
CLAUDE.md note that LinkedIn fingerprints headless aggressively). Holds
the linkedin-browser lock for the entire run; expects the caller (shell)
to have already done lock acquisition + ensure_browser_healthy so the MCP
Chrome is gone and the profile is free.

Sister script for SERP discovery: scripts/discover_linkedin_candidates.py
(replaces the Claude-driven SERP nav inside skill/run-linkedin.sh Phase A).
That script imports PROFILE_DIR / VIEWPORT / SYSTEM_CHROME / LOCK_*
constants + _acquire_browser_lock + _is_login_or_checkpoint from this
module so both tools cooperate on the same Chrome profile and lock file.

Output (stdout, JSON):
    {
        "ok": true,
        "url": "https://www.linkedin.com/messaging/",
        "total_threads": 13,
        "unread_count": 0,
        "threads": [...],
    }

Failure shapes:
    {"ok": false, "error": "session_invalid", "url": "..."}
    {"ok": false, "error": "profile_locked", "detail": "..."}
    {"ok": false, "error": "navigation_failed", "detail": "..."}

Exits 0 on success, 1 on failure.
"""

import atexit
import json
import os
import re
import subprocess
import sys
import time
from typing import Optional


def _is_holder_alive(holder: str) -> bool:
    """Mirror ~/.claude/hooks/linkedin-agent-lock.sh is_holder_alive().

    A live Claude session puts its UUID on the cmdline as
    `claude --session-id <UUID>`. pgrep matches it; absence means the
    holder is dead and the lock is stale, even if its JSONL transcript
    is still tail-flushing. This is the canonical liveness signal.
    """
    if not holder:
        return False
    try:
        return (
            subprocess.run(
                ["pgrep", "-f", f"claude.*--session-id {holder}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).returncode
            == 0
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # On error, assume alive to err on the side of NOT stealing the lock.
        return True

# Profile dir is overridable so the harness migration (2026-05-26) can point
# the cold-launch fallback at ~/.claude/browser-profiles/browser-harness-linkedin
# while leaving legacy linkedin-agent callers unchanged. The Twitter harness
# uses the same pattern (TWITTER_CDP_URL + harness profile dir).
PROFILE_DIR = os.path.expanduser(
    os.environ.get(
        "LINKEDIN_PROFILE_DIR",
        "~/.claude/browser-profiles/linkedin",
    )
)
LOCK_FILE = os.path.expanduser("~/.claude/linkedin-agent-lock.json")
LOCK_EXPIRY = 300  # Must match ~/.claude/hooks/linkedin-agent-lock.sh
LOCK_WAIT_MAX = 30  # seconds; pre-check should not block long
LOCK_POLL_INTERVAL = 2
VIEWPORT = {"width": 911, "height": 1016}
# linkedin-agent uses the system Google Chrome binary, not Playwright's
# bundled "Chrome for Testing". Profile was created/migrated by system
# Chrome and "Chrome for Testing" fails to open it (SIGTRAP / kill EPERM
# observed 2026-04-29). Match the agent's binary so the profile stays
# compatible.
SYSTEM_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

_LOCK_SESSION_ID = f"python:{os.getpid()}"
_LOCK_INHERITED = False
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _release_browser_lock():
    if _LOCK_INHERITED:
        return
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                lock = json.load(f)
            if lock.get("session_id") == _LOCK_SESSION_ID:
                os.remove(LOCK_FILE)
    except (json.JSONDecodeError, OSError):
        pass


atexit.register(_release_browser_lock)


def _acquire_browser_lock():
    """Mirror twitter_browser._acquire_browser_lock semantics."""
    global _LOCK_SESSION_ID, _LOCK_INHERITED
    deadline = time.time() + LOCK_WAIT_MAX
    while True:
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE) as f:
                    lock = json.load(f)
                age = time.time() - lock.get("timestamp", 0)
                holder = lock.get("session_id", "")
                # pgrep alive-check is authoritative: a Claude UUID holder
                # whose process is gone leaves a stale lockfile (the unlock
                # hook only refreshes timestamp, not deletes). This is what
                # caused the 2026-05-01 14:33 LinkedIn false positive.
                if _UUID_RE.match(holder or "") and not _is_holder_alive(
                    holder
                ):
                    break  # stale, take it
                if age >= LOCK_EXPIRY:
                    break  # stale, take it
                if _UUID_RE.match(holder or ""):
                    # parent Claude session holds it; inherit
                    _LOCK_SESSION_ID = holder
                    _LOCK_INHERITED = True
                    break
                if time.time() >= deadline:
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "error": "profile_locked",
                                "detail": (
                                    f"holder={holder} age={int(age)}s "
                                    f"waited={LOCK_WAIT_MAX}s"
                                ),
                            }
                        )
                    )
                    sys.exit(1)
                time.sleep(LOCK_POLL_INTERVAL)
                continue
            except (json.JSONDecodeError, OSError):
                pass
        break
    with open(LOCK_FILE, "w") as f:
        json.dump(
            {"session_id": _LOCK_SESSION_ID, "timestamp": int(time.time())}, f
        )


def _is_login_or_checkpoint(url: str) -> bool:
    if not url:
        return True
    return any(
        marker in url
        for marker in (
            "/login",
            "/checkpoint",
            "/uas/login",
            "linkedin.com/authwall",
        )
    )


def _read_devtools_active_port() -> Optional[int]:
    """Return the CDP port the linkedin-agent MCP Chrome is listening on.

    The persistent profile dir holds a `DevToolsActivePort` file written
    by Chrome on startup whenever `--remote-debugging-port=0` is passed
    (the linkedin-agent MCP launches Chrome that way). First line is the
    port, second line is the browser uuid path. Missing file -> None
    (MCP is cold; caller raises RuntimeError — cold-launch removed
    2026-05-27, never attach to the wrong profile).
    """
    port_file = os.path.join(PROFILE_DIR, "DevToolsActivePort")
    try:
        with open(port_file) as f:
            first = f.readline().strip()
        port = int(first)
        if port <= 0 or port >= 65536:
            return None
        return port
    except (FileNotFoundError, ValueError, OSError):
        return None


def _pid_listening_on(port: int) -> Optional[int]:
    """Return the PID listening on a TCP port, via lsof. Best-effort.

    Used purely for diagnostic logging in `_connect_to_running_or_launch`
    so failure logs can answer "did we attach to an existing Chrome or
    cold-launch one ourselves?" without guesswork. Never raises.
    """
    try:
        out = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).stdout.decode("utf-8", "replace").strip()
        if out:
            return int(out.splitlines()[0])
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, OSError):
        pass
    return None


def _connect_to_running_or_launch(p, *, prefer_cdp: bool = True):
    """Get a BrowserContext for LinkedIn via CDP attach to a running harness.

    Cold-launch fallback REMOVED 2026-05-27. Per explicit user instruction:
    the pipeline must NEVER spawn its own Chrome on the `linkedin` profile.
    That fallback would attach to a *different* profile from the one the
    harness Chrome owns (`browser-harness-linkedin`), and the two have
    drifted in practice: the harness profile holds the active `li_at`
    session cookie, the `linkedin` profile does not. Cold-launching it
    sent every scrape straight to /authwall and silently masked the real
    failure (the harness was unreachable).

    Strategy now:
      1. If `LINKEDIN_CDP_URL` env var is set (skill/lib/linkedin-backend.sh
         sets it to http://127.0.0.1:9556), attach to the linkedin-harness
         Chrome via connect_over_cdp. Returns owns_context=False; caller
         closes only the page they opened, never the context.
      2. Otherwise try the legacy DevToolsActivePort discovery path inside
         PROFILE_DIR for backwards compatibility with manually-launched
         linkedin-agent MCP sessions. Same owns_context=False semantics.
      3. If both attach paths fail, RAISE. Do not launch a sibling Chrome.

    `prefer_cdp` kept as a kwarg for caller-API stability but no longer
    has a meaningful False branch (cold-launch is gone).

    Returns:
        (context, owns_context)  # owns_context is always False on success

    Raises:
        RuntimeError if no warm harness/MCP Chrome is reachable.
    """
    from playwright.sync_api import sync_playwright  # noqa: F401

    last_err: Optional[Exception] = None

    # Lane 1: explicit harness CDP URL (preferred — set by linkedin-backend.sh
    # when the browser-harness Chrome is up on port 9556).
    harness_cdp_url = os.environ.get("LINKEDIN_CDP_URL", "").strip()
    if prefer_cdp and harness_cdp_url:
        try:
            browser = p.chromium.connect_over_cdp(
                harness_cdp_url,
                timeout=5000,
            )
            contexts = browser.contexts
            if contexts:
                print(
                    f"[linkedin_browser] mode=harness_cdp_attach "
                    f"url={harness_cdp_url} profile=browser-harness-linkedin",
                    file=sys.stderr,
                    flush=True,
                )
                return contexts[0], False
            last_err = RuntimeError("harness CDP attach: zero contexts")
        except Exception as e:
            last_err = e
            print(
                f"[linkedin_browser] harness_cdp_attach failed: {e}",
                file=sys.stderr,
                flush=True,
            )

    # Lane 2: legacy DevToolsActivePort attach inside PROFILE_DIR (for a
    # manually-launched linkedin-agent MCP). NOT a cold launch — this only
    # attaches to a Chrome that is already running.
    if prefer_cdp:
        port = _read_devtools_active_port()
        if port is not None:
            try:
                # Pin to 127.0.0.1; localhost resolves IPv6 first on macOS
                # and Chrome --remote-debugging-port listens IPv4-only.
                browser = p.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}",
                    timeout=5000,
                )
                contexts = browser.contexts
                if contexts:
                    chrome_pid = _pid_listening_on(port)
                    print(
                        f"[linkedin_browser] mode=cdp_attach port={port} "
                        f"chrome_pid={chrome_pid} "
                        f"profile={PROFILE_DIR}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return contexts[0], False
                last_err = RuntimeError(
                    f"DevToolsActivePort attach port={port}: zero contexts"
                )
            except Exception as e:
                last_err = e

    # No warm Chrome reachable. Fail loudly — never cold-launch.
    raise RuntimeError(
        "linkedin_browser: no warm Chrome reachable. Cold-launch fallback "
        "was removed 2026-05-27 (would attach to wrong profile and mask "
        "real failures). Restart the linkedin-harness Chrome (port 9556) "
        f"and retry. Last error: {last_err}"
    )


def unread_dms() -> dict:
    """Scan LinkedIn /messaging/ sidebar in headed mode, read-only.

    Cold-launch removed 2026-05-27 — this now requires the linkedin-harness
    Chrome to be reachable via CDP; otherwise it returns
    error='no_warm_browser' so the caller can surface the real cause
    instead of silently attaching to a logged-out sibling profile.
    """
    from playwright.sync_api import sync_playwright

    _acquire_browser_lock()

    with sync_playwright() as p:
        try:
            context, _owns_context = _connect_to_running_or_launch(p)
        except RuntimeError as e:
            return {
                "ok": False,
                "error": "no_warm_browser",
                "detail": str(e),
            }

        page = None
        _reused_page = False
        try:
            # Reuse an existing harness tab instead of spawning a throwaway one
            # (mirrors reddit_browser). new_page() also steals OS focus every
            # call. Prefer a tab already on linkedin.com (not login/checkpoint),
            # else the first open page; only new_page() when the context has no
            # usable tab. A reused tab is left open in the finally below so the
            # next consumer can reuse it too.
            for pg in context.pages:
                u = pg.url or ""
                if "linkedin.com" in u and "login" not in u and "checkpoint" not in u:
                    page, _reused_page = pg, True
                    break
            if page is None and context.pages:
                page, _reused_page = context.pages[0], True
            if page is None:
                page = context.new_page()
            try:
                page.goto(
                    "https://www.linkedin.com/messaging/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                return {
                    "ok": False,
                    "error": "navigation_failed",
                    "detail": str(e),
                }

            # Settle: wait for the conversation list to render. LinkedIn's
            # messaging UI lazy-loads after DOMContentLoaded.
            try:
                page.wait_for_selector(
                    "ul.msg-conversations-container__conversations-list, "
                    "ul[class*='conversations-list'], "
                    "main [role='list']",
                    timeout=10000,
                )
            except Exception:
                pass  # we'll still try to read whatever's there
            page.wait_for_timeout(1500)

            cur_url = page.url
            if _is_login_or_checkpoint(cur_url):
                return {
                    "ok": False,
                    "error": "session_invalid",
                    "url": cur_url,
                }

            # Read sidebar. Strategy:
            #   - For each conversation list item, derive partner name
            #     (bolded participant), preview text, time, and unread state.
            #   - Unread signal: visual blue dot (.notification-badge--show)
            #     OR data-test-unread, NOT generic [aria-label*=unread].
            #     LinkedIn renders hover "Mark as unread" buttons that
            #     contain the substring 'unread' on every thread.
            #   - thread_url: try the <a href> if rendered; otherwise null.
            threads = page.evaluate(
                """
                () => {
                  const out = [];
                  // Find conversation list items. LinkedIn renders these as
                  // <li> inside the conversations list; fall back to any
                  // [role=listitem] anchored under the messaging main.
                  const candidates = document.querySelectorAll(
                    "ul.msg-conversations-container__conversations-list > li, "
                    + "ul[class*='conversations-list'] > li, "
                    + "main [role='listitem']"
                  );
                  for (const item of candidates) {
                    // Skip ad slots / non-conversation rows.
                    const link = item.querySelector(
                      "a.msg-conversation-listitem__link, a[href*='/messaging/thread/']"
                    );
                    const innerText = (item.innerText || "").trim();
                    if (!innerText) continue;

                    // Unread badge: blue dot. Avoid the broad
                    // [aria-label*=unread] selector which matches the
                    // hover "Mark as unread" affordance.
                    const blueDot = item.querySelector(
                      ".notification-badge--show, "
                      + "[data-test-unread='true'], "
                      + ".msg-conversation-card__unread-count, "
                      + ".notification-badge.notification-badge--show"
                    );
                    const unread = !!blueDot;

                    // Partner name: prefer h3 / participant-names node.
                    const nameEl = item.querySelector(
                      "h3, .msg-conversation-listitem__participant-names, "
                      + ".msg-conversation-card__participant-names"
                    );
                    const partner = nameEl
                      ? (nameEl.textContent || "").trim()
                      : "";

                    // Time element: usually a small time/timestamp span.
                    const timeEl = item.querySelector(
                      "time, .msg-conversation-listitem__time-stamp, "
                      + ".msg-conversation-card__time-stamp"
                    );
                    const time = timeEl
                      ? (timeEl.textContent || "").trim()
                      : "";

                    // Preview (snippet of last message). Take first text
                    // node after the participant name that isn't the time.
                    const previewEl = item.querySelector(
                      ".msg-conversation-card__message-snippet, "
                      + ".msg-conversation-listitem__message-snippet, "
                      + "p.msg-conversation-card__message-snippet"
                    );
                    let preview = previewEl
                      ? (previewEl.textContent || "").trim()
                      : "";
                    if (!preview) {
                      // Fallback: trim partner+time off the innerText.
                      preview = innerText
                        .replace(partner, "")
                        .replace(time, "")
                        .trim();
                    }

                    let threadUrl = null;
                    if (link) {
                      const href = link.getAttribute("href") || "";
                      if (href && /\\/messaging\\/thread\\//.test(href)) {
                        threadUrl = href.startsWith("http")
                          ? href
                          : ("https://www.linkedin.com" + href);
                      }
                    }

                    out.push({
                      partner,
                      preview: preview,
                      time,
                      thread_url: threadUrl,
                      unread,
                    });
                  }
                  return JSON.stringify(out);
                }
                """
            )
            try:
                threads_list = json.loads(threads or "[]")
            except json.JSONDecodeError:
                threads_list = []

            unread_count = sum(1 for t in threads_list if t.get("unread"))

            return {
                "ok": True,
                "url": cur_url,
                "total_threads": len(threads_list),
                "unread_count": unread_count,
                "threads": threads_list,
            }

        finally:
            # CDP-attach branch: NEVER close the context — that would
            # terminate the harness Chrome we just attached to. Only close a
            # page WE created; if we reused an existing tab, leave it open so
            # the next consumer can reuse it (tab-reuse convention).
            if page is not None and not _reused_page:
                try:
                    page.close()
                except Exception:
                    pass


def unread_dms_with_retry(max_attempts: int = 2) -> dict:
    """Wrap unread_dms with one retry on TargetClosedError-style transient
    failures. The headed Chrome launch races against atexit lock release on
    the previous run; a single retry after a short delay clears most cases.
    """
    last_result: dict = {"ok": False, "error": "no_attempts"}
    for attempt in range(1, max_attempts + 1):
        try:
            result = unread_dms()
        except Exception as e:
            result = {
                "ok": False,
                "error": "exception",
                "detail": f"{type(e).__name__}: {e}",
                "attempt": attempt,
            }
        last_result = result
        # Only retry on transient browser-target failures, not on
        # session_invalid / profile_locked which won't self-heal.
        err = (result.get("error") or "").lower()
        detail = (result.get("detail") or "").lower()
        transient = (
            "targetclosed" in detail
            or "target page" in detail
            or "browser has been closed" in detail
            or err == "navigation_failed"
        )
        if result.get("ok") or not transient or attempt >= max_attempts:
            if attempt > 1:
                result["retry_attempt"] = attempt
            return result
        print(
            f"[linkedin_browser] transient failure attempt {attempt}: "
            f"{result.get('detail') or result.get('error')}; retrying...",
            file=sys.stderr,
        )
        time.sleep(2)
    return last_result


def main():
    # Guard: only authorized pipelines may invoke this helper. Other Claude
    # subprocess planners (post_reddit, post_twitter, etc.) auto-load
    # CLAUDE.md as system context, see this helper documented there, and
    # have wandered off-task to "smoke test" it — racing the linkedin
    # profile's SingletonLock and triggering server-side session
    # invalidation. The legitimate caller sets the matching env var
    # immediately before invoking; nothing else does.
    if os.environ.get("SOCIAL_AUTOPOSTER_LINKEDIN_PRECHECK") != "1":
        print(
            json.dumps({
                "ok": False,
                "error": "unauthorized_caller",
                "detail": (
                    "linkedin_browser.py is invoked only by the "
                    "engage-dm-replies pre-check. Set "
                    "SOCIAL_AUTOPOSTER_LINKEDIN_PRECHECK=1 from the caller "
                    "if this invocation is legitimate. (For SERP discovery, "
                    "use scripts/discover_linkedin_candidates.py instead.)"
                ),
            }),
            file=sys.stderr,
        )
        sys.exit(2)
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "unread-dms":
        result = unread_dms_with_retry()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)
    print(f"Unknown command: {cmd}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
