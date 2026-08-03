#!/usr/bin/env python3
"""Attribution monitor for harness-Chrome focus steals (2026-08-03).

Connects to a harness Chrome's browser-level CDP websocket, enables target
discovery, and appends one timestamped line per Target lifecycle event
(created / destroyed / info-changed => navigations) to a log file. Correlate
these against the `[browser-foreground]` activation lines in
~/.social-autoposter-mcp/menubar/menubar.err.log to attribute WHICH tab
operation coincided with an app activation, something none of the existing
logs capture (the daemon log has no timestamps; python new_page sites have
no logging at all; bh [bh_tab_event] covers only the bh lanes).

Read-only: never creates, closes, or navigates anything.

Usage:
    harness_target_monitor.py [--port 9557] [--log PATH]

Runs forever; reconnects with backoff when Chrome restarts. Intended to run
under nohup during a diagnosis window. It is NOT part of the pipeline.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log_line(path: str, msg: str) -> None:
    with open(path, "a") as f:
        f.write(f"[{ts()}] {msg}\n")


def monitor_once(port: int, log_path: str) -> None:
    import urllib.request
    import websocket

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    info = json.loads(opener.open(f"http://127.0.0.1:{port}/json/version", timeout=3).read())
    ws = websocket.create_connection(
        info["webSocketDebuggerUrl"], timeout=5, suppress_origin=True
    )
    try:
        ws.send(json.dumps({"id": 1, "method": "Target.setDiscoverTargets",
                            "params": {"discover": True}}))
        log_line(log_path, f"monitor attached port={port} chrome={info.get('Browser','?')}")
        ws.settimeout(60)
        last_url = {}
        while True:
            try:
                msg = json.loads(ws.recv())
            except Exception as e:
                if "timed out" in str(e).lower():
                    # Idle is fine; poke the connection so a dead Chrome errors out.
                    ws.send(json.dumps({"id": 2, "method": "Browser.getVersion"}))
                    continue
                raise
            method = msg.get("method", "")
            p = msg.get("params", {})
            t = p.get("targetInfo", {})
            if t.get("type") not in ("page", ""):
                continue
            tid = t.get("targetId") or p.get("targetId", "?")
            url = t.get("url", "")
            if method == "Target.targetCreated":
                log_line(log_path, f"CREATED  {tid} url={url[:100]}")
            elif method == "Target.targetDestroyed":
                log_line(log_path, f"DESTROYED {tid} (last_url={last_url.get(tid, '?')[:100]})")
            elif method == "Target.targetInfoChanged":
                if url and url != last_url.get(tid):
                    log_line(log_path, f"NAVIGATED {tid} url={url[:100]}")
            if tid != "?" and url:
                last_url[tid] = url
    finally:
        try:
            ws.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9557)
    ap.add_argument("--log", default=os.path.expanduser(
        "~/social-autoposter/skill/logs/harness-target-events-9557.log"))
    args = ap.parse_args()
    while True:
        try:
            monitor_once(args.port, args.log)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            try:
                log_line(args.log, f"monitor disconnected ({type(e).__name__}: {str(e)[:120]}); retry in 15s")
            except OSError:
                pass
            time.sleep(15)


if __name__ == "__main__":
    sys.exit(main())
