#!/usr/bin/env python3
"""Append a summary line to the persistent run monitor log.

Usage:
    python3 scripts/log_run.py --script post_reddit --posted 5 --skipped 2 --failed 0 --cost 3.45 --elapsed 600
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

LOG_PATH = os.path.expanduser("~/social-autoposter/skill/logs/run_monitor.log")


# Map a script-name prefix to the blocklist platform value. The blocklist
# table uses canonical 'x' for Twitter; everything else matches the prefix.
_PLATFORM_MAP = [
    ("reddit", "reddit"),
    ("twitter", "x"),
    ("linkedin", "linkedin"),
    ("github", "github_issues"),
    ("instagram", "instagram"),
]


def _platform_from_script(script_name):
    name = (script_name or "").lower()
    for prefix, plat in _PLATFORM_MAP:
        if prefix in name:
            return plat
    return None


def _detect_escape_hatch(script_name, elapsed_seconds):
    """Query /api/v1/blocklist for LLM/manual escape-hatch firings during
    this run window, filtered by the script's platform.

    Returns (count, details_list) where details_list contains 'handle:class'
    strings. Velocity-auto rows are EXCLUDED — they fire programmatically
    via the route.ts SQL path on every reply and would flood the pill on
    discovery cycles. We only surface model-judgment / operator-judgment
    classifications (bot, engagement_loop, manual_block).

    Fail-safe: any API error returns (0, []) so the run line still writes.
    """
    if not elapsed_seconds:
        return 0, []
    platform = _platform_from_script(script_name)
    if not platform:
        return 0, []
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get  # noqa: E402
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=float(elapsed_seconds) + 30)
        resp = api_get("/api/v1/blocklist", query={"platform": platform, "limit": 100})
        rows = ((resp or {}).get("data") or {}).get("blocklist") or []
        hits = []
        for r in rows:
            created_at = r.get("created_at")
            if not created_at:
                continue
            try:
                ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            except Exception:
                continue
            # Rows are ordered DESC by created_at, so once we cross the
            # cutoff every remaining row is older — short-circuit.
            if ts < cutoff:
                break
            classification = r.get("classification") or ""
            if classification not in ("bot", "engagement_loop", "manual_block"):
                continue
            handle = (r.get("handle") or "?").replace(",", "").replace(":", "")
            hits.append(f"{handle}:{classification}")
        return len(hits), hits
    except Exception:
        return 0, []


def main():
    parser = argparse.ArgumentParser(description="Log a run summary line")
    parser.add_argument("--script", required=True, help="Script name (e.g. post_reddit, engage_reddit)")
    parser.add_argument("--posted", type=int, default=0, help="Number of successful posts")
    parser.add_argument("--skipped", type=int, default=0, help="Number of skipped items")
    parser.add_argument("--failed", type=int, default=0, help="Number of failures")
    parser.add_argument("--cost", type=float, default=0.0, help="Total cost in USD")
    parser.add_argument("--elapsed", type=float, default=0.0, help="Elapsed time in seconds")
    parser.add_argument("--model", default="", help="Dominant Claude model id used in the run (optional)")
    parser.add_argument("--replies-refreshed", type=int, default=0,
                        help="Number of per-reply stat rows refreshed in this run "
                             "(stats_*, engage_github). Surfaces as a separate pill "
                             "in the dashboard Jobs table.")
    parser.add_argument("--checked", type=int, default=0,
                        help="Stats jobs only: rows the run actually hit the "
                             "platform API for (Reddit JSON, fxtwitter, LinkedIn "
                             "feed scrape, etc.). Excludes skipped-as-fresh and "
                             "skipped-as-stable. Renders as 'checked' pill.")
    parser.add_argument("--updated", type=int, default=0,
                        help="Stats jobs only: legacy/back-compat field. Pre-2026-05-18 "
                             "this was 'rows where any tracked metric moved' but it "
                             "silently summed in Step 1 view-scrape counts too. Use "
                             "`--changed` for the new clean semantics; keep `--updated` "
                             "wired only for old log lines.")
    parser.add_argument("--removed", type=int, default=0,
                        help="Stats jobs only: posts newly flagged deleted/removed in this run. "
                             "Renders as 'removed'.")
    # 2026-05-18 stats-pill relabel pass. The legacy `updated` field conflated
    # two distinct things (Step 1 view scrape count + Step 2 detail-leg
    # changed count) which made "updated" balloon meaninglessly. The new
    # split lets the dashboard show four clean pills:
    #   scanned         -> total rows considered this run (= polled + skipped)
    #   checked         -> rows we actually hit the platform API for
    #   changed         -> subset of checked where any tracked metric moved
    #   views-refreshed -> rows where the cheap view-scrape leg wrote a value
    # All four are optional, additive to the existing stats_segment, and
    # default to 0 so existing callers don't have to change.
    parser.add_argument("--scanned", type=int, default=0,
                        help="Stats jobs only: TOTAL rows considered this run "
                             "(polled + skipped + bypassed-as-fresh). "
                             "Renders as a 'scanned' pill on stats rows.")
    parser.add_argument("--changed", type=int, default=0,
                        help="Stats jobs only: subset of `checked` where any "
                             "tracked metric actually moved. Renders as a "
                             "'changed' pill. Distinct from `--updated` which "
                             "stays for back-compat with the old field name.")
    parser.add_argument("--views-refreshed", dest="views_refreshed", type=int, default=0,
                        help="Stats jobs only: rows where the cheap view-scrape "
                             "leg (Step 1 profile scrape on Reddit; built-in on "
                             "Twitter) wrote a fresh view count. Distinct from "
                             "`--changed`, which is the per-row JSON-API leg.")
    parser.add_argument("--unavailable", type=int, default=0,
                        help="Stats jobs (LinkedIn): posts where the platform "
                             "explicitly returned a 'post unavailable' string. "
                             "Subset of removed; rendered as a separate pill.")
    parser.add_argument("--not-found", dest="not_found", type=int, default=0,
                        help="Stats jobs (LinkedIn): posts still active but our "
                             "comment couldn't be located. Renders as 'not_found'.")
    parser.add_argument("--salvaged", type=int, default=0,
                        help="Twitter cycle: number of pending candidates from "
                             "prior cycles re-assigned to this batch in Phase 0. "
                             "Surfaces as a 'salvaged' pill in the dashboard "
                             "Result column so an operator can tell that work "
                             "from a previously-failed cycle is being retried "
                             "rather than lost. Optional; 0 = omit segment.")
    # ---- Discovery-stage counters (Twitter cycle, mirrors LinkedIn) -----
    # Twitter wires the same shape LinkedIn already exposes
    # (queries / candidates_found / dropped_below_floor) so the dashboard can
    # render a single 'discover' tooltip across platforms. Each is an
    # independent integer flag; pass 0 / omit to skip the segment. Stay
    # backward-compatible: emitted as `key=N` after `salvaged` and before
    # the cost/elapsed pair so older log lines (no discovery info) still
    # parse via the existing positional regex in bin/server.js.
    parser.add_argument("--queries", type=int, default=0,
                        help="Discovery: number of search queries the cycle "
                             "actually ran (raw count, including duds). "
                             "Twitter Phase 1 / LinkedIn Phase A.")
    parser.add_argument("--duds", type=int, default=0,
                        help="Discovery: subset of --queries that returned "
                             "zero candidates. Used by the dashboard to show "
                             "query-quality drift over time.")
    parser.add_argument("--tweets-pulled", dest="tweets_pulled", type=int, default=0,
                        help="Discovery: raw tweets/posts the scraper pulled "
                             "before the floor filter. Twitter only — "
                             "LinkedIn doesn't have a directly comparable "
                             "raw-volume number.")
    parser.add_argument("--candidates", type=int, default=0,
                        help="Discovery: candidates that survived the "
                             "post-floor filter (Twitter T0 snapshot rows / "
                             "LinkedIn candidates_found). Includes salvaged "
                             "rows from prior cycles.")
    parser.add_argument("--above-floor", dest="above_floor", type=int, default=0,
                        help="Discovery: candidates that cleared the "
                             "review-cap floor — for Twitter this is "
                             "Δ≥10 momentum (the same signal that flips "
                             "POST_LIMIT 1→3); for LinkedIn this is the "
                             "post-virality-floor count. Smaller than "
                             "--candidates.")
    parser.add_argument("--failure-reasons", dest="failure_reasons", default="",
                        help="Optional comma-separated `reason:count` pairs "
                             "describing why a run reported failed>0 "
                             "(e.g. 'monthly_limit:5,timeout:1'). Surfaced in "
                             "the dashboard Result column so operators can "
                             "tell a hard cap from a transient error without "
                             "opening the log file. Reason keys are free-form "
                             "snake_case; the dashboard sorts by count desc "
                             "and shows the top one with the rest in tooltip.")
    parser.add_argument("--skip-reasons", dest="skip_reasons", default="",
                        help="Optional comma-separated `reason:count` pairs "
                             "describing why a run reported skipped>0 "
                             "(e.g. 'duplicate_thread_pre_post:3,empty_reply_text:1'). "
                             "Distinct from --failure-reasons: skips are "
                             "intentional (dedup race guards, empty drafts, "
                             "rate-limited threads) and the dashboard renders "
                             "them as a yellow 'skipped: <reason>' pill rather "
                             "than the red 'failed: <reason>' pill. Same "
                             "sanitization rules as --failure-reasons.")
    # Inbox/feed scan counters (engage-reddit, engage-twitter, etc.). Lets a
    # pipeline that scans an inbox before engaging surface scan-stage
    # granularity (seen / new / excluded / unmatched) in the dashboard Result
    # column, so an empty cycle reads as "scanned 100 / 0 new" instead of just
    # "0 0 0 0". Comma-separated `key=N` pairs; whitespace and the pipe char are
    # stripped. Empty = omit the segment entirely (preserves backward compat).
    parser.add_argument("--scan", dest="scan", default="",
                        help="Optional comma-separated `key=N` pairs from an "
                             "inbox/feed scan stage (e.g. "
                             "'seen=100,new=0,excluded=1,unmatched=0'). "
                             "Surfaces as scan-stage pills in the dashboard "
                             "Result column for engage runs.")
    # Invent-topics hourly job counters. Carries the project picked, how many
    # topics were invented, how many queries were drafted in total, how many
    # queries surfaced ANY supply, and the per-topic query counts. Free-form
    # key=value comma-separated string — keys with integer values are parsed
    # as ints in the dashboard, `project` is parsed as a string, `qpt` is
    # parsed as a `+`-separated int list. Example:
    #   --invent='project=fazm,topics=3,queries=15,queries_w_supply=1,qpt=5+5+5'
    # Tails after scan= so the bin/server.js positional regex extends
    # backward-compatibly (the new group is optional).
    parser.add_argument("--invent", dest="invent", default="",
                        help="Invent-topics job stats. Comma-separated "
                             "key=value pairs (e.g. 'project=fazm,topics=3,"
                             "queries=15,queries_w_supply=1,qpt=5+5+5'). "
                             "Surfaces as the result-column pills on the "
                             "Invent Topics rows in the Status > Job History "
                             "tab.")
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    model_suffix = f" model={args.model}" if args.model else ""
    # Inserted between failed=N and cost= so the existing positional regex in
    # bin/server.js still parses old lines (the segment is optional in the regex).
    replies_segment = (
        f" replies_refreshed={args.replies_refreshed}"
        if args.replies_refreshed else ""
    )
    # Stats-job per-run counters. The base segment (checked/updated/removed)
    # stays as a single optional capture group for the bin/server.js regex.
    # The LinkedIn-specific extras (unavailable/not_found) tail the base
    # segment as their own optional groups so older lines still parse.
    # 2026-05-18 relabel: scanned/changed/views_refreshed tail the segment as
    # their own optional groups. Old log lines without them still parse.
    # Trigger the segment if ANY stats-job field is set so the new fields
    # surface even when the legacy three are zero.
    _any_stats = (args.checked or args.updated or args.removed
                  or args.unavailable or args.not_found
                  or args.scanned or args.changed or args.views_refreshed)
    stats_segment = (
        f" checked={args.checked} updated={args.updated} removed={args.removed}"
        if _any_stats else ""
    )
    if args.unavailable:
        stats_segment += f" unavailable={args.unavailable}"
    if args.not_found:
        stats_segment += f" not_found={args.not_found}"
    if args.scanned:
        stats_segment += f" scanned={args.scanned}"
    if args.changed:
        stats_segment += f" changed={args.changed}"
    if args.views_refreshed:
        stats_segment += f" views_refreshed={args.views_refreshed}"
    # `salvaged=N` segment tails the stats segment as its own optional capture
    # so old log lines (no salvage info) still parse cleanly. Twitter-cycle
    # specific today, but any pipeline that retries pending work cross-cycle
    # can emit it.
    salvaged_segment = f" salvaged={args.salvaged}" if args.salvaged else ""
    # `discover` segment carries Phase-1/discovery counters the dashboard
    # surfaces as a tooltip on the Result column (queries / duds /
    # tweets_pulled / candidates / above_floor). Each sub-key is only
    # emitted when non-zero so old log lines without discovery info still
    # parse via the existing positional regex. The whole segment is opt-in:
    # if every counter is zero, no `discover=` token appears at all.
    discover_parts = []
    if args.queries:
        discover_parts.append(f"queries={args.queries}")
    if args.duds:
        discover_parts.append(f"duds={args.duds}")
    if args.tweets_pulled:
        discover_parts.append(f"tweets_pulled={args.tweets_pulled}")
    if args.candidates:
        discover_parts.append(f"candidates={args.candidates}")
    if args.above_floor:
        discover_parts.append(f"above_floor={args.above_floor}")
    discover_segment = (
        " discover=" + ",".join(discover_parts) if discover_parts else ""
    )
    # `failure_reasons` segment is appended after elapsed (and after the
    # optional model suffix) so the existing positional regex in bin/server.js
    # still parses old lines. Sanitize: strip whitespace and forbid the pipe
    # char so the value can't break out of the log line column. Empty string
    # = omit the segment entirely (preserves backward compat).
    fr_raw = (args.failure_reasons or "").strip()
    fr_clean = fr_raw.replace("|", "").replace(" ", "")
    failure_segment = f" failure_reasons={fr_clean}" if fr_clean else ""
    # `skip_reasons=` segment is the skip-side companion to failure_reasons.
    # Tails after failure_reasons so the existing positional regex stays
    # back-compat: old log lines that ended at failure_reasons (or earlier)
    # still parse, the new group is optional. Same sanitization rules.
    sr_raw = (args.skip_reasons or "").strip()
    sr_clean = sr_raw.replace("|", "").replace(" ", "")
    skip_segment = f" skip_reasons={sr_clean}" if sr_clean else ""
    # `scan=` segment carries inbox/feed scan-stage counters. Same sanitization
    # rules as failure_reasons (strip whitespace + pipe). Appended after
    # discover= so the existing positional regex in bin/server.js can extend
    # without breaking back-compat on old lines.
    scan_raw = (args.scan or "").strip()
    scan_clean = scan_raw.replace("|", "").replace(" ", "")
    scan_segment = f" scan={scan_clean}" if scan_clean else ""
    # `invent=` segment carries the invent-topics hourly job's per-run counts.
    # Same sanitization rules as scan=/failure_reasons (strip whitespace + pipe).
    # Tails after scan= so the bin/server.js positional regex extends
    # backward-compatibly — old lines with no invent= still parse.
    invent_raw = (args.invent or "").strip()
    invent_clean = invent_raw.replace("|", "").replace(" ", "")
    invent_segment = f" invent={invent_clean}" if invent_clean else ""
    # `escape_hatch=` segment surfaces author_blocklist writes that happened
    # during this run window (LLM-judgment via reply_db.py CLI, or manual
    # operator adds). Auto-detected via the API so callers don't have to
    # plumb it; fail-safe to empty on any error. Velocity-auto rows are
    # excluded inside _detect_escape_hatch — they fire on every reply and
    # would flood the pill. Tails after skip_reasons so old log lines (no
    # escape-hatch info) still parse via the bin/server.js positional regex.
    eh_count, eh_details = _detect_escape_hatch(args.script, args.elapsed)
    if eh_count:
        eh_details_clean = ",".join(eh_details).replace("|", "").replace(" ", "")
        escape_hatch_segment = (
            f" escape_hatch={eh_count} escape_hatch_details={eh_details_clean}"
        )
    else:
        escape_hatch_segment = ""
    line = (
        f"{timestamp} | {args.script} | "
        f"posted={args.posted} skipped={args.skipped} failed={args.failed}"
        f"{replies_segment}{stats_segment}{salvaged_segment}{discover_segment}{scan_segment}{invent_segment} "
        f"cost=${args.cost:.2f} elapsed={args.elapsed:.0f}s{model_suffix}{failure_segment}{skip_segment}{escape_hatch_segment}"
    )

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

    print(line)

    # Silent-failure warning fires when a posting job claims `failed>0` but
    # never posted anything. Stats/audit jobs legitimately run with posted=0
    # while doing real work (scanning rows, checking the API); suppress the
    # warning when any of `--checked / --scanned / --replies-refreshed` is
    # non-zero so audit and stats rows don't trip a false positive.
    _real_work = (args.checked or args.scanned or args.replies_refreshed
                  or args.changed or args.updated or args.views_refreshed)
    if args.posted == 0 and args.failed > 0 and not _real_work:
        warning = f"WARNING: {args.script} posted=0 failed={args.failed} -- possible silent failure"
        with open(LOG_PATH, "a") as f:
            f.write(f"{timestamp} | {warning}\n")
        print(warning)


if __name__ == "__main__":
    main()
