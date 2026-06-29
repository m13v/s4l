#!/usr/bin/env python3
"""Classify whether our logged-in X account can see a tweet.

This is deliberately separate from twitter_browser.py because that file is
locked in this repo. It reuses the same harness Chrome/CDP session and lock via
twitter_browser.get_browser_and_page(), but returns a small access diagnosis:

  visible           - the target tweet article rendered for our account
  visible_no_anchor - tweet articles rendered, but the exact status id was not
                      found in article links (usable, but less certain)
  blocked           - X rendered a block-specific message
  protected         - X rendered protected-account copy
  unavailable       - X rendered deleted/suspended/not-found/unavailable copy
  access_gated      - X redirected to /account/access ("verify it's you") or a
                      Cloudflare "security verification" interstitial gated the
                      page. The session cookie is valid but X is limiting it
                      (commonly datacenter-IP trust degradation). Acting on this
                      session yields phantom "doesn't exist" results, so callers
                      should STOP rather than treat the empty render as truth.
  app_error         - X rendered a generic retry/error state
  logged_out        - the harness session is no longer logged in
  app_not_hydrated  - X served the app shell but no DOM content rendered
  unknown           - no reliable signal

The optional fxtwitter public control proves public existence only. It cannot
prove whether our logged-in account is blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twitter_browser as tb  # noqa: E402


def parse_tweet_url(tweet_url: str) -> tuple[str, str]:
    m = re.search(r"(?:twitter|x)\.com/([^/?#]+)/status/(\d+)", tweet_url or "")
    if not m:
        return "", ""
    return m.group(1).lstrip("@"), m.group(2)


def public_control(tweet_url: str) -> dict:
    handle, tweet_id = parse_tweet_url(tweet_url)
    if not handle or not tweet_id:
        return {"checked": False, "error": "bad_tweet_url"}
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"https://api.fxtwitter.com/{handle}/status/{tweet_id}",
            headers={"User-Agent": "social-autoposter/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read() or b"{}")
            except Exception:
                return {"checked": True, "exists": False, "code": e.code}

        tweet = data.get("tweet")
        if isinstance(tweet, dict) and tweet.get("type") == "tombstone":
            return {
                "checked": True,
                "exists": True,
                "code": data.get("code"),
                "tweet_type": "tombstone",
                "reason": tweet.get("reason") or "tombstone",
            }
        return {
            "checked": True,
            "exists": bool(tweet),
            "code": data.get("code"),
            "author": ((tweet or {}).get("author") or {}).get("screen_name"),
            "text_prefix": ((tweet or {}).get("text") or "")[:220],
        }
    except Exception as e:
        return {"checked": True, "exists": None, "error": str(e)}


def page_state(page) -> dict:
    try:
        return page.evaluate(
            r"""() => {
              const bodyText = document.body ? (document.body.innerText || '') : '';
              const main = document.querySelector('main');
              const mainText = main ? (main.innerText || '') : '';
              const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
              return {
                href: location.href,
                title: document.title || '',
                ready_state: document.readyState,
                html_len: document.documentElement ? document.documentElement.outerHTML.length : 0,
                body_len: bodyText.length,
                main_len: mainText.length,
                text_prefix: (mainText || bodyText).slice(0, 1800),
                article_count: articles.length,
                article_texts: articles.slice(0, 5).map(a => (a.innerText || '').slice(0, 700))
              };
            }"""
        )
    except Exception as e:
        return {"error": str(e)}


def _norm(text: str) -> str:
    return (
        (text or "")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .lower()
    )


def classify_current_page(page, tweet_url: str, tweets: list[dict] | None = None) -> dict:
    handle, tweet_id = parse_tweet_url(tweet_url)
    if tweets is None:
        try:
            tweets = page.evaluate(tb.THREAD_EXTRACTOR_JS)
        except Exception:
            tweets = []

    state = page_state(page)
    text = (state.get("text_prefix") or "") + "\n" + "\n".join(state.get("article_texts") or [])
    text_norm = _norm(text)
    href = (state.get("href") or "").lower()
    rendered_ids = [str(t.get("tweet_id") or "") for t in tweets if t.get("tweet_id")]
    rendered_handles = sorted({
        (t.get("handle") or "").lstrip("@").lower()
        for t in tweets
        if t.get("handle")
    })
    matched = bool(tweet_id and tweet_id in rendered_ids)
    phrases: list[str] = []

    def has_any(candidates: list[str]) -> bool:
        for phrase in candidates:
            if phrase.lower() in text_norm:
                phrases.append(phrase)
                return True
        return False

    status = "unknown"
    reason = "no_access_signal"
    if matched:
        status, reason = "visible", "anchor_tweet_rendered"
    elif "/account/access" in href:
        # X 302'd the session to its "verify it's you" gate. Valid cookie, but
        # X is limiting this session — treat as gated, not deleted/blocked.
        status, reason = "access_gated", "account_access_redirect"
    elif has_any([
        "performing security verification",
        "verify you are human",
        "checking if the site connection is secure",
        "security service to protect",
        "needs to review the security of your connection",
    ]):
        # Cloudflare interstitial in front of x.com (datacenter-IP trust gate).
        status, reason = "access_gated", "cloudflare_challenge"
    elif has_any([
        "you're blocked",
        "you are blocked",
        "blocked you",
        "has blocked you",
        "you can't follow or see",
    ]):
        status, reason = "blocked", "block_phrase_rendered"
    elif has_any([
        "these posts are protected",
        "only approved followers",
        "follow to see their posts",
    ]):
        status, reason = "protected", "protected_phrase_rendered"
    elif has_any([
        "this post is unavailable",
        "this page doesn't exist",
        "account suspended",
        "this account doesn't exist",
    ]):
        status, reason = "unavailable", "unavailable_phrase_rendered"
    elif has_any([
        "something went wrong",
        "try reloading",
        "retry",
    ]):
        status, reason = "app_error", "generic_x_error_rendered"
    elif "/login" in href or "/i/flow/login" in href:
        status, reason = "logged_out", "login_url"
    elif (state.get("article_count") or 0) > 0:
        status, reason = "visible_no_anchor", "tweet_articles_rendered_but_anchor_not_found"
    elif (state.get("body_len") or 0) == 0 and (state.get("article_count") or 0) == 0:
        status, reason = "app_not_hydrated", "empty_x_app_shell"

    return {
        "status": status,
        "reason": reason,
        "tweet_url": tweet_url,
        "handle": handle,
        "tweet_id": tweet_id,
        "matched_tweet": matched,
        "rendered_tweet_ids": rendered_ids[:12],
        "rendered_handles": rendered_handles[:12],
        "current_url": state.get("href"),
        "title": state.get("title"),
        "body_len": state.get("body_len"),
        "main_len": state.get("main_len"),
        "article_count": state.get("article_count"),
        "phrases": phrases,
        **({"state_error": state.get("error")} if state.get("error") else {}),
    }


def diagnose_tweet_access(
    tweet_url: str,
    wait_ms: int = 12000,
    include_public: bool = True,
) -> dict:
    handle, tweet_id = parse_tweet_url(tweet_url)
    if not handle or not tweet_id:
        return {
            "ok": False,
            "status": "bad_tweet_url",
            "tweet_url": tweet_url,
            "public_control": public_control(tweet_url) if include_public else None,
        }

    from playwright.sync_api import sync_playwright

    retryable = {"app_not_hydrated", "unknown"}
    final: dict | None = None
    with sync_playwright() as p:
        browser, page, is_cdp = tb.get_browser_and_page(p)
        try:
            for attempt in (1, 2):
                try:
                    page.goto(tweet_url, wait_until="domcontentloaded", timeout=45000)
                except Exception as e:
                    print(f"[twitter_access] navigate attempt={attempt} failed: {e}", file=sys.stderr)

                deadline = time.time() + max(2.0, wait_ms / 1000.0)
                while True:
                    page.wait_for_timeout(1000)
                    try:
                        tweets = page.evaluate(tb.THREAD_EXTRACTOR_JS)
                    except Exception:
                        tweets = []
                    final = classify_current_page(page, tweet_url, tweets=tweets)
                    if final["status"] not in retryable or time.time() >= deadline:
                        break
                if final["status"] not in retryable:
                    break
                try:
                    page.evaluate("window.stop()")
                except Exception:
                    pass
            if final is None:
                final = classify_current_page(page, tweet_url)
        finally:
            if not is_cdp:
                page.close()
                browser.close()

    if include_public:
        final["public_control"] = public_control(tweet_url)
    final["ok"] = final.get("status") == "visible"
    return final


def diagnose_session_access(
    probe_url: str = "https://x.com/home",
    wait_ms: int = 9000,
) -> dict:
    """Navigate one authenticated route and report whether X is gating us.

    Unlike a cookie probe (which only proves an auth_token exists), this loads a
    real authenticated page and classifies the rendered result. It returns a
    `gated` boolean that callers (e.g. the cycle preflight) use to STOP before
    scanning/posting against a session X is limiting, instead of mistaking the
    resulting phantom "doesn't exist" renders for real, empty results.

    status: access_gated | logged_out | ok | unknown
    """
    from playwright.sync_api import sync_playwright

    cf_phrases = (
        "performing security verification",
        "verify you are human",
        "checking if the site connection is secure",
        "security service to protect",
        "needs to review the security of your connection",
    )
    final: dict = {"status": "unknown", "reason": "no_signal"}
    with sync_playwright() as p:
        browser, page, is_cdp = tb.get_browser_and_page(p)
        try:
            try:
                page.goto(probe_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"[twitter_access] session navigate failed: {e}", file=sys.stderr)

            deadline = time.time() + max(2.0, wait_ms / 1000.0)
            while True:
                page.wait_for_timeout(1000)
                state = page_state(page)
                href = (state.get("href") or "").lower()
                text_norm = _norm(state.get("text_prefix") or "")
                articles = state.get("article_count") or 0
                if "/account/access" in href:
                    final = {"status": "access_gated", "reason": "account_access_redirect"}
                elif "/login" in href or "/i/flow/login" in href or "/logout" in href:
                    final = {"status": "logged_out", "reason": "login_url"}
                elif any(s in text_norm for s in cf_phrases):
                    final = {"status": "access_gated", "reason": "cloudflare_challenge"}
                elif articles > 0:
                    final = {"status": "ok", "reason": "timeline_rendered"}
                else:
                    final = {"status": "unknown", "reason": "no_signal_yet"}
                if final["status"] in ("access_gated", "logged_out", "ok") or time.time() >= deadline:
                    final["current_url"] = state.get("href")
                    final["title"] = state.get("title")
                    final["body_len"] = state.get("body_len")
                    final["article_count"] = articles
                    break
        finally:
            if not is_cdp:
                page.close()
                browser.close()

    final["probe_url"] = probe_url
    # Only the two positively-detected gate states halt the caller. ok/unknown
    # never block, so a transient hydration miss can't silently stop posting.
    final["gated"] = final.get("status") in ("access_gated", "logged_out")
    return final


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tweet_url", nargs="?")
    parser.add_argument("--wait-ms", type=int, default=12000)
    parser.add_argument("--no-public-control", action="store_true")
    parser.add_argument(
        "--session-probe",
        action="store_true",
        help="Navigate an authenticated route and report whether X is gating "
        "this session (access_gated/logged_out/ok). No tweet_url needed.",
    )
    parser.add_argument("--probe-url", default="https://x.com/home")
    args = parser.parse_args()

    if args.session_probe:
        result = diagnose_session_access(
            probe_url=args.probe_url,
            wait_ms=min(args.wait_ms, 12000),
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if not args.tweet_url:
        parser.error("tweet_url is required unless --session-probe is given")

    result = diagnose_tweet_access(
        args.tweet_url,
        wait_ms=args.wait_ms,
        include_public=not args.no_public_control,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
