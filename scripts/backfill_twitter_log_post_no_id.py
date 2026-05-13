"""Backfill `posts` rows for Twitter replies that landed on x.com but failed
to be logged due to the `generation_trace exceeds 64 KB` API rejection
(2026-05-12 → 2026-05-13).

Background: when the generation_trace JSONB column landed on 2026-05-12, the
server-side cap (64 KB) was tighter than the actual size of Twitter cycle
traces (~85 KB). Every POST /api/v1/posts came back HTTP 400 bad_request, so
log_post.py returned no post_id and twitter_post_plan.py marked the
candidate `skipped` (correctly, to avoid double-posting on x.com) while
reporting `log_post_no_id` to the run summary. Net effect: the replies WERE
posted, but the database forgot them, dashboards showed posted=0, and
~50 posts since 2026-05-12 have no row.

This script reconstructs the missing rows from `skill/logs/twitter-cycle-*.log`:

  1. Walks each cycle log.
  2. Finds each `[post] candidate N log_post.py did not return post_id` event.
  3. Walks backward to extract the reply JSON ({reply_url, final_text,
     tweet_url, applied_campaigns}), the [gen] line (link_source), and the
     [post] candidate line (project name via the surrounding context).
  4. Pulls the candidate's project_name / thread_author / thread_text /
     engagement_style / language out of the structured_output.candidates
     block emitted by the Phase 2b-prep Claude result (logged verbatim).
  5. Calls log_post.py without --generation-trace (sidesteps the 64 KB cap
     entirely until the website cap-bump finishes deploying).

Idempotent: the API dedups on (platform, thread_url) so re-runs no-op for
already-backfilled posts.

Usage:
    python3 scripts/backfill_twitter_log_post_no_id.py [--dry-run]
        [--since 2026-05-12] [--logs-dir skill/logs]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
LOG_POST = REPO / "scripts" / "log_post.py"

FAIL_LINE_RE = re.compile(
    r"\[post\] candidate (\d+) log_post\.py did not return post_id"
)
GEN_LINE_RE = re.compile(
    r"\[gen\] candidate_id=(\d+) link_url=\S+ source=(\S+)"
)
REPLY_STDOUT_START_RE = re.compile(r"\[post\]\[reply\.stdout\]")
POST_BLOCK_HDR_RE = re.compile(r"\[post\] candidate (\d+) -> posting")

# The Phase 2b-prep Claude response is logged verbatim as a single very long
# line containing a JSON envelope with the structured_output. We regex into
# it rather than json.loads-ing the whole thing because the line is wrapped
# inside the run log with other framing characters.
STRUCTURED_OUTPUT_RE = re.compile(
    r'"structured_output":\s*({.*?"candidates":\s*\[.*?\])\s*(?:,"rejected"|,"queries_used"|})',
    re.DOTALL,
)


def load_plan_candidates(log_text: str) -> dict[int, dict]:
    """Pull the {candidate_id -> plan_entry} map out of the Claude prep
    structured_output block embedded in the cycle log. Returns empty dict
    if the block is missing or unparseable (older logs, partial runs).
    """
    out: dict[int, dict] = {}
    # There can be more than one Claude turn in a cycle (scan + prep). The
    # one we care about is the prep step whose candidates have reply_text.
    # Walk every block; prefer the latest one with reply_text fields.
    for m in re.finditer(
        r'"structured_output":\s*\{(?P<body>.*?)\}\s*,\s*"terminal_reason"',
        log_text, re.DOTALL,
    ):
        body = "{" + m.group("body") + "}"
        try:
            obj = json.loads(body)
        except Exception:
            continue
        cands = obj.get("candidates") or []
        for c in cands:
            cid = c.get("candidate_id")
            if cid is None:
                continue
            # Prep-phase entries have reply_text + engagement_style. Scan-phase
            # entries don't. Prefer prep when both exist.
            existing = out.get(cid)
            if c.get("reply_text") or c.get("engagement_style"):
                out[cid] = c
            elif existing is None:
                out[cid] = c
    return out


def extract_reply_payload(lines: list[str], fail_idx: int) -> Optional[dict]:
    """Walk back from the fail line to find the most recent reply.stdout
    JSON block for this candidate. Returns parsed dict or None.
    """
    # The `[post][reply.stdout]` marker is followed by a JSON object spanning
    # several lines. Find the marker, then read until the matching closing
    # brace.
    marker_idx = None
    for i in range(fail_idx, max(-1, fail_idx - 200), -1):
        if REPLY_STDOUT_START_RE.search(lines[i]):
            marker_idx = i
            break
    if marker_idx is None:
        return None
    # JSON starts on the next line that begins with '{'
    json_start = None
    for i in range(marker_idx + 1, min(len(lines), marker_idx + 5)):
        if lines[i].lstrip().startswith("{"):
            json_start = i
            break
    if json_start is None:
        return None
    # Walk forward until depth returns to 0.
    depth = 0
    buf = []
    for i in range(json_start, min(len(lines), json_start + 60)):
        buf.append(lines[i])
        for ch in lines[i]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        if depth == 0:
            break
    try:
        return json.loads("\n".join(buf))
    except Exception:
        return None


def extract_link_source(lines: list[str], fail_idx: int, cid: int) -> Optional[str]:
    """Walk back to find the [gen] candidate_id=N ... source=X line."""
    for i in range(fail_idx, max(-1, fail_idx - 200), -1):
        m = GEN_LINE_RE.search(lines[i])
        if m and int(m.group(1)) == cid:
            return m.group(2)
    return None


def reconstruct_events(log_path: Path) -> list[dict]:
    """Walk one cycle log and return a list of backfill records."""
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return []
    plan = load_plan_candidates(text)
    lines = text.splitlines()
    out = []
    for idx, line in enumerate(lines):
        m = FAIL_LINE_RE.search(line)
        if not m:
            continue
        cid = int(m.group(1))
        reply = extract_reply_payload(lines, idx) or {}
        link_source = extract_link_source(lines, idx, cid)
        entry = plan.get(cid) or {}
        record = {
            "log": str(log_path.name),
            "candidate_id": cid,
            "thread_url": reply.get("tweet_url") or entry.get("candidate_url") or "",
            "our_url": reply.get("reply_url") or "",
            "our_content": reply.get("final_text") or entry.get("reply_text") or "",
            "project": entry.get("matched_project") or "",
            "thread_author": entry.get("thread_author") or "",
            "thread_title": entry.get("thread_text") or "",
            "engagement_style": entry.get("engagement_style") or "",
            "language": entry.get("language") or "",
            "link_source": link_source or "",
            "applied_campaigns": reply.get("applied_campaigns") or [],
        }
        out.append(record)
    return out


def call_log_post(rec: dict) -> tuple[bool, str]:
    """Invoke log_post.py for one backfill record. Returns (ok, message)."""
    if not rec["thread_url"] or not rec["our_url"] or not rec["our_content"]:
        return False, "missing required field(s)"
    if not rec["project"]:
        return False, "missing project (no plan data)"
    args = [
        sys.executable, str(LOG_POST),
        "--platform", "twitter",
        "--thread-url", rec["thread_url"],
        "--our-url", rec["our_url"],
        "--our-content", rec["our_content"],
        "--project", rec["project"],
        "--thread-author", rec["thread_author"],
        "--thread-title", rec["thread_title"],
    ]
    if rec["engagement_style"]:
        args += ["--engagement-style", rec["engagement_style"]]
    if rec["language"]:
        args += ["--language", rec["language"]]
    if rec["link_source"]:
        args += ["--link-source", rec["link_source"]]
    # CRITICAL: do NOT pass --generation-trace. We are bypassing the cap
    # entirely for backfill. The audit-trail loss for these ~50 rows is
    # acceptable; new posts post-cap-bump will carry their trace normally.
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=60,
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    # Parse last JSON line from stdout
    last_json = None
    for ln in stdout.splitlines()[::-1]:
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            try:
                last_json = json.loads(ln)
                break
            except Exception:
                continue
    if last_json is None:
        return False, f"no JSON in stdout. rc={proc.returncode} stderr={stderr[:200]!r}"
    if last_json.get("error") == "DUPLICATE_THREAD":
        return True, f"dup → existing post_id={last_json.get('existing_post_id')}"
    if last_json.get("logged"):
        return True, f"inserted post_id={last_json.get('post_id')}"
    return False, f"unexpected log_post.py response: {last_json}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", default=str(REPO / "skill" / "logs"))
    parser.add_argument("--since", default="2026-05-12",
                        help="date prefix; logs whose filenames sort >= "
                             "this string are included (YYYY-MM-DD).")
    parser.add_argument("--dry-run", action="store_true",
                        help="print plan, do not call log_post.py")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap on number of records to backfill (0 = no cap)")
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.is_dir():
        print(f"ERROR: logs_dir not found: {logs_dir}", file=sys.stderr)
        return 2

    # Cycle log filenames look like: twitter-cycle-2026-05-13_083005.log
    pat = re.compile(r"twitter-cycle-(\d{4}-\d{2}-\d{2})_")
    log_files = []
    for p in sorted(logs_dir.glob("twitter-cycle-*.log")):
        m = pat.search(p.name)
        if not m:
            continue
        if m.group(1) >= args.since:
            log_files.append(p)
    print(f"Scanning {len(log_files)} cycle logs since {args.since}…")

    all_records = []
    for p in log_files:
        recs = reconstruct_events(p)
        if recs:
            print(f"  {p.name}: {len(recs)} log_post_no_id event(s)")
        all_records.extend(recs)

    print(f"\nTotal backfill candidates: {len(all_records)}")

    if not all_records:
        return 0

    # Dedup by our_url so we never insert the same reply twice within this run.
    seen = set()
    unique = []
    for r in all_records:
        key = r["our_url"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    if len(unique) != len(all_records):
        print(f"De-duped by our_url: {len(all_records)} → {len(unique)}")

    if args.limit > 0:
        unique = unique[: args.limit]
        print(f"Capped at --limit={args.limit}")

    if args.dry_run:
        for r in unique:
            print(json.dumps({k: r[k] for k in (
                "log", "candidate_id", "project", "thread_url", "our_url",
                "engagement_style", "language", "link_source",
            )}, ensure_ascii=False))
        return 0

    n_ok = n_fail = n_dup = 0
    for r in unique:
        ok, msg = call_log_post(r)
        tag = "OK " if ok else "ERR"
        if ok and "dup" in msg:
            n_dup += 1
        elif ok:
            n_ok += 1
        else:
            n_fail += 1
        print(f"{tag} cid={r['candidate_id']} project={r['project']!r} "
              f"thread={r['thread_url']!s} | {msg}")
        time.sleep(0.2)  # gentle on the API

    print(f"\nDone. inserted={n_ok} dup={n_dup} failed={n_fail}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
