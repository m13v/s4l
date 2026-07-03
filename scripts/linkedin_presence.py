#!/usr/bin/env python3
"""Read-only LinkedIn presence pass through the existing harness Chrome.

Simulates a short human browsing session:
  - attach to the already-running linkedin-harness Chrome via CDP
  - start on one first-party LinkedIn surface
  - random walk of scrolls, dwells, and read-only navigation clicks
    (top nav tabs, profile links from the feed, company pages,
    LinkedIn News stories), with occasional back-navigation

Hard safety rules:
  - clicks ONLY <a href> elements whose URL matches a strict allowlist of
    read-only linkedin.com surfaces; never clicks buttons or coordinates
  - never likes, follows, connects, messages, comments, posts, or types
  - never opens messaging conversations or external domains
  - never calls Voyager

The shell wrapper owns scheduling, locking, killswitch checks, and run_monitor
logging. This Python file only does the browser session.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from linkedin_browser import _connect_to_running_or_launch, _is_login_or_checkpoint  # noqa: E402


START_URLS = {
    "feed": "https://www.linkedin.com/feed/",
    "notifications": "https://www.linkedin.com/notifications/",
    "mynetwork": "https://www.linkedin.com/mynetwork/",
    "profile": "https://www.linkedin.com/in/me/",
}
# Feed-heavy start distribution: (surface, weight)
START_WEIGHTS = [("feed", 55), ("notifications", 20), ("mynetwork", 15), ("profile", 10)]

# Click allowlist: read-only destinations only. Matched against href with the
# query string stripped. Anything not matching is never clicked.
CLICK_ALLOWLIST = {
    "nav": re.compile(
        r"^https://www\.linkedin\.com/(feed|mynetwork|notifications|jobs)/?$"
    ),
    "profile": re.compile(r"^https://www\.linkedin\.com/in/[^/?#]+/?$"),
    "company": re.compile(
        r"^https://www\.linkedin\.com/company/[^/?#]+/?(posts/?|about/?)?$"
    ),
    "news": re.compile(r"^https://www\.linkedin\.com/news/[^?#]+$"),
}

# Random-walk action distribution: (action, weight)
ACTION_WEIGHTS = [
    ("scroll", 38),
    ("read", 14),
    ("scroll_up", 8),
    ("click_nav", 12),
    ("click_profile", 14),
    ("click_company", 7),
    ("click_news", 7),
]

CLICK_CATEGORY = {
    "click_nav": "nav",
    "click_profile": "profile",
    "click_company": "company",
    "click_news": "news",
}

ANCHOR_SCAN_JS = """
() => {
  const vh = window.innerHeight, vw = window.innerWidth;
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || '';
    if (!href.startsWith('https://www.linkedin.com/')) continue;
    if (href.includes('"')) continue;
    const r = a.getBoundingClientRect();
    if (r.width < 24 || r.height < 10) continue;
    if (r.bottom < 60 || r.top > vh - 20 || r.right < 40 || r.left > vw - 40) continue;
    const key = href.split('?')[0];
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({href: href, text: (a.innerText || '').trim().slice(0, 60)});
    if (out.length >= 150) break;
  }
  return out;
}
"""


def _weighted_choice(pairs: list[tuple[str, int]]) -> str:
    total = sum(w for _, w in pairs)
    roll = random.uniform(0, total)
    acc = 0.0
    for name, w in pairs:
        acc += w
        if roll <= acc:
            return name
    return pairs[-1][0]


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


def _session_invalid(page: Any) -> tuple[bool, str]:
    url = page.url or ""
    title = _safe_title(page)
    if _is_login_or_checkpoint(url.lower()):
        return True, f"url:{url}"
    if any(s in title.lower() for s in ("security verification", "captcha", "checkpoint")):
        return True, f"title:{title}"
    # Skip body-text markers on messaging (private previews); URL/title cover it.
    if "/messaging" in url:
        return False, ""
    text = _safe_eval(
        page,
        "() => (document.body && document.body.innerText || '').slice(0, 1200)",
        "",
    )
    text_l = str(text or "").lower()
    for marker in (
        "security verification",
        "verify you are human",
        "captcha",
        "sign in to linkedin",
        "join linkedin",
    ):
        if marker in text_l:
            return True, f"text:{marker}"
    return False, ""


def _settle(page: Any) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(random.randint(1500, 3200))


def _scroll(page: Any, up: bool = False) -> None:
    dims = _safe_eval(
        page,
        "() => ({w: window.innerWidth || 1180, h: window.innerHeight || 900})",
        {"w": 1180, "h": 900},
    )
    w = int((dims or {}).get("w") or 1180)
    h = int((dims or {}).get("h") or 900)
    x = int(w * random.uniform(0.35, 0.62))
    y = int(h * random.uniform(0.40, 0.68))
    try:
        page.mouse.move(x, y, steps=random.randint(4, 12))
    except Exception:
        pass
    amount = random.randint(380, 760)
    if up:
        amount = -random.randint(240, 520)
    page.mouse.wheel(0, amount)
    time.sleep(random.uniform(1.2, 4.5))


def _candidate_links(page: Any, category: str, visited: set[str]) -> list[str]:
    anchors = _safe_eval(page, ANCHOR_SCAN_JS, []) or []
    rx = CLICK_ALLOWLIST[category]
    current = (page.url or "").split("?")[0].rstrip("/")
    out = []
    for a in anchors:
        href = str(a.get("href") or "")
        base = href.split("?")[0]
        if not rx.match(base):
            continue
        if base.rstrip("/") == current:
            continue
        if base in visited and category != "nav":
            continue
        out.append(href)
    return out


def _click_link(context: Any, page: Any, href: str) -> tuple[Any, bool]:
    """Click the anchor with this exact href. Returns (page, navigated)."""
    before_url = page.url
    before_pages = len(context.pages)
    loc = page.locator(f'a[href="{href}"]').first
    try:
        loc.scroll_into_view_if_needed(timeout=4000)
        page.wait_for_timeout(random.randint(300, 900))
        loc.click(timeout=5000, delay=random.randint(40, 140))
    except Exception:
        return page, False
    page.wait_for_timeout(1200)
    # target=_blank case: browse briefly in the new tab, then close it.
    if len(context.pages) > before_pages:
        new_page = context.pages[-1]
        try:
            _settle(new_page)
            _scroll(new_page)
            time.sleep(random.uniform(1.0, 3.0))
            new_page.close()
        except Exception:
            pass
        return page, True
    _settle(page)
    return page, page.url != before_url


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.seed:
        random.seed(args.seed)
    start = args.start or _weighted_choice(START_WEIGHTS)
    start_url = START_URLS.get(start)
    if not start_url:
        return {"ok": False, "error": "bad_start", "start": start}

    steps_target = args.steps if args.steps > 0 else random.randint(4, 9)
    deadline = time.monotonic() + max(30, args.max_seconds)
    max_navs = 4

    counters = {"scrolls": 0, "clicks": 0, "navs": 0, "reads": 0, "skipped": 0}
    visited: set[str] = set()
    trail: list[str] = []
    back_depth = 0

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context, _owns_context = _connect_to_running_or_launch(p, prefer_cdp=True)
        page = _pick_page(context)
        page.goto(start_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        _settle(page)

        invalid, detail = _session_invalid(page)
        if invalid:
            return {
                "ok": False,
                "error": "session_invalid",
                "detail": detail,
                "start": start,
                "url": page.url,
                "title": _safe_title(page),
            }
        visited.add((page.url or "").split("?")[0])
        trail.append(f"open:{start}")

        steps_done = 0
        while steps_done < steps_target and time.monotonic() < deadline:
            action = _weighted_choice(ACTION_WEIGHTS)
            if action in CLICK_CATEGORY and counters["navs"] >= max_navs:
                action = "scroll"

            if action == "scroll":
                _scroll(page)
                counters["scrolls"] += 1
                trail.append("scroll")
            elif action == "scroll_up":
                _scroll(page, up=True)
                counters["scrolls"] += 1
                trail.append("scroll_up")
            elif action == "read":
                time.sleep(random.uniform(2.5, 7.0))
                counters["reads"] += 1
                trail.append("read")
            else:
                category = CLICK_CATEGORY[action]
                links = _candidate_links(page, category, visited)
                if not links:
                    counters["skipped"] += 1
                    trail.append(f"no_{category}")
                    steps_done += 1
                    continue
                href = random.choice(links)
                page, navigated = _click_link(context, page, href)
                if navigated:
                    counters["clicks"] += 1
                    counters["navs"] += 1
                    visited.add((page.url or "").split("?")[0])
                    trail.append(f"{category}->{(page.url or '').split('?')[0]}")
                    invalid, detail = _session_invalid(page)
                    if invalid:
                        return {
                            "ok": False,
                            "error": "session_invalid",
                            "detail": detail,
                            "start": start,
                            "url": page.url,
                            "title": _safe_title(page),
                            "trail": trail,
                        }
                    # Skim the destination a little.
                    if random.random() < 0.8:
                        _scroll(page)
                        counters["scrolls"] += 1
                    back_depth += 1
                    # Usually wander back the way a person does.
                    if category != "nav" and back_depth > 0 and random.random() < 0.65:
                        try:
                            page.go_back(wait_until="domcontentloaded", timeout=15000)
                            _settle(page)
                            back_depth -= 1
                            trail.append("back")
                        except Exception:
                            pass
                else:
                    counters["skipped"] += 1
                    trail.append(f"miss_{category}")
            steps_done += 1

        return {
            "ok": True,
            "start": start,
            "steps": steps_done,
            "counters": counters,
            "pages_visited": sorted(visited),
            "trail": trail,
            "url": page.url,
            "title": _safe_title(page),
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a read-only LinkedIn presence browse session")
    ap.add_argument("--start", default="", choices=[""] + sorted(START_URLS))
    ap.add_argument("--steps", type=int, default=0, help="0 = random 4-9")
    ap.add_argument("--max-seconds", type=int, default=150)
    ap.add_argument("--timeout-ms", type=int, default=30000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    try:
        result = run(args)
    except Exception as e:
        result = {"ok": False, "error": "exception", "detail": str(e)}

    if result.get("ok"):
        c = result.get("counters") or {}
        print(
            "LINKEDIN_PRESENCE_SUMMARY: "
            f"start={result.get('start')} steps={result.get('steps')} "
            f"pages={len(result.get('pages_visited') or []) or 1} "
            f"scrolls={c.get('scrolls', 0)} clicks={c.get('clicks', 0)} session=ok"
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
