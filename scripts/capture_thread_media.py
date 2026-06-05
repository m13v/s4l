#!/usr/bin/env python3
"""Deterministically capture + persist + format thread media for the prep step.

Companion to the main Twitter posting cycle (run-twitter-cycle.sh Phase 2b-prep,
2026-06-03 thread-media feature). The prep prompt forbids the model from calling
twitter_browser.py, so the SHELL pre-fetches the media of every candidate the
model is about to draft against, in ONE cheap browser pass, then:

  1. persists each candidate's media into twitter_candidates.thread_media (so the
     record survives independent of the model), and
  2. emits a "MEDIA CONTEXT" prompt block to stdout so the reply-writer can "see"
     the image / video / GIF / link-card it is replying to instead of replying
     text-blind.

Input: a TSV file, one `candidate_id<TAB>tweet_url` per line (built by the
CANDIDATE_BLOCK loop in run-twitter-cycle.sh).

Media shape per item: {url, alt, type}, type in image|video|gif|card. An empty
list [] is valid and meaningful ("captured, none found", distinct from NULL =
"never captured").

Usage:
    python3 scripts/capture_thread_media.py --urls-file /tmp/urls.tsv \\
        [--scroll 1] [--no-persist]

Output:
    stdout  -> the MEDIA CONTEXT prompt block (empty string if no media at all)
    stderr  -> per-candidate diagnostics + a final JSON summary line
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_patch  # noqa: E402

# Imported lazily inside main() so --help works without a browser / playwright.


def _load_pairs(urls_file):
    """Return [(candidate_id:str, url:str)] from a `cid<TAB>url` TSV file."""
    pairs = []
    with open(urls_file) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if "\t" in line:
                cid, url = line.split("\t", 1)
            else:
                # Tolerate a bare-URL line (no cid); skip it, we can't key it.
                continue
            cid = cid.strip()
            url = url.strip()
            if cid and url:
                pairs.append((cid, url))
    return pairs


def _persist(candidate_id, media, repost=None):
    """Persist media (+ repost provenance) onto twitter_candidates via set_media.

    repost is {"is_repost": bool, "reposted_by": str} or None.

    POSITIVE-ONLY for repost: we send is_repost/reposted_by ONLY when we detected
    a repost (is_repost True). The candidate URL is the ORIGINAL tweet permalink,
    where X does NOT render the "<X> reposted" timeline banner, so fresh detection
    here is almost always False; sending that False would clobber the authoritative
    flag set at discovery time (the server COALESCEs only on null, not on false).
    Omitting the keys leaves COALESCE(null, is_repost) = the stored value intact.
    """
    payload = {"id": int(candidate_id), "action": "set_media", "thread_media": media}
    if repost is not None and bool(repost.get("is_repost", False)):
        payload["is_repost"] = True
        payload["reposted_by"] = repost.get("reposted_by", "") or ""
    resp = api_patch(
        "/api/v1/twitter-candidates/by-id", payload,
        ok_on_conflict=True, ok_on_404=True,
    )
    if (resp or {}).get("_not_found"):
        return False, "CANDIDATE_NOT_FOUND"
    if not (resp or {}).get("ok"):
        return False, (resp or {}).get("error") or "SET_MEDIA_FAILED"
    return True, None


def _fetch_stored_repost(candidate_id):
    """Read the repost flag stored at discovery time for one candidate.

    Repost provenance is detected and stored by the DISCOVERY scan (the timeline
    is the only place X renders the "<X> reposted" banner). The prep step runs on
    the original-tweet permalink, which never shows that banner, so we read the
    authoritative stored value here to surface it to the model in the prompt block.

    Returns {"is_repost": bool, "reposted_by": str}; defaults to a non-repost on
    any error / missing candidate (fail-open: never block the cycle).
    """
    try:
        resp = api_get(
            "/api/v1/twitter-candidates/by-id",
            {"id": int(candidate_id)}, ok_on_404=True,
        )
    except Exception:
        return {"is_repost": False, "reposted_by": ""}
    cand = (resp or {}).get("candidate") or {}
    return {
        "is_repost": bool(cand.get("is_repost", False)),
        "reposted_by": cand.get("reposted_by", "") or "",
    }


def _format_item(item):
    """One '  - <type>: "<alt>" (<url>)' line for the prompt block."""
    t = (item.get("type") or "media").strip()
    alt = (item.get("alt") or "").strip()
    url = (item.get("url") or "").strip()
    alt_part = f'"{alt}"' if alt else "[no description]"
    return f"  - {t}: {alt_part} ({url})"


def _build_block(captured):
    """captured: list of (candidate_id, media_list, repost). Returns prompt block.

    A section is emitted for any candidate that has media OR is a repost, so the
    model is told about repost provenance even when the tweet carries no media.
    """
    sections = []
    for cid, media, repost in captured:
        is_repost = bool((repost or {}).get("is_repost"))
        if not media and not is_repost:
            continue
        body = []
        if is_repost:
            rb = ((repost or {}).get("reposted_by") or "").strip()
            who = f"@{rb}" if rb else "another account"
            body.append(
                f"  - REPOST: this is a repost surfaced by {who}. The tweet text "
                "and any media below were written by the ORIGINAL author, not the "
                "reposter. Reply to the original author's content; do not address "
                "the reposter."
            )
        if media:
            body.extend(_format_item(it) for it in media)
        sections.append(f"Candidate {cid}:\n" + "\n".join(body))
    if not sections:
        return ""
    header = (
        "## MEDIA IN THESE THREADS\n"
        "Some candidate threads contain images, videos, GIFs, link-cards, or are "
        "reposts. This is part of the content you are replying to: react to what "
        "the tweet VISUALLY shows, not just its text, and treat reposted content "
        "as the original author's. A candidate NOT listed here had no media and is "
        "not a repost (or capture was skipped); reply to its text as usual. "
        "Descriptions marked [no description] mean the media had no alt-text, so "
        "infer from the thread text and the media type."
    )
    return header + "\n\n" + "\n".join(sections) + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--urls-file", required=True,
                   help="TSV: one candidate_id<TAB>tweet_url per line.")
    p.add_argument("--scroll", type=int, default=1,
                   help="scroll_count passed to the batch scraper (default 1).")
    p.add_argument("--no-persist", action="store_true",
                   help="Skip writing thread_media to the DB (format only).")
    args = p.parse_args()

    pairs = _load_pairs(args.urls_file)
    if not pairs:
        # Nothing to do; emit empty block, exit clean so the shell continues.
        print("", end="")
        print(json.dumps({"captured": 0, "persisted": 0, "with_media": 0, "reposts": 0}), file=sys.stderr)
        return

    # Lazy import so an empty/short-circuit run never pays the playwright cost.
    from twitter_browser import scrape_many_thread_media

    urls = [url for _cid, url in pairs]
    try:
        batch = scrape_many_thread_media(urls, scroll_count=args.scroll)
    except Exception as e:
        # Browser failure must NOT break the cycle: emit empty block, log, exit 0.
        print("", end="")
        print(json.dumps({"error": "SCRAPE_FAILED", "detail": str(e)}), file=sys.stderr)
        return

    # Map url -> {media, repost} (results echo the input url verbatim as thread_url).
    by_url = {}
    for r in (batch or {}).get("results", []):
        by_url[r.get("thread_url")] = {
            "media": r.get("media") or [],
            "repost": {
                "is_repost": bool(r.get("is_repost", False)),
                "reposted_by": r.get("reposted_by", "") or "",
            },
        }

    captured = []          # (cid, media, repost) for ALL pairs (media may be [])
    persisted = 0
    with_media = 0
    reposts = 0
    for cid, url in pairs:
        rec = by_url.get(url) or {}
        media = rec.get("media", [])
        fresh = rec.get("repost", {"is_repost": False, "reposted_by": ""})
        # Authoritative repost flag comes from discovery (stored). Fresh permalink
        # detection is a rare bonus; prefer stored, fall back to fresh.
        stored = _fetch_stored_repost(cid)
        if stored.get("is_repost"):
            repost = stored
        elif fresh.get("is_repost"):
            repost = fresh
        else:
            repost = {"is_repost": False, "reposted_by": ""}
        captured.append((cid, media, repost))
        if media:
            with_media += 1
        if repost.get("is_repost"):
            reposts += 1
        if not args.no_persist:
            # _persist is positive-only for repost: pass the FRESH detection (so a
            # newly-seen banner is recorded) but never the stored value back (no
            # round-trip clobber). Media is always persisted.
            ok, err = _persist(cid, media, fresh)
            if ok:
                persisted += 1
            else:
                print(f"[capture_thread_media] persist failed cid={cid}: {err}",
                      file=sys.stderr)

    block = _build_block(captured)
    # stdout = the prompt block ONLY (shell captures it verbatim).
    sys.stdout.write(block)
    print(json.dumps({
        "captured": len(captured),
        "persisted": persisted,
        "with_media": with_media,
        "reposts": reposts,
        "urls_visited": (batch or {}).get("urls_visited", 0),
    }), file=sys.stderr)


if __name__ == "__main__":
    main()
