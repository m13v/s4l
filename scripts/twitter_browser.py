#!/usr/bin/env python3
"""Twitter/X browser automation functions for Social Autoposter.

Replaces multi-step Claude browser MCP calls with single Python function calls.
Each function does all browser work internally and returns structured JSON.

Usage:
    # Reply to a tweet
    python3 twitter_browser.py reply "https://x.com/user/status/123" "reply text"

    # Scan DM inbox for unread conversations
    python3 twitter_browser.py unread-dms

    # Read messages from a DM conversation
    python3 twitter_browser.py read-conversation "https://x.com/i/chat/123-456"

    # Send a DM message
    python3 twitter_browser.py send-dm "https://x.com/i/chat/123-456" "message text"

Requires: pip install playwright && playwright install chromium

Connects to the running twitter-agent MCP browser via CDP (Chrome DevTools Protocol)
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


PROFILE_DIR = os.path.expanduser("~/.claude/browser-profiles/twitter")
LOCK_FILE = os.path.expanduser("~/.claude/twitter-agent-lock.json")
LOCK_EXPIRY = 300  # Must match twitter-agent-lock.sh
LOCK_WAIT_MAX = 45  # seconds to wait for lock to free before giving up
LOCK_POLL_INTERVAL = 2
VIEWPORT = {"width": 911, "height": 1016}
OUR_HANDLE = "m13v_"

# DM encryption passcode from .env
DM_PASSCODE = os.environ.get("TWITTER_DM_PASSCODE", "")
if not DM_PASSCODE:
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("TWITTER_DM_PASSCODE="):
                    DM_PASSCODE = line.strip().split("=", 1)[1]
                    break


def _load_active_twitter_campaigns():
    """Best-effort loader for active Twitter campaigns with literal suffixes.

    Returns [(id, suffix, sample_rate), ...]. On any failure (no DB, missing
    module, etc.) returns []. This keeps twitter_browser.py usable in non-DB
    contexts (e.g. ad-hoc invocations from a shell). Mirrors the
    `_load_active_reddit_campaigns_for_dm` helper in reddit_browser.py.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import db as _db
        _db.load_env()
        conn = _db.get_conn()
        try:
            cur = conn.execute(
                """SELECT id, suffix, COALESCE(sample_rate, 1.000)
                   FROM campaigns
                   WHERE status='active'
                     AND (',' || platforms || ',') LIKE '%,twitter,%'
                     AND max_posts_total IS NOT NULL
                     AND posts_made < max_posts_total
                     AND suffix IS NOT NULL AND suffix <> ''
                   ORDER BY id"""
            )
            return [(r[0], r[1], float(r[2])) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[twitter_browser] _load_active_twitter_campaigns failed: {e}",
              file=sys.stderr)
        return []


def _log_twitter_dm_outbound(dm_id, content, minted_codes=None):
    """After a verified send, log via dm_conversation.py log-outbound so the
    suffix-detection path attributes the message to the active campaign and
    advances the counter. `minted_codes` is the list of dm_links codes minted
    for the URLs in this message; passed via env so log-outbound can backfill
    dm_links.message_id after RETURNING id. Best-effort; failures are non-fatal."""
    if not dm_id:
        return False
    try:
        env = os.environ.copy()
        if minted_codes:
            env["WRAP_MINTED_CODES"] = ",".join(minted_codes)
        subprocess.run(
            ["python3",
             os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "dm_conversation.py"),
             "log-outbound", "--dm-id", str(dm_id),
             "--content", content, "--verified"],
            capture_output=True, text=True, timeout=20, env=env,
        )
        return True
    except Exception as e:
        print(f"[twitter_browser] internal log-outbound failed: {e}",
              file=sys.stderr)
        return False


def find_twitter_cdp_port():
    """Find the CDP port of the running twitter-agent MCP browser."""
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

        best_port = None
        for port in sorted(ports):
            try:
                resp = urllib.request.urlopen(
                    f"http://localhost:{port}/json", timeout=2
                )
                pages = json.loads(resp.read())
                twitter_urls = [
                    p.get("url", "")
                    for p in pages
                    if "x.com" in p.get("url", "") or "twitter.com" in p.get("url", "")
                ]
                if not twitter_urls:
                    continue
                # Prefer ports with logged-in pages (home, chat, notifications)
                logged_in = any(
                    ("home" in u or "chat" in u or "notifications" in u or "status" in u)
                    and "login" not in u
                    for u in twitter_urls
                )
                if logged_in:
                    return port
                if best_port is None:
                    best_port = port
            except Exception:
                continue
        return best_port
    except Exception:
        pass
    return None


_LOCK_SESSION_ID = f"python:{os.getpid()}"
_LOCK_INHERITED = False
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _release_browser_lock():
    """Release the lock if we hold it.

    If we inherited the lock from a Claude session (UUID holder), leave it for
    the hook/session-end handler to release — don't clobber the parent's lock.
    """
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


def _is_holder_alive(holder: str) -> bool:
    """Mirror ~/.claude/hooks/twitter-agent-lock.sh is_holder_alive().

    A live Claude session puts its UUID on the cmdline as
    `claude --session-id <UUID>`. pgrep matches it; absence means the
    holder is dead and the lock is stale, even if its JSONL transcript
    is still tail-flushing.
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
        return True  # err on the side of NOT stealing


def _acquire_browser_lock():
    """Check if another session holds the Twitter browser lock, then acquire it.

    Claude Code does not export session_id to subprocesses, so we can't directly
    match our parent session. But the PreToolUse hook (twitter-agent-lock.sh)
    enforces one Claude UUID owner at a time: any other Claude session would
    have been blocked before it could reach this script via Bash. So if the
    current lock is held by a UUID-style id, it must be our parent — inherit
    it instead of failing. Only python:PID holders represent a real conflict.
    """
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
                # hook only refreshes timestamp, not deletes). Same fix as
                # linkedin_browser.py — see 2026-05-01 14:33 incident.
                if _UUID_RE.match(holder or "") and not _is_holder_alive(holder):
                    break  # stale, take it
                if age >= LOCK_EXPIRY:
                    break
                if _UUID_RE.match(holder or ""):
                    _LOCK_SESSION_ID = holder
                    _LOCK_INHERITED = True
                    break
                if time.time() >= deadline:
                    print(json.dumps({
                        "success": False,
                        "error": f"Twitter browser locked by session {holder} ({int(age)}s); waited {LOCK_WAIT_MAX}s, giving up."
                    }))
                    sys.exit(1)
                time.sleep(LOCK_POLL_INTERVAL)
                continue
            except (json.JSONDecodeError, OSError):
                pass
        break
    with open(LOCK_FILE, "w") as f:
        json.dump({"session_id": _LOCK_SESSION_ID, "timestamp": int(time.time())}, f)


def _refresh_browser_lock():
    """Refresh the lock timestamp to prevent expiry during long operations."""
    try:
        with open(LOCK_FILE, "w") as f:
            json.dump({"session_id": _LOCK_SESSION_ID, "timestamp": int(time.time())}, f)
    except OSError:
        pass


def get_browser_and_page(playwright):
    """Connect to the twitter-agent MCP browser via CDP, or launch a new one.

    Returns (browser, page, is_cdp). When is_cdp=True, `page` is a reused
    existing Twitter tab (navigate it, don't close it). When is_cdp=False,
    it's a new headless page.

    2026-05-13: TWITTER_CDP_URL env var (set by engage-twitter.sh when
    TWITTER_BACKEND=harness) routes directly to the browser-harness Chrome
    (http://127.0.0.1:9555), skipping the ps-based discovery. The caller
    explicitly asked for this endpoint, so failure here is fatal — silently
    falling back to launch_persistent_context(twitter profile) would defeat
    the whole point of the backend switch.
    """
    _acquire_browser_lock()

    cdp_url_override = os.environ.get("TWITTER_CDP_URL", "").strip()
    if cdp_url_override:
        try:
            browser = playwright.chromium.connect_over_cdp(cdp_url_override)
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
                # Prefer a reusable Twitter tab if one exists.
                for pg in context.pages:
                    if ("x.com" in pg.url or "twitter.com" in pg.url) and "login" not in pg.url:
                        return browser, pg, True
                # Otherwise reuse the first page (caller will navigate it).
                if context.pages:
                    return browser, context.pages[0], True
                return browser, context.new_page(), True
            # No contexts present (unusual on a fresh harness Chrome) — create one.
            context = browser.new_context()
            return browser, context.new_page(), True
        except Exception as e:
            _release_browser_lock()
            print(json.dumps({
                "success": False,
                "error": f"TWITTER_CDP_URL connect failed ({cdp_url_override}): {e}"
            }))
            sys.exit(1)

    cdp_port = find_twitter_cdp_port()

    if cdp_port:
        try:
            browser = playwright.chromium.connect_over_cdp(
                f"http://localhost:{cdp_port}"
            )
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
                for pg in context.pages:
                    if ("x.com" in pg.url or "twitter.com" in pg.url) and "login" not in pg.url:
                        return browser, pg, True
                if context.pages:
                    return browser, context.pages[0], True
        except Exception:
            pass

    # Fallback: launch persistent browser with saved profile.
    # Retry on Chromium SingletonLock collisions (MCP holds the OS-level profile
    # lock for its entire server lifetime; the JSON lock can expire while the
    # OS lock is still held).
    deadline = time.time() + LOCK_WAIT_MAX
    while True:
        try:
            # IMPORTANT (2026-05-13): headless MUST be False. Twitter rolled out
            # stricter headless-Chrome fingerprint detection around 2026-04-30
            # that silently serves a stripped DM page: /i/chat never redirects
            # to /i/chat/pin/recovery, the passcode prompt never appears, and
            # the sidebar `main li` selector returns 0 results. We confirmed
            # this empirically by running the same script with headless=True
            # vs headless=False against the same persistent profile after a
            # fresh login: True → empty list, False → passcode page appears
            # and sidebar renders normally. This caused a 13-day inbound DM
            # cliff (2026-05-01 .. 05-13) where ~50 inbound messages went
            # uningested. Do NOT revert to headless=True without a working
            # stealth/fingerprint patch — silent failure mode.
            context = playwright.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport=VIEWPORT,
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            break
        except Exception as e:
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


def _handle_dm_passcode(page):
    """Handle the DM encryption passcode dialog if it appears.

    Twitter/X requires a 4-digit passcode to decrypt DMs.
    Returns True if passcode was entered, False if not needed.
    """
    if "pin/recovery" not in page.url:
        return False

    if not DM_PASSCODE:
        print("Warning: DM passcode required but TWITTER_DM_PASSCODE not set", file=sys.stderr)
        return False

    try:
        digits = list(DM_PASSCODE)
        # Find the 4 passcode input boxes
        inputs = page.locator('input')
        count = inputs.count()
        for i in range(min(len(digits), count)):
            inp = inputs.nth(i)
            inp.click()
            page.keyboard.type(digits[i])
            page.wait_for_timeout(300)

        page.wait_for_timeout(3000)
        return "pin/recovery" not in page.url
    except Exception as e:
        print(f"Warning: Failed to enter DM passcode: {e}", file=sys.stderr)
        return False



def _install_rate_limit_listener(page):
    """Count 429 responses on x.com DM API endpoints.

    X throttles the account (not per-tab) after too many /i/chat navigations
    and GetInboxPageRequestQuery hits in a window. Returns a mutable counter
    dict; caller reads counter['429'] after the page settles.
    """
    counter = {"429": 0, "first_429_url": None}

    def on_response(resp):
        try:
            if resp.status != 429:
                return
            url = resp.url
            if "api.x.com" not in url and "x.com/i/api" not in url:
                return
            counter["429"] += 1
            if counter["first_429_url"] is None:
                counter["first_429_url"] = url
        except Exception:
            pass

    page.on("response", on_response)
    return counter


def _is_x_unreachable(page):
    """Return (True, reason) if Chrome rendered its own error page for x.com.

    Happens when x.com drops the TCP connection after sustained 429s; Chrome
    shows `chrome-error://chromewebdata/` with "This site can't be reached".
    Distinct from "normal" x.com errors (which still render a valid x.com DOM).
    """
    try:
        url = page.url or ""
        if url.startswith("chrome-error:"):
            return True, f"chrome_error_url:{url}"
        body_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        if "ERR_FAILED" in body_text and "site can" in body_text.lower():
            return True, "err_failed_body"
    except Exception:
        pass
    return False, None


def _rate_limit_response(reason, counter=None, url=None):
    """Build the JSON payload we return when X has blocked us.

    Also prints a loud stderr marker so grep finds it in launchd logs.
    """
    payload = {
        "ok": False,
        "error": "rate_limited",
        "reason": reason,
        "rate_limit_count": counter["429"] if counter else 0,
        "url": url,
        "conversations": [],
    }
    print(
        f"RATE_LIMITED_TWITTER: reason={reason} "
        f"429s={payload['rate_limit_count']} url={url}",
        file=sys.stderr,
    )
    return payload


def _collect_our_reply_links(page):
    """Collect all /OUR_HANDLE/status/ links currently in the DOM."""
    return set(page.evaluate(f"""() => {{
        const links = new Set();
        document.querySelectorAll('a[href*="/{OUR_HANDLE}/status/"]').forEach(a => {{
            const href = a.getAttribute('href');
            if (href && /\\/{OUR_HANDLE}\\/status\\/\\d+$/.test(href))
                links.add(href);
        }});
        return [...links];
    }}"""))


def reply_to_tweet(tweet_url, text, apply_campaigns=True):
    """Reply to a tweet.

    Navigates to the tweet, clicks the reply box, types the reply, and submits.

    Active Twitter campaigns with a `suffix` are applied at this tool layer:
    the suffix is appended to `text` (per `sample_rate` coin flip per campaign)
    before typing, so the literal text is guaranteed to land. Caller opts out
    via `apply_campaigns=False` (used by the self-reply path so the project URL
    follow-up doesn't carry the campaign tag).

    Returns: {"ok": true, "tweet_url": "...", "reply_url": "...",
              "applied_campaigns": [...], "final_text": "..."}
              or {"ok": false, "error": "..."}
    """
    print(f"[twitter_browser] reply_to_tweet called: {tweet_url}", file=sys.stderr)

    applied_campaigns = []
    if apply_campaigns:
        for cid, suffix, sample_rate in _load_active_twitter_campaigns():
            if random.random() < sample_rate:
                # Wrap any URLs in the suffix through dm_short_links so clicks
                # attribute. The suffix carries no project_name, so we detect
                # the project from the URL hostname against config.json before
                # minting. Falls back to raw suffix if no project matches (e.g.
                # plain-text suffix like " written with ai", or third-party URL).
                wrapped_suffix = suffix
                if 'http' in suffix:
                    try:
                        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                        from dm_short_links import wrap_text_for_post, _classify_url, _load_projects, _URL_RE
                        projects = _load_projects()
                        # Detect project_name from the first URL in the suffix.
                        m = _URL_RE.search(suffix)
                        detected_project = None
                        if m:
                            _, detected_project = _classify_url(m.group(0), projects)
                        if detected_project:
                            wrap_res = wrap_text_for_post(text=suffix, platform='twitter',
                                                          project_name=detected_project)
                            if wrap_res.get('ok') and wrap_res.get('codes'):
                                wrapped_suffix = wrap_res['text']
                                print(f"[reply_to_tweet] suffix wrap project={detected_project} "
                                      f"codes={wrap_res['codes']}", file=sys.stderr)
                    except Exception as _e:
                        print(f"[reply_to_tweet] suffix wrap failed ({_e}); raw",
                              file=sys.stderr)
                text = text + wrapped_suffix
                applied_campaigns.append(cid)
        print(f"[reply_to_tweet] applied_campaigns={applied_campaigns} text_len={len(text)}",
              file=sys.stderr)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            # Set up Network interception to capture CreateTweet response.
            # Two parallel paths for redundancy:
            #   (a) page.on("response") — Playwright's event-loop hook.
            #   (b) CDP Network.responseReceived — slightly faster + less
            #       body-fetch overhead, Chromium-only.
            # Both write into _created_tweet_ids; dedup-on-append keeps the
            # list a set of unique rest_ids regardless of which path fired.
            _cdp_session = None
            _created_tweet_ids = []

            def _on_response_event(resp):
                # Engine-agnostic CreateTweet capture. Filter by URL FIRST so
                # we don't pay a body-fetch round-trip on every graphql call.
                try:
                    if "CreateTweet" not in resp.url:
                        return
                    if resp.status != 200:
                        return
                    data = resp.json()
                    rest_id = (
                        data.get("data", {})
                        .get("create_tweet", {})
                        .get("tweet_results", {})
                        .get("result", {})
                        .get("rest_id")
                    )
                    if rest_id and rest_id not in _created_tweet_ids:
                        _created_tweet_ids.append(rest_id)
                except Exception:
                    pass

            page.on("response", _on_response_event)

            try:
                _cdp_session = page.context.new_cdp_session(page)
                _cdp_session.send("Network.enable")

                def _on_cdp_response(params):
                    try:
                        url = params.get("response", {}).get("url", "")
                        if "CreateTweet" in url:
                            body_resp = _cdp_session.send(
                                "Network.getResponseBody",
                                {"requestId": params["requestId"]},
                            )
                            data = json.loads(body_resp.get("body", "{}"))
                            rest_id = (
                                data.get("data", {})
                                .get("create_tweet", {})
                                .get("tweet_results", {})
                                .get("result", {})
                                .get("rest_id")
                            )
                            if rest_id and rest_id not in _created_tweet_ids:
                                _created_tweet_ids.append(rest_id)
                    except Exception:
                        pass

                _cdp_session.on("Network.responseReceived", _on_cdp_response)
            except Exception:
                pass

            page.goto(tweet_url, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

            # Check if page exists
            page_text = page.text_content("main") or ""
            if "this page doesn't exist" in page_text.lower():
                return {"ok": False, "error": "tweet_not_found"}

            # Snapshot our reply links before posting (to detect the new one)
            links_before = _collect_our_reply_links(page)

            # Find the reply textbox
            reply_box = None
            try:
                reply_box = page.get_by_role("textbox", name="Post text")
                reply_box.wait_for(timeout=10000)
            except Exception:
                # Scroll down to find the reply box
                page.evaluate("window.scrollBy(0, 500)")
                page.wait_for_timeout(2000)
                try:
                    reply_box = page.get_by_role("textbox", name="Post text")
                    reply_box.wait_for(timeout=5000)
                except Exception:
                    return {"ok": False, "error": "reply_box_not_found"}

            # Click and type the reply
            reply_box.click()
            page.wait_for_timeout(500)
            page.keyboard.type(text, delay=10)
            page.wait_for_timeout(1000)

            # Click the Reply submit button. MUST target tweetButtonInline by
            # testid; substring-matching "Reply" by accessible name matches
            # every reply-icon on the page and picks the wrong one.
            try:
                reply_btn = page.locator('[data-testid="tweetButtonInline"]').first
                reply_btn.wait_for(state="visible", timeout=5000)
                for _ in range(20):
                    if reply_btn.get_attribute("aria-disabled") != "true":
                        break
                    page.wait_for_timeout(100)
                reply_btn.click()
            except Exception:
                page.keyboard.press("Meta+Enter")

            page.wait_for_timeout(4000)

            # Verify: check if the reply box is empty (cleared after posting)
            try:
                box_text = reply_box.text_content() or ""
                verified = len(box_text.strip()) == 0 or text not in box_text
            except Exception:
                verified = True

            # Clean up CDP session
            if _cdp_session:
                try:
                    _cdp_session.detach()
                except Exception:
                    pass

            # Capture reply URL
            reply_url = None

            # Method 1: CDP network interception (most reliable)
            if _created_tweet_ids:
                reply_url = f"https://x.com/{OUR_HANDLE}/status/{_created_tweet_ids[-1]}"
                print(f"[reply_url] captured via CDP+response-listener: {reply_url}", file=sys.stderr)

            # Method 2: DOM diff (check if new reply links appeared)
            if not reply_url:
                for attempt in range(3):
                    links_after = _collect_our_reply_links(page)
                    new_links = links_after - links_before
                    if new_links:
                        reply_path = max(new_links, key=lambda x: int(re.search(r'/status/(\d+)', x).group(1)))
                        reply_url = f"https://x.com{reply_path}" if not reply_path.startswith("http") else reply_path
                        break
                    page.wait_for_timeout(2000)

            # Method 3 REMOVED 2026-05-01: profile-page (`/with_replies`)
            # scrape was returning the wrong URL under parallel cycles. It
            # picked `max(status_id)` of any m13v_ reply on the profile page
            # and de-duped against a shared `/tmp` tracker file, but with
            # multiple cycles posting in parallel that "latest" reply often
            # belonged to a DIFFERENT thread than the one we just posted to.
            # Observed cross-thread contamination on 2026-05-01: cycles
            # 074506 and 080006 both captured 2050228098633982405 as "their"
            # reply URL but for different parent tweets. Better to leave
            # reply_url=None and let the caller treat it as soft-skip than
            # to attribute someone else's tweet to this candidate's row.
            if reply_url:
                print(f"[reply_url] found: {reply_url}", file=sys.stderr)
            else:
                print("[reply_url] capture failed (CDP+DOM both empty); "
                      "returning null — caller should skip without retry",
                      file=sys.stderr)

            return {
                "ok": True,
                "tweet_url": tweet_url,
                "reply_url": reply_url,
                "verified": verified,
                "applied_campaigns": applied_campaigns,
                "final_text": text,
            }

        finally:
            if not is_cdp:
                page.close()
                browser.close()


def unread_dms():
    """Scan Twitter/X DM inbox for conversations.

    Navigates to /i/chat, handles the encryption passcode if needed,
    and extracts all visible conversations with their author, preview text,
    timestamp, and conversation URL.

    Returns: [{"author": "...", "handle": "...", "preview": "...", "time": "...",
               "thread_url": "...", "is_from_us": bool, "has_unread": bool}, ...]

    `has_unread` is the signal callers should filter on. It is derived from the
    sidebar's visual unread state (aria-label "unread", bold font weight on the
    preview/name, or a notification dot SVG). Threads where we sent last AND have
    no new inbound show `has_unread: false` even when the "You:" prefix is
    truncated, so this avoids opening every thread to verify.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            rl_counter = _install_rate_limit_listener(page)
            page.goto("https://x.com/i/chat", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

            unreachable, reason = _is_x_unreachable(page)
            if unreachable:
                return _rate_limit_response(reason, rl_counter, page.url)

            # Handle DM passcode if needed
            _handle_dm_passcode(page)
            page.wait_for_timeout(2000)

            # Verify we're on the DM inbox
            if "chat" not in page.url:
                unreachable, reason = _is_x_unreachable(page)
                if unreachable:
                    return _rate_limit_response(reason, rl_counter, page.url)
                return {"ok": False, "error": "not_on_dm_page", "url": page.url}

            # Extract conversation list by walking the real DOM structure.
            # As of 2026-04: aria-label on conversation links is empty, so we
            # can't rely on it. Instead, every conversation row has:
            #   - a leaf div with font-weight 700 = author name
            #   - a leaf div with font-weight 400 = relative time (e.g. "4h")
            #   - optional span with font-weight 500 and text "You:" = we sent last
            #   - a leaf span with the preview text. font-weight 400 = read,
            #     font-weight >= 600 = unread (bold).
            conversations = page.evaluate("""() => {
                const results = [];
                const items = document.querySelectorAll('main li, main [role="listitem"]');

                for (const item of items) {
                    // Find the conversation link
                    const link = item.querySelector('a[href*="/i/chat/"]');
                    if (!link) continue;

                    const threadUrl = link.href;
                    if (!threadUrl.match(/\\/i\\/chat\\/[\\d-g]/)) continue;

                    // Get the handle from the avatar link
                    let handle = '';
                    const avatarLink = item.querySelector('a[href^="https://x.com/"]');
                    if (avatarLink) {
                        const href = avatarLink.getAttribute('href') || '';
                        const m = href.match(/x\\.com\\/([^/]+)/);
                        if (m) handle = m[1];
                    }

                    // Walk leaf text nodes inside the link, capturing tag,
                    // computed font-weight, and trimmed text. Order matches
                    // visual order: [name, time, optional "You:", preview].
                    const leaves = [];
                    const all = link.querySelectorAll('*');
                    for (const el of all) {
                        if (el.children.length !== 0) continue;
                        const t = (el.textContent || '').trim();
                        if (!t) continue;
                        const fw = parseInt(window.getComputedStyle(el).fontWeight, 10) || 400;
                        leaves.push({tag: el.tagName.toLowerCase(), fw: fw, t: t});
                    }

                    let author = '';
                    let time = '';
                    let preview = '';
                    let isFromUs = false;
                    let previewFw = 400;

                    for (const node of leaves) {
                        // Author: first bold (fw>=700) leaf, short text, not a timestamp
                        if (!author && node.fw >= 700 && node.t.length < 80 &&
                            !/^(\\d+[hmd]|\\d+w|Just now)$/.test(node.t)) {
                            author = node.t;
                            continue;
                        }
                        // Time: short text matching relative-time pattern
                        if (!time && /^(\\d+[hmd]|\\d+w|Just now)$/.test(node.t)) {
                            time = node.t;
                            continue;
                        }
                        // "You:" prefix: standalone span with text "You:"
                        if (!isFromUs && node.tag === 'span' && /^You:?$/.test(node.t)) {
                            isFromUs = true;
                            continue;
                        }
                        // Preview: any remaining text. The bolded preview span
                        // (fw >= 600) signals an unread message; fw 400 is read.
                        if (!preview && node.t.length > 0) {
                            preview = node.t;
                            previewFw = node.fw;
                        }
                    }

                    // Unread detection. Primary signal: preview span is bolded
                    // (fw >= 600) when there is an unread inbound. Backup:
                    // aria-label anywhere in the row containing "unread", or a
                    // small visible dot/badge. If we sent last ("You:" prefix),
                    // override to read regardless.
                    let hasUnread = previewFw >= 600;

                    if (!hasUnread) {
                        const ariaUnread = item.querySelector(
                            '[aria-label*="unread" i]'
                        );
                        if (ariaUnread) hasUnread = true;
                    }

                    if (!hasUnread) {
                        // Tiny coloured pill/dot heuristic
                        const candidates = item.querySelectorAll('span, div');
                        for (const el of candidates) {
                            if (el.children.length !== 0) continue;
                            const style = window.getComputedStyle(el);
                            const bg = style.backgroundColor || '';
                            if (!bg || bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') continue;
                            const w = el.offsetWidth, h = el.offsetHeight;
                            if (w > 0 && w <= 14 && h > 0 && h <= 14 && Math.abs(w - h) <= 2) {
                                hasUnread = true;
                                break;
                            }
                        }
                    }

                    if (isFromUs) hasUnread = false;

                    if (author || handle) {
                        results.push({
                            author: author,
                            handle: handle,
                            preview: preview,
                            time: time,
                            thread_url: threadUrl,
                            is_from_us: isFromUs,
                            has_unread: hasUnread,
                        });
                    }
                }

                return results;
            }""")

            # Deduplicate by thread_url
            seen = set()
            unique = []
            for c in conversations:
                if c["thread_url"] not in seen:
                    seen.add(c["thread_url"])
                    unique.append(c)

            # If the inbox API was throttled hard AND we got nothing back,
            # treat this as rate-limited so the caller can back off instead
            # of reporting "0 new inbounds" (which then silently skips work).
            if not unique and rl_counter["429"] >= 3:
                return _rate_limit_response(
                    "inbox_api_throttled", rl_counter, page.url
                )

            return unique

        finally:
            if not is_cdp:
                page.close()
                browser.close()


def read_conversation(thread_url, max_messages=20):
    """Read messages from a specific Twitter/X DM conversation.

    Navigates to the thread URL and extracts the most recent messages
    with their sender, content, and timestamp.

    Returns: {"partner_name": "...", "partner_handle": "...",
              "messages": [{"sender": "...", "content": "...", "time": "...",
                            "is_from_us": bool}, ...], "total_found": N}
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            rl_counter = _install_rate_limit_listener(page)
            # Navigate using JS to avoid SPA navigation timeouts
            page.evaluate(f"window.location.href = '{thread_url}'")
            page.wait_for_timeout(6000)

            unreachable, reason = _is_x_unreachable(page)
            if unreachable:
                return _rate_limit_response(reason, rl_counter, page.url)

            # Handle DM passcode if needed
            _handle_dm_passcode(page)
            page.wait_for_timeout(2000)

            result = page.evaluate("""(params) => {
                const maxMessages = params.maxMessages;
                const ourHandle = params.ourHandle;

                let partnerName = '';
                let partnerHandle = '';
                const main = document.querySelector('main');
                if (!main) return {partner_name: '', partner_handle: '', messages: [], total_found: 0};

                // Find the conversation panel (the section containing the
                // message textbox), NOT the sidebar conversation list.
                // The textbox has aria-label like "Unencrypted message".
                const textbox = main.querySelector('[role="textbox"]');
                // Walk up from textbox to find the conversation container
                // that holds the message list items.
                let convPanel = null;
                if (textbox) {
                    // The conversation panel is typically a sibling of or
                    // ancestor of the textbox container. Walk up to find
                    // the div that contains BOTH the message list and textbox.
                    let el = textbox;
                    for (let i = 0; i < 10; i++) {
                        el = el.parentElement;
                        if (!el) break;
                        const lis = el.querySelectorAll('li, [role="listitem"]');
                        if (lis.length >= 2) {
                            convPanel = el;
                            break;
                        }
                    }
                }

                // Fallback: if no textbox found, try to find the panel
                // that has "View Profile" text (the conversation header)
                if (!convPanel) {
                    const allDivs = main.querySelectorAll('div');
                    for (const d of allDivs) {
                        if (d.textContent.includes('View Profile') &&
                            d.textContent.includes('Joined ') &&
                            d.querySelectorAll('li').length >= 2) {
                            convPanel = d;
                            break;
                        }
                    }
                }

                // Last fallback: use main but filter out sidebar items
                if (!convPanel) convPanel = main;

                // Extract partner info from profile card in the conversation
                const profileLink = convPanel.querySelector('a[href*="x.com/"]');
                if (profileLink) {
                    const href = profileLink.getAttribute('href') || '';
                    const m = href.match(/x\\.com\\/([^/]+)/);
                    if (m && m[1] !== ourHandle) partnerHandle = m[1];
                }

                // Look for @handle text
                const handleEls = convPanel.querySelectorAll('div, span');
                for (const el of handleEls) {
                    const t = el.textContent.trim();
                    if (t.startsWith('@') && t.length > 2 && t.length < 50 &&
                        !t.includes(' ') && t.substring(1) !== ourHandle) {
                        partnerHandle = t.substring(1);
                        break;
                    }
                }

                // Find messages — only from the conversation panel
                const items = convPanel.querySelectorAll('li, [role="listitem"]');
                const messages = [];
                let currentDate = '';

                for (const item of items) {
                    const text = item.textContent || '';

                    // Skip sidebar conversation items (they contain
                    // avatar links to x.com/username profiles)
                    const sidebarLink = item.querySelector('a[href*="/i/chat/"]');
                    if (sidebarLink) continue;

                    // Date separator
                    if (text.match(/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Today|Yesterday)/) &&
                        text.length < 30) {
                        currentDate = text.trim();
                        continue;
                    }

                    // Profile card
                    if (text.includes('View Profile') || text.includes('Joined ')) {
                        const nameEl = item.querySelector('div[dir="ltr"], span');
                        if (nameEl && !partnerName) {
                            const n = nameEl.textContent.trim();
                            if (n && n.length > 1 && n.length < 50 &&
                                !n.startsWith('@') && !n.includes('View') &&
                                !n.includes('Joined')) {
                                partnerName = n;
                            }
                        }
                        continue;
                    }

                    if (text.trim().length < 2) continue;

                    // Extract message content and time
                    let content = '';
                    let time = '';
                    let isFromUs = false;

                    const timeMatch = text.match(/(\\d{1,2}:\\d{2}\\s*[AP]M)/);
                    if (timeMatch) {
                        time = timeMatch[1];
                    }

                    // Content: find the deepest div with message text
                    const contentDivs = item.querySelectorAll('div');
                    for (const cd of contentDivs) {
                        const t = cd.textContent.trim();
                        if (t.match(/^\\d{1,2}:\\d{2}\\s*[AP]M$/)) continue;
                        if (t === time) continue;
                        if (t.length > 2 && t.length < 5000 &&
                            !t.includes('View Profile') && !t.includes('Joined ')) {
                            const childDivs = cd.querySelectorAll('div');
                            if (childDivs.length <= 2) {
                                content = t.replace(/(\\d{1,2}:\\d{2}\\s*[AP]M)/g, '').trim();
                                if (content.length > 0) break;
                            }
                        }
                    }

                    if (!content || content.length < 1) continue;

                    // Determine isFromUs via multiple signals. The previous
                    // heuristic (any SVG present => ours) misclassified inbound
                    // messages that contained a link-preview card, because the
                    // card itself renders SVG icons (GitHub logo, external-link
                    // glyph, etc.). See DM #1486 / session d986d23e where an
                    // inbound "U can check its open source" + auto-unfurled
                    // GitHub card was labeled as ours and the agent then
                    // reconciled to DB with a bare-URL outbound.
                    //
                    // Signal 1 (strong): delivery receipt text. Seen/Delivered/
                    //   Sent only render on our outgoing messages.
                    let hasStatusText = false;
                    const statusCandidates = item.querySelectorAll('span, div');
                    for (const s of statusCandidates) {
                        const t = (s.textContent || '').trim();
                        if (t === 'Seen' || t === 'Delivered' || t === 'Sent') {
                            hasStatusText = true;
                            break;
                        }
                        if (/^Seen\\s+\\d/.test(t) || /^Delivered\\s+\\d/.test(t)) {
                            hasStatusText = true;
                            break;
                        }
                    }

                    // Signal 2: horizontal alignment. X right-aligns our bubbles.
                    let hasRightAlign = false;
                    const alignCandidates = item.querySelectorAll('div[style]');
                    for (const a of alignCandidates) {
                        const style = a.getAttribute('style') || '';
                        if (style.indexOf('flex-end') !== -1 ||
                            style.indexOf('justify-content: end') !== -1) {
                            hasRightAlign = true;
                            break;
                        }
                    }

                    // Signal 3 (fallback): SVG presence, but only delivery-status
                    //   SVGs. Exclude SVGs inside <a>, inside card/article wrappers,
                    //   and inside any element that also contains an <img>
                    //   (all strong tells of a link-preview, not a receipt).
                    let hasDeliverySvg = false;
                    const allSvgs = item.querySelectorAll('svg');
                    for (const svg of allSvgs) {
                        if (svg.closest('a')) continue;
                        if (svg.closest('article')) continue;
                        if (svg.closest('[data-testid*="card"]')) continue;
                        if (svg.closest('[role="link"]')) continue;
                        const wrapperWithImg = svg.closest('div');
                        if (wrapperWithImg && wrapperWithImg.querySelector('img')) continue;
                        hasDeliverySvg = true;
                        break;
                    }

                    isFromUs = hasStatusText || hasRightAlign || hasDeliverySvg;

                    messages.push({
                        sender: isFromUs ? 'us' : partnerName || partnerHandle || 'them',
                        content: content,
                        time: currentDate ? currentDate + ' ' + time : time,
                        is_from_us: isFromUs,
                    });
                }

                const recent = messages.slice(-maxMessages);

                return {
                    partner_name: partnerName,
                    partner_handle: partnerHandle,
                    messages: recent,
                    total_found: messages.length,
                };
            }""", {"maxMessages": max_messages, "ourHandle": OUR_HANDLE})

            return result

        finally:
            if not is_cdp:
                page.close()
                browser.close()


def send_dm(thread_url, message, dm_id=None):
    """Send a message in a Twitter/X DM conversation.

    Navigates to the thread URL, types the message in the compose box,
    and sends it.

    Active Twitter campaigns with a `suffix` are applied at this tool layer:
    the suffix is appended to `message` (per `sample_rate` coin flip per
    campaign) before typing, so the literal text is guaranteed to be
    delivered. After a verified send, logs via dm_conversation.py log-outbound
    so the campaign counter advances automatically (the CLI auto-detects the
    suffix in stored content). `dm_id` is required for the auto-log; without
    it the suffix still applies but counter attribution is skipped.

    Returns: {"ok": true, "thread_url": "...", "verified": true,
              "applied_campaigns": [...], "message_sent": "..."}
              or {"ok": false, "error": "..."}
    """
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

    applied_campaigns = []
    for cid, suffix, sample_rate in _load_active_twitter_campaigns():
        if random.random() < sample_rate:
            # Wrap any URLs in the suffix through dm_short_links (DM rail) so
            # clicks attribute to this DM. Falls back to raw suffix if dm_id
            # missing or wrap fails (e.g. plain-text suffix " written with ai").
            wrapped_suffix = suffix
            if 'http' in suffix and dm_id is not None:
                try:
                    from dm_short_links import wrap_text as _wrap_text_dm
                    wrap_res2 = _wrap_text_dm(dm_id=dm_id, text=suffix)
                    if wrap_res2.get('ok') and wrap_res2.get('minted_codes'):
                        wrapped_suffix = wrap_res2['text']
                        minted_link_codes.extend(wrap_res2.get('minted_codes', []))
                        print(f"[send_dm] suffix wrap codes={wrap_res2['minted_codes']}",
                              file=sys.stderr)
                except Exception as _e:
                    print(f"[send_dm] suffix wrap failed ({_e}); raw",
                          file=sys.stderr)
            message = message + wrapped_suffix
            applied_campaigns.append(cid)
    print(f"[send_dm] applied_campaigns={applied_campaigns} minted_links={minted_link_codes} message_len={len(message)} dm_id={dm_id}",
          file=sys.stderr)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)

        try:
            rl_counter = _install_rate_limit_listener(page)
            # Navigate to DM inbox first, then click into conversation.
            # Direct URL navigation to DM conversations often hangs
            # because X's SPA doesn't fire domcontentloaded for DM routes.
            page.evaluate("window.location.href = 'https://x.com/i/chat/'")
            page.wait_for_timeout(5000)

            unreachable, reason = _is_x_unreachable(page)
            if unreachable:
                return _rate_limit_response(reason, rl_counter, page.url)

            # Handle DM passcode if needed
            _handle_dm_passcode(page)
            page.wait_for_timeout(2000)

            # Extract conversation ID from URL and click into it
            conv_id = thread_url.rstrip("/").split("/")[-1]
            try:
                conv_link = page.locator(f'a[href*="{conv_id}"]').first
                conv_link.click()
                page.wait_for_timeout(3000)
            except Exception:
                return {"ok": False, "error": "conversation_not_found_in_sidebar"}

            # Find the message input box
            msg_box = None
            for label in ["Unencrypted message", "Start a new message"]:
                try:
                    msg_box = page.get_by_role("textbox", name=label)
                    msg_box.wait_for(state="visible", timeout=5000)
                    break
                except Exception:
                    msg_box = None

            if not msg_box:
                try:
                    msg_box = page.locator(
                        'div[role="textbox"][contenteditable="true"]'
                    ).last
                    msg_box.wait_for(state="visible", timeout=3000)
                except Exception:
                    return {"ok": False, "error": "message_box_not_found"}

            # Click and type
            msg_box.click()
            page.wait_for_timeout(500)
            page.keyboard.type(message, delay=10)
            page.wait_for_timeout(1000)

            # Send: press Enter (Twitter DMs send on Enter)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2000)

            # Verify: check if the message appears in the conversation
            msg_start = message[:50]
            verified = page.evaluate("""(msgStart) => {
                const main = document.querySelector('main');
                if (!main) return false;
                const text = main.textContent || '';
                return text.includes(msgStart);
            }""", msg_start)

            if verified and dm_id is not None:
                _log_twitter_dm_outbound(dm_id, message, minted_codes=minted_link_codes)

            return {
                "ok": verified,
                "thread_url": page.url,
                "verified": verified,
                "error": None if verified else "send_unverified_no_dom_confirmation",
                "applied_campaigns": applied_campaigns,
                "minted_link_codes": minted_link_codes,
                "message_sent": message,
            }

        finally:
            if not is_cdp:
                page.close()
                browser.close()


def discover_notifications(scroll_count=8, tab="all"):
    """Scrape tweet notifications from x.com/notifications[/{tab}].

    tab:
        "all"       -> /notifications       (default; includes replies to our tweets,
                                             replies to our replies without @-tag,
                                             plus mentions — superset of "mentions")
        "mentions"  -> /notifications/mentions (only explicit @-mentions)
        "verified"  -> /notifications/verified

    Scrolls the selected tab and extracts each tweet as a notification record.
    No API cost (uses the logged-in session via CDP).

    Returns: {"notifications": [...], "total": N, "tab": "..."} or {"error": "..."}
    """
    valid_tabs = {"all": "", "mentions": "/mentions", "verified": "/verified"}
    if tab not in valid_tabs:
        return {"error": f"invalid tab {tab!r}; valid: {sorted(valid_tabs)}"}
    target_url = f"https://x.com/notifications{valid_tabs[tab]}"
    print(f"[twitter_browser] discover_notifications called (scroll_count={scroll_count}, tab={tab}, url={target_url})", file=sys.stderr)
    from playwright.sync_api import sync_playwright

    EXTRACTOR_JS = r"""() => {
      const out = [];
      for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
        try {
          let handle = '';
          let displayName = '';
          for (const link of article.querySelectorAll('a[role="link"]')) {
            const href = link.getAttribute('href');
            if (href && href.startsWith('/') && !href.includes('/status/') && !href.includes('/i/') && href.length > 1 && href.split('/').length === 2) {
              handle = href.replace('/', '');
              const nameEl = link.querySelector('span');
              if (nameEl) displayName = nameEl.textContent || '';
              break;
            }
          }
          const tweetText = article.querySelector('[data-testid="tweetText"]');
          const text = tweetText ? tweetText.textContent : '';
          const timeEl = article.querySelector('time');
          const timeParent = timeEl ? timeEl.closest('a') : null;
          const tweetHref = timeParent ? timeParent.getAttribute('href') : '';
          const tweetUrl = tweetHref ? ('https://x.com' + tweetHref) : '';
          const datetime = timeEl ? timeEl.getAttribute('datetime') : '';
          const idMatch = tweetHref ? tweetHref.match(/\/status\/(\d+)/) : null;
          const tweetId = idMatch ? idMatch[1] : '';
          let replies=0, retweets=0, likes=0, views=0, bookmarks=0;
          for (const btn of article.querySelectorAll('[role="group"] button, [role="group"] a')) {
            const al = btn.getAttribute('aria-label') || '';
            let m;
            if (m=al.match(/([\d,]+)\s*repl/i)) replies=parseInt(m[1].replace(/,/g,''));
            if (m=al.match(/([\d,]+)\s*repost/i)) retweets=parseInt(m[1].replace(/,/g,''));
            if (m=al.match(/([\d,]+)\s*like/i)) likes=parseInt(m[1].replace(/,/g,''));
            if (m=al.match(/([\d,]+)\s*view/i)) views=parseInt(m[1].replace(/,/g,''));
            if (m=al.match(/([\d,]+)\s*bookmark/i)) bookmarks=parseInt(m[1].replace(/,/g,''));
          }
          // Detect reply-to target (if tweet is a reply, there's a "Replying to" block)
          let replyingTo = '';
          const socialContext = article.querySelector('[data-testid="socialContext"]');
          const ariaLabel = article.getAttribute('aria-label') || '';
          for (const span of article.querySelectorAll('a[href^="/"]')) {
            const href = span.getAttribute('href') || '';
            if (href.includes('/status/') && span.textContent && span.textContent.trim().startsWith('@')) {
              replyingTo = span.textContent.trim().replace(/^@/, '');
              break;
            }
          }
          if (tweetId && handle) {
            out.push({
              tweet_id: tweetId,
              handle: handle,
              display_name: displayName.trim(),
              text: (text || ''),
              tweet_url: tweetUrl,
              datetime: datetime,
              replies: replies, retweets: retweets, likes: likes, views: views, bookmarks: bookmarks,
              replying_to: replyingTo
            });
          }
        } catch(e) {}
      }
      return out;
    }"""

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)
        try:
            page.goto(target_url, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)

            seen = set()
            all_tweets = []
            for i in range(scroll_count):
                try:
                    new_tweets = page.evaluate(EXTRACTOR_JS)
                except Exception as e:
                    print(f"[notifications] extractor error on scroll {i}: {e}", file=sys.stderr)
                    new_tweets = []
                added = 0
                for t in new_tweets:
                    tid = t.get('tweet_id')
                    if tid and tid not in seen:
                        seen.add(tid)
                        all_tweets.append(t)
                        added += 1
                print(f"[notifications] scroll {i+1}/{scroll_count}: +{added} new, total {len(all_tweets)}", file=sys.stderr)
                page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                page.wait_for_timeout(1500)
                _refresh_browser_lock()

            return {"notifications": all_tweets, "total": len(all_tweets), "tab": tab}
        finally:
            if not is_cdp:
                page.close()
                browser.close()


def scrape_thread_followups(thread_url, scroll_count=3):
    """Navigate to a tweet's permalink and extract reply articles below it.

    Used to detect depth-2+ replies to our own replies that the notifications
    tab may not surface (X default behavior drops @-tags inside active threads).

    Returns: {"thread_url": "...", "anchor_tweet_id": "...", "followups": [...]}
             where each followup has the same shape as a notifications record.
    """
    print(f"[twitter_browser] scrape_thread_followups({thread_url!r}, scroll={scroll_count})", file=sys.stderr)
    from playwright.sync_api import sync_playwright

    anchor_match = re.search(r"/status/(\d+)", thread_url or "")
    anchor_tweet_id = anchor_match.group(1) if anchor_match else ""

    EXTRACTOR_JS = r"""() => {
      const out = [];
      for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
        try {
          let handle = '';
          let displayName = '';
          for (const link of article.querySelectorAll('a[role="link"]')) {
            const href = link.getAttribute('href');
            if (href && href.startsWith('/') && !href.includes('/status/') && !href.includes('/i/') && href.length > 1 && href.split('/').length === 2) {
              handle = href.replace('/', '');
              const nameEl = link.querySelector('span');
              if (nameEl) displayName = nameEl.textContent || '';
              break;
            }
          }
          const tweetText = article.querySelector('[data-testid="tweetText"]');
          const text = tweetText ? tweetText.textContent : '';
          const timeEl = article.querySelector('time');
          const timeParent = timeEl ? timeEl.closest('a') : null;
          const tweetHref = timeParent ? timeParent.getAttribute('href') : '';
          const tweetUrl = tweetHref ? ('https://x.com' + tweetHref) : '';
          const datetime = timeEl ? timeEl.getAttribute('datetime') : '';
          const idMatch = tweetHref ? tweetHref.match(/\/status\/(\d+)/) : null;
          const tweetId = idMatch ? idMatch[1] : '';
          // Detect reply-to target (article with "Replying to" block)
          let replyingTo = '';
          for (const span of article.querySelectorAll('a[href^="/"]')) {
            const href = span.getAttribute('href') || '';
            if (!href.includes('/status/') && span.textContent && span.textContent.trim().startsWith('@')) {
              replyingTo = span.textContent.trim().replace(/^@/, '');
              break;
            }
          }
          if (tweetId && handle) {
            out.push({
              tweet_id: tweetId,
              handle: handle,
              display_name: displayName.trim(),
              text: (text || ''),
              tweet_url: tweetUrl,
              datetime: datetime,
              replying_to: replyingTo
            });
          }
        } catch(e) {}
      }
      return out;
    }"""

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)
        try:
            page.goto(thread_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)

            seen = set()
            all_tweets = []
            for i in range(scroll_count):
                try:
                    new_tweets = page.evaluate(EXTRACTOR_JS)
                except Exception as e:
                    print(f"[thread_followups] extractor error on scroll {i}: {e}", file=sys.stderr)
                    new_tweets = []
                for t in new_tweets:
                    tid = t.get('tweet_id')
                    if tid and tid not in seen:
                        seen.add(tid)
                        all_tweets.append(t)
                page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                page.wait_for_timeout(1200)
                _refresh_browser_lock()

            followups = [t for t in all_tweets if t.get('tweet_id') != anchor_tweet_id]
            return {
                "thread_url": thread_url,
                "anchor_tweet_id": anchor_tweet_id,
                "followups": followups,
                "total": len(followups),
            }
        finally:
            if not is_cdp:
                page.close()
                browser.close()


def scrape_many_thread_followups(thread_urls, scroll_count=3, per_url_delay_ms=2500):
    """Iterate scrape_thread_followups over a list of URLs.

    Keeps one browser session open (cheaper) and applies a polite delay between URLs.
    """
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)
        try:
            for url in thread_urls:
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(3500)
                    anchor_match = re.search(r"/status/(\d+)", url or "")
                    anchor_tweet_id = anchor_match.group(1) if anchor_match else ""

                    EXTRACTOR_JS = r"""() => {
                      const out = [];
                      for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
                        try {
                          let handle = '';
                          let displayName = '';
                          for (const link of article.querySelectorAll('a[role="link"]')) {
                            const href = link.getAttribute('href');
                            if (href && href.startsWith('/') && !href.includes('/status/') && !href.includes('/i/') && href.length > 1 && href.split('/').length === 2) {
                              handle = href.replace('/', '');
                              const nameEl = link.querySelector('span');
                              if (nameEl) displayName = nameEl.textContent || '';
                              break;
                            }
                          }
                          const tweetText = article.querySelector('[data-testid="tweetText"]');
                          const text = tweetText ? tweetText.textContent : '';
                          const timeEl = article.querySelector('time');
                          const timeParent = timeEl ? timeEl.closest('a') : null;
                          const tweetHref = timeParent ? timeParent.getAttribute('href') : '';
                          const tweetUrl = tweetHref ? ('https://x.com' + tweetHref) : '';
                          const datetime = timeEl ? timeEl.getAttribute('datetime') : '';
                          const idMatch = tweetHref ? tweetHref.match(/\/status\/(\d+)/) : null;
                          const tweetId = idMatch ? idMatch[1] : '';
                          let replyingTo = '';
                          for (const span of article.querySelectorAll('a[href^="/"]')) {
                            const href = span.getAttribute('href') || '';
                            if (!href.includes('/status/') && span.textContent && span.textContent.trim().startsWith('@')) {
                              replyingTo = span.textContent.trim().replace(/^@/, '');
                              break;
                            }
                          }
                          if (tweetId && handle) {
                            out.push({tweet_id: tweetId, handle, display_name: displayName.trim(),
                                      text: (text || ''), tweet_url: tweetUrl,
                                      datetime, replying_to: replyingTo});
                          }
                        } catch(e) {}
                      }
                      return out;
                    }"""

                    seen = set()
                    collected = []
                    for i in range(scroll_count):
                        try:
                            new_tweets = page.evaluate(EXTRACTOR_JS)
                        except Exception:
                            new_tweets = []
                        for t in new_tweets:
                            tid = t.get('tweet_id')
                            if tid and tid not in seen:
                                seen.add(tid)
                                collected.append(t)
                        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                        page.wait_for_timeout(1200)
                        _refresh_browser_lock()

                    followups = [t for t in collected if t.get('tweet_id') != anchor_tweet_id]
                    print(f"[thread_followups] {url}: {len(followups)} candidate follow-ups", file=sys.stderr)
                    results.append({
                        "thread_url": url,
                        "anchor_tweet_id": anchor_tweet_id,
                        "followups": followups,
                    })
                except Exception as e:
                    print(f"[thread_followups] error on {url}: {e}", file=sys.stderr)
                    results.append({"thread_url": url, "error": str(e), "followups": []})
                page.wait_for_timeout(per_url_delay_ms)
            return {"results": results, "urls_visited": len(thread_urls)}
        finally:
            if not is_cdp:
                page.close()
                browser.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "reply":
        if len(sys.argv) < 4:
            print(
                "Usage: twitter_browser.py reply <tweet_url> <reply_text>",
                file=sys.stderr,
            )
            sys.exit(1)
        result = reply_to_tweet(sys.argv[2], sys.argv[3])
        print(json.dumps(result, indent=2))

    elif cmd == "self-reply":
        # Self-reply with guaranteed project URL. The URL is passed as a
        # separate arg and appended at the tool level so the LLM cannot
        # strip it from the text (which happened repeatedly when relying
        # on prompt instructions alone).
        if len(sys.argv) < 5:
            print(
                "Usage: twitter_browser.py self-reply <our_reply_url> <text> <project_url>",
                file=sys.stderr,
            )
            sys.exit(1)
        our_url, text, project_url = sys.argv[2], sys.argv[3], sys.argv[4]
        if not project_url.startswith("http"):
            print(
                f"self-reply: project_url must start with http(s), got: {project_url!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        stripped = text.rstrip()
        if project_url in stripped:
            final = stripped
        else:
            final = f"{stripped} {project_url}"
        # Self-reply opts out of the campaign suffix: this turn is the
        # project-URL follow-up, not the primary post that gets tagged.
        result = reply_to_tweet(our_url, final, apply_campaigns=False)
        result["final_text"] = final
        print(json.dumps(result, indent=2))

    elif cmd == "unread-dms":
        result = unread_dms()
        print(json.dumps(result, indent=2))

    elif cmd == "read-conversation":
        if len(sys.argv) < 3:
            print(
                "Usage: twitter_browser.py read-conversation <thread_url>",
                file=sys.stderr,
            )
            sys.exit(1)
        result = read_conversation(sys.argv[2])
        print(json.dumps(result, indent=2))

    elif cmd == "send-dm":
        if len(sys.argv) < 4:
            print(
                "Usage: twitter_browser.py send-dm <thread_url> <message> [dm_id]",
                file=sys.stderr,
            )
            sys.exit(1)
        dm_id_arg = None
        if len(sys.argv) >= 5 and sys.argv[4].strip():
            try:
                dm_id_arg = int(sys.argv[4])
            except ValueError:
                print(f"send-dm: dm_id must be int, got {sys.argv[4]!r}", file=sys.stderr)
                sys.exit(1)
        result = send_dm(sys.argv[2], sys.argv[3], dm_id=dm_id_arg)
        print(json.dumps(result, indent=2))

    elif cmd == "notifications":
        scroll_count = 8
        tab = "all"
        if len(sys.argv) >= 3:
            try:
                scroll_count = int(sys.argv[2])
            except ValueError:
                print(f"notifications: scroll_count must be int, got {sys.argv[2]!r}", file=sys.stderr)
                sys.exit(1)
        if len(sys.argv) >= 4:
            tab = sys.argv[3]
        result = discover_notifications(scroll_count=scroll_count, tab=tab)
        print(json.dumps(result, indent=2))

    elif cmd == "thread-followups":
        if len(sys.argv) < 3:
            print(
                "Usage: twitter_browser.py thread-followups <urls_file.txt>\n"
                "  urls_file.txt: one tweet permalink per line (our reply URLs)",
                file=sys.stderr,
            )
            sys.exit(1)
        urls_path = sys.argv[2]
        scroll_count = 3
        if len(sys.argv) >= 4:
            try:
                scroll_count = int(sys.argv[3])
            except ValueError:
                print(f"thread-followups: scroll_count must be int, got {sys.argv[3]!r}", file=sys.stderr)
                sys.exit(1)
        with open(urls_path) as f:
            urls = [line.strip() for line in f if line.strip()]
        if not urls:
            print(json.dumps({"results": [], "urls_visited": 0}, indent=2))
            sys.exit(0)
        result = scrape_many_thread_followups(urls, scroll_count=scroll_count)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
