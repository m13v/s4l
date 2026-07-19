#!/usr/bin/env python3
"""Reddit browser automation functions for Social Autoposter.

Replaces multi-step Claude browser MCP calls with single Python function calls.
Each function does all browser work internally and returns structured JSON.

Usage:
    # Post a top-level comment on a Reddit thread
    python3 reddit_browser.py post-comment "https://old.reddit.com/r/sub/comments/abc/title/" "comment text"

    # Reply to an existing comment
    python3 reddit_browser.py reply "https://old.reddit.com/r/sub/comments/abc/title/def/" "reply text"

    # Scan DM inbox for unread conversations
    python3 reddit_browser.py unread-dms

    # Read messages from a Reddit chat conversation
    python3 reddit_browser.py read-conversation "https://www.reddit.com/chat/..."

    # Send a DM in a Reddit chat
    python3 reddit_browser.py send-dm "https://www.reddit.com/chat/..." "message text"

Requires: pip install playwright && playwright install chromium

Connects to the running reddit-agent MCP browser via CDP (Chrome DevTools Protocol)
to reuse the existing logged-in session.
"""

import atexit
import json
import os
import random
import re
import subprocess
import sys
import time


def _bh_activity_log(action: str, cdp_url: str) -> None:
    """Append to the universal browser-activity.log (Python-CDP path coverage)."""
    try:
        import time as _t
        import os as _o
        from pathlib import Path as _P
        _p = _P(_o.environ.get(
            "BH_ACTIVITY_LOG",
            str(_P.home() / ".claude" / "browser-profiles" / "browser-activity.log"),
        ))
        _port = (cdp_url or "").rsplit(":", 1)[-1].split("/")[0] or "-"
        _p.parent.mkdir(parents=True, exist_ok=True)
        with _p.open("a") as _f:
            _f.write(
                f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] pycdp "
                f"script={_o.path.basename(__file__)} action={action} "
                f"pid={_o.getpid()} ppid={_o.getppid()} cdp={cdp_url or '-'} "
                f"port={_port}\n"
            )
    except Exception:
        pass


PROFILE_DIR = os.path.expanduser("~/.claude/browser-profiles/reddit")
LOCK_FILE = os.path.expanduser("~/.claude/reddit-agent-lock.json")
LOCK_EXPIRY = 300  # Must match reddit-agent-lock.sh
LOCK_WAIT_MAX = 45  # seconds to wait for lock to free before giving up
LOCK_POLL_INTERVAL = 2

# Side log for tool-layer diagnostics. Stdout would corrupt the JSON contract
# every CLI caller relies on; stderr is dropped on the floor by both
# subprocess.check_output(stderr=DEVNULL) callers AND by claude -p without
# --output-format stream-json. A side file is the only place these lines
# survive the round-trip, so verification (e.g. "did the suffix gate fire
# this run") becomes a cheap grep.
DIAG_LOG = os.path.expanduser("~/social-autoposter/skill/logs/reddit_browser_diag.log")


def _diag_log(msg):
    try:
        os.makedirs(os.path.dirname(DIAG_LOG), exist_ok=True)
        with open(DIAG_LOG, "a") as f:
            from datetime import datetime
            f.write(f"{datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}\n")
    except Exception:
        pass
VIEWPORT = {"width": 911, "height": 1016}

# Our Reddit username, via the ONE resolver (account_resolver.resolve('reddit'):
# env -> reddit_account.username login ground truth -> accounts.reddit.username).
# The dual-key precedence used to be duplicated here; drift between the two keys
# silently broke the post-permalink lookup on the VM (wrong username → JS finds 0
# matching comments → permalink=None → pipeline records `failed` despite the
# comment landing on Reddit). "" means "unknown account" — never a hardcoded
# fallback, which would mis-attribute on a misconfigured install.
_config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from account_resolver import resolve as _resolve_account
    OUR_USERNAME = _resolve_account("reddit") or ""
except Exception:
    OUR_USERNAME = ""


def _ban_entry_to_slug(entry):
    """Extract sub slug from a comment_blocked / thread_blocked entry.

    Handles both shapes: bare string (pre-2026-05-11) and audit dict
    {"sub": ..., "added_at": ..., "reason": ..., "project": ...}.
    Returns lowercased slug or None.
    """
    if isinstance(entry, str):
        s = entry.strip().lower()
        return s or None
    if isinstance(entry, dict):
        s = (entry.get("sub") or "").strip().lower()
        return s or None
    return None


def _load_comment_blocked_subs():
    """Return the set of subreddits (lowercased) we cannot post comments in.

    Mirrors reddit_tools._load_comment_blocked_subs so the reply path can
    pre-flight without taking that import (and its db dependency).

    Scope model (2026-05-19 cleanup): comment_blocked entries are always
    account-level. Filter by the entry's `account` field against the local
    machine's reddit_account.username so this MacBook's bans don't suppress
    subs on the sandbox VM (which posts as a different account). The
    legacy `project` field on entries is IGNORED here too — comment_blocked
    is account-scoped by nature; project-specific rejects live in
    project_search_excludes.

    Handles both ban-list shapes: bare-string entries (pre-2026-05-11) and
    audit dicts {"sub": ..., "added_at": ..., "reason": ..., "account": ...}.
    """
    try:
        with open(_config_path) as f:
            cfg = json.load(f)
        # Same value as OUR_USERNAME above; resolved once through the ONE
        # resolver so ban scoping and permalink lookup can never disagree.
        current_account = OUR_USERNAME or None
        blocked = set()
        bans = cfg.get("subreddit_bans") or {}
        if isinstance(bans, dict):
            for entry in bans.get("comment_blocked") or []:
                slug = _ban_entry_to_slug(entry)
                if not slug:
                    continue
                entry_account = None
                if isinstance(entry, dict):
                    entry_account = entry.get("account") or None
                # account=null = global (apply on every account; back-compat).
                # account=set + mismatch = skip; this entry belongs to a
                # different machine's account.
                if (entry_account is not None and current_account is not None
                        and entry_account.lower() != current_account.lower()):
                    continue
                blocked.add(slug)
        for s in cfg.get("exclusions", {}).get("subreddits", []):
            blocked.add(s.lower())
        return blocked
    except Exception:
        return set()


def _subreddit_from_permalink(url):
    """Extract subreddit name (lowercased, no r/ prefix) from a Reddit URL."""
    if not url:
        return None
    m = re.search(r"/r/([^/?#]+)", url)
    return m.group(1).lower() if m else None


def find_reddit_cdp_port():
    """Find the CDP port of the running reddit-agent MCP browser.

    Scans all Chrome/Chromium processes for remote-debugging-port flags,
    then queries each port's /json endpoint for pages with reddit.com
    or old.reddit.com URLs. Strongly prefers old.reddit.com pages
    (the MCP agent browser) over new reddit pages.
    """
    try:
        ps_out = subprocess.check_output(
            ["ps", "aux"], text=True, stderr=subprocess.DEVNULL
        )
        ports = set()
        for line in ps_out.splitlines():
            if "chromium" not in line.lower() and "chrome" not in line.lower():
                continue
            m = re.search(r"remote-debugging-port=(\d+)", line)
            if m:
                ports.add(int(m.group(1)))

        import urllib.request

        old_reddit_port = None
        new_reddit_port = None
        any_reddit_port = None
        for port in sorted(ports):
            try:
                resp = urllib.request.urlopen(
                    f"http://localhost:{port}/json", timeout=2
                )
                pages = json.loads(resp.read())
                reddit_urls = [
                    p.get("url", "")
                    for p in pages
                    if "reddit.com" in p.get("url", "")
                ]
                if not reddit_urls:
                    continue

                # Strongly prefer old.reddit.com (the MCP agent browser)
                has_old = any(
                    "old.reddit.com" in u and "login" not in u
                    for u in reddit_urls
                )
                if has_old and not old_reddit_port:
                    old_reddit_port = port

                # New reddit with actual content pages
                has_new = any(
                    ("/r/" in u or "/chat" in u or "/message" in u
                     or "reddit.com/u/" in u)
                    and "old.reddit.com" not in u
                    and "login" not in u
                    for u in reddit_urls
                )
                if has_new and not new_reddit_port:
                    new_reddit_port = port

                if not any_reddit_port:
                    any_reddit_port = port
            except Exception:
                continue

        return old_reddit_port or new_reddit_port or any_reddit_port
    except Exception:
        pass
    return None


# Path to the bash-lock lease helper. Bumping this lease from inside reddit_browser.py
# is what shields Python-CDP pipelines (run-reddit-search, audit-reddit-resurrect,
# stats.sh reddit phase, engage-reddit) from the watchdog's 60-90s reclaim. Those
# pipelines never go through MCP, so the MCP PreToolUse heartbeat hook never fires
# for them. Each subprocess invocation of reddit_browser.py is a CDP step, so
# bumping `expires_at` on every subprocess start gives the watchdog a clear "this
# pipeline is alive and using the browser" signal.
_BASH_LEASE_HEARTBEAT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "reddit_browser_lock.py"
)
_BASH_LEASE_TTL_SEC = 90


def _heartbeat_bash_lease():
    """Best-effort: bump the bash-lock lease's expires_at by `_BASH_LEASE_TTL_SEC`.

    Silent on every outcome (OK / NOT_HELD / HELD_BY_OTHER / errors). This is a
    pure peace-keeping signal to the watchdog, not load-bearing for correctness.
    Times out fast (3s) so a hung lease helper can't stall a CDP step.

    NOT_HELD is fine: the bash lock may genuinely not be acquired (e.g. ad-hoc
    use of reddit_browser.py outside a pipeline). HELD_BY_OTHER is also fine:
    a peer holds the bash lock; we shouldn't touch their lease.
    """
    try:
        subprocess.run(
            ["python3", _BASH_LEASE_HEARTBEAT_PATH, "heartbeat",
             "--ttl", str(_BASH_LEASE_TTL_SEC)],
            capture_output=True, timeout=3, check=False,
        )
    except Exception:
        pass  # Best-effort. Never fail a CDP op because of lease bookkeeping.


# ---- browser-session mutex (2026-07-14: shared implementation) -------------
# The old inline lock here still had the check-then-write claim race AND the
# dead-holder starvation (a SIGKILLed peer blocked everyone for the full
# 300s LOCK_EXPIRY) that twitter_browser.py fixed on 2026-06-16. Both drivers
# now share scripts/browser_mutex.py (twitter's proven mutex, parameterized):
# reddit gains the O_EXCL atomic claim, dead-PID reclaim, batch inherit via
# S4L_LOCK_OWNER, and role-aware posting priority (only active when a caller
# sets S4L_LOCK_ROLE=post; default scan keeps today's no-preemption behavior).
# The bash-lease bump rides the mutex's on_touch hook, so every acquire AND
# refresh keeps the watchdog fed exactly as before.
from browser_mutex import BrowserMutex

_MUTEX = BrowserMutex(
    lock_file=LOCK_FILE,
    label="Reddit browser",
    lock_expiry=LOCK_EXPIRY,
    wait_max=LOCK_WAIT_MAX,
    poll_interval=LOCK_POLL_INTERVAL,
    on_touch=_heartbeat_bash_lease,
)


def _acquire_browser_lock():
    _MUTEX.acquire()


def _refresh_browser_lock():
    _MUTEX.refresh()


def _release_browser_lock():
    _MUTEX.release()


atexit.register(_release_browser_lock)


# --- L1/L2 browser-hang guards (2026-07-13, mirrors twitter_browser.py) ------
# L1: default per-op/navigation timeouts so no Playwright call can hang forever.
# L2: liveness heartbeat on the shell reddit-browser lock dir; lock held +
# stale heartbeat = wedged holder, TERMed by watchdog_hung_runs.py in ~30 min
# instead of the multi-hour age cap. See twitter_browser.py for the incident
# writeup (2026-07-13 pid 1380 media-capture wedge during a network flap).
BROWSER_OP_TIMEOUT_MS = 30_000
BROWSER_NAV_TIMEOUT_MS = 60_000
_BROWSER_LOCK_HEARTBEAT = "/tmp/social-autoposter-reddit-browser.lock/heartbeat"
_HB_THROTTLE_S = 5
_hb_last_touch = 0.0


def _touch_browser_heartbeat(*_args):
    global _hb_last_touch
    now = time.time()
    if now - _hb_last_touch < _HB_THROTTLE_S:
        return
    _hb_last_touch = now
    try:
        if os.path.isdir(os.path.dirname(_BROWSER_LOCK_HEARTBEAT)):
            with open(_BROWSER_LOCK_HEARTBEAT, "w") as f:
                f.write(str(int(now)))
    except Exception:
        pass


def _register_reddit_park_on_exit(cdp_base=""):
    """Shared tab lifecycle (2026-07-17 unification): park reddit tabs on
    reddit's own /robots.txt at process exit — the SAME single implementation
    twitter_browser uses (scripts/browser_lifecycle.py). Static page = no SPA
    to leak/crash while idle, URL still matches the reddit.com reuse
    preference above so no new tabs (Target.createTarget is the proven
    focus-steal trigger). S4L_NO_TAB_PARK=1 escape hatch inside the module.
    Best-effort: parking must never fail an attach."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from browser_lifecycle import register_park_on_exit

        register_park_on_exit(
            (cdp_base or os.environ.get("REDDIT_CDP_URL", "").strip()
             or "http://127.0.0.1:9557").rstrip("/"),
            ("reddit.com",),
            "https://www.reddit.com/robots.txt",
            "reddit_browser",
        )
    except Exception:
        pass


def get_browser_and_page(playwright):
    """Instrumented entry point: attach via _get_browser_and_page_raw, then
    (L1) set default per-op/navigation timeouts so no page call can hang
    forever, and (L2) wire the browser-lock liveness heartbeat. All new code
    should use THIS, never the raw variant."""
    browser, page, is_cdp = _get_browser_and_page_raw(playwright)
    try:
        page.set_default_timeout(BROWSER_OP_TIMEOUT_MS)
        page.set_default_navigation_timeout(BROWSER_NAV_TIMEOUT_MS)
    except Exception:
        pass
    _touch_browser_heartbeat()
    try:
        page.on("request", _touch_browser_heartbeat)
    except Exception:
        pass
    return browser, page, is_cdp


def _get_browser_and_page_raw(playwright):
    """Get a logged-in Reddit page, preferring CDP-attach over launch_persistent_context.

    Two paths:
      1. CDP-attach (preferred on appmaker/e2b VM and any host running a visible
         logged-in Chromium): connect to the existing browser, find a context with
         a live reddit_session cookie, open a NEW PAGE on that context.
      2. launch_persistent_context fallback: when CDP isn't available OR contexts
         have no reddit_session (laptop where reddit-agent MCP isolates its session
         in an invisible context).

    Why CDP-attach matters: appmaker's visible Chromium permanently holds
    /root/.chromium-profile. launch_persistent_context collides on profile leveldb
    locks, loads a partial session, and EVERY post returns account_blocked_in_sub
    because the comment form never renders. Attaching to the live context dodges
    the collision entirely.

    Returns (browser, page, is_cdp). When is_cdp=True, callers must close ONLY
    the page (not page.context) and NOT the browser; closing context[0] or the
    CDP browser would kill the user's visible session.
    """
    _acquire_browser_lock()

    # Preferred: explicit harness CDP endpoint (REDDIT_CDP_URL, set by
    # skill/lib/reddit-backend.sh -> http://127.0.0.1:9557). When present we
    # attach DIRECTLY to that URL and skip the ps-based port scan entirely.
    # This is the reddit-harness migration path (2026-05-29): the whole Reddit
    # pipeline rides a dedicated browser-harness Chrome on port 9557, profile
    # ~/.claude/browser-profiles/reddit-harness, mirroring twitter-harness.
    # Mirrors twitter's TWITTER_CDP_URL direct-attach pattern.
    cdp_url_env = (os.environ.get("REDDIT_CDP_URL") or "").strip()
    if cdp_url_env:
        try:
            cdp_browser = playwright.chromium.connect_over_cdp(cdp_url_env)
            _bh_activity_log("attach_harness", cdp_url_env)
            _register_reddit_park_on_exit(cdp_url_env)
            # Prefer a context that already carries a live reddit_session; else
            # fall back to the first context (harness is single-profile, logged in).
            chosen = None
            for ctx in cdp_browser.contexts:
                try:
                    cookies = ctx.cookies("https://www.reddit.com/")
                except Exception:
                    cookies = []
                if any(c.get("name") == "reddit_session" and c.get("value") for c in cookies):
                    chosen = ctx
                    break
            if chosen is None and cdp_browser.contexts:
                chosen = cdp_browser.contexts[0]
            if chosen is not None:
                # Reuse an existing tab instead of opening a new one. new_page()
                # steals OS focus every call (annoying when working in other apps);
                # navigating a background tab does not. Mirrors twitter_browser.
                # Prefer a tab already on reddit.com (not login); else pages[0];
                # else, only if the context has zero tabs, create one. The caller
                # navigates it and leaves it open (finally blocks don't close CDP tabs).
                for pg in chosen.pages:
                    if "reddit.com" in (pg.url or "") and "login" not in (pg.url or ""):
                        return cdp_browser, pg, True
                if chosen.pages:
                    return cdp_browser, chosen.pages[0], True
                page = chosen.new_page()
                return cdp_browser, page, True
            # No usable context: do NOT close the CDP browser (would kill the
            # harness Chrome); just disconnect by falling through.
        except Exception:
            pass

    cdp_port = find_reddit_cdp_port()

    if cdp_port:
        try:
            cdp_browser = playwright.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            _bh_activity_log("attach_legacy", f"http://localhost:{cdp_port}")
            for ctx in cdp_browser.contexts:
                try:
                    cookies = ctx.cookies("https://www.reddit.com/")
                except Exception:
                    cookies = []
                has_session = any(
                    c.get("name") == "reddit_session" and c.get("value")
                    for c in cookies
                )
                if has_session:
                    # Reuse an existing tab (no focus-steal); only new_page if none.
                    for pg in ctx.pages:
                        if "reddit.com" in (pg.url or "") and "login" not in (pg.url or ""):
                            return cdp_browser, pg, True
                    if ctx.pages:
                        return cdp_browser, ctx.pages[0], True
                    page = ctx.new_page()
                    return cdp_browser, page, True
            try:
                cdp_browser.close()
            except Exception:
                pass
        except Exception:
            pass

    # Fallback: launch our own persistent context against PROFILE_DIR.
    # Retry on Chromium SingletonLock collisions (MCP holds the OS-level profile
    # lock for its entire server lifetime; the JSON lock can expire while the
    # OS lock is still held).
    deadline = time.time() + LOCK_WAIT_MAX
    last_err = None
    while True:
        try:
            context = playwright.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
                viewport=VIEWPORT,
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            break
        except Exception as e:
            last_err = e
            if time.time() >= deadline:
                _release_browser_lock()
                print(json.dumps({
                    "success": False,
                    "error": f"chromium profile locked by another process; waited {LOCK_WAIT_MAX}s: {e}"
                }))
                sys.exit(1)
            time.sleep(LOCK_POLL_INTERVAL)
    page = context.new_page()
    return context, page, False


def _to_old_reddit(url):
    """Convert any reddit URL to old.reddit.com."""
    url = re.sub(r"https?://(www\.)?reddit\.com", "https://old.reddit.com", url)
    # Remove trailing query params that old reddit doesn't use
    url = re.sub(r"\?.*$", "", url)
    return url


def _ensure_old_reddit(page):
    """If page redirected to new reddit, navigate to old.reddit.com equivalent."""
    current = page.url
    if "old.reddit.com" in current:
        return
    if "reddit.com" in current and "old.reddit.com" not in current:
        old_url = _to_old_reddit(current)
        page.goto(old_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)


def post_comment(thread_url, text):
    """Post a top-level comment on a Reddit thread.

    Navigates to old.reddit.com thread, finds the comment textarea,
    types the comment text, and submits.

    Returns: {"ok": true, "permalink": "..."} or {"ok": false, "error": "..."}
    """
    # Identity gate (mirrors twitter_browser.reply_to_tweet): refuse to post
    # when no Reddit account resolves. Without it we cannot attribute the
    # comment or find our own permalink afterwards, and posting with a blank
    # identity pollutes the shared DB. Fail fast and loud instead.
    if not OUR_USERNAME:
        print("[reddit_browser] no reddit account configured "
              "(accounts.reddit.username / reddit_account.username / "
              "AUTOPOSTER_REDDIT_USERNAME); refusing to post.", file=sys.stderr)
        return {"ok": False, "error": "no_account_configured"}
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            old_url = _to_old_reddit(thread_url)
            page.goto(old_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            _ensure_old_reddit(page)

            # Check if thread exists using visible content only (old reddit hides
            # template strings like "there doesn't seem to be anything here" in the
            # page markup on every page, so text_content("body") gives false positives).
            content_el = page.locator("#siteTable, .sitetable.linklisting").first
            try:
                content_el.wait_for(state="attached", timeout=5000)
            except Exception:
                return {"ok": False, "error": "thread_not_found"}

            # A real 404 page shows an interstitial with class "interstitial"
            if page.locator(".interstitial").count() > 0:
                interstitial_text = page.locator(".interstitial").first.text_content() or ""
                if "page not found" in interstitial_text.lower():
                    return {"ok": False, "error": "thread_not_found"}
                if "this is an archived post" in interstitial_text.lower():
                    return {"ok": False, "error": "thread_archived"}

            # Check if the THREAD itself is locked. Must be scoped to the OP
            # container (`#siteTable .thing.self`). A bare `.locked-tagline`
            # lookup matches the per-comment "locked comment" badge that subs
            # like r/selfhosted use on their stickied moderator comments, which
            # caused false-positive thread_locked errors for ~3 NightOwl posts
            # on 2026-05-18 against perfectly open threads.
            if page.locator("#siteTable .thing.self .locked-tagline").count() > 0:
                return {"ok": False, "error": "thread_locked"}

            # Check if we're actually logged in (login redirect or no user element)
            if "login" in page.url.lower():
                return {"ok": False, "error": "not_logged_in"}

            # Tab-collision guard (2026-07-14): every reddit pipeline shares
            # the ONE harness tab, and a concurrent scan/fetch/engage session
            # can navigate it between our goto and the checks below. The
            # missing comment form then misclassified as account_blocked_in_sub
            # (a PERMANENT verdict) — 5/5 approved cards false-positived this
            # way on 2026-07-14. Verify the tab still shows OUR thread; re-goto
            # once if hijacked, and classify a persistent hijack as the
            # TRANSIENT tab_contention so the row is retried, never buried.
            import re as _re
            _tid_m = _re.search(r"/comments/([a-z0-9]+)", old_url)
            _tid = _tid_m.group(1) if _tid_m else None

            def _tab_is_ours():
                return bool(_tid) and f"/comments/{_tid}" in (page.url or "")

            if not _tab_is_ours():
                page.goto(old_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                _ensure_old_reddit(page)
                if not _tab_is_ours():
                    return {"ok": False, "error": "tab_contention"}

            # Check if the top-level comment form exists at all.
            # When the sub gates top-level commenting on this account (CrowdControl,
            # AutoMod karma/age threshold, mod-approved-only, shadowban), old reddit
            # silently omits the form for us while still rendering the rest of the
            # page. The sub itself may be public; the gate is account-level. There
            # is no error banner and no API field that exposes this, so the only
            # signal is the missing form on a logged-in page load.
            _form_sel = ".commentarea .usertext.cloneable, .commentarea > form.usertext"
            has_comment_form = page.locator(_form_sel).count() > 0
            if not has_comment_form:
                # Slow-render tolerance: give the form a short explicit wait
                # before concluding the sub gates this account (an instant
                # count() on a slow load is another false-positive source).
                try:
                    page.locator(_form_sel).first.wait_for(state="attached", timeout=6000)
                    has_comment_form = True
                except Exception:
                    pass
            if not has_comment_form:
                if not _tab_is_ours():
                    return {"ok": False, "error": "tab_contention"}
                return {"ok": False, "error": "account_blocked_in_sub"}

            # Some subs render the form but show a gate notice instead of a usable
            # textarea (CrowdControl on AutoMod-flagged users, "you must have X karma
            # to comment in r/sub", subreddit quarantine consent, etc.). Detect these
            # before burning 5+3s of textarea polling. The "infobar" / "md-container"
            # banner above .commentarea carries the gate text. We pattern-match a few
            # well-known phrases so we return early with the correct error.
            gate_phrases = [
                "you must be a subscriber",
                "you don't have permission to comment",
                "you must have at least",
                "minimum karma",
                "minimum account age",
                "crowdcontrol",
                "this community has restricted",
                "verified email",
                "only approved users",
                "you must agree",  # quarantine consent
            ]
            try:
                preamble = (page.locator(
                    ".commentarea, .infobar, .md-container, .interstitial"
                ).first.text_content(timeout=1500) or "").lower()
                if any(p in preamble for p in gate_phrases):
                    return {"ok": False, "error": "account_blocked_in_sub"}
            except Exception:
                pass

            # Find the top-level comment form textarea.
            comment_form = page.locator(
                ".commentarea > form.usertext textarea, "
                ".commentarea > .usertext-edit textarea, "
                ".commentarea > .usertext textarea"
            ).first

            try:
                comment_form.wait_for(state="visible", timeout=5000)
            except Exception:
                # Broader fallback: any textarea in the comment area that's
                # NOT inside a .comment (those are reply forms)
                try:
                    comment_form = page.locator(
                        ".commentarea textarea"
                    ).first
                    comment_form.wait_for(state="visible", timeout=3000)
                except Exception:
                    return {"ok": False, "error": "comment_box_not_found"}

            # Even if the textarea is "visible", a sub may render it disabled or
            # with a readonly attribute (some quarantined / restricted-mode subs do
            # this). Fail fast as account_blocked_in_sub so salvage doesn't keep
            # retrying the same dead thread.
            try:
                is_disabled = comment_form.evaluate(
                    "el => !!(el.disabled || el.readOnly || "
                    "el.getAttribute('aria-disabled') === 'true' || "
                    "el.closest('.disabled,.usertext-disabled'))"
                )
                if is_disabled:
                    return {"ok": False, "error": "account_blocked_in_sub"}
            except Exception:
                pass

            # Fill the textarea (old reddit uses standard textareas)
            comment_form.fill(text)
            page.wait_for_timeout(1000)

            # Click the save/submit button
            save_btn = page.locator(
                ".commentarea button.save[type='submit'], "
                ".commentarea > form.usertext button[type='submit'], "
                ".commentarea > .usertext button[type='submit'], "
                ".commentarea > .usertext-edit button[type='submit']"
            ).first

            try:
                save_btn.wait_for(state="visible", timeout=3000)
                save_btn.click()
            except Exception:
                # Fallback: find any visible save button in the comment area
                try:
                    save_btn = page.locator(
                        ".commentarea button:has-text('save')"
                    ).first
                    save_btn.click()
                except Exception:
                    return {"ok": False, "error": "save_button_not_found"}

            page.wait_for_timeout(5000)

            # Check for errors (rate limit, etc.)
            error_el = page.locator(".status.error, .error").first
            try:
                if error_el.is_visible():
                    error_text = error_el.text_content() or "unknown_error"
                    return {"ok": False, "error": error_text.strip()}
            except Exception:
                pass

            # Capture the final URL. On a successful submit, old.reddit
            # redirects to the new permalink (.../comments/<thread>/.../<comment_id>/),
            # so a URL still equal to the thread URL is a strong signal the
            # comment never landed (silent shadow-reject / anti-spam).
            try:
                final_url = page.url
            except Exception:
                final_url = ""

            # Try to find the permalink of our new comment
            permalink = page.evaluate("""(ourUsername) => {
                // Find comments by our username, get the last one (most recent)
                const authorLinks = document.querySelectorAll(
                    '.comment a.author[href*="/' + ourUsername + '"]'
                );
                if (authorLinks.length === 0) return null;
                const lastAuthor = authorLinks[authorLinks.length - 1];
                // Walk up to the .comment container
                let comment = lastAuthor.closest('.comment');
                if (!comment) return null;
                // Find the permalink
                const perma = comment.querySelector('a.bylink[href*="/comments/"]');
                if (perma) return perma.getAttribute('href');
                return null;
            }""", OUR_USERNAME)

            if not permalink:
                # Dump HTML + screenshot so we can post-mortem (silent shadow-reject
                # vs slow render vs DOM selector miss). final_url tells us which.
                try:
                    debug_dir = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "log",
                    )
                    os.makedirs(debug_dir, exist_ok=True)
                    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                    base = os.path.join(
                        debug_dir,
                        f"reddit_browser_post_fail_{stamp}_{os.getpid()}",
                    )
                    try:
                        html = page.content()[:200000]
                        with open(base + ".html", "w") as f:
                            f.write(html)
                    except Exception:
                        pass
                    try:
                        page.screenshot(path=base + ".png", full_page=False)
                    except Exception:
                        pass
                    print(
                        f"[reddit_browser] post-comment no permalink; "
                        f"final_url={final_url} thread_url={thread_url} "
                        f"dump={base}",
                        file=sys.stderr,
                    )
                except Exception:
                    pass

                # If the URL never redirected away from the thread page, the
                # submit didn't take. Surface as an explicit error so callers
                # can distinguish this from "submitted but slow render".
                try:
                    norm_old = _to_old_reddit(thread_url).rstrip("/")
                    norm_final = (final_url or "").rstrip("/")
                except Exception:
                    norm_old = thread_url
                    norm_final = final_url or ""
                if norm_final == norm_old:
                    return {
                        "ok": False,
                        "error": "no_redirect_after_submit",
                        "thread_url": thread_url,
                        "final_url": final_url,
                    }

            return {
                "ok": True,
                "permalink": permalink,
                "thread_url": thread_url,
                "final_url": final_url,
            }

        finally:
            # Harness (is_cdp) tabs are REUSED across invocations, so never close
            # the page here: closing it forces the next run to new_page(), which
            # steals OS focus from whatever app is in front. Mirrors twitter_browser
            # (only the persistent-context fallback closes). cleanup_harness_tabs at
            # cycle start bounds tab count to one.
            try:
                if not is_cdp:
                    page.context.close()
            except Exception:
                pass
            if not is_cdp:
                try:
                    browser.close()
                except Exception:
                    pass


def reply_to_comment(comment_permalink, text, dm_id=None):
    """Reply to an existing Reddit comment.

    Navigates to the comment permalink on old.reddit.com, clicks the
    "reply" link to expand the reply box, fills in the text, and submits.

    Active Reddit campaigns with a `suffix` are applied at this tool layer:
    the suffix is appended to `text` (per `sample_rate` coin flip per
    campaign) before typing, so the literal text is guaranteed to be
    delivered. When `dm_id` is provided, after a verified post the message
    is logged via dm_conversation.py log-outbound so dm_messages.campaign_id
    auto-attributes via suffix detection (single source of truth).

    Returns: {"ok": true, "applied_campaigns": [...], "reply_text": "..."}
              or {"ok": false, "error": "..."}
    """
    # Identity gate (mirrors twitter_browser.reply_to_tweet): no resolved
    # account -> no post. See post_comment above for the rationale.
    if not OUR_USERNAME:
        print("[reddit_browser] no reddit account configured "
              "(accounts.reddit.username / reddit_account.username / "
              "AUTOPOSTER_REDDIT_USERNAME); refusing to post.", file=sys.stderr)
        return {"ok": False, "error": "no_account_configured"}
    from playwright.sync_api import sync_playwright

    # Pre-flight: refuse to attempt a reply in a sub we know we can't comment in.
    # Without this gate, an inbound that landed in `dms` while the sub was still
    # allowed will keep cycling through the engage-dm-replies pipeline, fail with
    # `reply_link_not_found`, and trigger a needs_human escalation every run.
    sub = _subreddit_from_permalink(comment_permalink)
    if sub and sub in _load_comment_blocked_subs():
        auto_closed = False
        if dm_id is not None:
            try:
                subprocess.run(
                    ["python3",
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "dm_conversation.py"),
                     "set-status", "--dm-id", str(dm_id), "--status", "closed"],
                    capture_output=True, text=True, timeout=20,
                )
                auto_closed = True
            except Exception as e:
                print(f"[reply_to_comment] auto-close failed for dm_id={dm_id}: {e}",
                      file=sys.stderr)
        return {
            "ok": False,
            "error": "subreddit_blocked",
            "subreddit": sub,
            "auto_closed": auto_closed,
        }

    # Tool-level URL wrap pass: every URL in the model's reply gets minted
    # through dm_short_links.wrap_text so clicks attribute to this DM. Runs
    # BEFORE campaign-suffix injection so suffixes (which are short literals,
    # not URLs) aren't fed back through the wrapper. Refuses if any URL points
    # at a project not in dms.target_projects[]; the engage pipeline is expected
    # to set-target-project --append and retry.
    minted_link_codes = []
    if dm_id is not None:
        from dm_short_links import wrap_text as _wrap_text  # local import: avoid import cost when dm_id is None
        wrap_res = _wrap_text(dm_id=dm_id, text=text)
        if not wrap_res.get("ok"):
            return {
                "ok": False,
                "error": "link_wrap_failed",
                "wrap_error": wrap_res.get("error"),
                "needed_project": wrap_res.get("needed_project"),
                "url": wrap_res.get("url"),
            }
        text = wrap_res["text"]
        minted_link_codes = wrap_res.get("minted_codes", [])

    # Tool-level campaign suffix injection (mirrors send_dm), gated on dm_id.
    # The DM-replies pipeline passes dm_id and relies on this layer to
    # guarantee the suffix is delivered. The standalone reply pipeline
    # (engage_reddit.py) runs its OWN pre-append at the engage_reddit layer
    # and does NOT pass dm_id, so we skip injection here to avoid a second
    # coin flip stacking on top of the first (which would push the effective
    # tag rate to ~1-(1-r)^2 and burn the campaign budget faster than
    # intended). The endsWith guard is still useful when engage_reddit's
    # gate fired: surface the cid so the caller can bump if desired.
    applied_campaigns = []
    if dm_id is not None:
        for cid, suffix, sample_rate in _load_active_reddit_campaigns_for_dm():
            if random.random() < sample_rate:
                text = text + suffix
                applied_campaigns.append(cid)
    else:
        for cid, suffix, _ in _load_active_reddit_campaigns_for_dm():
            if suffix and text.endswith(suffix):
                applied_campaigns.append(cid)
    _diag_msg = f"[reply_to_comment] applied_campaigns={applied_campaigns} minted_links={minted_link_codes} text_len={len(text)} dm_id={dm_id}"
    print(_diag_msg, file=sys.stderr)
    _diag_log(_diag_msg)

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            old_url = _to_old_reddit(comment_permalink)
            # Don't add ?context= — it shifts the target comment up
            page.goto(old_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            _ensure_old_reddit(page)

            # Check if comment exists
            page_text = page.text_content("body") or ""
            if "page not found" in page_text.lower():
                return {"ok": False, "error": "comment_not_found"}

            # Dedup: check if we already replied to this specific comment
            already = page.evaluate("""(ourUsername) => {
                // Find the target comment (highlighted or first in nested listing)
                const target = document.querySelector(
                    '.nestedlisting > .comment, .comment.target'
                );
                if (!target) return null;
                // Check direct child replies for our username
                const childComments = target.querySelectorAll(
                    ':scope > .child .comment'
                );
                for (const c of childComments) {
                    const author = c.querySelector('a.author');
                    if (author && author.textContent.trim() === ourUsername) {
                        const body = c.querySelector('.usertext-body');
                        const perma = c.querySelector('a.bylink');
                        return {
                            already_replied: true,
                            text: body ? body.textContent.trim() : '',
                            url: perma ? perma.getAttribute('href') : '',
                        };
                    }
                }
                return null;
            }""", OUR_USERNAME)

            if already and already.get("already_replied"):
                return {
                    "ok": True,
                    "already_replied": True,
                    "existing_text": already.get("text", ""),
                    "existing_url": already.get("url", ""),
                    "comment_permalink": comment_permalink,
                }

            # Click the reply link on the target comment
            reply_clicked = False

            # Strategy 1: Find the target/highlighted comment's reply link
            try:
                reply_link = page.locator(
                    ".nestedlisting > .comment .flat-list a:has-text('reply'), "
                    ".comment.target .flat-list a:has-text('reply')"
                ).first
                reply_link.wait_for(state="visible", timeout=5000)
                reply_link.click()
                reply_clicked = True
            except Exception:
                pass

            # Strategy 2: If only one comment visible, use its reply link
            if not reply_clicked:
                try:
                    reply_link = page.locator(
                        ".comment .flat-list a:has-text('reply')"
                    ).first
                    reply_link.wait_for(state="visible", timeout=3000)
                    reply_link.click()
                    reply_clicked = True
                except Exception:
                    pass

            if not reply_clicked:
                return {"ok": False, "error": "reply_link_not_found"}

            page.wait_for_timeout(1000)

            # Find the reply textarea that just appeared (pick the visible one)
            reply_box = None
            all_ta = page.locator(".comment .usertext-edit textarea")
            for i in range(all_ta.count()):
                if all_ta.nth(i).is_visible():
                    reply_box = all_ta.nth(i)
                    break

            if not reply_box:
                return {"ok": False, "error": "reply_textarea_not_found"}

            # Fill the reply text
            reply_box.fill(text)
            page.wait_for_timeout(1000)

            # Click the save button nearest to the visible reply box
            save_btn = None
            all_btns = page.locator(
                ".comment .usertext-edit button[type='submit']"
            )
            for i in range(all_btns.count()):
                if all_btns.nth(i).is_visible():
                    save_btn = all_btns.nth(i)
                    break

            if not save_btn:
                return {"ok": False, "error": "reply_save_button_not_found"}

            save_btn.click()

            page.wait_for_timeout(5000)

            # Check for errors
            error_el = page.locator(".status.error, .error").first
            try:
                if error_el.is_visible():
                    error_text = error_el.text_content() or "unknown_error"
                    return {"ok": False, "error": error_text.strip()}
            except Exception:
                pass

            # Verify: check if our comment appeared
            verified = page.evaluate("""(ourUsername) => {
                const authorLinks = document.querySelectorAll(
                    '.comment a.author[href*="/' + ourUsername + '"]'
                );
                return authorLinks.length > 0;
            }""", OUR_USERNAME)

            # When invoked from the DM-replies pipeline (dm_id provided), log
            # the outbound through the canonical CLI so dm_messages.campaign_id
            # auto-attributes via the suffix-detection path. Mirrors send_dm.
            if verified and dm_id is not None:
                _log_dm_outbound("", text, dm_id=dm_id, minted_codes=minted_link_codes)

            return {
                "ok": True,
                "verified": verified,
                "comment_permalink": comment_permalink,
                "reply_text": text,
                "applied_campaigns": applied_campaigns,
                "minted_link_codes": minted_link_codes,
            }

        finally:
            # Harness (is_cdp) tabs are REUSED across invocations, so never close
            # the page here: closing it forces the next run to new_page(), which
            # steals OS focus from whatever app is in front. Mirrors twitter_browser
            # (only the persistent-context fallback closes). cleanup_harness_tabs at
            # cycle start bounds tab count to one.
            try:
                if not is_cdp:
                    page.context.close()
            except Exception:
                pass
            if not is_cdp:
                try:
                    browser.close()
                except Exception:
                    pass


def edit_comment(comment_permalink, new_text):
    """Edit an existing Reddit comment.

    Navigates to the comment permalink on old.reddit.com, clicks "edit",
    replaces the textarea content, and saves.

    Returns: {"ok": true} or {"ok": false, "error": "..."}
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            old_url = _to_old_reddit(comment_permalink)
            page.goto(old_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            _ensure_old_reddit(page)

            page_text = page.text_content("body") or ""
            if "page not found" in page_text.lower():
                return {"ok": False, "error": "comment_not_found"}

            # Find the target comment: on a permalink page, it's the
            # top-level comment in the nested listing, or has .target class
            target_comment = page.locator(
                ".nestedlisting > .comment"
            ).first
            try:
                target_comment.wait_for(state="visible", timeout=5000)
            except Exception:
                # Fallback: try .comment.target
                target_comment = page.locator(".comment.target").first
                try:
                    target_comment.wait_for(state="visible", timeout=3000)
                except Exception:
                    return {"ok": False, "error": "target_comment_not_found"}

            # Click the "edit" link within the target comment's own flat-list
            # (use :scope > to avoid matching nested child comments)
            edit_clicked = False
            try:
                edit_link = target_comment.locator(
                    ":scope > .entry .flat-list a:has-text('edit')"
                ).first
                edit_link.wait_for(state="visible", timeout=5000)
                edit_link.click()
                edit_clicked = True
            except Exception:
                pass

            if not edit_clicked:
                return {"ok": False, "error": "edit_link_not_found"}

            page.wait_for_timeout(1000)

            # Find the edit textarea within the target comment's own entry
            edit_box = None
            all_ta = target_comment.locator(
                ":scope > .entry .usertext-edit textarea"
            )
            for i in range(all_ta.count()):
                if all_ta.nth(i).is_visible():
                    edit_box = all_ta.nth(i)
                    break

            if not edit_box:
                return {"ok": False, "error": "edit_textarea_not_found"}

            # Clear and fill with new text
            edit_box.fill(new_text)
            page.wait_for_timeout(1000)

            # Click save within the target comment's own entry
            save_btn = None
            all_btns = target_comment.locator(
                ":scope > .entry .usertext-edit button[type='submit']"
            )
            for i in range(all_btns.count()):
                if all_btns.nth(i).is_visible():
                    save_btn = all_btns.nth(i)
                    break

            if not save_btn:
                return {"ok": False, "error": "edit_save_button_not_found"}

            save_btn.click()

            page.wait_for_timeout(4000)

            # Verify the edit was saved within the target comment
            target_id = target_comment.get_attribute("data-fullname") or ""
            verified = page.evaluate("""([newTextStart, targetId]) => {
                let comment;
                if (targetId) {
                    comment = document.querySelector(
                        '.comment[data-fullname="' + targetId + '"]'
                    );
                } else {
                    comment = document.querySelector(
                        '.nestedlisting > .comment'
                    );
                }
                if (!comment) return false;
                const body = comment.querySelector(
                    ':scope > .entry .usertext-body'
                );
                return body && body.textContent &&
                    body.textContent.includes(newTextStart);
            }""", [new_text[:50], target_id])

            return {
                "ok": True,
                "verified": verified,
                "comment_permalink": comment_permalink,
            }

        finally:
            # Harness (is_cdp) tabs are REUSED across invocations, so never close
            # the page here: closing it forces the next run to new_page(), which
            # steals OS focus from whatever app is in front. Mirrors twitter_browser
            # (only the persistent-context fallback closes). cleanup_harness_tabs at
            # cycle start bounds tab count to one.
            try:
                if not is_cdp:
                    page.context.close()
            except Exception:
                pass
            if not is_cdp:
                try:
                    browser.close()
                except Exception:
                    pass


def edit_thread(thread_permalink, new_body):
    """Edit the selftext of a Reddit thread we authored.

    Used by the campaign system to append a literal suffix to original
    threads after submit. Only works on selftext posts (link posts have no
    edit link). Mirrors edit_comment but targets the main post (#siteTable
    .thing.self) instead of a nested comment.

    Returns: {"ok": true, "verified": bool} or {"ok": false, "error": "..."}
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            old_url = _to_old_reddit(thread_permalink)
            page.goto(old_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            _ensure_old_reddit(page)

            page_text = page.text_content("body") or ""
            if "page not found" in page_text.lower():
                return {"ok": False, "error": "thread_not_found"}

            target = page.locator("#siteTable .thing.self").first
            try:
                target.wait_for(state="visible", timeout=5000)
            except Exception:
                return {"ok": False, "error": "thread_not_found"}

            edit_clicked = False
            try:
                edit_link = target.locator(
                    ":scope .entry .flat-list a:has-text('edit')"
                ).first
                edit_link.wait_for(state="visible", timeout=5000)
                edit_link.click()
                edit_clicked = True
            except Exception:
                pass

            if not edit_clicked:
                return {"ok": False, "error": "edit_link_not_found"}

            page.wait_for_timeout(1000)

            edit_box = None
            all_ta = target.locator(
                ":scope .entry .usertext-edit textarea"
            )
            for i in range(all_ta.count()):
                if all_ta.nth(i).is_visible():
                    edit_box = all_ta.nth(i)
                    break

            if not edit_box:
                return {"ok": False, "error": "edit_textarea_not_found"}

            edit_box.fill(new_body)
            page.wait_for_timeout(1000)

            save_btn = None
            all_btns = target.locator(
                ":scope .entry .usertext-edit button[type='submit']"
            )
            for i in range(all_btns.count()):
                if all_btns.nth(i).is_visible():
                    save_btn = all_btns.nth(i)
                    break

            if not save_btn:
                return {"ok": False, "error": "edit_save_button_not_found"}

            save_btn.click()
            page.wait_for_timeout(4000)

            verified = page.evaluate("""(newTextStart) => {
                const t = document.querySelector('#siteTable .thing.self');
                if (!t) return false;
                const body = t.querySelector('.entry .usertext-body');
                return body && body.textContent &&
                    body.textContent.includes(newTextStart);
            }""", new_body[-50:] if len(new_body) >= 50 else new_body)

            return {
                "ok": True,
                "verified": verified,
                "thread_permalink": thread_permalink,
            }

        finally:
            # Harness (is_cdp) tabs are REUSED across invocations, so never close
            # the page here: closing it forces the next run to new_page(), which
            # steals OS focus from whatever app is in front. Mirrors twitter_browser
            # (only the persistent-context fallback closes). cleanup_harness_tabs at
            # cycle start bounds tab count to one.
            try:
                if not is_cdp:
                    page.context.close()
            except Exception:
                pass
            if not is_cdp:
                try:
                    browser.close()
                except Exception:
                    pass


def unread_dms():
    """Scan Reddit for unread DMs/chat conversations.

    Navigates to old.reddit.com/message/unread/ for traditional messages,
    then checks reddit.com/chat for chat-style conversations.

    Returns: list of conversations with author, preview, time, thread_url.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            conversations = []

            # Part 1: Check old.reddit.com/message/unread/ for traditional PMs
            page.goto(
                "https://old.reddit.com/message/unread/",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(3000)
            _ensure_old_reddit(page)

            old_messages = page.evaluate("""() => {
                const results = [];
                const messages = document.querySelectorAll('.message');
                for (const msg of messages) {
                    // Author
                    const authorEl = msg.querySelector('.author');
                    const author = authorEl ? authorEl.textContent.trim() : '';

                    // Subject
                    const subjectEl = msg.querySelector('a.title, .subject a');
                    const subject = subjectEl ? subjectEl.textContent.trim() : '';

                    // Body preview
                    const bodyEl = msg.querySelector('.md');
                    const body = bodyEl ? bodyEl.textContent.trim() : '';

                    // Time
                    const timeEl = msg.querySelector('time, .live-timestamp');
                    const time = timeEl
                        ? (timeEl.getAttribute('title') || timeEl.textContent.trim())
                        : '';

                    // Detect comment replies vs actual PMs
                    // Comment replies link to /comments/ threads in the subject
                    const commentLink = msg.querySelector('a[href*="/comments/"]');
                    const isCommentReply = !!commentLink;

                    let threadUrl = '';
                    let msgType = 'pm';

                    if (isCommentReply) {
                        // Comment reply: extract the thread permalink
                        msgType = 'comment_reply';
                        const href = commentLink.getAttribute('href') || '';
                        threadUrl = href.startsWith('http')
                            ? href
                            : 'https://old.reddit.com' + href;
                    } else {
                        // Actual PM: use the message's own permalink
                        const permaLink = msg.querySelector(
                            'a.bylink, a[data-event-action="permalink"]'
                        );
                        if (permaLink) {
                            const href = permaLink.getAttribute('href') || '';
                            threadUrl = href.startsWith('http')
                                ? href
                                : 'https://old.reddit.com' + href;
                        }
                    }

                    if (author) {
                        results.push({
                            author: author,
                            subject: subject,
                            preview_short: body,
                            time: time,
                            thread_url: threadUrl,
                            type: msgType,
                        });
                    }
                }
                return results;
            }""")

            conversations.extend(old_messages)

            # Part 2: Check reddit.com/chat for chat-style messages
            page.goto(
                "https://www.reddit.com/chat",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(5000)

            # Reddit Chat sidebar has links like:
            #   <a href="/chat/room/ID">topic name</a>
            # Each contains a last-message preview in a child element
            # with text like "Username: message preview"
            chat_rooms = page.evaluate("""() => {
                const results = [];
                const links = document.querySelectorAll(
                    'nav a[href*="/chat/"], a[href*="/chat/room/"]'
                );

                for (const link of links) {
                    const href = link.getAttribute('href') || '';
                    if (!href.includes('/chat/')) continue;
                    // Skip non-room links
                    if (href === '/chat/' || href.includes('create')) continue;

                    const threadUrl = href.startsWith('http')
                        ? href
                        : 'https://www.reddit.com' + href;

                    // Topic/room name from the link's accessible name or text
                    const topic = (link.getAttribute('aria-label')
                        || link.textContent || '').trim();

                    // Last message preview — look for child elements
                    // Format: "Username: message text"
                    let author = '';
                    let preview = '';
                    const allText = link.textContent || '';
                    // The preview is usually in a nested element
                    const spans = link.querySelectorAll('span, div, p');
                    for (const s of spans) {
                        const t = s.textContent.trim();
                        // Match "Username: preview text"
                        const m = t.match(/^(\\S+):\\s*(.+)/);
                        if (m && m[1].length < 30) {
                            author = m[1];
                            preview = m[2];
                            break;
                        }
                    }

                    // Check for unread badge (aria-label with "unread")
                    const hasUnread = link.querySelector(
                        '[aria-label*="unread"]'
                    ) !== null;

                    if (topic.length > 1) {
                        results.push({
                            author: author || topic,
                            subject: topic,
                            preview: preview,
                            time: '',
                            thread_url: threadUrl,
                            type: 'chat',
                            has_unread: hasUnread,
                        });
                    }
                }

                return results;
            }""")

            conversations.extend(chat_rooms)

            # Deduplicate by author
            seen = set()
            unique = []
            for c in conversations:
                key = c.get("author", "").lower()
                if key and key not in seen:
                    seen.add(key)
                    unique.append(c)

            return unique

        finally:
            # Harness (is_cdp) tabs are REUSED across invocations, so never close
            # the page here: closing it forces the next run to new_page(), which
            # steals OS focus from whatever app is in front. Mirrors twitter_browser
            # (only the persistent-context fallback closes). cleanup_harness_tabs at
            # cycle start bounds tab count to one.
            try:
                if not is_cdp:
                    page.context.close()
            except Exception:
                pass
            if not is_cdp:
                try:
                    browser.close()
                except Exception:
                    pass


def read_conversation(chat_url, max_messages=20):
    """Read messages from a Reddit chat or PM thread.

    For chat URLs (reddit.com/chat/...), navigates to the chat and extracts
    messages. For PM URLs (old.reddit.com/message/...), reads the PM thread.

    Returns: {"partner_name": "...", "messages": [...], "total_found": N}
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            is_chat = "/chat" in chat_url and "message" not in chat_url

            if is_chat:
                # Reddit Chat (SPA on new reddit)
                page.goto(chat_url, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)

                # Reddit Chat uses accessible names on message elements:
                # "USERNAME said TIME_AGO, MESSAGE_TEXT, N replies, N reactions"
                # Extract via aria labels on generic elements
                result = page.evaluate("""(params) => {
                    const maxMessages = params.maxMessages;
                    const ourUsername = params.ourUsername;
                    let partnerName = '';
                    const messages = [];

                    // Get chat room name from the header
                    const headerEls = document.querySelectorAll(
                        '[aria-label*="Current chat"]'
                    );
                    for (const h of headerEls) {
                        const label = h.getAttribute('aria-label') || '';
                        const m = label.match(/Current chat,\\s*(.+)/);
                        if (m) { partnerName = m[1]; break; }
                    }
                    // Fallback: look for header text
                    if (!partnerName) {
                        const headers = document.querySelectorAll('h1, h2, h3');
                        for (const h of headers) {
                            const t = h.textContent.trim();
                            if (t.length > 1 && t.length < 60 && !t.includes('Chat')
                                && !t.includes('Reddit')) {
                                partnerName = t;
                                break;
                            }
                        }
                    }

                    // Find message elements by their accessible name pattern:
                    // "USERNAME said TIME, TEXT, N replies, N reactions"
                    const allEls = document.querySelectorAll('[aria-label]');
                    for (const el of allEls) {
                        const label = el.getAttribute('aria-label') || '';
                        // Match: "Username said time_ago, message text, N replies"
                        const m = label.match(
                            /^(\\S+) said (.+?),\\s*(.+?),\\s*\\d+ repl/
                        );
                        if (!m) continue;

                        const sender = m[1];
                        const time = m[2];
                        let content = m[3];

                        // Clean up content (remove trailing ", 0 reactions" etc)
                        content = content.replace(/,\\s*\\d+ reactions?$/, '').trim();

                        const isFromUs = sender.toLowerCase()
                            === ourUsername.toLowerCase();
                        if (!isFromUs && sender) {
                            partnerName = partnerName || sender;
                        }

                        messages.push({
                            sender: sender,
                            content: content,
                            time: time,
                            is_from_us: isFromUs,
                        });
                    }

                    const recent = messages.slice(-maxMessages);
                    return {
                        partner_name: partnerName,
                        messages: recent,
                        total_found: messages.length,
                    };
                }""", {"maxMessages": max_messages, "ourUsername": OUR_USERNAME})

                return result

            else:
                # Traditional PM thread on old.reddit.com
                old_url = _to_old_reddit(chat_url)
                page.goto(old_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                _ensure_old_reddit(page)

                result = page.evaluate("""(params) => {
                    const maxMessages = params.maxMessages;
                    const ourUsername = params.ourUsername;
                    let partnerName = '';
                    const messages = [];

                    const msgEls = document.querySelectorAll('.message');
                    for (const msg of msgEls) {
                        const authorEl = msg.querySelector('.author');
                        const sender = authorEl
                            ? authorEl.textContent.trim() : '';

                        const bodyEl = msg.querySelector('.md');
                        const content = bodyEl
                            ? bodyEl.textContent.trim() : '';

                        const timeEl = msg.querySelector(
                            'time, .live-timestamp'
                        );
                        const time = timeEl
                            ? (timeEl.getAttribute('title')
                               || timeEl.textContent.trim())
                            : '';

                        const isFromUs = sender.toLowerCase()
                            === ourUsername.toLowerCase();

                        if (!isFromUs && sender) {
                            partnerName = sender;
                        }

                        if (content) {
                            messages.push({
                                sender: sender,
                                content: content,
                                time: time,
                                is_from_us: isFromUs,
                            });
                        }
                    }

                    const recent = messages.slice(-maxMessages);
                    return {
                        partner_name: partnerName,
                        messages: recent,
                        total_found: messages.length,
                    };
                }""", {"maxMessages": max_messages, "ourUsername": OUR_USERNAME})

                return result

        finally:
            # Harness (is_cdp) tabs are REUSED across invocations, so never close
            # the page here: closing it forces the next run to new_page(), which
            # steals OS focus from whatever app is in front. Mirrors twitter_browser
            # (only the persistent-context fallback closes). cleanup_harness_tabs at
            # cycle start bounds tab count to one.
            try:
                if not is_cdp:
                    page.context.close()
            except Exception:
                pass
            if not is_cdp:
                try:
                    browser.close()
                except Exception:
                    pass


def _load_active_reddit_campaigns_for_dm():
    """Best-effort: returns [(id, suffix, sample_rate), ...] for active reddit
    campaigns. On any failure (no API reachable, transient error, etc.)
    returns []. This keeps reddit_browser.py usable when the website route
    is down without blocking the DM send.

    Migrated 2026-05-12 from direct SQL (campaigns table) to the
    /api/v1/campaigns route. The route's status/platform/has_suffix/
    with_budget_remaining filter set is an exact match for the legacy
    SELECT clauses (status='active', platforms ILIKE '%,reddit,%',
    suffix NOT NULL/empty, posts_made < max_posts_total).
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get
        resp = api_get(
            "/api/v1/campaigns",
            query={
                "status": "active",
                "platform": "reddit",
                "has_suffix": "true",
                "with_budget_remaining": "true",
                "limit": 500,
            },
        )
        data = (resp or {}).get("data") or {}
        rows = data.get("campaigns") or []
        out = []
        for r in rows:
            try:
                sample_rate = float(r.get("sample_rate") if r.get("sample_rate") is not None else 1.0)
                out.append((int(r["id"]), r["suffix"], sample_rate))
            except (TypeError, ValueError, KeyError):
                # Skip malformed rows rather than blowing up the entire load.
                continue
        return out
    except Exception:
        return []


def _log_dm_outbound(chat_url, content, dm_id=None, minted_codes=None):
    """After a successful send, log via the canonical CLI so the suffix-
    detection path attributes the message to the active campaign.

    If `dm_id` is provided (preferred), skip the lookup. Otherwise fall back
    to looking up the most recent dms row by chat_url. Many production rows
    have an empty `dms.chat_url`, so the dm_id path is the reliable one.
    `minted_codes` is the list of dm_links codes minted for this outbound's
    URLs; passed through env so log-outbound can backfill dm_links.message_id
    after RETURNING id. Returns True if log-outbound was invoked."""
    try:
        if dm_id is None:
            # chat_url -> dms.id lookup. Migrated 2026-05-12 from direct
            # SQL to GET /api/v1/dms?platform=reddit&chat_url=<url>&limit=1.
            # The route filters by exact chat_url and orders by
            # discovered_at DESC, which (like the legacy id DESC) picks
            # the most recent row when duplicates exist.
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            try:
                from http_api import api_get
                resp = api_get(
                    "/api/v1/dms",
                    query={"platform": "reddit", "chat_url": chat_url, "limit": 1},
                )
                rows = ((resp or {}).get("data") or {}).get("dms") or []
            except Exception as e:
                print(f"[reddit_browser] log-outbound chat_url lookup failed: {e}",
                      file=sys.stderr)
                return False
            if not rows:
                print("[reddit_browser] log-outbound skipped: no dm_id and chat_url lookup miss",
                      file=sys.stderr)
                return False
            dm_id = rows[0].get("id")
            if not dm_id:
                print("[reddit_browser] log-outbound skipped: API row missing id",
                      file=sys.stderr)
                return False
        env = os.environ.copy()
        if minted_codes:
            env["WRAP_MINTED_CODES"] = ",".join(minted_codes)
        subprocess.run(
            ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "dm_conversation.py"),
             "log-outbound", "--dm-id", str(dm_id), "--content", content, "--verified"],
            capture_output=True, text=True, timeout=20, env=env,
        )
        return True
    except Exception as e:
        print(f"[reddit_browser] internal log-outbound failed: {e}", file=sys.stderr)
        return False


def send_dm(chat_url, message, dm_id=None, apply_campaigns=True):
    """Send a message in a Reddit chat or PM thread.

    For chat URLs (reddit.com/chat/...), navigates to the chat room and
    types/sends the message. For PM URLs, uses old.reddit.com message compose.

    Active Reddit campaigns with a `suffix` are applied at this tool layer:
    the suffix is appended to `message` (per `sample_rate` coin flip per
    campaign) before typing, so the literal text is guaranteed to be
    delivered. After a verified send, logs via dm_conversation.py log-outbound
    so the campaign counter advances automatically (the CLI auto-detects the
    suffix in stored content).

    `dm_id` (optional) is preferred over chat_url for the post-send log; many
    rows have empty `dms.chat_url` so the chat_url lookup misses.

    Returns: {"ok": true, "thread_url": "...", "message_sent": "...",
              "applied_campaigns": [...]} or {"ok": false, "error": "..."}
    """
    from playwright.sync_api import sync_playwright

    # Tool-level URL wrap pass: every URL in the model's message gets minted
    # through dm_short_links.wrap_text so clicks attribute to this DM. Runs
    # BEFORE campaign-suffix injection. Refuses if any URL points at a project
    # not in dms.target_projects[]; the pipeline must set-target-project
    # --append before retrying.
    minted_link_codes = []
    if dm_id is not None:
        from dm_short_links import wrap_text as _wrap_text
        wrap_res = _wrap_text(dm_id=dm_id, text=message)
        if not wrap_res.get("ok"):
            return {
                "ok": False,
                "error": "link_wrap_failed",
                "wrap_error": wrap_res.get("error"),
                "needed_project": wrap_res.get("needed_project"),
                "url": wrap_res.get("url"),
            }
        message = wrap_res["text"]
        minted_link_codes = wrap_res.get("minted_codes", [])

    # Tool-level campaign suffix injection (guaranteed delivery of literal text).
    # apply_campaigns=False (via S4L_SKIP_CAMPAIGN_SUFFIX on the send-dm CLI)
    # opts this DM out — set ONLY for human-drafted escalation replies
    # (engage-dm-replies Phase 0); see twitter_browser.py send_dm for the
    # incident rationale. The autopilot never sets it.
    applied_campaigns = []
    for cid, suffix, sample_rate in (_load_active_reddit_campaigns_for_dm() if apply_campaigns else []):
        if random.random() < sample_rate:
            message = message + suffix
            applied_campaigns.append(cid)
    _diag_msg = f"[send_dm] applied_campaigns={applied_campaigns} minted_links={minted_link_codes} message_len={len(message)} dm_id={dm_id}"
    print(_diag_msg, file=sys.stderr)
    _diag_log(_diag_msg)

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            is_chat = "/chat" in chat_url and "message" not in chat_url

            if is_chat:
                # Reddit Chat (SPA)
                page.goto(chat_url, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)

                # Reddit Chat uses a textbox with placeholder "Message"
                msg_box = page.get_by_role("textbox", name="Write message")
                try:
                    msg_box.wait_for(state="visible", timeout=10000)
                except Exception:
                    # Fallback selectors
                    msg_box = None
                    for selector in [
                        'textarea[placeholder*="Message"]',
                        '[role="textbox"]',
                        'div[contenteditable="true"]',
                    ]:
                        try:
                            el = page.locator(selector).last
                            if el.is_visible():
                                msg_box = el
                                break
                        except Exception:
                            continue

                if not msg_box:
                    return {"ok": False, "error": "chat_input_not_found"}

                # Check if textbox is disabled (no chat selected)
                is_disabled = msg_box.evaluate(
                    "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
                )
                if is_disabled:
                    return {"ok": False, "error": "chat_input_disabled_no_chat_selected"}

                # Click and type the message
                msg_box.click()
                page.wait_for_timeout(500)

                # Use keyboard.type for contenteditable, fill for textarea
                tag = msg_box.evaluate("el => el.tagName.toLowerCase()")
                if tag == "textarea":
                    msg_box.fill(message)
                else:
                    page.keyboard.type(message, delay=10)

                page.wait_for_timeout(1000)

                # Send via Enter key (Reddit Chat sends on Enter)
                page.keyboard.press("Enter")
                page.wait_for_timeout(3000)

                # Verify message appeared in aria-labels
                msg_start = message[:50]
                verified = page.evaluate("""(msgStart) => {
                    const body = document.body.textContent || '';
                    return body.includes(msgStart);
                }""", msg_start)

                if verified:
                    _log_dm_outbound(chat_url, message, dm_id=dm_id, minted_codes=minted_link_codes)

                return {
                    "ok": verified,
                    "thread_url": page.url,
                    "verified": verified,
                    "message_sent": message,
                    "applied_campaigns": applied_campaigns,
                    "minted_link_codes": minted_link_codes,
                    "error": None if verified else "send_unverified_no_dom_confirmation",
                }

            else:
                # Traditional PM reply on old.reddit.com
                old_url = _to_old_reddit(chat_url)
                page.goto(old_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                _ensure_old_reddit(page)

                # Find the reply textarea in the PM thread
                reply_box = page.locator(
                    ".usertext-edit textarea, textarea[name='text']"
                ).last

                try:
                    reply_box.wait_for(state="visible", timeout=5000)
                except Exception:
                    return {"ok": False, "error": "pm_reply_box_not_found"}

                reply_box.fill(message)
                page.wait_for_timeout(1000)

                # Click save/submit
                save_btn = page.locator(
                    "button[type='submit']:has-text('save'), "
                    "button[type='submit']"
                ).last

                try:
                    save_btn.click()
                except Exception:
                    return {"ok": False, "error": "pm_save_button_not_found"}

                page.wait_for_timeout(4000)

                _log_dm_outbound(chat_url, message, dm_id=dm_id, minted_codes=minted_link_codes)

                return {
                    "ok": True,
                    "thread_url": page.url,
                    "verified": True,
                    "message_sent": message,
                    "applied_campaigns": applied_campaigns,
                    "minted_link_codes": minted_link_codes,
                }

        finally:
            # Harness (is_cdp) tabs are REUSED across invocations, so never close
            # the page here: closing it forces the next run to new_page(), which
            # steals OS focus from whatever app is in front. Mirrors twitter_browser
            # (only the persistent-context fallback closes). cleanup_harness_tabs at
            # cycle start bounds tab count to one.
            try:
                if not is_cdp:
                    page.context.close()
            except Exception:
                pass
            if not is_cdp:
                try:
                    browser.close()
                except Exception:
                    pass


def compose_dm(recipient, subject, body):
    """Compose and send a new Reddit DM/chat to a user.

    Navigates to reddit.com/message/compose/?to=recipient and fills in
    the subject and body fields. Supports both old reddit and new reddit
    compose forms.

    Returns: {"ok": true} or {"ok": false, "error": "..."}
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            # Use new reddit compose page directly (old reddit often redirects)
            compose_url = (
                f"https://www.reddit.com/message/compose/?to={recipient}"
            )
            page.goto(compose_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            # Check if we got redirected to new reddit chat
            if "chat" in page.url and "message/compose" not in page.url:
                # We're on new reddit chat - type and send
                page.wait_for_timeout(3000)

                # Find the message input
                msg_box = None
                for selector in [
                    'textarea',
                    'div[contenteditable="true"]',
                    '[role="textbox"]',
                ]:
                    try:
                        el = page.locator(selector).last
                        if el.is_visible():
                            msg_box = el
                            break
                    except Exception:
                        continue

                if not msg_box:
                    return {"ok": False, "error": "chat_input_not_found"}

                full_msg = f"{subject}\n\n{body}" if subject else body
                msg_box.click()
                page.wait_for_timeout(500)

                tag = msg_box.evaluate("el => el.tagName.toLowerCase()")
                if tag == "textarea":
                    msg_box.fill(full_msg)
                else:
                    page.keyboard.type(full_msg, delay=10)

                page.wait_for_timeout(1000)

                # Send
                try:
                    send_btn = page.locator(
                        'button[aria-label*="Send"], '
                        'button:has-text("Send")'
                    ).first
                    if send_btn.is_visible():
                        send_btn.click()
                    else:
                        page.keyboard.press("Enter")
                except Exception:
                    page.keyboard.press("Enter")

                page.wait_for_timeout(3000)

                # Verify message appeared in conversation DOM
                msg_start = full_msg[:50]
                verified = page.evaluate("""(msgStart) => {
                    const body = document.body.textContent || '';
                    return body.includes(msgStart);
                }""", msg_start)

                return {
                    "ok": verified,
                    "thread_url": page.url,
                    "verified": verified,
                    "error": None if verified else "compose_unverified_no_dom_confirmation",
                }

            elif "old.reddit.com" in page.url:
                # Old reddit compose form
                _ensure_old_reddit(page)

                # Fill subject
                subject_input = page.locator(
                    'input[name="subject"]'
                ).first
                try:
                    subject_input.wait_for(state="visible", timeout=3000)
                    subject_input.fill(subject)
                except Exception:
                    return {"ok": False, "error": "subject_field_not_found"}

                # Fill body
                body_input = page.locator(
                    'textarea[name="text"]'
                ).first
                try:
                    body_input.wait_for(state="visible", timeout=3000)
                    body_input.fill(body)
                except Exception:
                    return {"ok": False, "error": "body_field_not_found"}

                page.wait_for_timeout(1000)

                # Submit
                submit_btn = page.locator(
                    'button[type="submit"]'
                ).first
                try:
                    submit_btn.click()
                except Exception:
                    return {"ok": False, "error": "submit_button_not_found"}

                page.wait_for_timeout(4000)

                # Check for success (redirects to sent messages)
                if "sent" in page.url or "message" in page.url:
                    return {"ok": True, "thread_url": page.url}

                # Check for errors
                error_el = page.locator(".error").first
                try:
                    if error_el.is_visible():
                        return {
                            "ok": False,
                            "error": (error_el.text_content() or ""),
                        }
                except Exception:
                    pass

                return {"ok": True, "thread_url": page.url}

            else:
                # New reddit compose form (www.reddit.com/message/compose)
                # Reddit uses faceplate-text-input / faceplate-textarea-input
                # web components with shadow DOMs containing real inputs.

                page.wait_for_timeout(4000)

                # Fill form fields via shadow DOM inputs
                fill_result = page.evaluate("""(args) => {
                    const {recipient, subject, body} = args;

                    // Helper: find real input inside shadow root
                    function findShadowInput(host) {
                        if (!host || !host.shadowRoot) return null;
                        return host.shadowRoot.querySelector('input, textarea');
                    }

                    // Helper: set value with native setter + events
                    function setVal(el, value) {
                        const proto = el.tagName === 'TEXTAREA'
                            ? HTMLTextAreaElement.prototype
                            : HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        setter.call(el, value);
                        el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                    }

                    // Deep search through shadow roots
                    function deepQuery(root, selector) {
                        let result = root.querySelector(selector);
                        if (result) return result;
                        const all = root.querySelectorAll('*');
                        for (const el of all) {
                            if (el.shadowRoot) {
                                result = deepQuery(el.shadowRoot, selector);
                                if (result) return result;
                            }
                        }
                        return null;
                    }

                    // Find the faceplate custom elements (may be in shadow DOM)
                    const recipientHost = deepQuery(document, 'faceplate-text-input[name="message-recipient-input"]');
                    const titleHost = deepQuery(document, 'faceplate-text-input[name="message-title"]');
                    const messageHost = deepQuery(document, 'faceplate-textarea-input[name="message-content"]');

                    if (!recipientHost || !titleHost || !messageHost) {
                        // Debug: check what's on the page
                        const url = window.location.href;
                        const title_text = document.title;
                        const bodyText = (document.body ? document.body.textContent : '').substring(0, 500);
                        return {ok: false, error: 'faceplate_elements_not_found',
                                found: {recipient: !!recipientHost, title: !!titleHost, message: !!messageHost},
                                debug: {url, title_text, bodyText}};
                    }

                    const recipientInput = findShadowInput(recipientHost);
                    const titleInput = findShadowInput(titleHost);
                    const messageInput = findShadowInput(messageHost);

                    if (!recipientInput || !titleInput || !messageInput) {
                        return {ok: false, error: 'shadow_inputs_not_found',
                                found: {recipient: !!recipientInput, title: !!titleInput, message: !!messageInput}};
                    }

                    // Fill recipient if needed
                    if (!recipientInput.value || recipientInput.value.trim() !== recipient) {
                        setVal(recipientInput, recipient);
                        recipientHost.setAttribute('value', recipient);
                    }

                    // Fill title
                    setVal(titleInput, subject);
                    titleHost.setAttribute('value', subject);

                    // Fill message
                    setVal(messageInput, body);
                    messageHost.setAttribute('value', body);

                    return {ok: true};
                }""", {"recipient": recipient, "subject": subject, "body": body})

                if not fill_result.get("ok"):
                    return {"ok": False, "error": fill_result.get("error", "js_fill_failed")}

                page.wait_for_timeout(1500)

                # Click Send button
                send_clicked = page.evaluate("""() => {
                    // Search in shadow roots too
                    function findButtons(root) {
                        const btns = [];
                        root.querySelectorAll('button').forEach(b => btns.push(b));
                        root.querySelectorAll('*').forEach(el => {
                            if (el.shadowRoot) {
                                el.shadowRoot.querySelectorAll('button').forEach(b => btns.push(b));
                            }
                        });
                        return btns;
                    }
                    const buttons = findButtons(document);
                    for (const btn of buttons) {
                        const text = (btn.textContent || '').trim().toLowerCase();
                        if (text === 'send' && !btn.disabled) {
                            btn.click();
                            return {ok: true};
                        }
                    }
                    for (const btn of buttons) {
                        const text = (btn.textContent || '').trim().toLowerCase();
                        if (text === 'send') {
                            btn.click();
                            return {ok: true, was_disabled: true};
                        }
                    }
                    return {ok: false, error: 'send_button_not_found'};
                }""")

                if not send_clicked.get("ok"):
                    return {"ok": False, "error": "send_button_not_found"}

                page.wait_for_timeout(4000)

                # Check for "Message sent" confirmation
                try:
                    page_text = page.text_content("body") or ""
                    if "Message sent" in page_text:
                        return {"ok": True, "thread_url": page.url}
                except Exception:
                    pass

                # Check for error messages
                try:
                    error_el = page.locator('[role="alert"]').first
                    if error_el.is_visible():
                        return {
                            "ok": False,
                            "error": (error_el.text_content() or ""),
                        }
                except Exception:
                    pass

                # If we're still on compose page, assume success
                if "message" in page.url:
                    return {"ok": True, "thread_url": page.url}

                return {"ok": True, "thread_url": page.url}

        finally:
            # Harness (is_cdp) tabs are REUSED across invocations, so never close
            # the page here: closing it forces the next run to new_page(), which
            # steals OS focus from whatever app is in front. Mirrors twitter_browser
            # (only the persistent-context fallback closes). cleanup_harness_tabs at
            # cycle start bounds tab count to one.
            try:
                if not is_cdp:
                    page.context.close()
            except Exception:
                pass
            if not is_cdp:
                try:
                    browser.close()
                except Exception:
                    pass


def scrape_views(username, max_scrolls=300):
    """Scrape Reddit view counts from the user's profile pages.

    Navigates to 4 profile page variants (comments sorted by top/new,
    submitted sorted by top/new) and extracts view counts from articles.

    Returns: {"ok": true, "total": N, "with_views": N, "results": [{url, views}]}
    """
    from playwright.sync_api import sync_playwright

    profile_urls = [
        f"https://www.reddit.com/user/{username}/comments/?sort=top",
        f"https://www.reddit.com/user/{username}/comments/?sort=new",
        f"https://www.reddit.com/user/{username}/submitted/?sort=top&t=all",
        f"https://www.reddit.com/user/{username}/submitted/?sort=new",
    ]

    # Extract per-article: url (permalink), views (via visible text scan),
    # score + comment-count. Sources:
    #   Thread rows: <shreddit-post> SSR attrs → score + comment-count
    #   Comment rows: <shreddit-comment-action-row> nested in
    #                 <shreddit-profile-comment> → score (no reply count)
    extract_js = """() => {
        const results = [];
        document.querySelectorAll("article").forEach(article => {
            const post = article.querySelector("shreddit-post");
            let url = null;
            let score = null;
            let commentsCount = null;
            if (post) {
                const permalink = post.getAttribute("permalink");
                if (permalink) url = permalink;
                const s = post.getAttribute("score");
                if (s !== null && s !== "") {
                    const n = parseInt(s, 10);
                    if (!Number.isNaN(n)) score = n;
                }
                const cc = post.getAttribute("comment-count");
                if (cc !== null && cc !== "") {
                    const n = parseInt(cc, 10);
                    if (!Number.isNaN(n)) commentsCount = n;
                }
            } else {
                const row = article.querySelector("shreddit-comment-action-row");
                if (row) {
                    const permalink = row.getAttribute("permalink");
                    if (permalink) url = permalink;
                    const s = row.getAttribute("score");
                    if (s !== null && s !== "") {
                        const n = parseInt(s, 10);
                        if (!Number.isNaN(n)) score = n;
                    }
                }
            }
            if (!url) {
                const links = article.querySelectorAll('a[href*="/comments/"]');
                for (const link of links) {
                    const href = link.getAttribute("href");
                    if (href && href.includes("/comments/")) {
                        if (!url || href.includes("/comment/")) url = href;
                    }
                }
            }
            let views = null;
            for (const el of article.querySelectorAll("*")) {
                const text = el.textContent.trim();
                const match = text.match(/^([\\d,.]+)\\s*([KkMm])?\\s+views?$/);
                if (match) {
                    let v = parseFloat(match[1].replace(/,/g, ""));
                    if (match[2] && match[2].toLowerCase() === "k") v *= 1000;
                    if (match[2] && match[2].toLowerCase() === "m") v *= 1000000;
                    views = Math.round(v);
                    break;
                }
            }
            if (url) {
                results.push({
                    url: url.startsWith("http") ? url : "https://www.reddit.com" + url,
                    views: views,
                    score: score,
                    comments_count: commentsCount,
                });
            }
        });
        return results;
    }"""

    # url -> {views, score, comments_count} — keep non-null values across pages
    all_results = {}

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            for page_url in profile_urls:
                page.goto(page_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                def merge_items(items):
                    for item in items:
                        url = item["url"]
                        prev = all_results.get(url)
                        if prev is None:
                            all_results[url] = {
                                "views": item.get("views"),
                                "score": item.get("score"),
                                "comments_count": item.get("comments_count"),
                            }
                            continue
                        # Keep non-null values across repeated sightings.
                        for k in ("views", "score", "comments_count"):
                            v = item.get(k)
                            if v is not None:
                                prev[k] = v

                merge_items(page.evaluate(extract_js))

                # Scroll to load more
                prev_height = 0
                same_count = 0
                scroll_count = 0
                per_page_max = max_scrolls // 4

                while same_count < 4 and scroll_count < per_page_max:
                    cur_height = page.evaluate("document.body.scrollHeight")
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(2000)

                    merge_items(page.evaluate(extract_js))

                    if cur_height == prev_height:
                        same_count += 1
                    else:
                        same_count = 0
                    prev_height = cur_height
                    scroll_count += 1

            results_list = [
                {"url": url, "views": d.get("views"),
                 "score": d.get("score"), "comments_count": d.get("comments_count")}
                for url, d in all_results.items()
            ]
            with_views = sum(1 for d in all_results.values() if d.get("views") is not None)
            with_score = sum(1 for d in all_results.values() if d.get("score") is not None)
            with_cc = sum(1 for d in all_results.values() if d.get("comments_count") is not None)

            return {
                "ok": True,
                "total": len(results_list),
                "with_views": with_views,
                "with_score": with_score,
                "with_comments_count": with_cc,
                "results": results_list,
            }

        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            # Harness (is_cdp) tabs are REUSED across invocations, so never close
            # the page here: closing it forces the next run to new_page(), which
            # steals OS focus from whatever app is in front. Mirrors twitter_browser
            # (only the persistent-context fallback closes). cleanup_harness_tabs at
            # cycle start bounds tab count to one.
            try:
                if not is_cdp:
                    page.context.close()
            except Exception:
                pass
            if not is_cdp:
                try:
                    browser.close()
                except Exception:
                    pass


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "post-comment":
        if len(sys.argv) < 4:
            print(
                "Usage: reddit_browser.py post-comment <thread_url> <text>",
                file=sys.stderr,
            )
            sys.exit(1)
        result = post_comment(sys.argv[2], sys.argv[3])
        print(json.dumps(result, indent=2))
        try:
            print(f"[reddit_browser] result={json.dumps(result)}", file=sys.stderr)
        except Exception:
            pass

    elif cmd == "reply":
        if len(sys.argv) < 4:
            print(
                "Usage: reddit_browser.py reply <comment_permalink> <text> [dm_id]",
                file=sys.stderr,
            )
            sys.exit(1)
        dm_id_arg = None
        if len(sys.argv) >= 5 and sys.argv[4].strip():
            try:
                dm_id_arg = int(sys.argv[4])
            except ValueError:
                print(f"[reply] ignoring non-int dm_id arg: {sys.argv[4]!r}", file=sys.stderr)
        result = reply_to_comment(sys.argv[2], sys.argv[3], dm_id=dm_id_arg)
        print(json.dumps(result, indent=2))

    elif cmd == "edit":
        if len(sys.argv) < 4:
            print(
                "Usage: reddit_browser.py edit <comment_permalink> <new_text>",
                file=sys.stderr,
            )
            sys.exit(1)
        result = edit_comment(sys.argv[2], sys.argv[3])
        print(json.dumps(result, indent=2))

    elif cmd == "edit-thread":
        if len(sys.argv) < 4:
            print(
                "Usage: reddit_browser.py edit-thread <thread_permalink> <new_body>",
                file=sys.stderr,
            )
            sys.exit(1)
        result = edit_thread(sys.argv[2], sys.argv[3])
        print(json.dumps(result, indent=2))

    elif cmd == "unread-dms":
        result = unread_dms()
        print(json.dumps(result, indent=2))

    elif cmd == "read-conversation":
        if len(sys.argv) < 3:
            print(
                "Usage: reddit_browser.py read-conversation <chat_url>",
                file=sys.stderr,
            )
            sys.exit(1)
        result = read_conversation(sys.argv[2])
        print(json.dumps(result, indent=2))

    elif cmd == "send-dm":
        if len(sys.argv) < 4:
            print(
                "Usage: reddit_browser.py send-dm <chat_url> <message> [dm_id]",
                file=sys.stderr,
            )
            sys.exit(1)
        dm_id_arg = None
        if len(sys.argv) >= 5 and sys.argv[4].strip():
            try:
                dm_id_arg = int(sys.argv[4])
            except ValueError:
                print(f"[send-dm] ignoring non-int dm_id arg: {sys.argv[4]!r}", file=sys.stderr)
        # S4L_SKIP_CAMPAIGN_SUFFIX=1 opts this DM out of campaign suffixes.
        # Set ONLY by engage-dm-replies Phase 0 when delivering a human-drafted
        # escalation reply — see send_dm's apply_campaigns comment.
        _skip_camp = os.environ.get("S4L_SKIP_CAMPAIGN_SUFFIX", "").strip().lower() in ("1", "true", "yes")
        result = send_dm(sys.argv[2], sys.argv[3], dm_id=dm_id_arg, apply_campaigns=not _skip_camp)
        print(json.dumps(result, indent=2))

    elif cmd == "compose-dm":
        if len(sys.argv) < 5:
            print(
                "Usage: reddit_browser.py compose-dm <recipient> <subject> <body>",
                file=sys.stderr,
            )
            sys.exit(1)
        result = compose_dm(sys.argv[2], sys.argv[3], sys.argv[4])
        print(json.dumps(result, indent=2))

    elif cmd == "scrape-views":
        if len(sys.argv) < 3:
            print(
                "Usage: reddit_browser.py scrape-views <username> [max_scrolls]",
                file=sys.stderr,
            )
            sys.exit(1)
        max_scrolls = int(sys.argv[3]) if len(sys.argv) > 3 else 300
        result = scrape_views(sys.argv[2], max_scrolls)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


def _install_stderr_tee():
    """Mirror stderr to <repo>/log/reddit_browser.<utc>.<pid>.err.

    Each reddit_browser.py invocation is captured by the parent post_reddit.py
    with stderr=PIPE; when the child raises before printing JSON to stdout, the
    parent prints only a truncated slice of the captured stderr (post_reddit
    historically clipped at 200 chars). Teeing here keeps the full traceback
    on disk regardless of parent-side handling.
    """
    try:
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        log_dir = os.path.join(repo_dir, "log")
        os.makedirs(log_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = os.path.join(log_dir, f"reddit_browser.{ts}.{os.getpid()}.err")
        fh = open(path, "a", encoding="utf-8", buffering=1)
        cmd_repr = " ".join(sys.argv[1:3])[:160] if len(sys.argv) > 1 else "(no cmd)"
        fh.write(f"--- reddit_browser invocation pid={os.getpid()} cmd={cmd_repr!r} ts={ts} ---\n")
        fh.flush()
        real_stderr = sys.stderr

        class _Tee:
            def write(self, s):
                try:
                    fh.write(s)
                except Exception:
                    pass
                return real_stderr.write(s)

            def flush(self):
                try:
                    fh.flush()
                except Exception:
                    pass
                return real_stderr.flush()

            def __getattr__(self, name):
                return getattr(real_stderr, name)

        sys.stderr = _Tee()

        def _close():
            try:
                fh.close()
            except Exception:
                pass

        atexit.register(_close)
    except Exception:
        # Never let logging setup kill the invocation.
        pass


if __name__ == "__main__":
    _install_stderr_tee()
    main()
