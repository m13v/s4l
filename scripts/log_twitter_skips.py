#!/usr/bin/env python3
"""
log_twitter_skips.py

Writes Phase 2b skip decisions back to twitter_candidates so we can audit why
each pre-scored candidate was rejected without ever posting.

Input shape (stdin or --file):

    {
      "skips": [
        {"candidate_id": 1234, "reason": "off-topic for Mediar"},
        {"candidate_id": 1235, "reason": "thread is toxic crypto promo",
         "proposed_excludes": ["cricket", "kohli", "ipl"]}
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

Optional `proposed_excludes` (per skip): each term is fed into
project_excludes.propose() with platform='twitter' and project read from the
candidate's matched_project column. Validation, reservation guards, and the
distinct-batch activation gate all live in project_excludes.py — this script
just forwards the proposals.

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
import project_excludes as pe_mod  # noqa: E402
from http_api import api_patch  # noqa: E402


REASON_MAX = 500
EXCLUDES_PER_SKIP_CAP = 3   # cap proposed_excludes per rejected entry


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

    updated = 0
    no_match = 0
    bad = 0
    seen_ids = set()
    excludes_pending = []   # collect (candidate_id, project, term, batch_id, reason) tuples

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

        # Server-side WHERE: status='pending' (+ optional batch_id guard) lives
        # inside /api/v1/twitter-candidates/by-id action=mark_skipped. 404 is
        # the "row not pending / batch mismatch" signal; we don't fail the
        # whole batch on it.
        body = {
            "id": cid,
            "action": "mark_skipped",
            "reason": reason,
        }
        if args.require_batch_id:
            body["require_batch_id"] = args.require_batch_id
        resp = api_patch(
            "/api/v1/twitter-candidates/by-id",
            body,
            ok_on_404=True,
        )
        if resp.get("_not_found"):
            no_match += 1
            row_match = False
        else:
            updated += 1
            row_match = True

        # Stage proposed_excludes for THIS skip (only if status flipped to skipped).
        # The PATCH response carries the full updated row, so we extract
        # matched_project + batch_id from it instead of issuing a second GET.
        proposed = entry.get("proposed_excludes")
        if proposed and isinstance(proposed, list) and row_match:
            data = resp.get("data") or {}
            cand_row = data.get("candidate") or {}
            project = cand_row.get("matched_project")
            cand_batch = cand_row.get("batch_id") or args.require_batch_id
            if project:
                for term in proposed[:EXCLUDES_PER_SKIP_CAP]:
                    excludes_pending.append((cid, project, term, cand_batch, reason))

    # Persist proposed excludes via project_excludes.propose(). Each call has
    # its own DB connection (cheap, the volume is tiny: <=POST_LIMIT*EXCLUDES_PER_SKIP_CAP per cycle).
    pe_inserted = 0
    pe_bumped = 0
    pe_dup = 0
    pe_rejected_invalid = 0
    pe_rejected_reserved = 0
    for cid, project, term, batch_id, reason in excludes_pending:
        try:
            out = pe_mod.propose(
                platform="twitter",
                project=project,
                term=term,
                candidate_id=cid,
                batch_id=batch_id,
                reason=reason,
            )
        except Exception as e:
            print(f"log_twitter_skips: propose error for {project}/{term}: {e}", file=sys.stderr)
            continue
        action = out.get("action")
        if action == "inserted":
            pe_inserted += 1
        elif action == "bumped":
            pe_bumped += 1
        elif action == "duplicate_batch":
            pe_dup += 1
        elif action == "rejected_invalid":
            pe_rejected_invalid += 1
        elif action == "rejected_reserved":
            pe_rejected_reserved += 1

    print(
        f"log_twitter_skips: updated={updated} no_match={no_match} bad_entries={bad} input={len(skips)}"
    )
    if excludes_pending:
        print(
            f"log_twitter_skips: excludes proposed={len(excludes_pending)} "
            f"inserted={pe_inserted} bumped={pe_bumped} dup_batch={pe_dup} "
            f"invalid={pe_rejected_invalid} reserved={pe_rejected_reserved}"
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
