"""Integrated menu-bar test (SAFE — nothing posts to X).

Runs the REAL menu bar process against an isolated state dir and a FAKE loopback
server, so the full flow is exercised end to end:
  menu bar detects review-request.json -> pops the cards on its own -> you click
  -> it calls post_drafts on the FAKE server (which sleeps ~4s so the title
  spinner is visible) -> returns {"posted": 2} -> "Posted" notification.

The fake server stands in for the MCP loopback, so the post never reaches the
real pipeline. Isolated SAPS_STATE_DIR + a "TEST" batch id keep it away from any
real state.
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = "/tmp/s4l-test-state"
os.makedirs(STATE, exist_ok=True)
os.environ["SAPS_STATE_DIR"] = STATE
sys.path.insert(0, os.path.expanduser("~/social-autoposter/mcp/menubar"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(ln).decode() if ln else "{}"
        if self.path == "/tool/post_drafts":
            print("FAKE_POST_DRAFTS_RECEIVED:", body, flush=True)
            time.sleep(4)  # simulate posting so the spinner is visible
            self._json(200, {"structuredContent": {"posted": 2, "batch_id": "TEST"}})
        else:
            self._json(404, {"error": "unknown tool"})


srv = HTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

with open(os.path.join(STATE, "panel-endpoint.json"), "w") as f:
    json.dump({"url": f"http://127.0.0.1:{port}/", "version": "1.6.69-test"}, f)

batch = "TEST"
plan = {
    "candidates": [
        {
            "thread_author": "@founderfomo",
            "thread_text": "anyone else drowning in unread Slack threads? lose ~2h/day catching up.",
            "reply_text": "this is exactly why we built overnight digests, so you start the day at inbox zero.",
            "link_url": "https://s4l.ai/r/abc123",
        },
        {
            "thread_author": "@devtoolsdaily",
            "thread_text": "best way to track competitor launches without living in RSS?",
            "reply_text": "we watch 40+ launch feeds and ping you the moment a competitor ships.",
            "link_url": "https://s4l.ai/r/def456",
        },
    ]
}
plan_path = f"/tmp/twitter_cycle_plan_{batch}.json"
with open(plan_path, "w") as f:
    json.dump(plan, f)
with open(os.path.join(STATE, "review-request.json"), "w") as f:
    json.dump(
        {
            "batch_id": batch,
            "project": "test",
            "count": 2,
            "plan_path": plan_path,
            "created_at": "2026-06-19T00:00:00Z",
        },
        f,
    )

print(f"FAKE loopback on 127.0.0.1:{port}, state={STATE}", flush=True)
print("Menu bar starting — 'S4L' appears in the menu bar; cards pop within ~5s.", flush=True)

import s4l_menubar  # noqa: E402

s4l_menubar.S4LMenuBar().run()
