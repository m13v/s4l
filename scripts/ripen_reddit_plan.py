#!/usr/bin/env python3
"""
ripen_reddit_plan.py

Reddit equivalent of Twitter's Phase 2a (T1 re-poll + delta gate). Reads a
plan JSON written by `post_reddit.py --phase discover`, captures T0 score/comments
for each target_thread_url, sleeps SLEEP_SECONDS (default 300), re-polls T1,
computes composite delta = Δupvotes + W_COMMENTS * Δcomments, and drops
decisions whose composite <= FLOOR (default 5).

Survivors are written to --out as a new plan JSON consumed by
`post_reddit.py --phase post`. Dropped decisions are logged to stderr and
into the output JSON under `ripen_dropped_details`.

Defaults match the design agreed on 2026-05-06, with a 2026-05-10 product-intent
boost added to mirror the Twitter cycle's hybrid sort:
    raw_composite = Δup + 4*Δcomments
    intent_boost  = +5 if title/selftext matches a product-discussion regex
                    (asking for a tool, venting a pain, comparing alternatives,
                    "anyone know a way to...", etc), else 0
    composite     = raw_composite + intent_boost
    floor         = composite >= 1  (any positive momentum OR an on-theme
                    intent signal passes; +1 upvote OR a clearly stated need
                    is enough to reach the LLM relevance gate)
    sleep         = 300s (5 min) by default; run-reddit-search.sh sets 1800s

Failure modes:
    - T0 fetch fails for a URL: drop that decision (fail-closed; we cannot
      measure delta without T0)
    - All T0 fetches fail: bail with passthrough (likely Reddit-wide rate
      limit; better to post stale than nothing on a bad-network cycle)
    - T1 fetch fails for a URL: drop that decision (same logic)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_DIR, "scripts")

sys.path.insert(0, SCRIPTS_DIR)

# Mirrors the Twitter cycle's product-discussion intent regex (run-twitter-cycle.sh
# Phase 2b). When a thread title or selftext contains an explicit "asking for a
# tool / venting a pain point / comparing alternatives" signal, add a +5 boost
# to the composite delta before the floor check. This lets quiet on-theme
# threads ("anyone know a way to track Claude Code usage?" with 1 upvote and 0
# new comments in 30 min) compete with viral drama on raw growth.
_INTENT_REGEX = re.compile(
    r"\b("
    r"wish|need a|need an|looking for|recommend|alternative to|frustrated|"
    r"hate (that|when)|should exist|would pay|missing.*(feature|tool|app)|"
    r"why (is there no|doesn't|don't)|anyone (know|use|tried|using)|"
    r"how do you|what do you use|best (tool|app|way)|any (good|decent) (tool|app|way)"
    r")\b",
    re.IGNORECASE,
)
INTENT_BOOST = 5.0


def _intent_boost(title, selftext):
    """Return INTENT_BOOST if the title shows product-discussion intent, else 0.

    TITLE-ONLY by design. Earlier versions matched title+selftext, but Reddit
    selftext can be 30k chars of narrative (camping ghost stories, long reviews)
    where words like "looking for", "wish", "recommend" appear in their plain-
    English sense ("looking for a spot to hang a bear bag"), causing 30%+ false-
    positive rates. Titles are short, deliberate, and intent-rich; if "anyone
    know" appears in the title, it's almost always a real product ask. The
    `selftext` arg is kept in the signature for future use but ignored today.

    The LLM relevance gate downstream (post_reddit.py draft phase, surfaces as
    `draft_gate_omit`) is still the real safety net for the small remaining
    false-positive rate. The boost only changes ranking + lets zero-momentum
    on-theme threads clear the floor; it does not auto-post anything.
    """
    if not title:
        return 0.0
    return INTENT_BOOST if _INTENT_REGEX.search(title) else 0.0


def _fetch_thread_text_map(thread_urls):
    """Batch-fetch (thread_title, thread_selftext) via /api/v1/reddit-candidates.

    Returns {url: (title, selftext)}. Missing rows return ('', '').
    """
    if not thread_urls:
        return {}
    try:
        from http_api import api_get
        # The route accepts a CSV `thread_urls` query param (up to 500 URLs).
        resp = api_get(
            "/api/v1/reddit-candidates",
            query={
                "thread_urls": ",".join(thread_urls),
                "limit": 500,
            },
        )
        rows = ((resp or {}).get("data") or {}).get("candidates") or []
        return {
            r.get("thread_url"): (r.get("thread_title") or "", r.get("thread_selftext") or "")
            for r in rows if r.get("thread_url")
        }
    except Exception as e:
        print(f"[ripen] _fetch_thread_text_map: {e}", file=sys.stderr)
        return {}


def _db_update_ripen_metrics(thread_url, t0_score, t0_comments,
                             t1_score, t1_comments, composite, bump_attempt):
    """Persist T0/T1/delta via /api/v1/reddit-candidates/by-thread-url action=set_ripen.

    Server-side: bump_attempt=True bumps attempt_count, sets
    last_failure_reason='ripen_floor_miss', and flips status='failed' (one-strike
    rule from 2026-05-07). bump_attempt=False just records the metrics.
    """
    if not thread_url:
        return
    try:
        from http_api import api_patch
        api_patch(
            "/api/v1/reddit-candidates/by-thread-url",
            {
                "thread_url": thread_url,
                "action": "set_ripen",
                "score_t0": int(t0_score) if t0_score is not None else None,
                "comments_t0": int(t0_comments) if t0_comments is not None else None,
                "score_t1": int(t1_score) if t1_score is not None else None,
                "comments_t1": int(t1_comments) if t1_comments is not None else None,
                "delta_score": float(composite) if composite is not None else None,
                "bump_attempt": bool(bump_attempt),
            },
            ok_on_404=True,
        )
    except Exception as e:
        print(f"[ripen] WARN: db update failed for {thread_url}: {e}",
              file=sys.stderr)


def _db_load_persisted_t0(urls):
    """Load score_t0 / comments_t0 via /api/v1/reddit-candidates.

    Returns dict {url: {"score": s, "comments": c, "ok": True}} for every row
    where BOTH score_t0 and comments_t0 are non-null. URLs without persisted
    T0 are absent so callers fall back to a live fetch.
    """
    if not urls:
        return {}
    try:
        from http_api import api_get
        resp = api_get(
            "/api/v1/reddit-candidates",
            query={
                "thread_urls": ",".join(urls),
                "has_t0": "true",
                "limit": 500,
            },
        )
        rows = ((resp or {}).get("data") or {}).get("candidates") or []
        out = {}
        for r in rows:
            url = r.get("thread_url")
            if not url:
                continue
            s = r.get("score_t0")
            c = r.get("comments_t0")
            if s is None or c is None:
                continue
            out[url] = {"score": int(s), "comments": int(c), "ok": True}
        return out
    except Exception as e:
        print(f"[ripen] WARN: load_persisted_t0 failed: {e}",
              file=sys.stderr)
        return {}


def _db_mark_html_locked(thread_url, state):
    """Mark a candidate as permanently failed via the action=mark_html_locked
    lane. The server flips status='failed', sets last_failure_reason='html_<state>',
    and stamps last_attempt_at=NOW().
    """
    if not thread_url:
        return
    try:
        from http_api import api_patch
        api_patch(
            "/api/v1/reddit-candidates/by-thread-url",
            {
                "thread_url": thread_url,
                "action": "mark_html_locked",
                "state": state,
            },
            ok_on_404=True,
        )
    except Exception as e:
        print(f"[ripen] WARN: html_locked db update failed for {thread_url}: {e}",
              file=sys.stderr)


def repoll(urls, timeout=120):
    """Call reddit_tools.py repoll with the given URLs. Returns the parsed
    {"results": {url: {ok, score, comments}}} dict (or {} on hard failure)."""
    if not urls:
        return {}
    payload = json.dumps({"urls": urls})
    try:
        proc = subprocess.run(
            ["python3", os.path.join(SCRIPTS_DIR, "reddit_tools.py"), "repoll"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"[ripen] ERROR: repoll subprocess timeout", file=sys.stderr)
        return {}
    if proc.returncode != 0:
        print(f"[ripen] ERROR: repoll exit={proc.returncode} stderr={proc.stderr[:200]}",
              file=sys.stderr)
        return {}
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"[ripen] ERROR: repoll bad JSON: {e}", file=sys.stderr)
        return {}
    return out.get("results") or {}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True, help="Input plan JSON path")
    p.add_argument("--out", required=True, help="Output filtered plan JSON path")
    p.add_argument("--floor", type=float, default=1.0,
                   help="Composite delta must be GREATER THAN OR EQUAL to this "
                        "(default: 1.0). composite = Δup + 4*Δcomments; +1 upvote in 5min "
                        "is enough signal that the thread is still alive.")
    p.add_argument("--w-comments", type=float, default=4.0,
                   help="Comment weight in composite formula (default: 4.0)")
    p.add_argument("--sleep", type=int, default=300,
                   help="Seconds to sleep between T0 and T1 (default: 300)")
    p.add_argument("--no-sleep", action="store_true",
                   help="Skip the sleep (for tests)")
    args = p.parse_args()

    with open(args.in_path) as f:
        plan = json.load(f)

    decisions = plan.get("decisions") or []
    if not decisions:
        print(f"[ripen] empty plan, passthrough", file=sys.stderr)
        with open(args.out, "w") as f:
            json.dump(plan, f)
        return 0

    urls = []
    for d in decisions:
        # post_reddit.py writes the field as `thread_url` (not target_thread_url).
        # Tolerate both for safety in case the schema ever changes.
        u = (d.get("thread_url") or d.get("target_thread_url") or "").strip()
        if u:
            urls.append(u)

    if not urls:
        print(f"[ripen] no thread_urls in {len(decisions)} decisions; passthrough",
              file=sys.stderr)
        with open(args.out, "w") as f:
            json.dump(plan, f)
        return 0

    # ---- T0 capture ---------------------------------------------------------
    # Always prefer PERSISTED T0 from reddit_candidates (captured at discover
    # time from the search response, no extra HTTP), falling back to a fresh
    # live fetch for URLs that don't have one yet. This unifies the salvage
    # and fresh-discover paths and mirrors twitter's behavior:
    #   - Fresh discoveries: T0 was just captured seconds ago at INSERT time,
    #     so cumulative delta over the upcoming 5-min sleep ≈ a fresh window.
    #   - Salvaged rows:    T0 is the FIRST-SIGHTING value (could be hours
    #     old), so delta is cumulative since discovery — catches slow-trickle
    #     threads a fresh 5-min window would miss.
    # Live fetch fallback only fires for URLs the orchestrator never INSERTed
    # (e.g. legacy tmpfiles from before the candidates migration). Pure
    # safety net.
    is_salvaged = bool(plan.get("salvaged"))
    persisted = _db_load_persisted_t0(urls)
    missing = [u for u in urls if u not in persisted]
    print(f"[ripen] T0: {len(persisted)} from reddit_candidates, "
          f"{len(missing)} need live fetch (salvaged={'yes' if is_salvaged else 'no'})",
          file=sys.stderr)
    if missing:
        live = repoll(missing)
        for u, r in live.items():
            if r.get("ok"):
                persisted[u] = r
    t0_ok = persisted
    if not t0_ok:
        print(f"[ripen] WARN: 0 of {len(urls)} T0 fetches succeeded; "
              "passthrough (likely rate limit)", file=sys.stderr)
        with open(args.out, "w") as f:
            json.dump(plan, f)
        return 0
    print(f"[ripen] T0: {len(t0_ok)}/{len(urls)} succeeded "
          f"(salvaged={'yes' if is_salvaged else 'no'})", file=sys.stderr)

    # ---- Sleep --------------------------------------------------------------
    if not args.no_sleep:
        print(f"[ripen] sleeping {args.sleep}s for engagement to develop...",
              file=sys.stderr)
        time.sleep(args.sleep)

    # ---- T1 re-poll ---------------------------------------------------------
    print(f"[ripen] T1: re-fetching {len(t0_ok)} thread(s)...", file=sys.stderr)
    t1 = repoll(list(t0_ok.keys()))

    # ---- Batch-fetch title+selftext for product-intent boost ----------------
    # Mirrors the Twitter cycle's hybrid sort: a thread asking for a tool /
    # venting a pain point gets +5 added to composite, so quiet on-theme rows
    # clear the floor and rank above pure noise of equivalent raw growth.
    intent_text_map = _fetch_thread_text_map(
        [(d.get("thread_url") or d.get("target_thread_url") or "").strip()
         for d in decisions]
    )

    # ---- Filter -------------------------------------------------------------
    survivors = []
    drops = []
    for d in decisions:
        url = (d.get("thread_url") or d.get("target_thread_url") or "").strip()
        t0r = t0_ok.get(url)
        t1r = t1.get(url, {}) if t1 else {}
        if not t0r:
            drops.append({"url": url, "reason": "no_t0"})
            continue
        if not t1r.get("ok"):
            drops.append({
                "url": url,
                "reason": f"t1_fail:{t1r.get('error', 'unknown')}",
            })
            continue
        d_up = int(t1r["score"]) - int(t0r["score"])
        d_co = int(t1r["comments"]) - int(t0r["comments"])
        raw_composite = d_up + args.w_comments * d_co
        title, selftext = intent_text_map.get(url, ("", ""))
        intent = _intent_boost(title, selftext)
        composite = raw_composite + intent
        # Annotate decision with measurement (always, even if dropped — useful
        # for downstream analysis/debug). Both raw and boosted composites are
        # surfaced so post-hoc analysis can separate growth signal from intent.
        d["ripen"] = {
            "t0_score": t0r["score"],
            "t0_comments": t0r["comments"],
            "t1_score": t1r["score"],
            "t1_comments": t1r["comments"],
            "delta_up": d_up,
            "delta_comments": d_co,
            "raw_composite": raw_composite,
            "intent_boost": intent,
            "composite": composite,
            "window_sec": args.sleep if not args.no_sleep else 0,
            "floor": args.floor,
            "w_comments": args.w_comments,
        }
        if composite >= args.floor:
            survivors.append(d)
            # Persist T0/T1/delta for the survivor; do NOT bump attempt_count
            # — passing the floor isn't an "attempt" against the post budget.
            _db_update_ripen_metrics(url, t0r["score"], t0r["comments"],
                                     t1r["score"], t1r["comments"],
                                     composite, bump_attempt=False)
            print(f"[ripen] PASS composite={composite:.1f} (Δup={d_up}, Δcomm={d_co}) "
                  f"{url}", file=sys.stderr)
        else:
            drops.append({
                "url": url,
                "reason": f"composite={composite:.1f} < floor={args.floor}",
                "delta_up": d_up,
                "delta_comments": d_co,
            })
            # Floor miss counts against the candidate's attempt budget so a
            # chronically-flat thread eventually drops out of the salvage
            # rotation. Phase 0's MAX_ATTEMPTS=3 ceiling auto-promotes it.
            _db_update_ripen_metrics(url, t0r["score"], t0r["comments"],
                                     t1r["score"], t1r["comments"],
                                     composite, bump_attempt=True)
            print(f"[ripen] DROP composite={composite:.1f} (Δup={d_up}, Δcomm={d_co}) "
                  f"{url}", file=sys.stderr)

    # 2026-05-10: top-k cap removed. The cap was disabled (--top-k 0) since
    # 2026-05-08 because trimming survivors before the LLM relevance gate threw
    # away potentially-good fits below the engagement-velocity cutoff. The
    # final cap now lives in _post_iteration via S4L_REDDIT_MAX_POSTS_PER_CYCLE
    # (default 10), which sorts decisions by ripen composite DESC.

    # ---- HTML lock pre-flight for delta-gate survivors ----------------------
    # cmd_repoll checks the JSON locked flag, but Reddit's AutoMod sometimes
    # renders .locked-tagline without setting locked=true in the JSON API
    # (observed on r/Entrepreneur). One unauthenticated GET per survivor (~1s).
    # Failures in the lock check are non-fatal: we log a warning and keep the
    # survivor rather than fail-closed on a network blip.
    check_locked_bin = os.path.join(SCRIPTS_DIR, "reddit_tools.py")
    if survivors:
        print(f"[ripen] HTML lock pre-flight for {len(survivors)} survivor(s)...",
              file=sys.stderr)
        clean_survivors = []
        for d in survivors:
            url = (d.get("thread_url") or d.get("target_thread_url") or "").strip()
            try:
                proc = subprocess.run(
                    ["python3", check_locked_bin, "check-locked", url],
                    capture_output=True, text=True, timeout=20,
                )
                out = json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}
                state = out.get("state", "ok")
                if state in ("locked", "archived"):
                    print(f"[ripen] HTML-{state}: dropping survivor {url}",
                          file=sys.stderr)
                    drops.append({"url": url, "reason": f"html_{state}"})
                    # Permanent failure in the queue: Phase 0 salvage skips
                    # status='failed', and the dashboard renders the reason
                    # via last_failure_reason. No retry on locked threads.
                    _db_mark_html_locked(url, state)
                    continue
            except Exception as e:
                print(f"[ripen] WARN: check-locked failed for {url}: {e}; keeping survivor",
                      file=sys.stderr)
            clean_survivors.append(d)
        survivors = clean_survivors

    plan["decisions"] = survivors
    plan["ripen_summary"] = {
        "input_count": len(decisions),
        "survivors": len(survivors),
        "drops": len(drops),
        "floor": args.floor,
        "w_comments": args.w_comments,
        "sleep_sec": args.sleep if not args.no_sleep else 0,
    }
    plan["ripen_dropped_details"] = drops

    with open(args.out, "w") as f:
        json.dump(plan, f)

    # Compact, parseable summary marker for the dashboard's
    # enrichPostCommentsRedditRuns() in bin/server.js. Field order matters; keep
    # in sync with the regex on the JS side.
    best_composite = None
    best_d_up = None
    best_d_co = None
    for d in survivors:
        rip = d.get("ripen") or {}
        c = rip.get("composite")
        if c is None:
            continue
        if best_composite is None or c > best_composite:
            best_composite = c
            best_d_up = rip.get("delta_up")
            best_d_co = rip.get("delta_comments")
    bc = "" if best_composite is None else f"{best_composite:.1f}"
    bu = "" if best_d_up is None else str(best_d_up)
    bk = "" if best_d_co is None else str(best_d_co)
    print(
        f"[ripen] summary input={len(decisions)} survivors={len(survivors)} "
        f"drops={len(drops)} floor={args.floor} w_comments={args.w_comments} "
        f"window_sec={args.sleep if not args.no_sleep else 0} "
        f"best_composite={bc} best_d_up={bu} best_d_co={bk}",
        file=sys.stderr,
    )
    print(f"[ripen] done: {len(survivors)} survivors, {len(drops)} drops "
          f"(floor>={args.floor}, w_comments={args.w_comments})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
