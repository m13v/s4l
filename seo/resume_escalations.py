#!/usr/bin/env python3
"""Resume any seo_escalations rows in 'replied' state.

Called once per cron tick from cron_seo.sh, before the per-product lanes
fan out. For each replied escalation, we shell out to:

    python3 seo/generate_page.py --resume-escalation <id>

generate_page.py loads the row, prepends the human_reply into the prompt
as `=== HUMAN GUIDANCE ===`, runs Claude, and on success calls
escalate.py mark-resumed which flips status='resumed'.

If a resume run fails, the escalation row stays in 'replied' state and
we will retry on the next tick. To stop retrying, cancel the row:

    python3 seo/escalate.py cancel --id <id> --note "..."

Usage:
    python3 seo/resume_escalations.py            # resume all replied rows
    python3 seo/resume_escalations.py --dry-run  # list, do not invoke
    python3 seo/resume_escalations.py --id 7     # single row
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "scripts"))
from http_api import api_get, load_env  # noqa: E402

load_env()

ESCALATION_LOG = SCRIPT_DIR / "escalations.log"


def _append_log(line):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(ESCALATION_LOG, "a") as f:
            f.write(f"{ts} {line}\n")
    except OSError:
        pass


def list_replied(only_id=None):
    resp = api_get("/api/v1/seo/escalations",
                   query={"mode": "list", "status": "replied", "limit": 500})
    rows = (resp.get("data") or {}).get("rows") or []
    rows = sorted(rows, key=lambda r: str(r.get("asked_at") or ""))
    if only_id is not None:
        rows = [r for r in rows if r.get("id") == only_id]
    return [(r.get("id"), r.get("product"), r.get("keyword"), r.get("slug")) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="List candidates without invoking generate_page.py")
    ap.add_argument("--id", type=int, default=None,
                    help="Resume a single escalation by id")
    ap.add_argument("--timeout", type=int, default=3600,
                    help="Per-resume timeout in seconds (default 3600)")
    args = ap.parse_args()

    rows = list_replied(only_id=args.id)

    if not rows:
        print("No replied escalations to resume.")
        return 0

    print(f"Found {len(rows)} replied escalation(s) to resume.")
    successes = 0
    failures = 0

    for eid, product, keyword, slug in rows:
        print(f"\n=== Resuming escalation #{eid} ({product} / {keyword}) ===")
        if args.dry_run:
            continue

        # We hand off the row id only; generate_page.py loads everything else
        # from seo_escalations so we cannot accidentally drift.
        cmd = [
            sys.executable, str(SCRIPT_DIR / "generate_page.py"),
            "--resume-escalation", str(eid),
        ]
        try:
            proc = subprocess.run(cmd, timeout=args.timeout)
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT after {args.timeout}s")
            ok = False
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            ok = False

        if ok:
            successes += 1
            _append_log(f"resume_attempt #{eid} product={product} outcome=success")
        else:
            failures += 1
            _append_log(f"resume_attempt #{eid} product={product} outcome=failure")
            print(f"  Resume failed. Row #{eid} stays in 'replied' state and "
                  f"will retry next tick. Cancel with: "
                  f"python3 seo/escalate.py cancel --id {eid} --note \"...\"")

    print(f"\nDone. successes={successes} failures={failures} total={len(rows)}")
    # Non-zero exit if anything failed, so cron_seo.sh can log it; but we
    # do NOT abort the rest of the tick -- the caller uses `|| true`.
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
