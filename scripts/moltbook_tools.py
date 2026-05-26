#!/usr/bin/env python3
"""Shared Moltbook API helpers with cooperative cross-process rate-limit state.

Mirrors the pattern in scripts/reddit_tools.py so multiple concurrent Moltbook
callers (scan_moltbook_replies.py, stats.py, find_threads.py,
moltbook_post.py) back off together when any one of them hits a 429.

State file: /tmp/moltbook_ratelimit.json
  {"remaining": int, "reset_at": epoch_seconds}

On 429, the Moltbook API returns a JSON body with `retry_after_seconds`.
We persist that reset into the shared file so the next caller (in any process)
can decide to wait inline or raise MoltbookRateLimitedError to exit early.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request


RATELIMIT_FILE = "/tmp/moltbook_ratelimit.json"

# Same threshold as Reddit: resets under 90s are absorbed inline, longer resets
# raise MoltbookRateLimitedError so the caller exits rather than blocking a slot.
MAX_INLINE_WAIT_SECONDS = 90

# Default 429 retry when the server does not return retry_after_seconds.
DEFAULT_RETRY_SECONDS = 160


class MoltbookRateLimitedError(Exception):
    """Raised when Moltbook returns 429 and reset is longer than MAX_INLINE_WAIT_SECONDS."""
    def __init__(self, reset_seconds):
        self.reset_seconds = reset_seconds
        super().__init__(f"moltbook_rate_limited_wait_{int(reset_seconds)}s")


class HttpNotFoundError(Exception):
    """Raised when a Moltbook GET returns HTTP 404."""
    pass


def _read_ratelimit():
    try:
        with open(RATELIMIT_FILE) as f:
            return json.load(f)
    except Exception:
        return {"remaining": 100, "reset_at": 0}


def _write_ratelimit(remaining, reset_seconds):
    reset_at = time.time() + reset_seconds
    try:
        with open(RATELIMIT_FILE, "w") as f:
            json.dump({"remaining": remaining, "reset_at": reset_at}, f)
    except Exception:
        pass


def _wait_if_needed():
    """Block or raise before making a Moltbook request if a prior 429 is still pending."""
    rl = _read_ratelimit()
    if rl.get("remaining", 100) <= 2 and rl.get("reset_at", 0) > time.time():
        wait = int(rl["reset_at"] - time.time()) + 2
        if wait <= 0:
            return
        if wait > MAX_INLINE_WAIT_SECONDS:
            raise MoltbookRateLimitedError(wait)
        print(f"Moltbook rate limit cooling down, waiting {wait}s...", file=sys.stderr)
        time.sleep(wait)


def _parse_retry_seconds(body_bytes):
    """Parse retry_after_seconds from a 429 response body."""
    try:
        payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
        retry = payload.get("retry_after_seconds")
        if isinstance(retry, (int, float)) and retry > 0:
            return float(retry)
    except Exception:
        pass
    return float(DEFAULT_RETRY_SECONDS)


def note_rate_limited(retry_seconds):
    """Public: record a rate-limit event so other processes back off.

    Used by moltbook_post.py (which uses the `requests` library) to feed
    the shared state without rewriting its HTTP layer.
    """
    _write_ratelimit(0, float(retry_seconds))


def fetch_moltbook_json(url, api_key=None, headers=None,
                       user_agent="social-autoposter/1.0", timeout=15):
    """GET a Moltbook JSON endpoint with cooperative rate-limit handling.

    - Waits or raises MoltbookRateLimitedError based on shared state before firing.
    - On 200: clears the shared "near zero" signal.
    - On 404: raises HttpNotFoundError (callers typically use this for deletion detection).
    - On 429: persists retry_after_seconds. If <= MAX_INLINE_WAIT_SECONDS, sleeps
      once and retries; otherwise raises MoltbookRateLimitedError.
    - On other HTTPError / network error: prints and returns None (preserves the
      existing callers' "return None on error" contract).
    """
    _wait_if_needed()

    hdrs = {"User-Agent": user_agent}
    if api_key:
        hdrs["Authorization"] = f"Bearer {api_key}"
    if headers:
        hdrs.update(headers)

    req = urllib.request.Request(url, headers=hdrs)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Success: clear any lingering "near zero" from a stale 429.
            _write_ratelimit(100, 0)
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HttpNotFoundError(url)
        if e.code == 429:
            body = e.read() if hasattr(e, "read") else b""
            retry = _parse_retry_seconds(body)
            _write_ratelimit(0, retry)
            if retry > MAX_INLINE_WAIT_SECONDS:
                raise MoltbookRateLimitedError(retry)
            print(f"Moltbook 429, waiting {int(retry)+2}s... ({url})", file=sys.stderr)
            time.sleep(int(retry) + 2)
            # Single retry, propagate any errors from the retry.
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    _write_ratelimit(100, 0)
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e2:
                if e2.code == 404:
                    raise HttpNotFoundError(url)
                if e2.code == 429:
                    body2 = e2.read() if hasattr(e2, "read") else b""
                    retry2 = _parse_retry_seconds(body2)
                    _write_ratelimit(0, retry2)
                    raise MoltbookRateLimitedError(retry2)
                print(f"  ERROR fetching {url}: {e2}", file=sys.stderr)
                return None
            except Exception as ex:
                print(f"  ERROR fetching {url}: {ex}", file=sys.stderr)
                return None
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None
    except HttpNotFoundError:
        raise
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None
