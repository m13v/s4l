#!/usr/bin/env python3
"""Real CDP readiness probe for the harness Chrome.

Exit 0 when a full Playwright connect_over_cdp handshake completes against the
given CDP URL, 1 when it does not. /json/version alone is a LIVENESS check: a
wedged Chrome (process alive, HTTP answering, websocket upgrade completing,
but the browser loop never servicing the CDP session) passes it, and every
downstream attach then eats Playwright's 180s default timeout while holding
the browser lock (S4L-4H, Karol 2026-07-11; identical wedge locally 2026-07-09,
twice, same Chrome instance both times).

Usage: cdp_ready_check.py [CDP_URL] [TIMEOUT_MS]

Prints a one-line JSON verdict to stdout so the caller can persist it
(twitter-backend.sh writes it to skill/logs/cdp-health.json, which
memory_snapshot.py carries onto the per-minute heartbeat sample).

Falls back to an HTTP-only probe when playwright is not importable under the
invoking interpreter, so a bare python3 caller degrades to the legacy
liveness behavior instead of hard-failing.
"""
import json
import sys
import time


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9555").rstrip("/")
    timeout_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    t0 = time.time()
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        import urllib.request
        try:
            # ProxyHandler({}): loopback CDP must never route through a proxy.
            # macOS system proxy settings leak into urllib's default opener, and
            # a box-wide forwarder 403s 127.0.0.1 probes (2026-07-13 root cause
            # of the "wedged Chrome" misdiagnosis).
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            opener.open(f"{url}/json/version", timeout=3)
            print(json.dumps({"ready": True, "mode": "http-only"}))
            return 0
        except Exception as e:
            print(json.dumps({
                "ready": False, "mode": "http-only", "error": str(e)[:120],
            }))
            return 1
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(url, timeout=timeout_ms)
            n_contexts = len(browser.contexts)
            # Renderer-liveness sweep (2026-07-14). A tab whose RENDERER
            # crashed ("Aw, Snap", error code 5) keeps its normal title/url in
            # every CDP listing and the browser-level handshake stays green,
            # so it sat visibly dead for 20+ minutes with nothing entitled to
            # touch it — below the wedge detector (browser-level) and the
            # stall guard (scan-progress-level). Probe each page with a
            # trivial evaluate; on failure reload the tab IN PLACE, which
            # spawns a fresh renderer. No kill, no new window, no focus
            # change. Best-effort: revival must never fail the readiness
            # verdict the wedge gate depends on.
            revived = 0
            for ctx in browser.contexts:
                for page in ctx.pages:
                    try:
                        page.set_default_timeout(4000)
                        page.evaluate("1")
                    except Exception:
                        try:
                            page.reload(timeout=15000, wait_until="commit")
                            revived += 1
                        except Exception:
                            pass
            browser.close()
        out = {
            "ready": True, "mode": "cdp", "contexts": n_contexts,
            "elapsed_s": round(time.time() - t0, 2),
        }
        if revived:
            out["revived"] = revived
        print(json.dumps(out))
        return 0
    except Exception as e:
        print(json.dumps({
            "ready": False, "mode": "cdp",
            "elapsed_s": round(time.time() - t0, 2),
            "error": str(e)[:200].replace("\n", " "),
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
