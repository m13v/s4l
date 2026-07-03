#!/usr/bin/env python3
"""Read-only LinkedIn presence pass through the existing harness Chrome.

This helper is deliberately small and deterministic:
  - attach to the already-running linkedin-harness Chrome via CDP
  - reuse the existing tab/context
  - navigate to one first-party LinkedIn surface
  - perform a bounded number of mouse-wheel scrolls with short dwell periods
  - never click, type, post, react, message, open permalinks, or call Voyager

The shell wrapper owns scheduling, locking, killswitch checks, and run_monitor
logging. This Python file only does the browser action.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from linkedin_browser import _connect_to_running_or_launch, _is_login_or_checkpoint  # noqa: E402


MODE_URLS = {
    "feed": "https://www.linkedin.com/feed/",
    "notifications": "https://www.linkedin.com/notifications/",
    "messaging": "https://www.linkedin.com/messaging/",
    "profile": "https://www.linkedin.com/in/me/",
}


def _csv_ints(raw: str, fallback: list[int]) -> list[int]:
    out: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out or list(fallback)


def _pick_page(context: Any) -> Any:
    pages = [p for p in context.pages if not p.is_closed()]
    if pages:
        return pages[0]
    return context.new_page()


def _safe_title(page: Any) -> str:
    try:
        return page.title() or ""
    except Exception:
        return ""


def _safe_eval(page: Any, expr: str, fallback: Any) -> Any:
    try:
        return page.evaluate(expr)
    except Exception:
        return fallback


def _session_invalid(page: Any, mode: str) -> tuple[bool, str]:
    url = page.url or ""
    title = _safe_title(page)
    url_l = url.lower()
    title_l = title.lower()
    if _is_login_or_checkpoint(url_l):
        return True, f"url:{url}"
    if any(s in title_l for s in ("security verification", "captcha", "checkpoint")):
        return True, f"title:{title}"

    # Avoid reading private message preview text in messaging mode. The shared
    # shell-level detect-gate has already verified /feed/ before this helper
    # runs, and URL/title catch the authwall redirects here.
    if mode == "messaging":
        return False, ""

    text = _safe_eval(
        page,
        "() => (document.body && document.body.innerText || '').slice(0, 1200)",
        "",
    )
    text_l = str(text or "").lower()
    bad_text_markers = (
        "security verification",
        "verify you are human",
        "captcha",
        "sign in to linkedin",
        "join linkedin",
    )
    for marker in bad_text_markers:
        if marker in text_l:
            return True, f"text:{marker}"
    return False, ""


def _viewport_point(page: Any) -> tuple[int, int]:
    dims = _safe_eval(
        page,
        "() => ({w: window.innerWidth || 1180, h: window.innerHeight || 900})",
        {"w": 1180, "h": 900},
    )
    if not isinstance(dims, dict):
        dims = {"w": 1180, "h": 900}
    w = int(dims.get("w") or 1180)
    h = int(dims.get("h") or 900)
    return max(1, int(w * 0.50)), max(1, int(h * 0.56))


def run(args: argparse.Namespace) -> dict[str, Any]:
    url = args.url or MODE_URLS.get(args.mode)
    if not url:
        return {"ok": False, "error": "bad_mode", "mode": args.mode}

    amounts = _csv_ints(args.amounts, [520])
    dwells = _csv_ints(args.dwells, [2])
    scrolls = max(0, min(int(args.scrolls), len(amounts)))
    amounts = amounts[:scrolls]
    if len(dwells) < scrolls:
        dwells.extend([dwells[-1] if dwells else 2] * (scrolls - len(dwells)))
    dwells = dwells[:scrolls]

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context, _owns_context = _connect_to_running_or_launch(p, prefer_cdp=True)
        page = _pick_page(context)
        page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        page.wait_for_timeout(args.settle_ms)

        invalid, detail = _session_invalid(page, args.mode)
        if invalid:
            return {
                "ok": False,
                "error": "session_invalid",
                "detail": detail,
                "mode": args.mode,
                "url": page.url,
                "title": _safe_title(page),
            }

        x, y = _viewport_point(page)
        page.mouse.move(x, y)
        for amount, dwell in zip(amounts, dwells):
            page.mouse.wheel(0, amount)
            time.sleep(max(0, dwell))

        scroll_state = _safe_eval(
            page,
            "() => ({x: window.scrollX || 0, y: window.scrollY || 0, h: document.documentElement.scrollHeight || 0})",
            {},
        )
        return {
            "ok": True,
            "mode": args.mode,
            "url": page.url,
            "title": _safe_title(page),
            "scrolls": scrolls,
            "amounts": amounts,
            "dwells": dwells,
            "point": {"x": x, "y": y},
            "scroll_state": scroll_state,
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a read-only LinkedIn presence pass")
    ap.add_argument("--mode", required=True, choices=sorted(MODE_URLS))
    ap.add_argument("--url", default="")
    ap.add_argument("--scrolls", type=int, default=1)
    ap.add_argument("--amounts", default="")
    ap.add_argument("--dwells", default="")
    ap.add_argument("--timeout-ms", type=int, default=30000)
    ap.add_argument("--settle-ms", type=int, default=2000)
    args = ap.parse_args()

    try:
        result = run(args)
    except Exception as e:
        result = {"ok": False, "error": "exception", "detail": str(e), "mode": args.mode}

    if result.get("ok"):
        print(
            "LINKEDIN_PRESENCE_SUMMARY: "
            f"mode={result.get('mode')} pages=1 scrolls={result.get('scrolls')} session=ok"
        )
        print(json.dumps(result, sort_keys=True))
        return 0

    if result.get("error") == "session_invalid":
        print("SESSION_INVALID")
        print(json.dumps(result, sort_keys=True))
        return 2

    print(json.dumps(result, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
