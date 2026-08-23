#!/usr/bin/env python3
"""Minimal CDP driver for the harness Chrome on port 9556.
Usage:
  cdp_drive.py nav <url>            -> navigate active page tab, print final href
  cdp_drive.py href                 -> print current href of active page tab
  cdp_drive.py shot <path>          -> screenshot active page tab to path
  cdp_drive.py click <x> <y>        -> dispatch mouse click at viewport coords
  cdp_drive.py eval <expr>          -> evaluate JS expression, print JSON result
"""
import sys, json, time, base64
import urllib.request
import websocket  # websocket-client

PORT = 9556
BASE = f"http://localhost:{PORT}"


def list_tabs():
    with urllib.request.urlopen(f"{BASE}/json/list", timeout=10) as r:
        return json.load(r)


def active_page():
    tabs = list_tabs()
    pages = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    # prefer a linkedin tab, else first page
    for t in pages:
        if "linkedin.com" in (t.get("url") or ""):
            return t
    if pages:
        return pages[0]
    raise RuntimeError("no page tab")


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(
            ws_url, max_size=None, timeout=60, suppress_origin=True
        )
        self._id = 0

    def cmd(self, method, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def get_href(c):
    r = c.cmd("Runtime.evaluate", expression="location.href", returnByValue=True)
    return r.get("result", {}).get("value")


def main():
    action = sys.argv[1]
    tab = active_page()
    c = CDP(tab["webSocketDebuggerUrl"])
    try:
        c.cmd("Page.enable")
        c.cmd("Runtime.enable")
        if action == "nav":
            url = sys.argv[2]
            c.cmd("Page.navigate", url=url)
            time.sleep(6)
            print(get_href(c))
        elif action == "href":
            print(get_href(c))
        elif action == "shot":
            path = sys.argv[2]
            r = c.cmd("Page.captureScreenshot", format="png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(r["data"]))
            print(path)
        elif action == "click":
            x = float(sys.argv[2]); y = float(sys.argv[3])
            c.cmd("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
            c.cmd("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
            time.sleep(4)
            print(get_href(c))
        elif action == "eval":
            expr = sys.argv[2]
            r = c.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
            print(json.dumps(r.get("result", {}).get("value")))
        else:
            print("unknown action", file=sys.stderr); sys.exit(2)
    finally:
        c.close()


if __name__ == "__main__":
    main()
