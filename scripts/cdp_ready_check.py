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

        # Renderer-liveness sweep: revive crashed tabs in place, best-effort;
        # revival must never fail the readiness verdict the wedge gate uses.
        revived = 0
        pages = []
        try:
            pages = [t for t in _get_json(f"{url}/json/list", timeout=3)
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            for t in pages:
                try:
                    _ws_call(t["webSocketDebuggerUrl"], "Runtime.evaluate",
                             4.0, expression="1")
                except Exception:
                    try:
                        _ws_call(t["webSocketDebuggerUrl"], "Page.reload", 8.0)
                        revived += 1
                    except Exception:
                        pass
        except Exception:
            pass

        out = {
            "ready": True, "mode": "raw_ws", "contexts": len(pages),
            "elapsed_s": round(time.time() - t0, 2),
        }
        if revived:
            out["revived"] = revived
        print(json.dumps(out))
        return 0
    except Exception as e:
        print(json.dumps({
            "ready": False, "mode": "raw_ws",
            "elapsed_s": round(time.time() - t0, 2),
            "error": str(e)[:200].replace("\n", " "),
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
