#!/usr/bin/env python3
"""Twitter/X browser automation functions for Social Autoposter.

Replaces multi-step Claude browser MCP calls with single Python function calls.
Each function does all browser work internally and returns structured JSON.

Usage:
    # Reply to a tweet (auto-likes the parent tweet after the reply lands)
    python3 twitter_browser.py reply "https://x.com/user/status/123" "reply text"

    # Like a tweet (standalone; same like the reply path fires automatically)
    python3 twitter_browser.py like "https://x.com/user/status/123"

    # Scan DM inbox for unread conversations
    python3 twitter_browser.py unread-dms

    # Read messages from a DM conversation
    python3 twitter_browser.py read-conversation "https://x.com/i/chat/123-456"

    # Send a DM message
    python3 twitter_browser.py send-dm "https://x.com/i/chat/123-456" "message text"

Requires: pip install playwright && playwright install chromium

Connects to the running twitter-harness MCP browser via CDP (Chrome DevTools
Protocol, http://127.0.0.1:9555 by default; override via TWITTER_CDP_URL env
var set by skill/lib/twitter-backend.sh) to reuse the existing logged-in
session on the browser-harness profile.
"""

import atexit
import json
import os
import random
import re
import signal
import subprocess
import sys
import time


LOCK_FILE = os.path.expanduser("~/.claude/twitter-browser-lock.json")
LOCK_EXPIRY = 300  # process-level mutex TTL; refreshed during long ops
# Posting-specific silence ceiling, DECOUPLED from the fleet-wide LOCK_EXPIRY.
# A role:"post" holder (an approved batch, or a single reply) is reclaimed by a
# peer once its lock has gone unrefreshed this long; a role:"scan" holder keeps
# the 300s LOCK_EXPIRY untouched. Posting refreshes the lock at every candidate
# boundary (twitter_post_plan holds it across the whole batch), so a healthy
# poster never goes silent this long -- only a genuinely hung poster (e.g.
# link_tail's `claude -p` wedged) trips it. Kept as its own knob so tuning the
# scan TTL never moves the poster's hang ceiling and vice-versa. Must exceed the
# worst-case single candidate step (one reply + the link_tail AI call), and stay
# well under any value that would let a hung poster block the browser for long.
POST_LOCK_EXPIRY = 180  # seconds; applies ONLY to a role:"post" holder
LOCK_WAIT_MAX = 45  # seconds to wait for lock to free before giving up
LOCK_POLL_INTERVAL = 2
PREEMPT_KILL_WAIT = 5  # secs to wait for a preempted scan holder to die before SIGKILL

# Lock role priority. A "post" holder is user-initiated (an approved reply) and
# outranks any "scan" holder (the scan/draft cycle, autopilot or plugin). When a
# poster finds a LIVE lower-priority holder it PREEMPTS it (SIGTERM + reclaim)
# instead of waiting LOCK_WAIT_MAX and giving up. This is what makes "posting
# takes priority over scanning" hold CROSS-PROCESS: the old in-process
# preemptScanForPost only killed the plugin's own scan, never a scan spawned by a
# separate autopilot agent / launchd cron, so an approved post kept losing the
# 45s race to a live scan that held the browser. Default "scan" so any unmarked
# browser op is preemptable; only the poster path sets SAPS_LOCK_ROLE=post.
LOCK_ROLE = (os.environ.get("SAPS_LOCK_ROLE") or "scan").strip() or "scan"
VIEWPORT = {"width": 911, "height": 1016}

# Posting handle. Resolved at call time from AUTOPOSTER_TWITTER_HANDLE env
# var (set by per-account launchd/systemd units) or config.json
# accounts.twitter.handle. Returns None when neither source is set.
#
# There is intentionally NO hardcoded fallback handle. The old "m13v_"
# default meant any install with an unset handle silently posted under the
# repo owner's identity: it stamped posts.our_account = m13v_ and built reply
# permalinks as x.com/m13v_/status/<id> for tweets that actually belonged to a
# different account, corrupting attribution in the shared DB. Callers that
# build a URL or post under this identity MUST treat None as "account not
# configured" and refuse, rather than impersonate someone.
def our_handle():
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import account_resolver
        return account_resolver.resolve("twitter")
    except Exception:
        return None

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

    Returns [(id, suffix, sample_rate), ...]. On any failure (no API, no
    creds, network glitch) returns []. This keeps twitter_browser.py usable
    in non-DB contexts (e.g. ad-hoc invocations from a shell). Mirrors the
    `_load_active_reddit_campaigns_for_dm` helper in reddit_browser.py.

    Migrated 2026-05-18: was a direct psycopg2 SELECT; now hits
    /api/v1/campaigns?platform=twitter&has_suffix=true&with_budget_remaining=true&status=active
    via scripts/http_api.py. Same WHERE clause runs server-side.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get
        resp = api_get(
            "/api/v1/campaigns",
            query={
                "status": "active",
                "platform": "twitter",
                "has_suffix": "true",
                "with_budget_remaining": "true",
                "limit": 50,
            },
        )
        rows = (resp.get("data") or {}).get("campaigns") or []
        out = []
        for r in rows:
            suffix = r.get("suffix")
            if not suffix:
                continue
            sample_rate = r.get("sample_rate")
            try:
                sample_rate = float(sample_rate if sample_rate is not None else 1.0)
            except (TypeError, ValueError):
                sample_rate = 1.0
            out.append((r.get("id"), suffix, sample_rate))
        return out
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
    """Find the CDP port of the running twitter-harness Chrome.

    Scans all chrome/chromium processes for --remote-debugging-port=NNNN and
    returns the first port whose /json index lists at least one x.com or
    twitter.com tab (preferring logged-in tabs over login pages). Used only
    as a fallback when TWITTER_CDP_URL isn't exported by the caller.
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
    """Check whether a Claude session UUID lock holder is still running.

    A live Claude session puts its UUID on the cmdline as
    `claude --session-id <UUID>`. pgrep matches it; absence means the
    holder is dead and the lock is stale, even if its JSONL transcript
    is still tail-flushing. Legacy semantics from the retired
    twitter-agent-lock.sh PreToolUse hook; only python:PID holders are
    written to the lock file today, so this code path is dormant unless
    a Claude session still inherits an in-flight UUID lock.
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


def _is_python_holder_alive(holder: str) -> bool:
    """Liveness probe for a `python:PID` lock holder.

    Holders written today are `python:<pid>` (see _LOCK_SESSION_ID). Before this
    check existed (defect a, 2026-06-16), a holder whose process died WITHOUT
    running its atexit _release_browser_lock (SIGKILL, OOM, watchdog SIGTERM,
    hard hang) left the lockfile behind, and _acquire_browser_lock had no way to
    tell it was dead -- so every peer waited the full LOCK_WAIT_MAX and gave up,
    and the lock only cleared after LOCK_EXPIRY (300s). os.kill(pid, 0) sends no
    signal; it just probes existence. Returns True (treat as held, do NOT steal)
    for anything we cannot prove dead, so the worst case degrades to the old
    LOCK_EXPIRY failsafe rather than stealing a live peer's lock.
    """
    if not holder.startswith("python:"):
        return True  # not a python holder; this probe makes no claim
    try:
        pid = int(holder.split(":", 1)[1])
    except (ValueError, IndexError):
        return True  # unparseable holder -> don't steal on this basis
    try:
        os.kill(pid, 0)
        return True            # process exists -> alive
    except ProcessLookupError:
        return False           # no such process -> dead, reclaimable
    except PermissionError:
        return True            # exists but another owner -> alive
    except OSError:
        return True            # ambiguous -> err toward NOT stealing


def _try_take_lock() -> bool:
    """Atomically claim LOCK_FILE for this process. Returns True iff we created
    it. O_CREAT|O_EXCL makes "is it free? then take it" a single syscall, so two
    cold-start acquirers can't both win the way the old os.path.exists +
    open(w) check-then-act allowed (defect c, 2026-06-16). A False return means a
    peer beat us to it; the caller re-loops and re-evaluates the holder.
    """
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, json.dumps(
            {"session_id": _LOCK_SESSION_ID, "timestamp": int(time.time()), "role": LOCK_ROLE}
        ).encode())
    finally:
        os.close(fd)
    return True


def _preempt_holder(pid: int) -> bool:
    """Preempt a live lock holder we outrank (a poster taking the browser from a
    scan). SIGTERM it, wait PREEMPT_KILL_WAIT for it to die so its pid frees the
    lock, then escalate to SIGKILL once. Returns True once the holder is gone
    (or was already gone). Best-effort; never raises. The caller then removes the
    stale lockfile and claims it via O_EXCL.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True            # already gone
    except OSError:
        return False           # not ours to signal / ambiguous -> don't claim
    deadline = time.time() + PREEMPT_KILL_WAIT
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True        # ProcessLookupError or perm change -> dead enough
        time.sleep(0.2)
    # Still alive after the SIGTERM grace window -> escalate once.
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def _acquire_browser_lock():
    """Acquire the Twitter browser session mutex (~/.claude/twitter-browser-lock.json).

    This file-mutex is the UNIVERSAL serializer for every twitter_browser.py
    browser op (all of them route through get_browser_and_page below). The shell
    FIFO lock in skill/lock.sh only serializes the pipelines that bother to take
    it; this one catches everything, including cross-pipeline handoff races and
    MCP-driven posts.

    Holders today are python:PID. UUID-style holders are a legacy artifact of the
    retired PreToolUse hook (twitter-agent-lock.sh); a live UUID holder is a
    parent Claude session still in flight, so we INHERIT rather than fight it.

    Reclaim priority (a holder we can PROVE is dead is taken immediately, so a
    crashed peer can never starve the fleet for LOCK_WAIT_MAX/LOCK_EXPIRY):
      1. holder == us            -> re-entrant; we already hold it.
      2. UUID holder, pid gone   -> stale legacy lock, reclaim.
      3. python:PID, pid gone    -> dead peer (defect a fix), reclaim.
      4. age >= LOCK_EXPIRY      -> failsafe for holders we cannot probe.
      5. live UUID holder        -> inherit (parent session).
      6. live python:PID holder  -> real peer; wait, then give up after
                                    LOCK_WAIT_MAX with a structured error.

    Acquisition itself is atomic (_try_take_lock / O_EXCL), so the moment we
    decide the lock is free, no concurrent acquirer can also claim it.

    NOTE for future maintainers: do NOT "simplify" this by having the shell
    pipelines `rm -f` the lockfile around release_lock. That blind rm deleted
    LIVE peers' locks (defect b) and was removed 2026-06-16. Dead holders are
    reclaimed here instead. See docs/twitter_browser_lock.md.
    """
    global _LOCK_SESSION_ID, _LOCK_INHERITED
    deadline = time.time() + LOCK_WAIT_MAX
    # Guarantee the lock dir exists so _try_take_lock's O_EXCL create can't fail
    # for a missing-parent reason (which would otherwise spin the no-file path).
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    except OSError:
        pass
    while True:
        if not os.path.exists(LOCK_FILE):
            if _try_take_lock():
                break
            # Lost the create race to a peer (or a persistent create failure).
            # Bound by `deadline` so this path can never spin forever.
            if time.time() >= deadline:
                print(json.dumps({
                    "success": False,
                    "error": f"Twitter browser lock contended on create; waited {LOCK_WAIT_MAX}s, giving up."
                }))
                sys.exit(1)
            time.sleep(LOCK_POLL_INTERVAL)
            continue
        try:
            with open(LOCK_FILE) as f:
                lock = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Corrupt / half-written / vanished between exists() and open().
            # Try to claim atomically; if a peer holds a valid lock our O_EXCL
            # create fails and we re-loop. Bounded by `deadline` so a persistently
            # unreadable lockfile gives up instead of hanging the pipeline.
            if _try_take_lock():
                break
            if time.time() >= deadline:
                print(json.dumps({
                    "success": False,
                    "error": f"Twitter browser lock unreadable; waited {LOCK_WAIT_MAX}s, giving up."
                }))
                sys.exit(1)
            time.sleep(LOCK_POLL_INTERVAL)
            continue
        age = time.time() - lock.get("timestamp", 0)
        holder = lock.get("session_id", "")
        holder_role = lock.get("role", "scan")  # legacy locks (no role) = preemptable

        # 1. Re-entrant: the lock is already ours (same process, or a stale lock
        # left by a previous process whose PID we have since reused). Refresh the
        # timestamp so a peer's LOCK_EXPIRY failsafe can't reclaim it under us.
        if holder == _LOCK_SESSION_ID and not _LOCK_INHERITED:
            _refresh_browser_lock()
            break

        # 1b. Batch-owner inherit (posting). The poster (twitter_post_plan.py)
        # acquires this lock ONCE and holds it across the WHOLE approved batch,
        # exporting its own session id as SAPS_LOCK_OWNER for the child
        # twitter_browser.py reply subprocesses it spawns. Each child INHERITS the
        # parent's hold instead of contending for it -- two role:"post" peers would
        # otherwise both fall to the case-6 peer-wait and give up after
        # LOCK_WAIT_MAX, breaking the post. The child refreshes the timestamp
        # (proof of progress at this candidate boundary, so the POST_LOCK_EXPIRY
        # failsafe only ever fires on a real hang) and, being _LOCK_INHERITED,
        # leaves the lock in place for the PARENT to release at batch end. A DEAD
        # owner is never inherited: the alive-probe fails here and we fall through
        # to the dead_python reclaim below, so a crashed batch can't wedge the
        # browser. This is what closes the inter-candidate gap (the link_tail
        # claude -p call, ~5-20s) the every-60s autopilot scan used to slip into.
        _batch_owner = os.environ.get("SAPS_LOCK_OWNER") or ""
        if holder and holder == _batch_owner and _is_python_holder_alive(holder):
            _LOCK_SESSION_ID = holder
            _LOCK_INHERITED = True
            _refresh_browser_lock()
            print(f"[browser_lock] inherited batch owner={holder} "
                  f"role={holder_role} -> pid={os.getpid()}", file=sys.stderr)
            break

        # 2-4. Reclaim a holder we can prove is dead/expired. Remove-then-take so
        # the O_EXCL claim wins; if a peer reclaims at the same instant exactly
        # one of us creates the file and the other re-loops (never both).
        reclaim_reason = ""
        if _UUID_RE.match(holder or "") and not _is_holder_alive(holder):
            reclaim_reason = "dead_uuid"
        elif holder.startswith("python:") and not _is_python_holder_alive(holder):
            reclaim_reason = "dead_python"
        elif age >= (POST_LOCK_EXPIRY if holder_role == "post" else LOCK_EXPIRY):
            # Role-aware failsafe: a hung poster self-clears on the posting-only
            # POST_LOCK_EXPIRY, a scan on the fleet-wide LOCK_EXPIRY. Scan
            # behaviour is unchanged; only the post ceiling is decoupled.
            reclaim_reason = "expired"
        if reclaim_reason:
            try:
                os.remove(LOCK_FILE)
            except OSError:
                pass
            if _try_take_lock():
                # Verifiable signal that defect-a starvation was prevented.
                print(f"[browser_lock] reclaimed holder={holder or '<none>'} "
                      f"reason={reclaim_reason} age={int(age)}s -> pid={os.getpid()}",
                      file=sys.stderr)
                break
            time.sleep(LOCK_POLL_INTERVAL)
            continue

        # 5. Live UUID holder = parent Claude session still in flight -> inherit.
        if _UUID_RE.match(holder or ""):
            _LOCK_SESSION_ID = holder
            _LOCK_INHERITED = True
            break

        # 5b. POSTING PRIORITY (cross-process). A LIVE python:PID peer running a
        # lower-priority op (role != "post": the scan/draft cycle, whether the
        # plugin's own, a separate autopilot agent's, or the launchd cron's) must
        # YIELD to an approved post. Preempt it by signal and reclaim, so the post
        # takes the browser at once instead of waiting LOCK_WAIT_MAX and giving up
        # while the scan holds it. The aborted scan just re-runs next cron tick;
        # posting is the scarce, user-initiated action. Only a poster
        # (LOCK_ROLE == "post") ever preempts, and only a non-post holder -- two
        # posters fall through to the normal peer-wait below so neither kills the
        # other. UUID holders are handled above (we inherit, never kill those).
        if (
            LOCK_ROLE == "post"
            and holder.startswith("python:")
            and holder_role != "post"
            and _is_python_holder_alive(holder)
        ):
            try:
                victim_pid = int(holder.split(":", 1)[1])
            except (ValueError, IndexError):
                victim_pid = 0
            if victim_pid and _preempt_holder(victim_pid):
                try:
                    os.remove(LOCK_FILE)
                except OSError:
                    pass
                if _try_take_lock():
                    print(
                        f"[browser_lock] post preempted holder={holder} "
                        f"role={holder_role} age={int(age)}s -> pid={os.getpid()}",
                        file=sys.stderr,
                    )
                    break
            # Preempt didn't land (couldn't kill, or a peer reclaimed first) ->
            # re-loop and re-evaluate rather than busy-spin.
            time.sleep(LOCK_POLL_INTERVAL)
            continue

        # 6. Live python:PID peer. Wait, then give up. Reaching the deadline now
        # means the holder is a genuinely LIVE peer (dead ones were reclaimed
        # above), i.e. real contention -- NOT the defect-a starvation. The
        # "locked by session" substring is preserved for downstream parsers.
        if time.time() >= deadline:
            print(json.dumps({
                "success": False,
                "error": f"Twitter browser locked by session {holder} ({int(age)}s, peer alive); waited {LOCK_WAIT_MAX}s, giving up."
            }))
            sys.exit(1)
        time.sleep(LOCK_POLL_INTERVAL)
        continue


def _refresh_browser_lock():
    """Refresh the lock timestamp to prevent expiry during long operations."""
    try:
        with open(LOCK_FILE, "w") as f:
            json.dump({"session_id": _LOCK_SESSION_ID, "timestamp": int(time.time()), "role": LOCK_ROLE}, f)
    except OSError:
        pass


def get_browser_and_page(playwright):
    """Connect to the running twitter-harness Chrome via CDP.

    Returns (browser, page, is_cdp=True). `page` is a reused existing Twitter
    tab when one is open; otherwise a freshly created page on the same
    browser-harness context. Caller should navigate it, not close it.

    Connection order:
      1. TWITTER_CDP_URL env (set by lib/twitter-backend.sh) — direct attach.
      2. find_twitter_cdp_port() — ps-based discovery of any Chrome serving
         x.com/twitter.com (fallback when env not exported by the caller).

    Both paths target the browser-harness Chrome since the legacy twitter-agent
    profile + MCP wrapper were retired on 2026-05-19. There is no
    launch_persistent_context fallback: if neither CDP attach succeeds the
    caller (skill/lib/twitter-backend.sh:ensure_twitter_browser_for_backend)
    is responsible for booting the harness Chrome first.
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
                return browser, context.new_page(), True
        except Exception as e:
            _release_browser_lock()
            print(json.dumps({
                "success": False,
                "error": f"harness CDP attach failed (port {cdp_port}): {e}"
            }))
            sys.exit(1)

    _release_browser_lock()
    print(json.dumps({
        "success": False,
        "error": (
            "No twitter-harness Chrome reachable. Set TWITTER_CDP_URL or boot "
            "harness Chrome via skill/lib/twitter-backend.sh:ensure_twitter_"
            "browser_for_backend before invoking twitter_browser.py."
        )
    }))
    sys.exit(1)


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
    """Collect all /<our_handle>/status/ links currently in the DOM."""
    handle = our_handle()
    return set(page.evaluate(f"""() => {{
        const links = new Set();
        document.querySelectorAll('a[href*="/{handle}/status/"]').forEach(a => {{
            const href = a.getAttribute('href');
            if (href && /\\/{handle}\\/status\\/\\d+$/.test(href))
                links.add(href);
        }});
        return [...links];
    }}"""))


def _wait_for_reply_textbox(page, total_timeout_ms=45000):
    """Wait for the reply composer textbox to mount. Returns a locator or None.

    Polls multiple selectors because the React composer sometimes attaches late
    on slow egress (E2B sandbox) and the aria-label has historically varied
    ("Post text" / "Tweet your reply" / "Post your reply"). The data-testid
    `tweetTextarea_0` has been stable for years and is the primary signal.
    """
    import time as _t
    selectors = (
        '[data-testid="tweetTextarea_0"]',
        '[role="textbox"][aria-label="Post text"]',
        '[role="textbox"][aria-label="Tweet your reply"]',
        '[role="textbox"][aria-label="Post your reply"]',
    )
    deadline = _t.monotonic() + (total_timeout_ms / 1000.0)
    while _t.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                pass
        page.wait_for_timeout(500)
    return None


# Post-action interstitials X shows AFTER a successful reply (e.g. the
# "Unlock more on X" graduated-access sheet). They don't block the post that
# triggered them, but the sheet stays up on screen and would overlay the
# composer on the NEXT reply in a batch -> spurious reply_box_not_found for
# posts 2..N. We dismiss them deterministically right after each successful
# post (not before the next reply), so the sheet never lingers. Targeted by the
# sheet's CTA label so we never touch a real compose/confirm dialog (those have
# no "Got it"); best-effort, fast, never raises.
_OVERLAY_DISMISS_LABELS = ("Got it", "Dismiss")


def _dismiss_known_overlays(page) -> bool:
    """Click-dismiss any known X nudge sheet currently covering the page.

    Returns True if something was dismissed. Safe to call on every reply: it is
    a no-op when no known overlay is present and swallows all errors."""
    for label in _OVERLAY_DISMISS_LABELS:
        try:
            btn = page.get_by_role("button", name=label, exact=True).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(800)
                print(f"[overlay] dismissed known interstitial via '{label}' button",
                      file=sys.stderr)
                return True
        except Exception:
            pass
    return False


def _dump_reply_failure_diag(page, tweet_url):
    """Dump screenshot + DOM state on reply_box_not_found. Returns a diag dict."""
    import time as _t
    ts = int(_t.time())
    diag = {"ts": ts, "tweet_url": tweet_url}
    try:
        diag["final_url"] = page.url
    except Exception as _e:
        diag["final_url_err"] = str(_e)
    try:
        png_path = f"/tmp/twitter_reply_failure_{ts}.png"
        page.screenshot(path=png_path, full_page=False)
        diag["screenshot"] = png_path
    except Exception as _e:
        diag["screenshot_err"] = str(_e)
    try:
        diag["dom"] = page.evaluate("""() => {
            const tbs = Array.from(document.querySelectorAll('[role="textbox"]'));
            const body = (document.body && document.body.innerText || '');
            const tweetRendered = !!document.querySelector('article[data-testid="tweet"]');
            // Reply-audience restriction: X renders one of these phrasings when the
            // author limits who can reply. "Only some accounts can reply" is the
            // confirmed live string; the others cover the documented variants.
            const RESTRICT = /Only some accounts can reply|People who follow .{0,40} can reply|Accounts .{0,40} (follows?|mentioned) can reply|People .{0,40} mentioned can reply|Verified accounts can reply|Subscribers can reply|You can.?t reply to this/i;
            const m = body.match(RESTRICT);
            // The audience control aria-label ("Everyone can reply" vs a restricted label).
            const audLabel = (Array.from(document.querySelectorAll('[aria-label]'))
                .map(e => e.getAttribute('aria-label') || '')
                .find(s => /can reply$/i.test(s)) || '');
            const restrictedByAud = !!audLabel && !/everyone can reply/i.test(audLabel);
            return {
                title: (document.title || '').slice(0, 120),
                textbox_count: tbs.length,
                textbox_labels: tbs.map(t => t.getAttribute('aria-label')),
                has_tweetTextarea_0: !!document.querySelector('[data-testid="tweetTextarea_0"]'),
                has_login_modal: !!document.querySelector('[data-testid="loginButton"]'),
                has_age_gate: !!document.querySelector('[data-testid="sensitive-media-button"]'),
                tweet_rendered: tweetRendered,
                reply_restricted: !!(m || restrictedByAud),
                restriction_label: (m ? m[0] : (restrictedByAud ? audLabel : '')).slice(0, 80),
                page_text_snippet: body.slice(0, 300),
            };
        }""")
    except Exception as _e:
        diag["dom_err"] = str(_e)
    return diag


def _like_first_tweet_on_page(page):
    """Like the primary (first) tweet currently rendered on the page.

    Operates on an already-open page positioned on a tweet permalink (the
    parent tweet is the first ``article[data-testid="tweet"]``). Used both by
    the standalone ``like`` command and inline by ``reply_to_tweet()`` right
    after a reply lands (the page is still on the thread).

    Strictly scoped to the FIRST article so we like the parent tweet, never a
    reply below it. Idempotent: if the tweet is already liked (button testid
    has flipped ``like`` -> ``unlike``) we report already_liked without
    clicking. Returns one of:
      {"ok": True,  "liked": True,  "already_liked": False}
      {"ok": True,  "liked": False, "already_liked": True}
      {"ok": False, "error": "..."}
    """
    try:
        first_article = page.locator('article[data-testid="tweet"]').first
        first_article.wait_for(state="visible", timeout=15000)

        # Already liked? The action-bar button testid flips like -> unlike.
        if first_article.locator('[data-testid="unlike"]').count() > 0:
            print("[like] parent tweet already liked; nothing to do", file=sys.stderr)
            return {"ok": True, "liked": False, "already_liked": True}

        like_btn = first_article.locator('[data-testid="like"]')
        if like_btn.count() == 0:
            print("[like] no like button found on parent tweet", file=sys.stderr)
            return {"ok": False, "error": "like_button_not_found"}

        like_btn.first.click()
        page.wait_for_timeout(1500)

        # Verify the click registered: testid should now be 'unlike'.
        if first_article.locator('[data-testid="unlike"]').count() > 0:
            print("[like] parent tweet liked OK", file=sys.stderr)
            return {"ok": True, "liked": True, "already_liked": False}
        print("[like] clicked like but unlike state not confirmed", file=sys.stderr)
        return {"ok": False, "liked": False, "error": "like_unconfirmed"}
    except Exception as e:
        print(f"[like] parent tweet not liked (non-fatal): {str(e).splitlines()[0]}", file=sys.stderr)
        return {"ok": False, "error": str(e).splitlines()[0]}


def like_tweet(tweet_url):
    """Standalone: navigate to a tweet and like it (CLI: ``like <tweet_url>``).

    Connects to the running twitter-harness Chrome via CDP (the same logged-in
    session the reply path uses) so the like comes from our account. Returns
    the dict from ``_like_first_tweet_on_page`` with ``tweet_url`` attached.
    """
    print(f"[twitter_browser] like_tweet called: {tweet_url}", file=sys.stderr)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)
        try:
            try:
                page.goto(tweet_url, wait_until="load", timeout=60000)
            except Exception:
                try:
                    page.goto(tweet_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
            page.wait_for_timeout(4000)
            try:
                page.wait_for_selector(
                    'article[data-testid="tweet"]', state="attached", timeout=20000
                )
            except Exception:
                return {"ok": False, "error": "tweet_not_rendered", "tweet_url": tweet_url}
            result = _like_first_tweet_on_page(page)
            result["tweet_url"] = tweet_url
            return result
        finally:
            if not is_cdp:
                page.close()
                browser.close()


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

    # Identity gate: refuse to post when no account is configured. Without a
    # resolved handle we cannot attribute the post or build a correct reply
    # permalink, and the old behaviour silently impersonated the repo owner
    # (handle "m13v_"). Fail fast and loud so the misconfiguration surfaces
    # instead of polluting the shared DB under someone else's identity.
    _handle = our_handle()
    if not _handle:
        print("[twitter_browser] no twitter account configured "
              "(set AUTOPOSTER_TWITTER_HANDLE or accounts.twitter.handle in "
              "config.json); refusing to post.", file=sys.stderr)
        return {"ok": False, "error": "no_account_configured"}

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
                            # Use the wrapped text whenever the wrap call succeeded.
                            # codes=[] is now valid (UTM-only fallback path for
                            # projects with short_links_live=false), and the
                            # rewritten text still carries full s4l attribution.
                            # Old guard `and wrap_res.get('codes')` silently
                            # skipped utm_only fallbacks and let bare URLs
                            # through in the suffix.
                            if wrap_res.get('ok'):
                                wrapped_suffix = wrap_res['text']
                                tag = 'codes' if wrap_res.get('codes') else 'utm_only'
                                print(f"[reply_to_tweet] suffix wrap project={detected_project} "
                                      f"{tag}={wrap_res.get('codes') or [s.get('reason') for s in wrap_res.get('skipped',[])]}",
                                      file=sys.stderr)
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

            # Navigate + locate reply box. Composer mount is flaky on E2B
            # sandbox egress (~1-in-5 misses on first attempt). Strategy:
            # up to 2 navigation attempts; on miss, scroll-nudge once before
            # re-navigating. On final miss, dump diagnostics for triage.
            reply_box = None
            tweet_not_found = False
            for nav_attempt in (1, 2):
                try:
                    page.goto(tweet_url, wait_until="load", timeout=60000)
                except Exception:
                    try:
                        page.goto(tweet_url, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                # Was a blind 15s/8s settle here -> pure dead latency. SPA
                # readiness is ALREADY gated actively below by
                # wait_for_selector("main") (up to 20s) and
                # _wait_for_reply_textbox (polls every 500ms up to 45s); both
                # return the instant the composer mounts, so the blind sleep
                # only delayed the start of that polling. Keep a short floor so
                # the initial JS kicks off (and the deleted-tweet text check
                # below has content to read), then let the active gates do the
                # real waiting. Cuts ~12s off every happy-path reply.
                # (optimized 2026-06-22: 15000/8000 -> 2500)
                page.wait_for_timeout(2500)

                # `wait_until="load"` fires before Twitter's SPA mounts the
                # <main> app shell, so "loaded" != "rendered". Explicitly gate
                # on <main> attaching. If it never mounts (rate-limit
                # interstitial, error page, logged-out shell, or a stalled SPA)
                # DO NOT let text_content("main") raise a bare TimeoutError that
                # crashes the whole script with no_reply_json and no diagnostics.
                # Swallow it, log the actual URL (rate-limit vs logout triage),
                # and fall through to the nudge + re-nav; on the final miss the
                # reply_box-None path reaches _dump_reply_failure_diag below.
                try:
                    page.wait_for_selector("main", state="attached", timeout=20000)
                    page_text = page.text_content("main", timeout=5000) or ""
                except Exception:
                    page_text = ""
                    try:
                        cur_url = page.url
                    except Exception:
                        cur_url = "<unknown>"
                    print(f"[reply_to_tweet] <main> not rendered on "
                          f"nav_attempt={nav_attempt} (url={cur_url!r}); "
                          f"nudging + re-navigating", file=sys.stderr)
                if "this page doesn't exist" in page_text.lower():
                    tweet_not_found = True
                    break

                reply_box = _wait_for_reply_textbox(page, total_timeout_ms=45000)
                if reply_box:
                    break

                # Nudge: small scroll + scroll back; sometimes coaxes the
                # composer to attach when React stalled on the initial mount.
                print(f"[reply_to_tweet] reply_box missing on nav_attempt={nav_attempt}; "
                      f"nudging + re-navigating", file=sys.stderr)
                try:
                    page.evaluate("window.scrollBy(0, 400)")
                    page.wait_for_timeout(1500)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(1500)
                except Exception:
                    pass

            if tweet_not_found:
                return {"ok": False, "error": "tweet_not_found"}

            if not reply_box:
                diag = _dump_reply_failure_diag(page, tweet_url)
                print(f"[reply_to_tweet] reply_box_not_found diag: "
                      f"{json.dumps(diag, default=str)}", file=sys.stderr)
                dom = diag.get("dom") or {}
                # Classify WHY the composer is missing so the poster can suppress
                # PERMANENT conditions (never re-attempt) vs retry TRANSIENT ones:
                #  - reply_restricted: author limits who can reply -> permanent,
                #    suppress thread + author.
                #  - tweet_unavailable: tweet deleted/suspended (nothing rendered)
                #    -> permanent, suppress thread. A login modal is OUR session
                #    problem, not the tweet's, so it stays transient.
                #  - else: composer just didn't mount -> transient, retry as before.
                if dom.get("reply_restricted"):
                    return {"ok": False, "error": "reply_restricted",
                            "restriction_label": dom.get("restriction_label") or "",
                            "diag": diag}
                if not dom.get("tweet_rendered") and not dom.get("has_login_modal"):
                    return {"ok": False, "error": "tweet_unavailable", "diag": diag}
                return {"ok": False, "error": "reply_box_not_found", "diag": diag}

            # Snapshot our reply links right before posting (to detect the new one)
            links_before = _collect_our_reply_links(page)

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

            # Post-submit settle: lets the CDP network response (which carries
            # the new tweet id -> reply_url, captured below) and the success
            # interstitial arrive. Trimmed from 4000ms 2026-06-22; the DOM-diff
            # fallback (3x2s, below) still covers a slow CDP response, so the
            # reply_url is not lost if 2000ms is short on a given run.
            page.wait_for_timeout(2000)

            # Verify: check if the reply box is empty (cleared after posting)
            try:
                box_text = reply_box.text_content() or ""
                verified = len(box_text.strip()) == 0 or text not in box_text
            except Exception:
                verified = True

            # Dismiss the post-success interstitial X shows right after a reply
            # (e.g. the "Unlock more on X" graduated-access sheet). It animates
            # in on top of the composer once the reply lands, so we close it
            # here, immediately after the post succeeds, rather than before the
            # next reply -> the sheet never lingers on screen and never masks
            # the next reply box. Best-effort, fast, never raises.
            _dismiss_known_overlays(page)

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
                reply_url = f"https://x.com/{_handle}/status/{_created_tweet_ids[-1]}"
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

            # Snapshot the single best-performing human reply on this thread
            # AT post-success time. The page is already on the candidate
            # thread URL with replies visible (we just posted there). We
            # filter out our own reply and the thread author, sort by likes,
            # and keep only the top one. Failures are swallowed: an empty
            # top_replies list is the correct downstream signal ("nothing
            # to track").
            #
            # Three-layer defense against X's "Discover more" /
            # "More replies" suggested-content cards, which render as
            # full article elements right alongside real replies and used to
            # leak in as the "top" reply (e.g. @mntruell 1343 likes on a
            # @zhenthebuilder thread, @OpenAIDevs 4050 likes on a @kr0der
            # thread — both viral standalone tweets X surfaced as
            # "discover more", neither was an actual reply). Layers:
            #   (1) DOM-position boundary: stop iterating at the first
            #       "Discover more" / "More replies" heading.
            #   (2) Snowflake age: real replies must be POSTED AFTER the
            #       thread, so reply_tweet_id > thread_tweet_id.
            #   (3) Quoted-tweet embeds: skip articles nested inside
            #       another article (rare but possible source of leaks).
            top_replies = []
            try:
                self_handle = (our_handle() or "").lower().lstrip("@")
                m_author = re.search(r"(?:x|twitter)\.com/([^/]+)/status/(\d+)", tweet_url)
                thread_author_handle = (m_author.group(1).lower() if m_author else "")
                thread_tweet_id = (m_author.group(2) if m_author else "")
                scrape_js = """
                (() => {
                  const headings = Array.from(document.querySelectorAll('div, h2, [role="heading"]'))
                    .filter(el => {
                      const t = (el.textContent || '').trim();
                      return t === 'Discover more' || t === 'More replies' || t === 'Show more replies';
                    });
                  const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
                  if (articles.length < 1) return JSON.stringify({replies: [], article_count: articles.length, dropped_after_discover: 0, dropped_nested: 0});
                  let dropped_after_discover = 0, dropped_nested = 0;
                  const replyArticles = articles.slice(1, 31);
                  const replies = [];
                  for (const art of replyArticles) {
                    try {
                      // Layer 1: hard boundary at "Discover more" heading.
                      // headings[0] is the FIRST such heading on the page;
                      // any article after it is a suggested-content card.
                      if (headings.length > 0) {
                        const cmp = art.compareDocumentPosition(headings[0]);
                        if (!(cmp & Node.DOCUMENT_POSITION_FOLLOWING)) {
                          dropped_after_discover += 1;
                          continue;
                        }
                      }
                      // Layer 3: skip quoted-tweet embeds (nested article).
                      let p = art.parentElement, nested = false;
                      while (p) { if (p.tagName === 'ARTICLE') { nested = true; break; } p = p.parentElement; }
                      if (nested) { dropped_nested += 1; continue; }

                      const linkEls = art.querySelectorAll('a[href*="/status/"]');
                      let reply_url = null;
                      for (const a of linkEls) {
                        const m = a.getAttribute('href').match(/^\\/[^/]+\\/status\\/\\d+$/);
                        if (m) { reply_url = 'https://x.com' + a.getAttribute('href'); break; }
                      }
                      if (!reply_url) continue;
                      const tid_m = reply_url.match(/\\/status\\/(\\d+)/);
                      const reply_tweet_id = tid_m ? tid_m[1] : null;
                      const handle_m = reply_url.match(/x\\.com\\/([^/]+)\\/status/);
                      const reply_author_handle = handle_m ? handle_m[1] : null;
                      const userName = art.querySelector('[data-testid="User-Name"]');
                      const reply_author = userName ? (userName.textContent || '').trim().slice(0, 80) : null;
                      const textEl = art.querySelector('[data-testid="tweetText"]');
                      const reply_content = textEl ? (textEl.textContent || '').trim().slice(0, 500) : null;
                      const groupEl = art.querySelector('[role="group"][aria-label]');
                      let likes = 0, replies_count = 0, retweets = 0, views = 0;
                      if (groupEl) {
                        const label = groupEl.getAttribute('aria-label') || '';
                        const lm = label.match(/(\\d[\\d,]*)\\s+(?:Like|Likes)/i);
                        const rm = label.match(/(\\d[\\d,]*)\\s+(?:Reply|Replies)/i);
                        const tm = label.match(/(\\d[\\d,]*)\\s+(?:Repost|Reposts)/i);
                        const vm = label.match(/(\\d[\\d,]*)\\s+(?:View|Views)/i);
                        likes = lm ? parseInt(lm[1].replace(/,/g, ''), 10) : 0;
                        replies_count = rm ? parseInt(rm[1].replace(/,/g, ''), 10) : 0;
                        retweets = tm ? parseInt(tm[1].replace(/,/g, ''), 10) : 0;
                        views = vm ? parseInt(vm[1].replace(/,/g, ''), 10) : 0;
                      }
                      // Link detection. Twitter exclusively shortens external
                      // links through t.co, so any <a href="https://t.co/..."]>
                      // inside the article (excluding any nested article like
                      // a quoted tweet) means the reply author posted an
                      // outbound link. Pick the first matching anchor whose
                      // nearest ancestor article IS this article (rules out
                      // links embedded inside a quoted-tweet block).
                      let reply_link_url = null;
                      let reply_link_display = null;
                      const tcoAnchors = art.querySelectorAll('a[href^="https://t.co/"]');
                      for (const a of tcoAnchors) {
                        let q = a.parentElement, owner = null;
                        while (q) { if (q.tagName === 'ARTICLE') { owner = q; break; } q = q.parentElement; }
                        if (owner === art) {
                          reply_link_url = a.getAttribute('href');
                          // The anchor's textContent is the unrolled display
                          // URL twitter shows the reader (e.g. "deno.com/blog
                          // /agents-deploy"). Strip whitespace + Unicode
                          // ellipsis that x.com inserts on long display URLs.
                          reply_link_display = ((a.textContent || '').trim()).slice(0, 500) || null;
                          break;
                        }
                      }
                      replies.push({reply_url, reply_tweet_id, reply_author_handle, reply_author, reply_content, likes, replies: replies_count, retweets, views, reply_link_url, reply_link_display});
                    } catch (e) {}
                  }
                  return JSON.stringify({replies, article_count: articles.length, dropped_after_discover, dropped_nested, headings_found: headings.length});
                })()
                """
                raw = page.evaluate(scrape_js)
                parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
                all_replies = parsed.get("replies", []) or []
                dropped_older = 0
                filtered = []
                for r in all_replies:
                    h = (r.get("reply_author_handle") or "").lower().lstrip("@")
                    if not h:
                        continue
                    if self_handle and h == self_handle:
                        continue
                    if thread_author_handle and h == thread_author_handle:
                        continue
                    # Layer 2: snowflake age. A real reply MUST have been
                    # posted after the thread; older snowflakes are
                    # quoted-tweet embeds or suggested-content leaks that
                    # somehow made it past the DOM boundary.
                    rtid = (r.get("reply_tweet_id") or "").strip()
                    if thread_tweet_id and rtid:
                        try:
                            if int(rtid) <= int(thread_tweet_id):
                                dropped_older += 1
                                continue
                        except ValueError:
                            pass
                    filtered.append(r)
                filtered.sort(key=lambda r: int(r.get("likes") or 0), reverse=True)
                # Two-row snapshot strategy (2026-05-22):
                #   rank=1 = top reply by likes regardless of link presence
                #            (the existing "what's winning here?" benchmark).
                #   rank=2 = top *link-bearing* reply, if one exists and is
                #            distinct from rank=1. This gives us an
                #            apples-to-apples comparison against our own
                #            link-bearing posts. ~96% of top replies don't
                #            include a link, so without this second row the
                #            benchmark population was too small.
                # If rank=1 already has a link, the rank=2 candidate is the
                # same row and we skip it to honor UNIQUE(post_id, reply_url).
                top_replies = []
                if filtered:
                    primary = filtered[0]
                    top_replies.append(primary)
                    primary_url = primary.get("reply_url")
                    if not primary.get("reply_link_url"):
                        for cand in filtered[1:]:
                            if cand.get("reply_link_url") and cand.get("reply_url") != primary_url:
                                top_replies.append(cand)
                                break
                print(f"[top_replies] scraped {len(all_replies)} articles "
                      f"(headings={parsed.get('headings_found', 0)}, "
                      f"dropped_after_discover={parsed.get('dropped_after_discover', 0)}, "
                      f"dropped_nested={parsed.get('dropped_nested', 0)}, "
                      f"dropped_older={dropped_older}), "
                      f"kept top {len(top_replies)} after self+author filter "
                      f"(rank2_has_link={'yes' if len(top_replies) > 1 else 'no'})",
                      file=sys.stderr)
            except Exception as e:
                print(f"[top_replies] scrape failed: {e}", file=sys.stderr)
                top_replies = []

            # Like the parent tweet we just replied to. Deterministic: fires on
            # EVERY successful reply. The page is still on the thread, so the
            # parent is the first article and no extra navigation is needed.
            # Wrapped so a like failure can NEVER fail the reply itself — we
            # carry the outcome out in `like_result` for the caller to log.
            like_result = {"ok": False, "error": "not_attempted"}
            try:
                like_result = _like_first_tweet_on_page(page)
            except Exception as _le:
                like_result = {"ok": False, "error": str(_le)}
                print(f"[like] unexpected error in reply_to_tweet: {_le}", file=sys.stderr)

            return {
                "ok": True,
                "tweet_url": tweet_url,
                "reply_url": reply_url,
                "verified": verified,
                "applied_campaigns": applied_campaigns,
                "final_text": text,
                "top_replies": top_replies,
                "liked": bool(like_result.get("liked") or like_result.get("already_liked")),
                "like_result": like_result,
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
            #
            # 2026-05-14: X redesigned the sidebar; all unread visual signals
            # moved and the list is now virtualized (~14-18 rows render at
            # once). This was the root cause of the 2026-05-01..05-14 inbound
            # DM ingestion cliff:
            #   - bolded preview text: was fw>=600, now fw=500
            #   - unread dot: was a small <div> with background-color, now
            #     <svg data-icon="icon-circle-fill"> 8x8 with transparent bg
            #     and color: rgb(30, 156, 241) (Twitter blue) via fill
            #   - aria-label "unread": gone entirely
            # Every row also exposes data-testid `dm-conversation-item-<ids>`.
            # We now (a) detect unread via the SVG dot AND any non-400 weight
            # on the preview span, and (b) scroll the chat panel until no new
            # rows surface for several iterations so older unreads (Prince
            # Canuma at 1w, Foad Green at 2w) are not buried beneath the fold.
            scrape_js = """() => {
                const results = [];
                const items = document.querySelectorAll(
                    '[data-testid^="dm-conversation-item-"], main li, main [role="listitem"]'
                );

                for (const item of items) {
                    const link = item.querySelector('a[href*="/i/chat/"]');
                    if (!link) continue;

                    const threadUrl = link.href;
                    if (!threadUrl.match(/\\/i\\/chat\\/[\\d-g]/)) continue;

                    let handle = '';
                    const avatarLink = item.querySelector('a[href^="https://x.com/"]');
                    if (avatarLink) {
                        const href = avatarLink.getAttribute('href') || '';
                        const m = href.match(/x\\.com\\/([^/]+)/);
                        if (m) handle = m[1];
                    }

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
                        if (!author && node.fw >= 700 && node.t.length < 80 &&
                            !/^(\\d+[hmd]|\\d+w|Just now)$/.test(node.t)) {
                            author = node.t;
                            continue;
                        }
                        if (!time && /^(\\d+[hmd]|\\d+w|Just now)$/.test(node.t)) {
                            time = node.t;
                            continue;
                        }
                        if (!isFromUs && node.tag === 'span' && /^You:?$/.test(node.t)) {
                            isFromUs = true;
                            continue;
                        }
                        if (!preview && node.t.length > 0) {
                            preview = node.t;
                            previewFw = node.fw;
                        }
                    }

                    // Primary: <svg data-icon="icon-circle-fill"> = blue unread dot.
                    let hasUnread = !!item.querySelector('svg[data-icon="icon-circle-fill"]');

                    // Secondary: any non-400 weight on the preview leaf (X
                    // currently uses 500 for unread; we accept >400 in case
                    // they tweak it again).
                    if (!hasUnread && previewFw > 400) hasUnread = true;

                    // Tertiary legacy signals (kept for safety).
                    if (!hasUnread && item.querySelector('[aria-label*="unread" i]')) {
                        hasUnread = true;
                    }
                    if (!hasUnread) {
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

                    // If we sent the last visible message ("You:" prefix), it
                    // can't be unread on our end regardless of bolding.
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
            }"""

            scroll_js = """() => {
                const items = document.querySelectorAll(
                    '[data-testid^="dm-conversation-item-"], main li, main [role="listitem"]'
                );
                let last = null;
                for (const item of items) {
                    if (item.querySelector('a[href*="/i/chat/"]')) last = item;
                }
                if (!last) return -1;
                last.scrollIntoView({behavior: 'instant', block: 'end'});
                let el = last;
                while (el) {
                    const s = window.getComputedStyle(el);
                    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') &&
                        el.scrollHeight > el.clientHeight) {
                        return el.scrollTop;
                    }
                    el = el.parentElement;
                }
                return 0;
            }"""

            seen = {}
            stuck_iters = 0
            max_iters = int(os.environ.get("TWITTER_UNREAD_SCROLL_MAX_ITERS", "60"))
            max_no_growth = int(os.environ.get("TWITTER_UNREAD_SCROLL_NO_GROWTH", "5"))
            for _ in range(max_iters):
                batch = page.evaluate(scrape_js)
                grew = False
                for c in batch:
                    if c["thread_url"] not in seen:
                        seen[c["thread_url"]] = c
                        grew = True
                if not grew:
                    stuck_iters += 1
                else:
                    stuck_iters = 0
                if stuck_iters >= max_no_growth:
                    break
                page.evaluate(scroll_js)
                page.wait_for_timeout(600)

            unique = list(seen.values())

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
            }""", {"maxMessages": max_messages, "ourHandle": our_handle()})

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
            # 2026-05-14: navigate directly to the thread URL via JS, mirroring
            # read_conversation. The previous implementation went to /i/chat/
            # first and clicked `a[href*="<conv_id>"]` from the sidebar, but X
            # virtualizes the sidebar so only ~14-18 rows render at once. Any
            # thread below the initial slice (3+ days old, ~20+ position) hit
            # `conversation_not_found_in_sidebar` as a terminal error,
            # producing 0 successful sends on the 19:14 cycle's 11 retries.
            # Direct nav was historically called out as flaky for DM routes;
            # in practice it works fine when given a 6s settle window, which
            # is what read_conversation does.
            conv_id = thread_url.rstrip("/").split("/")[-1]
            page.evaluate(f"window.location.href = '{thread_url}'")
            page.wait_for_timeout(6000)

            unreachable, reason = _is_x_unreachable(page)
            if unreachable:
                return _rate_limit_response(reason, rl_counter, page.url)

            # Handle DM passcode if needed
            _handle_dm_passcode(page)
            page.wait_for_timeout(2000)

            # Verify the SPA landed on the right conversation. If the URL
            # doesn't contain the conv_id, something redirected us (login
            # bounce, suspended account, deleted thread, etc.).
            if conv_id not in page.url:
                return {
                    "ok": False,
                    "error": "thread_url_redirected",
                    "expected_conv_id": conv_id,
                    "landed_url": page.url,
                }

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


# Single source of truth for the per-article extractor used by every thread
# reader below (scrape_thread_followups, scrape_many_thread_followups,
# scrape_thread_media, scrape_many_thread_media). Was previously duplicated
# inline in two places, which drifted. It extracts the same text fields as
# before PLUS a `media` array [{url, alt, type}] per tweet so the reply-writer
# can "see" images / video / GIF / link-card content instead of replying
# text-blind (2026-06-03 thread-media feature). `type` is image|video|gif|card;
# `alt` is the DOM alt-text / aria-label / card title (empty string when the
# DOM gives none, a flag a later vision pass can escalate on).
THREAD_EXTRACTOR_JS = r"""() => {
  function extractMedia(article) {
    const media = [];
    const seen = new Set();
    const push = (url, alt, type) => {
      if (!url || seen.has(url)) return;
      seen.add(url);
      media.push({ url: url, alt: (alt || '').trim(), type: type });
    };
    // Photos and animated GIFs live in tweetPhoto containers. A <video> inside
    // one is an animated GIF; a bare <img> is a still photo.
    for (const ph of article.querySelectorAll('[data-testid="tweetPhoto"]')) {
      const img = ph.querySelector('img');
      const vid = ph.querySelector('video');
      if (vid) {
        const poster = vid.getAttribute('poster') || (img ? img.getAttribute('src') : '') || '';
        const alt = img ? (img.getAttribute('alt') || '') : '';
        // Twitter thumb URLs disambiguate the kind: tweet_video_thumb is an
        // animated GIF; amplify_video_thumb / ext_tw_video_thumb is a real
        // (uploaded) video. Default to video when the pattern is unknown.
        const isGif = /tweet_video_thumb/.test(poster);
        push(poster, alt, isGif ? 'gif' : 'video');
      } else if (img) {
        push(img.getAttribute('src') || '', img.getAttribute('alt') || '', 'image');
      }
    }
    // Inline videos. Use the poster frame as the URL and the aria-label
    // (often a human description) as alt-text.
    for (const vp of article.querySelectorAll('[data-testid="videoPlayer"], [data-testid="videoComponent"]')) {
      const vid = vp.querySelector('video');
      const poster = vid ? (vid.getAttribute('poster') || '') : '';
      push(poster, vp.getAttribute('aria-label') || '', 'video');
    }
    // Link-preview card. URL = card href; alt = card image alt or the first
    // few text spans (title / domain / description).
    const card = article.querySelector('[data-testid="card.wrapper"]');
    if (card) {
      let curl = '';
      const a = card.querySelector('a[href]');
      if (a) curl = a.getAttribute('href') || '';
      let alt = '';
      const cimg = card.querySelector('img');
      if (cimg && cimg.getAttribute('alt')) alt = cimg.getAttribute('alt');
      if (!alt) {
        const txts = [];
        for (const span of card.querySelectorAll('span')) {
          const t = (span.textContent || '').trim();
          if (t) txts.push(t);
        }
        alt = txts.slice(0, 3).join(' | ');
      }
      push(curl, alt, 'card');
    }
    return media;
  }
  // Repost detection mirrors extractMedia: read the "<X> reposted" banner from
  // the same already-loaded DOM. socialContext is ALSO used for "Pinned", so
  // match the text /reposted/i, not mere presence. reposted_by = the account
  // whose profile link wraps the banner.
  function extractRepost(article) {
    const sc = article.querySelector('[data-testid="socialContext"]');
    if (!sc || !/\breposted\b/i.test(sc.textContent || '')) {
      return { is_repost: false, reposted_by: '' };
    }
    let reposted_by = '';
    const a = sc.closest('a');
    const rh = a ? (a.getAttribute('href') || '') : '';
    if (rh.startsWith('/') && rh.split('/').length === 2) reposted_by = rh.replace('/', '');
    return { is_repost: true, reposted_by: reposted_by };
  }
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
      // The status URL's first path segment is the AUTHORITATIVE author. The
      // bare-link scan above grabs the first /handle link, which on a repost is
      // the REPOSTER, not the author. Override from the URL so author + tweet_id
      // always agree (matches twitter_scan.py).
      const authorM = tweetHref ? tweetHref.match(/^\/([^\/]+)\/status\//) : null;
      if (authorM && authorM[1]) handle = authorM[1];
      const repost = extractRepost(article);
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
          replying_to: replyingTo,
          media: extractMedia(article),
          is_repost: repost.is_repost,
          reposted_by: repost.reposted_by
        });
      }
    } catch(e) {}
  }
  return out;
}"""


def scrape_thread_followups(thread_url, scroll_count=3):
    """Navigate to a tweet's permalink and extract reply articles below it.

    Used to detect depth-2+ replies to our own replies that the notifications
    tab may not surface (X default behavior drops @-tags inside active threads).

    Returns: {"thread_url": "...", "anchor_tweet_id": "...", "followups": [...]}
             where each followup has the same shape as a notifications record,
             plus a `media` array [{url, alt, type}] per article.
    """
    print(f"[twitter_browser] scrape_thread_followups({thread_url!r}, scroll={scroll_count})", file=sys.stderr)
    from playwright.sync_api import sync_playwright

    anchor_match = re.search(r"/status/(\d+)", thread_url or "")
    anchor_tweet_id = anchor_match.group(1) if anchor_match else ""

    EXTRACTOR_JS = THREAD_EXTRACTOR_JS

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
            # First article on a permalink page is the conversation root (OP).
            # Already scraped above — capture for free for thread_author_handle.
            root_author = (all_tweets[0].get('handle') or '').lstrip('@') if all_tweets else ''
            root_media = (all_tweets[0].get('media') or []) if all_tweets else []
            return {
                "thread_url": thread_url,
                "anchor_tweet_id": anchor_tweet_id,
                "root_author": root_author,
                "root_media": root_media,
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

                    EXTRACTOR_JS = THREAD_EXTRACTOR_JS

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
                    # First article on a permalink page is the conversation root (OP).
                    # Already scraped above — capture for free for thread_author_handle.
                    root_author = (collected[0].get('handle') or '').lstrip('@') if collected else ''
                    root_media = (collected[0].get('media') or []) if collected else []
                    print(f"[thread_followups] {url}: {len(followups)} candidate follow-ups", file=sys.stderr)
                    results.append({
                        "thread_url": url,
                        "anchor_tweet_id": anchor_tweet_id,
                        "root_author": root_author,
                        "root_media": root_media,
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


def _anchor_media_from_tweets(tweets, anchor_tweet_id):
    """Pick the media of the anchor tweet from a list of scraped articles.

    The anchor is the tweet we plan to reply to (the candidate URL's /status/ID).
    Match by tweet_id; if the anchor article is not found (X sometimes renders
    the focused tweet without a resolvable status href in the first paint), fall
    back to the first article on the page, which on a permalink is the focused
    tweet. Returns a list [{url, alt, type}] (possibly empty).
    """
    if not tweets:
        return []
    if anchor_tweet_id:
        for t in tweets:
            if t.get("tweet_id") == anchor_tweet_id:
                return t.get("media") or []
    return tweets[0].get("media") or []


def _anchor_repost_from_tweets(tweets, anchor_tweet_id):
    """Pick the repost provenance of the anchor tweet from scraped articles.

    Mirrors _anchor_media_from_tweets: match the anchor by tweet_id, else fall
    back to the first article (the focused tweet on a permalink). Returns
    {"is_repost": bool, "reposted_by": str}; defaults to a non-repost.
    """
    if not tweets:
        return {"is_repost": False, "reposted_by": ""}
    chosen = None
    if anchor_tweet_id:
        for t in tweets:
            if t.get("tweet_id") == anchor_tweet_id:
                chosen = t
                break
    if chosen is None:
        chosen = tweets[0]
    return {
        "is_repost": bool(chosen.get("is_repost", False)),
        "reposted_by": chosen.get("reposted_by", "") or "",
    }


def scrape_thread_media(thread_url, scroll_count=1):
    """Navigate to a tweet's permalink and return the media of the anchor tweet.

    Deterministic, model-free media capture for the MAIN posting cycle: the
    reply-writer needs to "see" the image / video / GIF / link-card on the tweet
    it is about to reply to. Returns:
        {"thread_url": ..., "anchor_tweet_id": ..., "media": [{url,alt,type}, ...]}
    media is [] when the tweet has none. Cheap: one navigation, minimal scroll
    (the anchor is at the top of a permalink page).
    """
    print(f"[twitter_browser] scrape_thread_media({thread_url!r})", file=sys.stderr)
    from playwright.sync_api import sync_playwright

    anchor_match = re.search(r"/status/(\d+)", thread_url or "")
    anchor_tweet_id = anchor_match.group(1) if anchor_match else ""

    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)
        try:
            page.goto(thread_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            tweets = []
            try:
                tweets = page.evaluate(THREAD_EXTRACTOR_JS)
            except Exception as e:
                print(f"[thread_media] extractor error: {e}", file=sys.stderr)
            # One short scroll can help lazy-loaded media of the focused tweet
            # render; re-extract and prefer the richer result.
            for _ in range(max(0, scroll_count - 1)):
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                page.wait_for_timeout(900)
                try:
                    more = page.evaluate(THREAD_EXTRACTOR_JS)
                    if more and len(more) >= len(tweets):
                        tweets = more
                except Exception:
                    pass
                _refresh_browser_lock()
            media = _anchor_media_from_tweets(tweets, anchor_tweet_id)
            repost = _anchor_repost_from_tweets(tweets, anchor_tweet_id)
            print(f"[thread_media] {thread_url}: {len(media)} media item(s)"
                  f"{' [repost]' if repost['is_repost'] else ''}", file=sys.stderr)
            return {
                "thread_url": thread_url,
                "anchor_tweet_id": anchor_tweet_id,
                "media": media,
                "is_repost": repost["is_repost"],
                "reposted_by": repost["reposted_by"],
            }
        finally:
            if not is_cdp:
                page.close()
                browser.close()


def scrape_many_thread_media(thread_urls, scroll_count=1, per_url_delay_ms=1500):
    """Batch scrape_thread_media over a list of candidate URLs in ONE session.

    Used by the main cycle (run-twitter-cycle.sh Phase 2b-prep) to pre-fetch the
    media of every candidate the model is about to draft against, in a single
    cheap browser pass, then persist each via scripts/log_thread_media.py.

    Returns: {"results": [{thread_url, anchor_tweet_id, media: [...]}], "urls_visited": N}
    """
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser, page, is_cdp = get_browser_and_page(p)
        try:
            for url in thread_urls:
                anchor_match = re.search(r"/status/(\d+)", url or "")
                anchor_tweet_id = anchor_match.group(1) if anchor_match else ""
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    tweets = []
                    try:
                        tweets = page.evaluate(THREAD_EXTRACTOR_JS)
                    except Exception:
                        tweets = []
                    for _ in range(max(0, scroll_count - 1)):
                        page.evaluate("window.scrollBy(0, window.innerHeight)")
                        page.wait_for_timeout(800)
                        try:
                            more = page.evaluate(THREAD_EXTRACTOR_JS)
                            if more and len(more) >= len(tweets):
                                tweets = more
                        except Exception:
                            pass
                        _refresh_browser_lock()
                    media = _anchor_media_from_tweets(tweets, anchor_tweet_id)
                    repost = _anchor_repost_from_tweets(tweets, anchor_tweet_id)
                    print(f"[thread_media] {url}: {len(media)} media item(s)"
                          f"{' [repost]' if repost['is_repost'] else ''}", file=sys.stderr)
                    results.append({
                        "thread_url": url,
                        "anchor_tweet_id": anchor_tweet_id,
                        "media": media,
                        "is_repost": repost["is_repost"],
                        "reposted_by": repost["reposted_by"],
                    })
                except Exception as e:
                    print(f"[thread_media] error on {url}: {e}", file=sys.stderr)
                    results.append({"thread_url": url, "anchor_tweet_id": anchor_tweet_id, "error": str(e), "media": [], "is_repost": False, "reposted_by": ""})
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
        # SAPS_SKIP_CAMPAIGN_SUFFIX=1 opts this reply out of active-campaign
        # suffixes (e.g. " written with ai"). Set ONLY by the MCP draft_cycle
        # post path (mcp/src/index.ts::postApproved) so manual/reviewed posts
        # land clean; the cron pipeline never sets it, so the A/B experiment
        # keeps running there and on Reddit. Reuses the existing apply_campaigns
        # plumbing (same flag the self-reply path uses below).
        _skip_camp = os.environ.get("SAPS_SKIP_CAMPAIGN_SUFFIX", "").strip().lower() in ("1", "true", "yes")
        result = reply_to_tweet(sys.argv[2], sys.argv[3], apply_campaigns=not _skip_camp)
        print(json.dumps(result, indent=2))

    elif cmd == "like":
        if len(sys.argv) < 3:
            print(
                "Usage: twitter_browser.py like <tweet_url>",
                file=sys.stderr,
            )
            sys.exit(1)
        result = like_tweet(sys.argv[2])
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

    elif cmd == "thread-media":
        # Single-URL anchor media fetch (deterministic, model-free).
        # Usage: twitter_browser.py thread-media <tweet_url> [scroll_count]
        if len(sys.argv) < 3:
            print(
                "Usage: twitter_browser.py thread-media <tweet_url> [scroll_count]\n"
                "  Returns {thread_url, anchor_tweet_id, media:[{url,alt,type}]}",
                file=sys.stderr,
            )
            sys.exit(1)
        scroll_count = 1
        if len(sys.argv) >= 4:
            try:
                scroll_count = int(sys.argv[3])
            except ValueError:
                print(f"thread-media: scroll_count must be int, got {sys.argv[3]!r}", file=sys.stderr)
                sys.exit(1)
        result = scrape_thread_media(sys.argv[2], scroll_count=scroll_count)
        print(json.dumps(result, indent=2))

    elif cmd == "thread-media-batch":
        # Batch anchor media fetch over a file of candidate URLs in ONE session.
        # Usage: twitter_browser.py thread-media-batch <urls_file.txt> [scroll_count]
        if len(sys.argv) < 3:
            print(
                "Usage: twitter_browser.py thread-media-batch <urls_file.txt> [scroll_count]\n"
                "  urls_file.txt: one candidate tweet permalink per line\n"
                "  Returns {results:[{thread_url, anchor_tweet_id, media:[...]}], urls_visited}",
                file=sys.stderr,
            )
            sys.exit(1)
        urls_path = sys.argv[2]
        scroll_count = 1
        if len(sys.argv) >= 4:
            try:
                scroll_count = int(sys.argv[3])
            except ValueError:
                print(f"thread-media-batch: scroll_count must be int, got {sys.argv[3]!r}", file=sys.stderr)
                sys.exit(1)
        with open(urls_path) as f:
            urls = [line.strip() for line in f if line.strip()]
        if not urls:
            print(json.dumps({"results": [], "urls_visited": 0}, indent=2))
            sys.exit(0)
        result = scrape_many_thread_media(urls, scroll_count=scroll_count)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
