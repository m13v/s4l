#!/usr/bin/env python3
"""Locked field-patcher for the review-queue store.

The review store (review-queue.json) has three writers: the menubar
(s4l_state._store_update), merge_review_queue.py, and the MCP server. The
first two hold an fcntl.flock on `<store>.lock` around their read-modify-write;
the MCP server is Node, which has no native flock, so it routes its store
writes through THIS script instead of writing the file itself. That closes the
last unlocked writer (the last-writer-wins race that erased posted stamps on
2026-07-17 — six live replies rendered as duplicate_thread_pre_post kills).

Protocol-compatible with s4l_state._store_update: same lock file, LOCK_EX for
the whole read-mutate-replace span, atomic os.replace. fcntl (not an O_EXCL
lockfile) on purpose: the kernel drops the lock when the holder dies, so there
is no stale-lock stealing logic to get wrong.

Usage: store_patch.py <patches.json>   (or '-' to read the JSON from stdin)

Input shape:
  {"patches": [
      {"candidate_id": 123,          # match ALL rows with this id (ids are NOT
                                     # unique: sandbox reruns/variant drafts
                                     # append sibling rows; a stamp that hits
                                     # only one sibling leaves a drainable twin)
       "n": 4,                       # 1-based index fallback when candidate_id
                                     # is absent/None (legacy rows)
       "set":   {"approved": true},  # fields to assign
       "unset": ["discard_reason"]}  # fields to delete
  ]}

Merge rules (mirror mergeApprovedStampsIntoStore):
  - posted is sticky: a patch may set posted=true, but `set.terminal=true` is
    ignored on a row whose posted is already true, and posted is never unset.
Prints {"ok": true, "patched": N} on stdout. Exit 0 even when N=0 (a patch
matching nothing is not an error; the row may have been absorbed elsewhere).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys


def store_path() -> str:
    state_dir = os.environ.get("S4L_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )
    return os.path.join(state_dir, "review-queue.json")


def _match_rows(cands: list, patch: dict) -> list:
    cid = patch.get("candidate_id")
    if cid is not None:
        rows = [c for c in cands if c.get("candidate_id") == cid]
        if rows:
            return rows
    n = patch.get("n")
    if isinstance(n, int) and 1 <= n <= len(cands):
        return [cands[n - 1]]
    return []


def apply_patches(data: dict, patches: list) -> int:
    cands = data.get("candidates") or []
    patched = 0
    for p in patches:
        for c in _match_rows(cands, p):
            sets = dict(p.get("set") or {})
            if c.get("posted") is True:
                sets.pop("terminal", None)
                sets.pop("terminal_reason", None)
            for k, v in sets.items():
                if k == "posted" and v is not True and c.get("posted") is True:
                    continue  # posted is sticky
                c[k] = v
            for k in p.get("unset") or []:
                if k == "posted":
                    continue
                c.pop(k, None)
            patched += 1
    return patched


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "-"
    raw = sys.stdin.read() if src == "-" else open(src).read()
    patches = (json.loads(raw) or {}).get("patches") or []
    sp = store_path()
    with open(sp + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            try:
                with open(sp) as f:
                    data = json.load(f)
            except Exception:
                data = {"candidates": []}
            patched = apply_patches(data, patches)
            tmp = f"{sp}.tmp.{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, sp)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
    print(json.dumps({"ok": True, "patched": patched}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
