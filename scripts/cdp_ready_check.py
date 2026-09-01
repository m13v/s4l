#!/usr/bin/env python3
"""Raw-websocket CDP readiness probe for the harness Chrome.

Exit 0 when a real DevTools websocket handshake completes against the given
CDP URL (browser-level connect + Browser.getVersion round-trip), 1 when it
does not. /json/version alone is a LIVENESS check: a Chrome whose ws accept
path died still passes it; this probe exercises the actual session dispatch.

HISTORY — why raw websocket and NOT Playwright (2026-07-17): the original
probe used sync_playwright().connect_over_cdp. Wedge-diag captures
(skill/logs/wedge-diag/, v3 discriminator) proved that during every observed
"wedge" the raw ws handshake completed in <5s while the Playwright connect
timed out at 20s — with Chrome's DevToolsHandlerThread idle in kevent64 the
whole time. The stall lives in Playwright's node driver under machine load,
NOT in Chrome; the Playwright-based verdict was executing a healthy browser
roughly hourly for days (every cdp_wedge relaunch 07-14..07-17). A probe must
not depend on machinery heavier than the thing it probes. The S4L-4H /
Karol-box incidents that motivated the deep probe are equally covered by the
ws round-trip below (and in hindsight may have been this same client-side
stall).

Also preserves the renderer-liveness sweep (2026-07-14): a tab whose RENDERER
crashed ("Aw, Snap") keeps its title/url in every listing while sitting dead;
probe each page with a trivial Runtime.evaluate and reload it IN PLACE on
failure — fresh renderer, no kill, no new window, no focus change.

DEAD SESSION DISPATCH (2026-08-20, recurred 2026-09-01): a parked tab whose
DevTools SESSION dispatch is dead never replies to any per-tab command, so the
in-place Page.reload hangs too and cannot revive it — while the browser-level
ws stays healthy and this probe used to report ready anyway (Playwright then
hung at connect for every posting attempt; on 09-01 that burned all 5 drain
retries on two human-approved drafts). The reload is therefore RE-VERIFIED
with a second Runtime.evaluate; a tab that still doesn't answer gets REPLACED
via browser-level Target.createTarget(background=True) + Target.closeTarget
(the proven fix both incidents — no Chrome restart, no focus steal, session
cookies intact). Only if replacement itself fails does the verdict flip to
ready=false, so hc_ensure_browser's two-strike reap finally has a real signal.

Usage: cdp_ready_check.py [CDP_URL] [TIMEOUT_MS]

Prints a one-line JSON verdict to stdout (same shape/keys as before; mode is
now "raw_ws"). Falls back to an HTTP-only probe when websocket-client is not
importable, so a bare python3 caller degrades to liveness instead of failing.
"""
import json
import sys
import time
import urllib.request

# ProxyHandler({}): loopback CDP must never route through a proxy. macOS
# system proxy settings leak into urllib's default opener, and a box-wide
# forwarder 403s 127.0.0.1 probes (2026-07-13 root cause of a "wedged
# Chrome" misdiagnosis).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(url: str, timeout: float):
    with _OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9555").rstrip("/")
    timeout_s = (int(sys.argv[2]) if len(sys.argv) > 2 else 20000) / 1000.0
    t0 = time.time()
    try:
        import websocket  # websocket-client, present in the owned runtime venv
    except Exception:
        try:
            _OPENER.open(f"{url}/json/version", timeout=3)
            print(json.dumps({"ready": True, "mode": "http-only"}))
            return 0
        except Exception as e:
            print(json.dumps({
                "ready": False, "mode": "http-only", "error": str(e)[:120],
            }))
            return 1

    def _ws_call(ws_url: str, method: str, timeout: float, **params):
        # suppress_origin: Chrome 111+ rejects ws clients whose Origin header
        # is not in --remote-allow-origins (same pattern as the tab parker).
        ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    return msg
            raise TimeoutError(f"no reply to {method}")
        finally:
            ws.close()

    try:
        # Browser-level handshake: the readiness verdict.
        info = _get_json(f"{url}/json/version", timeout=3)
        _ws_call(info["webSocketDebuggerUrl"], "Browser.getVersion", timeout_s)

        # Renderer-liveness sweep. A crashed renderer revives with an in-place
        # reload; a tab with dead session dispatch (reload hangs too) gets
        # REPLACED via browser-level commands. Only an unfixable page tab
        # fails the verdict — see DEAD SESSION DISPATCH in the docstring.
        revived = 0
        replaced = 0
        dead = 0
        pages = []
        try:
            pages = [t for t in _get_json(f"{url}/json/list", timeout=3)
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            for t in pages:
                try:
                    _ws_call(t["webSocketDebuggerUrl"], "Runtime.evaluate",
                             4.0, expression="1")
                    continue
                except Exception:
                    pass
                try:
                    _ws_call(t["webSocketDebuggerUrl"], "Page.reload", 8.0)
                except Exception:
                    pass
                # Re-verify: a reload that went through proves dispatch is
                # alive again; one that hung proves it never will be.
                try:
                    _ws_call(t["webSocketDebuggerUrl"], "Runtime.evaluate",
                             4.0, expression="1")
                    revived += 1
                    continue
                except Exception:
                    pass
                # Dead session dispatch: replace the TAB, keep Chrome. Only
                # http(s) tabs are recreated (the harness daemon's
                # is_real_page refuses about:/chrome: tabs); the dead one is
                # closed only after its replacement exists so the browser is
                # never left tabless.
                page_url = t.get("url") or ""
                try:
                    if not page_url.startswith("http"):
                        raise ValueError(f"unreplaceable url {page_url[:40]!r}")
                    _ws_call(info["webSocketDebuggerUrl"], "Target.createTarget",
                             8.0, url=page_url, background=True)
                    _ws_call(info["webSocketDebuggerUrl"], "Target.closeTarget",
                             8.0, targetId=t["id"])
                    replaced += 1
                except Exception:
                    dead += 1
        except Exception:
            pass

        out = {
            "ready": dead == 0, "mode": "raw_ws", "contexts": len(pages),
            "elapsed_s": round(time.time() - t0, 2),
        }
        if revived:
            out["revived"] = revived
        if replaced:
            out["replaced"] = replaced
        if dead:
            out["dead_pages"] = dead
            out["error"] = "page_session_dead_unreplaceable"
        print(json.dumps(out))
        return 0 if dead == 0 else 1
    except Exception as e:
        print(json.dumps({
            "ready": False, "mode": "raw_ws",
            "elapsed_s": round(time.time() - t0, 2),
            "error": str(e)[:200].replace("\n", " "),
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
