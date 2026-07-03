#!/usr/bin/env python3
"""Menu-bar-hosted dashboard HTTP server (Claude-independent "Open dashboard").

Lets the dashboard render in a normal browser even when Claude / the MCP is
closed. It serves the SAME dist/panel.html in HTTP-bridge mode and answers the
panel's READ tools (project_config status, runtime status) from
scripts/snapshot.py — the single source of truth. Action tools that genuinely
need the agent (setup, schedule, install, show-browser) degrade to an isError
result, which the panel already handles by telling the user to use Claude; the
lightweight mode toggle is the one action we CAN do here (write mode.json), so it
works offline too.

Runs on 127.0.0.1 / ephemeral port on a daemon thread started by the menu bar.
The menu bar still PREFERS the live MCP loopback URL when Claude is up (full
interactivity); this is the fallback for when it isn't.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")


def _repo_dir():
    # SAPS_REPO_DIR fallback: pre-rename (2026-07-03) launchd plists / parents.
    return (
        os.environ.get("S4L_REPO_DIR")
        or os.environ.get("SAPS_REPO_DIR")
        or os.path.join(HOME, "social-autoposter")
    )


def _scripts_dir():
    d = os.path.join(_repo_dir(), "scripts")
    if d not in sys.path:
        sys.path.insert(0, d)
    return d


def _find_panel_html():
    for c in (
        os.path.join(_repo_dir(), "mcp", "dist", "panel.html"),
        os.path.join(HOME, ".social-autoposter-mcp", "repo", "package", "mcp", "dist", "panel.html"),
    ):
        if os.path.isfile(c):
            return c
    return None


def _compute_snapshot():
    _scripts_dir()
    import snapshot as snap_mod  # scripts/snapshot.py
    return snap_mod.compute()


def _toggle_lane(lane):
    """Flip ONE engagement lane (personal_brand|promotion) via saps_mode.py and
    return the new flags dict."""
    if lane not in ("personal_brand", "promotion"):
        lane = "personal_brand"
    py = (
        os.environ.get("S4L_PYTHON")
        or os.environ.get("SAPS_PYTHON")  # pre-rename plists (2026-07-03)
        or sys.executable
        or "python3"
    )
    sm = os.path.join(_repo_dir(), "scripts", "saps_mode.py")
    out = (subprocess.run([py, sm, "toggle", lane], capture_output=True, text=True, timeout=15).stdout or "").strip()
    try:
        return json.loads(out)
    except Exception:
        return _compute_snapshot().get("flags") or {"personal_brand": True, "promotion": False}


def _result(data):
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


def _err(msg):
    return {"isError": True, "content": [{"type": "text", "text": msg}]}


_NEEDS_CLAUDE = (
    "This action needs Claude open — run it from the chat (or use the matching "
    "menu-bar button, which copies the prompt for you)."
)


def _handle_tool(name, args):
    if name == "project_config" and args.get("status"):
        s = _compute_snapshot()
        return _result({
            "configured": (s.get("projects_ready") or 0) > 0,
            "projects": s.get("projects", []),
            "x_connected": s.get("x_connected"),
            "x_state": s.get("x_state"),
            "x_handle": s.get("x_handle"),
            "runtime_ready": s.get("runtime_ready"),
            "mcp_version": s.get("version"),
            "latest_version": s.get("latest_version"),
            "update_available": s.get("update_available"),
            "mode": s.get("mode"),
            "flags": s.get("flags"),
            "onboarding": s.get("onboarding"),
        })
    if name == "runtime" and (args.get("action") in (None, "status")):
        s = _compute_snapshot()
        return _result({
            "runtime_ready": s.get("runtime_ready"),
            "provisioning": s.get("runtime_provisioning"),
            "onboarding": s.get("onboarding"),
        })
    if name == "engagement_mode":
        if (args.get("action") or "get") == "toggle":
            return _result({"flags": _toggle_lane(args.get("lane") or "personal_brand")})
        s = _compute_snapshot()
        return _result({"mode": s.get("mode"), "flags": s.get("flags")})
    # Everything else (setup, schedule, connect_x, install, show_browser, stats)
    # needs the agent/MCP — degrade gracefully.
    return _err(_NEEDS_CLAUDE)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def do_GET(self):
        if self.path in ("/", "/panel", "/index.html"):
            html_path = _find_panel_html()
            if not html_path:
                self._send(503, "dashboard unavailable (panel.html not found)", "text/plain")
                return
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    html = f.read()
            except Exception as e:
                self._send(500, f"read error: {e}", "text/plain")
                return
            inject = '<script>window.__SAPS_BRIDGE__="http";</script>'
            html = html.replace("</head>", inject + "</head>", 1) if "</head>" in html else inject + html
            self._send(200, html, "text/html; charset=utf-8")
            return
        if self.path == "/health":
            self._send(200, json.dumps({"ok": True}))
            return
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self.path.startswith("/tool/"):
            self._send(404, "not found", "text/plain")
            return
        name = self.path[len("/tool/"):]
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(ln).decode("utf-8") if ln else ""
            args = json.loads(raw) if raw.strip() else {}
        except Exception:
            args = {}
        try:
            result = _handle_tool(name, args if isinstance(args, dict) else {})
        except Exception as e:
            result = _err(str(e))
        self._send(200, json.dumps(result))


_server = None
_url = None
_lock = threading.Lock()


def start():
    """Start the dashboard server (idempotent). Returns its URL, or None on failure."""
    global _server, _url
    with _lock:
        if _url:
            return _url
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            _server = srv
            _url = f"http://127.0.0.1:{srv.server_address[1]}/"
            return _url
        except Exception:
            return None


def url():
    return _url


if __name__ == "__main__":
    u = start()
    print(u or "failed to start")
    if u:
        import time
        while True:
            time.sleep(3600)
