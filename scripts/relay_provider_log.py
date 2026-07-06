#!/usr/bin/env python3
"""relay_provider_log.py — ship the queue producer/consumer log to Cloud Logging.

claude_job.py writes the queue handoff lifecycle (enqueue / claim / consumed /
timed out, plus [salvage] orphan events) to <state>/claude-queue/provider.log — a
LOCAL file that is invisible remotely. This tails it incrementally (byte offset in
provider-relay-state.json, flock single-flight) and POSTs new lines through the
SAME relay lane the pipeline logs use (bin/server.js /api/v1/installations/logs),
tagged context="queue-provider", so a stranded/orphaned batch on any box is
queryable in Cloud Logging instead of only on the box.

Query:
  gcloud logging read 'jsonPayload.install_id="<uuid>" AND jsonPayload.context="queue-provider"' --project=s4l-app-prod

Forward-only baseline: the first run records the current EOF and ships NOTHING
(no backfill flood); `--from-start` overrides. Best-effort everywhere: an
unreadable file or a failed POST just retries next run.

Usage:
    python3 scripts/relay_provider_log.py [--max-lines 500] [--from-start] [--dry-run]
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

STATE_DIR = os.path.expanduser(os.environ.get("S4L_STATE_DIR", "~/.social-autoposter-mcp"))
PROVIDER_LOG = os.path.join(STATE_DIR, "claude-queue", "provider.log")
STATE_PATH = os.path.join(STATE_DIR, "provider-relay-state.json")
LOCK_PATH = os.path.join(STATE_DIR, "provider-relay.lock")

LOG_BASE = (os.environ.get("AUTOPOSTER_LOG_BASE") or "https://app.s4l.ai").rstrip("/")
POST_BATCH = 200        # relay accepts 1-200 lines per POST
MAX_LINE = 7500         # relay caps at 8192; leave headroom for the envelope
CONTEXT = "queue-provider"
LOG_TYPE = "client-pipeline"


def _install_header():
    ident = os.path.join(os.path.dirname(os.path.abspath(__file__)), "identity.py")
    if not os.path.exists(ident):
        return None
    try:
        out = subprocess.run([sys.executable, ident, "header"],
                             capture_output=True, text=True, timeout=15)
        h = (out.stdout or "").strip()
        return h if out.returncode == 0 and h else None
    except Exception:
        return None


def _post(lines, header):
    body = json.dumps({"lines": lines}).encode("utf-8")
    req = urllib.request.Request(
        f"{LOG_BASE}/api/v1/installations/logs",
        data=body,
        headers={"X-Installation": header, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[provider-relay] POST failed: {e}", file=sys.stderr)
        return False


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-lines", type=int, default=500,
                    help="cap on lines shipped this run (remainder ships next run)")
    ap.add_argument("--from-start", action="store_true",
                    help="ship from the start of the file, overriding the forward-only baseline")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args()

    if not os.path.exists(PROVIDER_LOG):
        return 0

    # Single-flight: skip silently if another run holds the lock.
    lock_fd = None
    if not ns.dry_run:
        try:
            import fcntl
            os.makedirs(STATE_DIR, exist_ok=True)
            lock_fd = open(LOCK_PATH, "w")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            return 0  # another instance is running

    state = _load_state()
    try:
        size = os.path.getsize(PROVIDER_LOG)
    except OSError:
        return 0
    offset = state.get("offset")
    inode = state.get("inode")
    try:
        cur_inode = os.stat(PROVIDER_LOG).st_ino
    except OSError:
        cur_inode = None

    # First run (or rotated/truncated file): baseline forward-only unless --from-start.
    if ns.from_start:
        offset = 0
    elif offset is None or inode != cur_inode or offset > size:
        _save_state({"offset": size, "inode": cur_inode})
        return 0

    try:
        with open(PROVIDER_LOG, "r", errors="replace") as f:
            f.seek(offset)
            chunk = f.read(2_000_000)  # bounded read
            new_offset = f.tell()
    except Exception:
        return 0

    raw_lines = [ln for ln in chunk.split("\n") if ln.strip()]
    if not raw_lines:
        _save_state({"offset": new_offset, "inode": cur_inode})
        return 0

    raw_lines = raw_lines[: ns.max_lines]
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    records = [{
        "logType": LOG_TYPE,
        "context": CONTEXT,
        "stream": "stdout",
        "client_ts": now_iso,
        "message": ln[:MAX_LINE],
    } for ln in raw_lines]

    if ns.dry_run:
        for r in records[:20]:
            print(f"[provider-relay] (dry-run) {r['message'][:140]}")
        print(f"[provider-relay] (dry-run) would ship {len(records)} line(s)")
        return 0

    header = _install_header()
    if not header:
        print("[provider-relay] no X-Installation header; skipping", file=sys.stderr)
        return 0

    shipped = 0
    for i in range(0, len(records), POST_BATCH):
        batch = records[i:i + POST_BATCH]
        if _post(batch, header):
            shipped += len(batch)
        else:
            break  # stop on failure; retry from the saved offset next run

    # Only advance the offset by what we actually shipped, so a mid-run POST failure
    # re-ships the un-acked tail next run instead of dropping it.
    if shipped >= len(raw_lines):
        # Shipped everything we read (and possibly capped by max_lines == read count).
        _save_state({"offset": new_offset, "inode": cur_inode})
    elif shipped > 0:
        # Shipped only a prefix; advance past exactly those lines (+ their newlines).
        partial = "\n".join(raw_lines[:shipped]) + "\n"
        _save_state({"offset": offset + len(partial.encode("utf-8")), "inode": cur_inode})
    print(f"[provider-relay] shipped {shipped}/{len(records)} provider.log line(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
