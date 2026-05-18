#!/usr/bin/env python3
"""Track per-cycle phase in twitter_batches so salvage can be phase-aware.

Owning run-twitter-cycle.sh stamps phase transitions; the NEXT cycle's
Phase 0 reads twitter_batches.current_phase + phase_started_at to decide
salvage timing per-phase instead of a flat 20-min wall-clock budget.

The flat cutoff salvaged live cycles mid Phase 2b-gen (SEO landing-page
build, 10-40 min), creating phantom failures and double-prep cost. See
the migration file 2026-05-01_twitter_batches.sql for context.

Usage:
    twitter_batch_phase.py start   <batch_id> --phase <name>
    twitter_batch_phase.py advance <batch_id> --phase <name>
    twitter_batch_phase.py end     <batch_id>

start    upserts the row (used at cycle init even if a stale row remains
         from a SIGKILLed prior run with the same batch_id, which is
         unlikely but harmless).
advance  updates current_phase + phase_started_at; auto-creates the row
         if start was missed for any reason.
end      deletes the row on clean cycle exit. SIGKILL/OOM intentionally
         leaves the row stale so the next cycle's Phase 0 can salvage
         our pending candidates after the per-phase budget elapses.

The owning shell wraps lock.sh's EXIT trap to call `end` on clean exit;
see run-twitter-cycle.sh _sa_combined_exit.

Migrated 2026-05-18: DB writes now go through the s4l.ai HTTP API
(scripts/http_api.py -> /api/v1/twitter-batches) instead of psycopg2.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_post  # noqa: E402

VALID_PHASES = {
    "phase0",
    "phase1",
    "phase2a",
    "phase2b-prep",
    "phase2b-gen",
    "phase2b-post",
}


def _validate_phase(phase: str) -> None:
    if phase not in VALID_PHASES:
        print(
            f"twitter_batch_phase: invalid phase {phase!r}; expected one of {sorted(VALID_PHASES)}",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_start(batch_id: str, phase: str) -> None:
    _validate_phase(phase)
    api_post(
        "/api/v1/twitter-batches",
        {
            "action": "start",
            "batch_id": batch_id,
            "phase": phase,
            "owner_pid": os.getppid(),
            "owner_host": socket.gethostname(),
        },
    )
    print(f"twitter_batches: started {batch_id} phase={phase}")


def cmd_advance(batch_id: str, phase: str) -> None:
    _validate_phase(phase)
    api_post(
        "/api/v1/twitter-batches",
        {
            "action": "advance",
            "batch_id": batch_id,
            "phase": phase,
            "owner_pid": os.getppid(),
            "owner_host": socket.gethostname(),
        },
    )
    print(f"twitter_batches: advanced {batch_id} phase={phase}")


def cmd_end(batch_id: str) -> None:
    api_post(
        "/api/v1/twitter-batches",
        {"action": "end", "batch_id": batch_id},
    )
    print(f"twitter_batches: ended {batch_id}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Track per-cycle phase in twitter_batches.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("batch_id")
    p_start.add_argument("--phase", required=True)

    p_adv = sub.add_parser("advance")
    p_adv.add_argument("batch_id")
    p_adv.add_argument("--phase", required=True)

    p_end = sub.add_parser("end")
    p_end.add_argument("batch_id")

    args = ap.parse_args()

    if args.cmd == "start":
        cmd_start(args.batch_id, args.phase)
    elif args.cmd == "advance":
        cmd_advance(args.batch_id, args.phase)
    elif args.cmd == "end":
        cmd_end(args.batch_id)


if __name__ == "__main__":
    main()
