#!/usr/bin/env python3
"""
Normalize reviewed TweetClaw X/Twitter results for score_twitter_candidates.py.

The script reads a TweetClaw/OpenClaw JSON export from stdin or --file and
prints a JSON array shaped like social-autoposter's Twitter scanner output.
It does not call X/Twitter, TweetClaw, OpenClaw, or the S4L API itself.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from typing import Any, Optional


_STATUS_RE = re.compile(r"/status/(\d{15,19})(?:[/?#]|$)")
_HANDLE_RE = re.compile(r"x\.com/([^/?#]+)/status/")


def _first(record: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _nested(record: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    for key in keys:
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _number(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned.endswith(("K", "k")):
            try:
                return int(float(cleaned[:-1]) * 1000)
            except ValueError:
                return 0
        if cleaned.endswith(("M", "m")):
            try:
                return int(float(cleaned[:-1]) * 1000000)
            except ValueError:
                return 0
        try:
            return int(float(cleaned))
        except ValueError:
            return 0
    return 0


def _status_id(url: str) -> Optional[str]:
    match = _STATUS_RE.search(url)
    return match.group(1) if match else None


def _handle_from_url(url: str) -> str:
    match = _HANDLE_RE.search(url)
    return match.group(1) if match else ""


def _clean_handle(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lstrip("@")


def _tweet_url(record: dict[str, Any], handle: str) -> str:
    direct = _first(
        record,
        (
            "tweetUrl",
            "tweet_url",
            "thread_url",
            "url",
            "link",
            "permalink",
            "status_url",
        ),
    )
    if isinstance(direct, str) and _status_id(direct):
        return direct

    tweet_id = _first(record, ("id", "tweet_id", "tweetId", "status_id", "statusId"))
    if tweet_id and handle:
        return f"https://x.com/{handle}/status/{tweet_id}"
    return ""


def _iter_records(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_records(item)
        return

    if not isinstance(value, dict):
        return

    text = _first(value, ("text", "full_text", "fullText", "content", "body"))
    url = _first(value, ("tweetUrl", "tweet_url", "thread_url", "url", "link", "permalink"))
    if text or (isinstance(url, str) and _status_id(url)):
        yield value

    for key in ("data", "tweets", "results", "items", "records", "statuses"):
        child = value.get(key)
        if child is not None:
            yield from _iter_records(child)


def normalize_record(
    record: dict[str, Any],
    *,
    project: str,
    search_topic: str,
    query: str,
) -> Optional[dict[str, Any]]:
    user = _nested(record, ("author", "user", "account", "profile"))
    handle = _clean_handle(
        _first(
            record,
            ("handle", "username", "screen_name", "screenName", "author_username"),
        )
        or _first(user, ("handle", "username", "screen_name", "screenName"))
    )

    url = _tweet_url(record, handle)
    if not handle:
        handle = _handle_from_url(url)
    if not _status_id(url):
        return None

    metrics = _nested(record, ("public_metrics", "metrics", "stats", "engagement"))
    text = _first(record, ("text", "full_text", "fullText", "content", "body")) or ""

    out = {
        "handle": handle,
        "text": str(text),
        "tweetUrl": url,
        "datetime": _first(
            record,
            ("datetime", "created_at", "createdAt", "created", "timestamp"),
        )
        or "",
        "replies": _number(
            _first(record, ("replies", "reply_count", "replyCount"))
            or _first(metrics, ("reply_count", "replies"))
        ),
        "retweets": _number(
            _first(record, ("retweets", "retweet_count", "retweetCount", "reposts"))
            or _first(metrics, ("retweet_count", "retweets", "reposts"))
        ),
        "likes": _number(
            _first(record, ("likes", "like_count", "likeCount", "favorites"))
            or _first(metrics, ("like_count", "likes", "favorites"))
        ),
        "views": _number(
            _first(record, ("views", "view_count", "viewCount", "impressions"))
            or _first(metrics, ("view_count", "views", "impressions"))
        ),
        "bookmarks": _number(
            _first(record, ("bookmarks", "bookmark_count", "bookmarkCount"))
            or _first(metrics, ("bookmark_count", "bookmarks"))
        ),
        "author_followers": _number(
            _first(record, ("author_followers", "followers", "followers_count", "followersCount"))
            or _first(user, ("followers", "followers_count", "followersCount"))
            or _first(metrics, ("author_followers", "followers"))
        ),
        "matched_project": project,
        "search_topic": search_topic,
        "query": query,
        "source": "tweetclaw",
    }

    media = record.get("media")
    if isinstance(media, list):
        out["media"] = media
    return out


def normalize_payload(
    payload: Any,
    *,
    project: str,
    search_topic: str,
    query: str,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for record in _iter_records(payload):
        item = normalize_record(
            record,
            project=project,
            search_topic=search_topic,
            query=query,
        )
        if not item:
            continue
        sid = _status_id(item["tweetUrl"]) or item["tweetUrl"]
        if sid in seen:
            continue
        seen.add(sid)
        out.append(item)
    return out


def _load_payload(path: Optional[str]) -> Any:
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return json.load(sys.stdin)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="TweetClaw JSON export. Reads stdin when omitted.")
    parser.add_argument("--project", required=True, help="social-autoposter project name.")
    parser.add_argument("--search-topic", required=True, help="Assigned search topic.")
    parser.add_argument("--query", required=True, help="Literal X/Twitter query that produced the export.")
    args = parser.parse_args()

    payload = _load_payload(args.file)
    json.dump(
        normalize_payload(
            payload,
            project=args.project,
            search_topic=args.search_topic,
            query=args.query,
        ),
        sys.stdout,
    )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
