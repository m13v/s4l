#!/usr/bin/env python3
"""admin_fetch_install_config.py — LOCAL OPERATOR USE ONLY.

Excluded from the published npm package (see package.json `files`), same
convention as db_direct.py: this needs the admin DATABASE_URL, which
customers never have.

Pulls ANOTHER install's real config.json + persona_corpus.txt out of
installations.state_snapshot (synced from their own machine) via direct
Postgres access, and materializes them into a local directory shaped for
S4L_SANDBOX_CONFIG_DIR (see skill/run-twitter-cycle.sh's sandbox override).

This deliberately bypasses GET /api/v1/installations/state-snapshot: that
route is correctly scoped to the CALLER's own install_id and will never
return another install's data — as it should not. This script exists
specifically for internal QA (testing a draft-prompt change against a real
customer's real voice/persona on real historical threads, never posting),
and only works with the admin DATABASE_URL a customer install never has.

Usage:
  python3 scripts/admin_fetch_install_config.py \\
      --install-id ba6519ca-edaf-4fee-95b9-446da86bd346 \\
      --out-dir /tmp/sandbox_karol
  S4L_SANDBOX_CONFIG_DIR=/tmp/sandbox_karol S4L_SANDBOX_CANDIDATES_FILE=... \\
      bash skill/run-twitter-cycle.sh
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_conn  # noqa: E402


def fetch_snapshot(install_id: str):
    db = get_conn()
    try:
        cur = db.execute(
            "SELECT state_snapshot, hostname, git_email FROM installations WHERE id = %s",
            [install_id],
        )
        row = cur.fetchone()
    finally:
        db.close()
    if not row:
        print(f"[admin_fetch_install_config] no installation found for id {install_id}", file=sys.stderr)
        sys.exit(1)
    snap = row["state_snapshot"]
    if isinstance(snap, str):
        snap = json.loads(snap)
    if not snap:
        print(
            f"[admin_fetch_install_config] installation {install_id} "
            f"({row.get('hostname')}) has no state_snapshot yet",
            file=sys.stderr,
        )
        sys.exit(1)
    return snap, row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install-id", required=True)
    ap.add_argument("--out-dir", required=True, help="directory to write config.json + persona_corpus.txt into")
    args = ap.parse_args()

    snap, row = fetch_snapshot(args.install_id)
    config = (snap or {}).get("config")
    persona_corpus = (snap or {}).get("persona_corpus") or ""
    if not config:
        print(
            f"[admin_fetch_install_config] state_snapshot for {args.install_id} has no 'config' key",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(json.dumps(config, indent=2))
    (out_dir / "persona_corpus.txt").write_text(persona_corpus)

    print(
        f"[admin_fetch_install_config] wrote {out_dir}/config.json + persona_corpus.txt "
        f"for install {args.install_id} ({row.get('hostname')}, {row.get('git_email')})",
        file=sys.stderr,
    )
    print(f"  {len(json.dumps(config))} bytes config, {len(persona_corpus)} bytes persona corpus", file=sys.stderr)
    print(f"  projects: {[p.get('name') for p in config.get('projects', [])]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
