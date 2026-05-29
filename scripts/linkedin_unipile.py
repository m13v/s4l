#!/usr/bin/env python3
"""Reusable UniPile LinkedIn functions for the post-commenting pipeline.

Scope is deliberately narrow: SEARCH posts + COMMENT on a post. No stats, no
DMs, no reactions. These are the units the engagement pipeline reuses.

Credentials resolve env-first, keychain-second:
  UNIPILE_DSN          | keychain "unipile-dsn"                      e.g. api45.unipile.com:17570
  UNIPILE_API_KEY      | keychain "unipile-api-key"
  UNIPILE_ACCOUNT_ID   | keychain "unipile-account-id-linkedin-m13v" e.g. wHDpysUnRbm7Q0lvyv9pQQ

CLI:
  python3 linkedin_unipile.py probe
  python3 linkedin_unipile.py search --keywords "ai agents" --date-posted past_week --limit 5
  python3 linkedin_unipile.py search --url "https://www.linkedin.com/search/results/posts/?keywords=..."
  python3 linkedin_unipile.py comment --social-id "urn:li:activity:7332661864792854528" --text "..."
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

COMMENT_CHAR_LIMIT = 1250  # LinkedIn comment limit, enforced by UniPile too.
DATE_POSTED_VALUES = ("past_24h", "past_week", "past_month")


class UnipileConfigError(RuntimeError):
    """Missing or unresolvable UniPile credentials."""


class UnipileApiError(RuntimeError):
    """UniPile returned a non-2xx response."""

    def __init__(self, message, status, response):
        super().__init__(message)
        self.status = status
        self.response = response


def _keychain(service):
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return (out.stdout.strip() or None)
    except Exception:
        pass
    return None


def get_config():
    dsn = os.environ.get("UNIPILE_DSN") or _keychain("unipile-dsn")
    api_key = os.environ.get("UNIPILE_API_KEY") or _keychain("unipile-api-key")
    account_id = (os.environ.get("UNIPILE_ACCOUNT_ID")
                  or _keychain("unipile-account-id-linkedin-m13v"))
    missing = [name for name, val in (
        ("UNIPILE_DSN / keychain:unipile-dsn", dsn),
        ("UNIPILE_API_KEY / keychain:unipile-api-key", api_key),
        ("UNIPILE_ACCOUNT_ID / keychain:unipile-account-id-linkedin-m13v", account_id),
    ) if not val]
    if missing:
        raise UnipileConfigError("missing UniPile config: " + "; ".join(missing))
    return {"dsn": dsn, "api_key": api_key, "account_id": account_id}


def _request(method, path, query=None, body=None, timeout=30):
    cfg = get_config()
    url = "https://" + cfg["dsn"] + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"X-API-KEY": cfg["api_key"], "accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        return 0, {"error": "network", "detail": str(exc)}
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {"_raw": raw}
    return status, parsed


def make_our_url(social_id, comment_urn=None):
    """LinkedIn permalink for a post (and our comment within it, when known)."""
    base = "https://www.linkedin.com/feed/update/" + social_id + "/"
    if comment_urn:
        return base + "?commentUrn=" + urllib.parse.quote(comment_urn, safe="")
    return base


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def _normalize_post(item):
    if not isinstance(item, dict):
        return {"raw": item}
    author = item.get("author")
    if isinstance(author, dict):
        author_name = _first(author, "name", "public_name", "full_name")
        author_headline = _first(author, "headline", "occupation", "subtitle")
        author_id = _first(author, "id", "public_identifier", "provider_id")
    else:
        author_name = _first(item, "author_name", "actor_name")
        author_headline = None
        author_id = None
    social_id = _first(item, "social_id", "share_urn", "urn")
    return {
        "social_id": social_id,
        "id": _first(item, "id"),
        "share_url": _first(item, "share_url", "permalink", "url"),
        "text": _first(item, "text", "commentary", "content"),
        "author_name": author_name,
        "author_headline": author_headline,
        "author_id": author_id,
        "reaction_count": _first(item, "reaction_counter", "reaction_count",
                                 "reactions_count", "likes"),
        "comment_count": _first(item, "comment_counter", "comment_count",
                                "comments_count"),
        "raw": item,
    }


def search_posts(keywords=None, *, url=None, date_posted=None, sort_by="date",
                 content_type=None, author_keywords=None, limit=10, cursor=None,
                 account_id=None):
    """Search LinkedIn posts. Pass keywords= (structured) or url= (paste a
    LinkedIn search-results URL). Returns {items, cursor, count, raw}."""
    cfg = get_config()
    query = {"account_id": account_id or cfg["account_id"], "limit": limit}
    if cursor:
        query["cursor"] = cursor
    if url:
        body = {"url": url}
    else:
        if not keywords:
            raise ValueError("search_posts requires keywords= or url=")
        body = {"api": "classic", "category": "posts", "keywords": keywords}
        if sort_by:
            body["sort_by"] = sort_by
        if date_posted:
            body["date_posted"] = date_posted
        if content_type:
            body["content_type"] = content_type
        if author_keywords:
            body["author"] = {"keywords": author_keywords}
    status, resp = _request("POST", "/api/v1/linkedin/search", query=query, body=body)
    if status != 200:
        raise UnipileApiError("search failed: HTTP %s" % status, status, resp)
    if isinstance(resp, dict):
        items = resp.get("items")
        next_cursor = resp.get("cursor")
        if not next_cursor and isinstance(resp.get("paging"), dict):
            next_cursor = resp["paging"].get("cursor")
    elif isinstance(resp, list):
        items, next_cursor = resp, None
    else:
        items, next_cursor = None, None
    items = items or []
    norm = [_normalize_post(it) for it in items]
    return {"items": norm, "cursor": next_cursor, "count": len(norm), "raw": resp}


def _extract_comment_urn(resp):
    """Best-effort: pull a comment URN from the create-comment response.
    UniPile's documented success body is just {object:"CommentSent"} with no
    URN, so this usually returns None; we parse anyway in case the live
    response is richer."""
    if not isinstance(resp, dict):
        return None
    for key in ("comment_id", "comment_urn", "social_id", "id", "urn"):
        val = resp.get(key)
        if isinstance(val, str) and "urn:li:comment" in val:
            return val
    return None


def comment_on_post(social_id, text, *, comment_id=None, mentions=None,
                    account_id=None):
    """Comment on a post identified by its social_id (urn:li:activity:...).
    comment_id replies to an existing comment; mentions uses {{0}} placeholders.
    Returns {ok, status, response, comment_urn, our_url}."""
    if not social_id:
        raise ValueError("social_id required")
    if not text or not text.strip():
        raise ValueError("text required")
    if len(text) > COMMENT_CHAR_LIMIT:
        raise ValueError("comment text exceeds LinkedIn %d-char limit (%d)"
                         % (COMMENT_CHAR_LIMIT, len(text)))
    cfg = get_config()
    body = {"account_id": account_id or cfg["account_id"], "text": text}
    if comment_id:
        body["comment_id"] = comment_id
    if mentions:
        body["mentions"] = mentions
    # social_id (urn:li:activity:N) carries only colons, valid as a path segment;
    # the UniPile docs use it unencoded, so we leave it as-is.
    status, resp = _request("POST", "/api/v1/posts/" + social_id + "/comments",
                            body=body)
    ok = status in (200, 201) and not (isinstance(resp, dict)
                                       and resp.get("object") == "error")
    comment_urn = _extract_comment_urn(resp)
    return {
        "ok": ok,
        "status": status,
        "response": resp,
        "comment_urn": comment_urn,
        "our_url": make_our_url(social_id, comment_urn),
    }


def accounts():
    """GET /api/v1/accounts — used by `probe` to validate credentials."""
    return _request("GET", "/api/v1/accounts")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _cmd_probe(_args):
    cfg = get_config()
    redacted = cfg["api_key"][:6] + "..." + cfg["api_key"][-4:]
    print("dsn=%s account_id=%s api_key=%s" % (cfg["dsn"], cfg["account_id"], redacted),
          file=sys.stderr)
    status, resp = accounts()
    if status != 200:
        print(json.dumps({"ok": False, "status": status, "response": resp}, indent=2))
        return 1
    items = resp.get("items", resp) if isinstance(resp, dict) else resp
    summary = []
    for a in (items if isinstance(items, list) else []):
        srcs = a.get("sources") or [{}]
        summary.append({"id": a.get("id"), "type": a.get("type"),
                        "name": a.get("name"),
                        "status": (srcs[0] or {}).get("status")})
    print(json.dumps({"ok": True, "status": status, "accounts": summary}, indent=2))
    return 0


def _cmd_search(args):
    res = search_posts(
        keywords=args.keywords, url=args.url, date_posted=args.date_posted,
        sort_by=args.sort_by, content_type=args.content_type,
        author_keywords=args.author_keywords, limit=args.limit, cursor=args.cursor,
    )
    if args.raw:
        print(json.dumps(res["raw"], indent=2))
        return 0
    slim = [{k: it[k] for k in ("social_id", "author_name", "author_headline",
                                "reaction_count", "comment_count", "share_url", "text")}
            for it in res["items"]]
    print(json.dumps({"count": res["count"], "cursor": res["cursor"],
                      "items": slim}, indent=2, ensure_ascii=False))
    return 0


def _cmd_comment(args):
    res = comment_on_post(args.social_id, args.text, comment_id=args.reply_to)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res["ok"] else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="UniPile LinkedIn search + comment")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="validate credentials via /accounts")

    s = sub.add_parser("search", help="search LinkedIn posts")
    s.add_argument("--keywords")
    s.add_argument("--url", help="paste a LinkedIn search-results URL instead of keywords")
    s.add_argument("--date-posted", dest="date_posted", choices=DATE_POSTED_VALUES)
    s.add_argument("--sort-by", dest="sort_by", default="date",
                   choices=["date", "relevance"])
    s.add_argument("--content-type", dest="content_type")
    s.add_argument("--author-keywords", dest="author_keywords")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--cursor")
    s.add_argument("--raw", action="store_true", help="print the raw API response")

    c = sub.add_parser("comment", help="comment on a post")
    c.add_argument("--social-id", dest="social_id", required=True,
                   help="urn:li:activity:... (use a post's social_id)")
    c.add_argument("--text", required=True)
    c.add_argument("--reply-to", dest="reply_to",
                   help="comment_id to reply to an existing comment")

    args = p.parse_args(argv)
    try:
        if args.cmd == "probe":
            return _cmd_probe(args)
        if args.cmd == "search":
            return _cmd_search(args)
        if args.cmd == "comment":
            return _cmd_comment(args)
    except UnipileConfigError as exc:
        print("CONFIG ERROR: %s" % exc, file=sys.stderr)
        return 2
    except UnipileApiError as exc:
        print(json.dumps({"ok": False, "status": exc.status,
                          "response": exc.response}, indent=2), file=sys.stderr)
        return 1
    except (ValueError, KeyError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
