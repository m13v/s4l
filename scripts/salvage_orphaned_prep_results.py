#!/usr/bin/env python3
"""salvage_orphaned_prep_results.py — recover twitter-prep drafts that were
stranded when the PRODUCER cycle died after the worker wrote its result but before
consuming it.

Mechanism: a producer (claude_job.py) that consumes a result os.remove()s it, so
any file that SURVIVES in claude-queue/result/ past the producer's max wait is
orphaned — the worker's drafts exist but were never turned into a plan / merged
into review cards. This scans for such files and merges their candidates into the
review queue via merge_review_queue.py, then renames them .salvaged so they are
not re-processed.

Safe by construction:
  - Only touches results OLDER than the queue timeout (default + buffer), so no
    live producer can still be polling for that job_id.
  - merge_review_queue.py dedupes by (thread_url), so a re-run or an overlap with a
    late-arriving producer cannot create duplicate cards.
  - Best-effort: any single failure is logged and skipped; never raises.

Degradation vs a normal cycle: salvaged candidates skip the cycle's post-provider
top-N selection (so MORE cards, which is fine) and lack the tail-link / experiments
arm stamp that run-twitter-cycle.sh's plan writer adds after the provider returns.
The reply text itself is complete. A salvaged card is strictly better than a lost
draft.

Usage:
    python3 scripts/salvage_orphaned_prep_results.py            # automated (safe age gate)
    python3 scripts/salvage_orphaned_prep_results.py --age-min 5 --dry-run
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from claude_job import result_dir, DEFAULT_TIMEOUT_S, _plog  # reuse exact dir + provider.log writer
except Exception:  # standalone fallbacks
    DEFAULT_TIMEOUT_S = int(os.environ.get("S4L_CLAUDE_QUEUE_TIMEOUT", "1800"))

    def _state_dir():
        return os.environ.get("S4L_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".social-autoposter-mcp")

    def result_dir():
        return os.path.join(_state_dir(), "claude-queue", "result")

    def _plog(msg):
        try:
            p = os.path.join(_state_dir(), "claude-queue", "provider.log")
            with open(p, "a") as f:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                f.write(f"{ts} pid={os.getpid()} {msg}\n")
        except Exception:
            pass


def _is_prep_result(obj):
    """True iff obj looks like a twitter-prep result (drafted candidates).

    "reply_text" was the single-draft field before the 2026-07-07/08 two-draft
    redesign (draft_a_text/draft_b_text per candidate, no single recommended
    reply). Checking only "reply_text" made every post-redesign orphaned
    result silently misclassified as non-prep and marked .skipped instead of
    recovered — the exact "worker drafted but no card" bug this script exists
    to prevent. Accept either field so both old and current schema results
    are recognized.
    """
    if not isinstance(obj, dict):
        return False
    cands = obj.get("candidates")
    if not isinstance(cands, list) or not cands:
        return False
    c0 = cands[0]
    if not isinstance(c0, dict):
        return False
    has_text = "reply_text" in c0 or "draft_a_text" in c0
    return has_text and ("candidate_url" in c0 or "candidate_id" in c0)


def main():
    ap = argparse.ArgumentParser(description="Merge orphaned twitter-prep results into the review queue.")
    ap.add_argument("--age-min", type=float, default=(DEFAULT_TIMEOUT_S / 60.0 + 5),
                    help="only salvage results older than this many minutes "
                         "(default = queue timeout + 5, so no live producer is still polling)")
    ap.add_argument("--max-age-hours", type=float,
                    default=float(os.environ.get("S4L_SALVAGE_MAX_AGE_HOURS", "6")),
                    help="do NOT salvage results older than this (stale threads not worth carding); "
                         "they are renamed .stale so they stop being rescanned")
    ap.add_argument("--repo-dir", default=os.environ.get("S4L_REPO_DIR") or os.path.dirname(HERE))
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args()

    rdir = result_dir()
    if not os.path.isdir(rdir):
        return 0
    merge = os.path.join(ns.repo_dir, "scripts", "merge_review_queue.py")
    now = time.time()
    young_cutoff = now - ns.age_min * 60.0
    stale_cutoff = now - ns.max_age_hours * 3600.0
    scanned = salvaged = skipped = stale = 0

    for name in sorted(os.listdir(rdir)):
        if not name.endswith(".json"):
            continue  # skip already-handled .salvaged / .skipped / .stale
        path = os.path.join(rdir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime > young_cutoff:
            continue  # too fresh: a producer may still be polling for this job
        if st.st_mtime < stale_cutoff:
            # too old: the threads have moved on; retire it so we stop rescanning.
            stale += 1
            if not ns.dry_run:
                try:
                    os.rename(path, path + ".stale")
                except OSError:
                    pass
            continue
        scanned += 1
        job_id = name[:-5]
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        obj = d.get("result") if isinstance(d, dict) else None
        if obj is None and isinstance(d, dict) and "candidates" in d:
            obj = d
        if not _is_prep_result(obj):
            skipped += 1
            if not ns.dry_run:
                try:
                    os.rename(path, path + ".skipped")
                except OSError:
                    pass
            continue

        n = len(obj["candidates"])
        age_min = (now - st.st_mtime) / 60.0
        _plog(f"[salvage] ORPHAN prep result job {job_id}: producer never consumed it "
              f"({n} drafts, {age_min:.0f}m old) -> merging into review queue")
        if ns.dry_run:
            print(f"[salvage] (dry-run) would merge {n} drafts from job {job_id} ({age_min:.0f}m old)")
            continue

        plan_path = os.path.join("/tmp", f"salvage_plan_{job_id}.json")
        plan = {
            "candidates": obj["candidates"],
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "salvaged_from_job": job_id,
        }
        try:
            with open(plan_path, "w") as f:
                json.dump(plan, f)
        except Exception as e:
            _plog(f"[salvage] job {job_id}: could not write plan: {e}")
            continue

        try:
            r = subprocess.run([sys.executable, merge, "--plan", plan_path],
                               capture_output=True, text=True, timeout=180)
        except Exception as e:
            _plog(f"[salvage] job {job_id}: merge subprocess errored: {e}")
            continue

        if r.returncode == 0:
            salvaged += 1
            try:
                os.rename(path, path + ".salvaged")
            except OSError:
                pass
            _plog(f"[salvage] job {job_id}: merged {n} orphaned drafts into review queue")
            print(f"[salvage] recovered {n} orphaned drafts from job {job_id}")
        else:
            _plog(f"[salvage] job {job_id}: merge failed rc={r.returncode}: {(r.stderr or '')[:200]}")

    if scanned or stale:
        print(f"[salvage] scanned={scanned} salvaged={salvaged} skipped_nonprep={skipped} retired_stale={stale}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
