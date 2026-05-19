#!/usr/bin/env python3
"""dm_db_update.py — single-row PATCH helper for the `dms` table.

Created 2026-05-18 as the replacement for the three inline
`psql "$DATABASE_URL" -c "UPDATE dms SET ..."` lines the dm-outreach-*
shell pipelines used to embed in their Claude prompts. The LLM is told to
shell out to this script instead of psql so all DB writes route through
/api/v1/dms/:id and we keep the credentials surface inside the helper.

Usage:
    python3 scripts/dm_db_update.py --dm-id N \
        [--status pending|sent|error|skipped|...] \
        [--skip-reason TEXT] \
        [--claude-session-id UUID]

At least one of --status / --skip-reason / --claude-session-id is
required. Status and skip_reason can be set independently (the PATCH
route uses COALESCE for every field, so omitted fields stay unchanged).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_patch  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dm-id", type=int, required=True)
    ap.add_argument("--status")
    ap.add_argument("--skip-reason")
    ap.add_argument("--claude-session-id")
    args = ap.parse_args()

    body: dict = {}
    if args.status:
        body["status"] = args.status
    if args.skip_reason:
        body["skip_reason"] = args.skip_reason
    if args.claude_session_id:
        body["claude_session_id"] = args.claude_session_id

    if not body:
        print(
            "dm_db_update: nothing to update; pass at least --status, "
            "--skip-reason, or --claude-session-id",
            file=sys.stderr,
        )
        return 1

    try:
        resp = api_patch(f"/api/v1/dms/{args.dm_id}", body)
    except SystemExit as e:
        print(f"dm_db_update: PATCH /api/v1/dms/{args.dm_id} failed: {e}", file=sys.stderr)
        return 1

    dm = (resp.get("data") or {}).get("dm") or {}
    print(
        f"dm_db_update: dm #{args.dm_id} status={dm.get('status')!r} "
        f"skip_reason={dm.get('skip_reason')!r}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
