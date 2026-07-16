#!/usr/bin/env python3
"""
merge_review_queue.py — deliver a DRAFT_ONLY cycle's plan into the approval cards.

The deterministic pipeline (run-twitter-cycle.sh DRAFT_ONLY) writes its drafts to
a per-batch plan file (/tmp/twitter_cycle_plan_<batch>.json) and prints
`DRAFT_ONLY_PLAN=<path>`. On a customer box NOTHING used to consume that — the
only writer of the review-queue cards was the (now-removed) host-draft
submit_drafts path. This script closes that gap: it merges the batch plan's
candidates into the single review-queue plan the menu-bar cards read, deduped by
thread/candidate URL, and refreshes the review-request marker the menu bar polls.

This is the SAME merge submit_drafts did, reimplemented in Python so the launchd
kicker (no node/MCP) can run it after the cycle. ONE pipeline, one set of cards.

Usage:
  merge_review_queue.py --plan /tmp/twitter_cycle_plan_<batch>.json [--project NAME]
  merge_review_queue.py --plan-from-marker '<stdout containing DRAFT_ONLY_PLAN=...>'

State dir (for review-request.json) honors $S4L_STATE_DIR; the review-queue plan
lives in $S4L_TMP_DIR or /tmp (matching the MCP's planPath()).

LEDGER SEMANTICS (read this before counting anything in review-queue.json):
the queue is APPEND-FOREVER — handled candidates are never removed, they are
flag-stamped in place (posted / terminal+terminal_reason / post_failed /
approved). "Not posted" is therefore NOT "awaiting review"; in an old queue
most rows are retired. The canonical classifier is
mcp/menubar/s4l_state.py::candidate_state(); the honest pending-cards count is
its awaiting_review bucket, mirrored into review-request.json's .count here at
merge time.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

REVIEW_QUEUE_ID = "review-queue"


def tmp_dir() -> str:
    return os.environ.get("S4L_TMP_DIR") or "/tmp"


def state_dir() -> str:
    return os.environ.get("S4L_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def plan_path(batch_id: str) -> str:
    return os.path.join(tmp_dir(), f"twitter_cycle_plan_{batch_id}.json")


def store_path() -> str:
    """Canonical, DURABLE home of the review-queue store: the state dir, not
    /tmp. The review queue is not a scratch artifact — it holds drafts awaiting
    a human decision plus their decision state, and /tmp dies on every reboot
    and periodic sweep (which both lost pending drafts outright and reset
    candidate numbering, letting stale ledger entries swallow new cards)."""
    return os.path.join(state_dir(), "review-queue.json")


def ensure_store_symlink() -> None:
    """Keep /tmp/twitter_cycle_plan_review-queue.json as a SYMLINK to the
    canonical store. The MCP server (repo.ts planPath/readPlan/writePlan) and
    the locked pipeline scripts resolve the /tmp path; fs.writeFileSync and
    Path.write_text both follow symlinks, so the link is a permanent
    compatibility bridge that needs no changes on their side. Recreated here on
    every merge (and by the menu bar at boot) so a reboot's tmp sweep only ever
    removes the LINK, never the data.

    Migration: a REAL file at the /tmp path (pre-upgrade layout, or written by
    an old merge after a reboot) is absorbed into the canonical store by the
    caller before this runs; here it is replaced by the link."""
    link = plan_path(REVIEW_QUEUE_ID)
    store = store_path()
    try:
        if os.path.islink(link):
            if os.readlink(link) == store:
                return
            os.unlink(link)
        elif os.path.exists(link):
            os.unlink(link)  # caller already absorbed its content
        tmp_link = f"{link}.lnk.{os.getpid()}"
        os.symlink(store, tmp_link)
        os.replace(tmp_link, link)
    except Exception as e:
        print(f"[merge_review_queue] symlink maintenance failed: {e}", file=sys.stderr)


def review_request_path() -> str:
    return os.path.join(state_dir(), "review-request.json")


def _atomic_write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _dedup_key(c: dict) -> str:
    """Match submit_drafts: dedup by the thread/candidate URL, else candidate_id."""
    # Sandbox cards (experiments.sandbox=true) replay real historical threads
    # ON PURPOSE, including ones already dealt with for real -- that's the
    # whole point of testing against past data. Namespace their key so they
    # never collide with (and get silently swallowed by) a real card for the
    # same URL, and keep the reply text IN the key so two different prompt
    # variants drafting the same thread both surface as distinct cards
    # instead of the second dropping as "already seen" (identical reruns
    # still naturally dedupe against each other, which is fine).
    if (c.get("experiments") or {}).get("sandbox"):
        url = c.get("candidate_url") or c.get("tweet_url") or c.get("thread_url") or ""
        return f"sandbox:{url}:{(c.get('reply_text') or '')[:80]}"
    for k in ("candidate_url", "tweet_url", "thread_url", "candidate_id"):
        v = c.get(k)
        if v:
            return str(v)
    # last resort: the reply text, so identical drafts don't double up
    return (c.get("reply_text") or "")[:120]


def _thread_url(c: dict) -> str:
    for k in ("candidate_url", "tweet_url", "thread_url"):
        v = c.get(k)
        if v:
            return str(v)
    return ""


def _reddit_plan_to_candidates(plan: dict) -> list:
    """Adapt a post_reddit.py draft-phase plan into review-queue card entries.

    The reddit plan shape is {project_name, batch_id, decisions: [...]}, where
    each decision is the full post-ready dict (_draft_iteration's merged
    candidate: thread_url, text, engagement_style, ripen data, campaign
    fields). The card carries the twitter-shaped display fields the menu bar
    knows how to render, PLUS the verbatim decision + plan metadata under
    reddit_* keys so an approval can reconstruct a one-decision plan and hand
    it to `post_reddit.py --phase post` unchanged (locks, URL wrapping,
    campaign suffixes, and log_post all stay in the one battle-tested poster).
    """
    import hashlib

    out = []
    for d in plan.get("decisions") or []:
        if not isinstance(d, dict) or not d.get("text") or not d.get("thread_url"):
            continue
        # candidate_id gets an "rd-" prefix ON PURPOSE: bare integer ids here
        # would collide with twitter_candidates ids in every id-keyed flow
        # (review-events row flips, bulk-discard PATCH /twitter-candidates/
        # by-id) and silently retire the WRONG twitter row. The prefixed id
        # still satisfies index.ts's merge-by-candidate_id stamping (ids are
        # compared as strings), while the twitter-table flips 404 harmlessly.
        _rid = d.get("id") or d.get("candidate_id")
        if not _rid:
            _rid = hashlib.sha1(d["thread_url"].encode()).hexdigest()[:10]
        out.append({
            "platform": "reddit",
            "candidate_id": f"rd-{_rid}",
            "candidate_url": d.get("thread_url"),
            "thread_url": d.get("thread_url"),
            "thread_author": d.get("thread_author"),
            "thread_text": d.get("thread_title") or "",
            "matched_project": plan.get("project_name"),
            "reply_text": d.get("text"),
            "engagement_style": d.get("engagement_style"),
            "assigned_style": d.get("assigned_style") or d.get("engagement_style"),
            "assigned_mode": d.get("assigned_mode"),
            "search_topic": d.get("search_topic"),
            # Two-draft A/B (2026-07-15): post_reddit.py's _draft_iteration
            # stamps a 2-element drafts[] (mirrors twitter's shape exactly:
            # variant/text/style/assigned_style/assigned_mode). s4l_card.py's
            # dual-box rendering, pairwise hover/choice tracking, and the
            # edit-learning digest already key off `isinstance(drafts, list)
            # and len == 2`, not platform, so passing it through is all this
            # needs. None on legacy single-draft decisions.
            "drafts": d.get("drafts"),
            # Experiment/scenario arms (2026-07-15): stamped at the source in
            # post_reddit.py's _draft_iteration (mirrors run-twitter-cycle.sh);
            # this is a pure passthrough, same convention as the twitter path.
            "experiments": d.get("experiments") or {},
            "reddit_batch_id": plan.get("batch_id"),
            "reddit_decision": d,
            "reddit_plan_meta": {
                k: plan.get(k)
                for k in ("project_name", "batch_id", "style_assignment",
                          "generation_trace_path", "session_id",
                          "draft_session_id")
                if plan.get(k) is not None
            },
        })
    return out


# Discovery-time author/engagement fields stamped onto each plan candidate so the
# approval card can show them. All already captured on the twitter_candidates row
# by the discovery pipeline (and refreshed at T1); no scrape happens here.
STATS_KEYS = (
    "author_handle",
    "author_followers",
    "likes",
    "retweets",
    "replies",
    "views",
    "virality_score",
    "tweet_posted_at",
)


def _sync_with_backend(cands: list) -> tuple[int, int]:
    """One bulk /api/v1/twitter-candidates lookup for every still-open candidate
    (not posted, not terminal), used for two things:

      - stamp the discovery-time `stats` sidecar the card renders (candidates
        that already have one are left alone), same as the old
        _enrich_with_stats this replaces.
      - notice when the backend has ALREADY retired a candidate this plan
        still thinks is 'pending' (most commonly the Phase 0 freshness gate
        flipping status='expired' after FRESHNESS_HOURS — see
        skill/run-twitter-cycle.sh) and mark it terminal here too, same as a
        human "discard all pending" would. Without this, a card can sit in
        the review queue as an approvable draft long after the backend has
        moved on; approving it later silently no-ops (post_drafts returns
        posted:0, no browser ever launches, no post-*.log — see the
        2026-07-09 "approved 3 cards, nothing posted" investigation).

    Runs on EVERY merge (every cycle), not just once per candidate, so status
    drift after the initial stamp is still caught while a card is still
    pending. Best-effort: any API failure leaves every candidate untouched
    (fail open, same as before). Returns (stamped_count, pruned_count).

    APPROVED cards are exempt from the prune: an approval is a settled human
    decision awaiting its serialized post, and stamping terminal on it makes
    post_drafts refuse it as already-decided (posted:0). That exact race ate
    2 of 3 approvals on 2026-07-10 (approved 04:30:02Z, merge stamped
    backend_status_expired 04:32:33Z, poster refused both). The freshness
    gate exists to stop stale UNDECIDED drafts from burning review attention;
    once a human has said "post it", the only honest gate left is the
    poster's own at-post-time tweet_unavailable check."""
    pending = [
        c
        for c in cands
        if not c.get("posted")
        and not c.get("terminal")
        and not c.get("approved")
        # The bulk lookup is /api/v1/twitter-candidates; reddit cards would
        # never match a row there, so skip them (their freshness gating stays
        # with the reddit pipeline's own salvage/expiry lanes).
        and c.get("platform") != "reddit"
        # Prompt-sandbox cards (experiments.sandbox=true, see
        # run-twitter-cycle.sh's S4L_SANDBOX_CANDIDATES_FILE short-circuit)
        # replay a REAL historical tweet_url on purpose. That row's real
        # status is almost always 'posted' or 'skipped' (it's old), which
        # would otherwise get read back here as "backend already retired
        # this" and prune the sandbox card the instant it syncs. Skip the
        # lookup for them entirely; they were never 'pending' in the live
        # sense this freshness gate exists to catch.
        and not (c.get("experiments") or {}).get("sandbox")
        and _thread_url(c)
    ]
    if not pending:
        return 0, 0
    urls = sorted({_thread_url(c) for c in pending})[:500]
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get

        resp = api_get(
            "/api/v1/twitter-candidates",
            query={"tweet_urls": ",".join(urls), "limit": 500},
        )
        rows = (resp.get("data") or {}).get("candidates") or []
    except BaseException as e:  # http_api raises SystemExit on terminal failure
        print(f"[merge_review_queue] backend sync skipped: {e}", file=sys.stderr)
        return 0, 0
    by_url = {str(r.get("tweet_url")): r for r in rows if r.get("tweet_url")}
    stamped = 0
    pruned = 0
    for c in pending:
        row = by_url.get(_thread_url(c))
        if not row:
            continue
        if not c.get("stats"):
            c["stats"] = {k: row.get(k) for k in STATS_KEYS}
            stamped += 1
        status = row.get("status")
        if status and status != "pending":
            c["terminal"] = True
            c["discard_reason"] = f"backend_status_{status}"
            pruned += 1
    return stamped, pruned


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge a DRAFT_ONLY plan into the review-queue cards")
    ap.add_argument("--plan", help="path to the per-batch DRAFT_ONLY plan file")
    ap.add_argument(
        "--plan-from-marker",
        help="text containing a DRAFT_ONLY_PLAN=<path> marker (e.g. cycle stdout)",
    )
    ap.add_argument(
        "--reddit-plan",
        help="path to a post_reddit.py draft-phase plan (decisions[] shape); "
        "adapted into reddit cards instead of the twitter candidates[] shape",
    )
    ap.add_argument("--project", default=None, help="project name for the review-request marker")
    ns = ap.parse_args()

    src = ns.plan or ns.reddit_plan
    if not src and ns.plan_from_marker:
        m = re.search(r"DRAFT_ONLY_PLAN=(\S+\.json)", ns.plan_from_marker)
        if m:
            src = m.group(1)
    if not src:
        print("[merge_review_queue] no source plan (need --plan, --reddit-plan or a DRAFT_ONLY_PLAN marker)", file=sys.stderr)
        return 2
    if not os.path.exists(src):
        print(f"[merge_review_queue] source plan not found: {src}", file=sys.stderr)
        return 2

    try:
        with open(src) as f:
            batch = json.load(f)
    except Exception as e:
        print(f"[merge_review_queue] could not read source plan: {e}", file=sys.stderr)
        return 2

    if ns.reddit_plan:
        new_cands = _reddit_plan_to_candidates(batch)
    else:
        new_cands = batch.get("candidates") or []
    if not new_cands:
        print("[merge_review_queue] source plan has 0 candidates; nothing to merge", file=sys.stderr)
        return 0

    # Experiment/scenario arms ride in on each candidate's `experiments` dict,
    # stamped at the SOURCE by run-twitter-cycle.sh's plan writer (see
    # scripts/active_experiments.py). This merge just carries them through to
    # review-queue.json; it does NOT stamp — the arms were assigned in the
    # cycle process and only that process knows them.

    dst = store_path()
    existing = []
    plan_created_at = None
    if os.path.exists(dst):
        try:
            with open(dst) as f:
                prev = json.load(f)
            existing = prev.get("candidates") or []
            plan_created_at = prev.get("created_at")
        except Exception:
            existing = []
            plan_created_at = None
    # Absorb a REAL file at the legacy /tmp location (pre-upgrade store, or one
    # written by old code after a reboot) so no pending draft or decision flag
    # is lost, then ensure_store_symlink() below replaces it with the link.
    legacy = plan_path(REVIEW_QUEUE_ID)
    if os.path.exists(legacy) and not os.path.islink(legacy):
        try:
            with open(legacy) as f:
                lp = json.load(f)
            lc = lp.get("candidates") or []
            have = {_dedup_key(c) for c in existing}
            absorbed = [c for c in lc if _dedup_key(c) not in have]
            existing.extend(absorbed)
            plan_created_at = plan_created_at or lp.get("created_at")
            if absorbed:
                print(
                    f"[merge_review_queue] absorbed {len(absorbed)} candidate(s) "
                    "from legacy /tmp plan into the durable store",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[merge_review_queue] legacy plan absorb failed: {e}", file=sys.stderr)
    # Generation stamp: set ONLY when starting a fresh plan (the /tmp plan dies
    # on reboot/tmp-sweep and numbering restarts at 1). The menu bar's durable
    # approved-queue ledger uses this to ignore decisions that belong to a dead
    # plan generation; without it, stale (batch, n) entries silently swallow
    # every new draft after a reset. An existing unstamped plan is left
    # unstamped: back-stamping it "now" would invalidate live decisions.
    if not existing and not plan_created_at:
        plan_created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    seen = {_dedup_key(c) for c in existing}
    added = 0
    merged = list(existing)
    for c in new_cands:
        k = _dedup_key(c)
        if k in seen:
            continue
        seen.add(k)
        merged.append(c)
        added += 1

    stamped, pruned = _sync_with_backend(merged)
    if stamped:
        print(f"[merge_review_queue] stamped stats on {stamped} candidate(s)", file=sys.stderr)
    if pruned:
        print(
            f"[merge_review_queue] pruned {pruned} candidate(s) already retired by the "
            "backend (expired/etc.) before they were reviewed",
            file=sys.stderr,
        )

    plan_obj = {"candidates": merged}
    if plan_created_at:
        plan_obj["created_at"] = plan_created_at

    # This is the actual delivery: if anything below throws, the cycle's drafts
    # were computed but never reached the store the menu bar reads, and the
    # wrapper (run-draft-and-publish.sh) captures this process's whole stdout+
    # stderr with `|| true`, so a crash here previously vanished into a local
    # log nothing central reads — the exact blind spot that cost the 2026-07-08
    # Karol investigation its root cause. Report it like any other handled
    # pipeline failure (see twitter_post_plan.py's post-failure capture).
    try:
        _atomic_write(dst, plan_obj)
        ensure_store_symlink()

        # Refresh the review-request marker the menu bar polls. count = cards
        # actually awaiting review (mirrors s4l_state.candidate_state()'s
        # awaiting_review bucket): approved-unposted and post_failed rows are
        # settled decisions, counting them inflated the badge and misled every
        # human/agent reading the marker.
        pending_count = len(
            [
                c
                for c in merged
                if not c.get("posted")
                and not c.get("terminal")
                and not c.get("post_failed")
                and not c.get("approved")
            ]
        )
        project = ns.project or batch.get("project") or (new_cands[0].get("matched_project") if new_cands else None)
        _atomic_write(
            review_request_path(),
            {
                "batch_id": REVIEW_QUEUE_ID,
                "project": project,
                "count": pending_count,
                "plan_path": dst,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
    except Exception as e:
        print(f"[merge_review_queue] delivery failed (drafts NOT merged into cards): {e}", file=sys.stderr)
        try:
            import sentry_init

            sentry_init.init()
            sentry_init.capture_message(
                f"merge_review_queue delivery failed: {e}",
                level="error",
                tags={"component": "merge_review_queue", "added": str(added)},
                extra={"plan_src": src},
            )
            sentry_init.flush(2.0)
        except Exception:
            pass
        return 1

    print(
        f"[merge_review_queue] merged {added} new draft(s) into {REVIEW_QUEUE_ID} "
        f"({pending_count} pending total) from {os.path.basename(src)}",
        file=sys.stderr,
    )
    # Clean up the consumed batch plan so /tmp doesn't fill with orphans.
    try:
        os.remove(src)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
