#!/usr/bin/env python3
"""twitter_scan_api.py — API-transport twin of twitter_scan.py.

Same job as twitter_scan.py (Phase 1c X search), with twitterapi.io's
advanced_search HTTP API as the eyes instead of a CDP scrape of a logged-in
Chrome. This is the hosted-lane transport: it runs headless on any Linux
container with one env var, no browser, no session cookies, no harness.

Contract parity with twitter_scan.py (the scorer and the cycle shell must not
be able to tell the transports apart):
  - scan(query, project, search_topic, freshness_hours, skip_ids,
    settle_seconds) — identical signature (settle_seconds accepted, unused).
  - Per-tweet dict: handle, text, tweetUrl, datetime (ISO 8601), replies,
    retweets, likes, views, bookmarks, is_repost, reposted_by, plus the
    stamped search_topic / matched_project / query. Additive extra:
    author_followers straight from the API (enrich_twitter_candidates.py
    still re-verifies via fxtwitter and overwrites; having it here means a
    fxtwitter outage no longer zeroes reach scoring on this transport).
  - SCAN_TWEETS_FILE JSONL records ({ts, query, project, search_topic,
    tweets}) — the cycle's shell-side parse reads these unchanged.
  - Sidecar attempt records appended to skill/logs/twitter-scan-attempts.jsonl
    with the same fields, plus additive "transport": "api".
  - Age gate: since_time pinned to freshness_hours in the query AND re-checked
    client-side, mirroring _build_url + the post-scrape filter.
  - Per-query result cap 8 (the scraper's first-8-articles slice) so candidate
    volume per query stays comparable across transports; override via
    S4L_SCAN_API_MAX_TWEETS.

The date-operator stripping regexes are copied from twitter_scan.py rather
than imported: importing that module drags in browser_harness.helpers at
module load, which is exactly the dependency this transport exists to avoid.

Standalone (replicates the cycle's heredoc loop; same env contract:
QUERIES_TMP, FRESHNESS_HOURS_DISCOVER, ENGAGED_TWEET_IDS, SCAN_TWEETS_FILE,
BATCH_ID):
    TWITTERAPI_IO_KEY=... QUERIES_TMP=/tmp/q.json python3 scripts/twitter_scan_api.py

Key resolution: $TWITTERAPI_IO_KEY, else macOS keychain `twitterapi-io-key`.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

_SIDECAR = (
    pathlib.Path.home()
    / "social-autoposter"
    / "skill"
    / "logs"
    / "twitter-scan-attempts.jsonl"
)

_API_BASE = "https://api.twitterapi.io/twitter/tweet/advanced_search"
_STATUS_ID_RE = re.compile(r"/status/(\d+)")

# Copied from twitter_scan.py (see module docstring for why not imported).
_DATE_OPS_RE = re.compile(
    r"\b(since|until|since_time|until_time):(?:\$\(\(.*?\)\)|\S+)",
    re.IGNORECASE,
)
_BASH_GARBAGE_RE = re.compile(
    r"\$\(\(|\$\([^)]*\)|\bFRESHNESS_HOURS_DISCOVER\s*\*\s*\d+\b|\)\)"
)

_MAX_TWEETS_PER_QUERY = int(os.environ.get("S4L_SCAN_API_MAX_TWEETS", "8"))
_HTTP_TIMEOUT_S = float(os.environ.get("S4L_SCAN_API_TIMEOUT_S", "20"))


def _api_key() -> str | None:
    key = os.environ.get("TWITTERAPI_IO_KEY", "").strip()
    if key:
        return key
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "twitterapi-io-key", "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (out.stdout or "").strip() or None
    except Exception:
        return None


def _build_query(query: str, freshness_hours: int) -> str:
    """Mirror twitter_scan._build_url: strip the model's date operators so a
    rogue `since:2020-01-01` can't widen the window, then pin since_time to
    freshness_hours ago. queryType=Latest on the API call plays the role of
    the scraper's forced `f=live` tab."""
    cleaned = _DATE_OPS_RE.sub("", query)
    cleaned = _BASH_GARBAGE_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cap_epoch = int(time.time()) - int(freshness_hours) * 3600
    return f"{cleaned} since_time:{cap_epoch}".strip()


def _parse_created_at(raw: str) -> str:
    """twitterapi.io createdAt is Twitter's classic format
    ('Tue Dec 10 07:00:30 +0000 2024'); the pipeline contract is ISO 8601.
    Pass ISO through untouched, return '' when unparseable (the caller drops
    the tweet, same as the scraper drops articles with a bad datetime)."""
    if not raw:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", raw):
        return raw
    try:
        dt = datetime.datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z") if dt.utcoffset() == datetime.timedelta(0) else dt.isoformat()
    except (ValueError, TypeError):
        return ""


def _dt_epoch(iso: str) -> int | None:
    if not iso:
        return None
    try:
        return int(
            datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        )
    except (ValueError, TypeError):
        return None


def _status_id(url: str) -> str | None:
    m = _STATUS_ID_RE.search(url or "")
    return m.group(1) if m else None


def _map_tweet(t: dict) -> dict | None:
    """One twitterapi.io tweet object -> the scraper's per-tweet dict shape."""
    url = t.get("url") or t.get("twitterUrl") or ""
    author = t.get("author") or {}
    handle = author.get("userName") or ""
    tid = t.get("id") or _status_id(url)
    if not tid:
        return None
    if not url:
        url = f"https://x.com/{handle or 'i'}/status/{tid}"
    # Normalize to x.com so _STATUS_ID_RE / enrich's host regexes both hit.
    url = url.replace("https://twitter.com/", "https://x.com/")
    iso = _parse_created_at(t.get("createdAt") or "")
    if not iso:
        return None
    retweeted = t.get("retweeted_tweet") or t.get("retweetedTweet")
    return {
        "handle": handle,
        "text": t.get("text") or "",
        "tweetUrl": url,
        "datetime": iso,
        "replies": int(t.get("replyCount") or 0),
        "retweets": int(t.get("retweetCount") or 0),
        "likes": int(t.get("likeCount") or 0),
        "views": int(t.get("viewCount") or 0),
        "bookmarks": int(t.get("bookmarkCount") or 0),
        "is_repost": bool(retweeted),
        "reposted_by": handle if retweeted else "",
        "author_followers": int(author.get("followers") or 0),
    }


def _search(query: str, key: str) -> list[dict]:
    url = (
        f"{_API_BASE}?queryType=Latest&query={urllib.parse.quote(query)}"
    )
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        body = json.loads(resp.read().decode())
    return body.get("tweets") or []


def _write_sidecar(rec: dict) -> None:
    try:
        _SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        with _SIDECAR.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # fail-open; sidecar is operator visibility only, not on the data path


def _write_scan_tweets_record(rec: dict) -> None:
    path = os.environ.get("SCAN_TWEETS_FILE")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # fail-open, matching twitter_scan.py


def scan(
    query: str,
    project: str,
    search_topic: str,
    freshness_hours: int = 6,
    skip_ids=None,
    settle_seconds: float = 0.0,  # signature parity with twitter_scan.scan; unused
) -> list:
    key = _api_key()
    if not key:
        raise RuntimeError(
            "no TWITTERAPI_IO_KEY in env and no keychain twitterapi-io-key entry"
        )
    skip = {str(s) for s in (skip_ids or [])}
    api_query = _build_query(query, int(freshness_hours))

    raw = _search(api_query, key)
    mapped = [m for m in (_map_tweet(t) for t in raw) if m]
    pre_count = len(mapped)

    cap_epoch = int(time.time()) - int(freshness_hours) * 3600
    fresh = [
        t
        for t in mapped
        if (_dt_epoch(t["datetime"]) or 0) >= cap_epoch
    ]
    dropped_age = pre_count - len(fresh)

    kept = [t for t in fresh if _status_id(t["tweetUrl"]) not in skip]
    dropped_skip = len(fresh) - len(kept)
    kept = kept[:_MAX_TWEETS_PER_QUERY]

    for t in kept:
        t["search_topic"] = search_topic
        t["matched_project"] = project
        t["query"] = query

    _write_sidecar(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ts_epoch": int(time.time()),
            "query": query,
            "project": project,
            "search_topic": search_topic,
            "freshness_hours": int(freshness_hours),
            "url": f"{_API_BASE}?queryType=Latest&query={urllib.parse.quote(api_query)}",
            "pre_count": pre_count,
            "kept_after_age": len(fresh),
            "dropped_age": dropped_age,
            "kept_after_skip": len(kept),
            "dropped_skip": dropped_skip,
            "batch_id": os.environ.get("BATCH_ID"),
            "cycle_variant": os.environ.get("TWITTER_CYCLE_VARIANT"),
            "transport": "api",
        }
    )

    _write_scan_tweets_record(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "query": query,
            "project": project,
            "search_topic": search_topic,
            "tweets": kept,
        }
    )
    return kept


def main() -> int:
    """Replicates the cycle's browser-harness heredoc loop, same env contract,
    same per-query ok/err stdout lines, so the hosted cycle swaps transports
    by swapping this one invocation."""
    queries_path = os.environ.get("QUERIES_TMP")
    if not queries_path or not os.path.exists(queries_path):
        print("twitter_scan_api: QUERIES_TMP unset or missing", file=sys.stderr)
        return 1
    with open(queries_path) as f:
        queries = json.load(f)
    freshness = int(os.environ.get("FRESHNESS_HOURS_DISCOVER", "6"))
    skip_ids = json.loads(os.environ.get("ENGAGED_TWEET_IDS", "[]"))
    failures = 0
    for q in queries:
        project = q.get("project", "")
        query = q.get("query", "")
        topic = q.get("search_topic", "")
        t0 = time.time()
        try:
            kept = scan(
                query=query,
                project=project,
                search_topic=topic,
                freshness_hours=freshness,
                skip_ids=skip_ids,
            )
            dt = time.time() - t0
            print(
                f"  ok  project={project!r}  q={query[:50]!r}  kept={len(kept)}  in {dt:.1f}s",
                flush=True,
            )
        except Exception as e:
            failures += 1
            dt = time.time() - t0
            print(
                f"  err project={project!r}  q={query[:50]!r}  in {dt:.1f}s  {type(e).__name__}: {e}",
                flush=True,
            )
    return 0 if failures < max(1, len(queries)) else 1


if __name__ == "__main__":
    sys.exit(main())
