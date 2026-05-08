#!/usr/bin/env python3
"""
log_twitter_skips.py

Writes Phase 2b skip decisions back to twitter_candidates so we can audit why
each pre-scored candidate was rejected without ever posting.

Input shape (stdin or --file):

    {
      "skips": [
        {"candidate_id": 1234, "reason": "off-topic for Mediar"},
        {"candidate_id": 1235, "reason": "thread is toxic crypto promo"}
      ]
    }

Or a bare list of skip objects (same fields).

Behavior per row:
    UPDATE twitter_candidates
       SET status      = 'skipped',
           skip_reason = <reason, trimmed to 500 chars>,
           skipped_at  = NOW()
     WHERE id = <candidate_id>
       AND status = 'pending';

Pending guard prevents clobbering rows Phase 2b-post already flipped to
'posted', or rows Phase 0 will salvage on the next cycle. We deliberately do
NOT touch rows Claude omitted from BOTH chosen and rejected arrays; those are
treated as "not reviewed" and stay pending so the salvage path can re-judge
them next cycle.

Exit codes:
    0 = ok (even if zero rows updated; script is idempotent)
    1 = malformed input or DB error
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


REASON_MAX = 500


def _coerce_payload(raw):
    """Accept either {"skips": [...]} or a bare list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        skips = raw.get("skips")
        if isinstance(skips, list):
            return skips
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Read skip JSON from this file instead of stdin")
    parser.add_argument(
        "--require-batch-id",
        help="If set, only update candidates whose batch_id matches this value (extra safety)",
    )
    args = parser.parse_args()

    if args.file:
        with open(args.file) as f:
            raw = json.load(f)
    else:
        text = sys.stdin.read().strip()
        if not text:
            print("log_twitter_skips: empty stdin; nothing to do")
            return 0
        raw = json.loads(text)

    skips = _coerce_payload(raw)
    if not skips:
        print("log_twitter_skips: no skip entries; nothing to do")
        return 0

    conn = dbmod.get_conn()

    updated = 0
    no_match = 0
    bad = 0
    seen_ids = set()

    for entry in skips:
        if not isinstance(entry, dict):
            bad += 1
            continue

        cid = entry.get("candidate_id")
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            bad += 1
            continue

        # Dedupe within this batch in case the model emits the same id twice.
        if cid in seen_ids:
            continue
        seen_ids.add(cid)

        reason = (entry.get("reason") or "").strip()
        if not reason:
            reason = "unspecified"
        if len(reason) > REASON_MAX:
            reason = reason[: REASON_MAX - 1] + "…"

        if args.require_batch_id:
            cur = conn.execute(
                """
                UPDATE twitter_candidates
                   SET status='skipped',
                       skip_reason=%s,
                       skipped_at=NOW()
                 WHERE id=%s
                   AND status='pending'
                   AND batch_id=%s
                """,
                [reason, cid, args.require_batch_id],
            )
        else:
            cur = conn.execute(
                """
                UPDATE twitter_candidates
                   SET status='skipped',
                       skip_reason=%s,
                       skipped_at=NOW()
                 WHERE id=%s
                   AND status='pending'
                """,
                [reason, cid],
            )

        rc = getattr(cur, "rowcount", None)
        if rc is None:
            # Fall back: a follow-up SELECT to confirm.
            row = conn.execute(
                "SELECT status FROM twitter_candidates WHERE id=%s",
                [cid],
            ).fetchone()
            if row and row[0] == "skipped":
                updated += 1
            else:
                no_match += 1
        elif rc >= 1:
            updated += rc
        else:
            no_match += 1

    conn.commit()
    conn.close()

    print(
        f"log_twitter_skips: updated={updated} no_match={no_match} bad_entries={bad} input={len(skips)}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except json.JSONDecodeError as e:
        print(f"log_twitter_skips: input is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"log_twitter_skips: error: {e}", file=sys.stderr)
        sys.exit(1)
