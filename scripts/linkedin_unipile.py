#!/usr/bin/env python3
"""Reusable UniPile LinkedIn functions for the post-commenting pipeline.

Scope: SEARCH posts + COMMENT on a post + REACT (like) to a post. No stats,
no DMs. The COMMENT path auto-likes the parent post by default (also_like),
mirroring the Twitter proactive-comment path. These are the units the LinkedIn
post-comments pipeline reuses.

Credentials resolve env-first, keychain-second:
  UNIPILE_DSN          | keychain "unipile-dsn"                      e.g. api45.unipile.com:17570
  UNIPILE_API_KEY      | keychain "unipile-api-key"
  UNIPILE_ACCOUNT_ID   | keychain "unipile-account-id-linkedin-m13v" e.g. wHDpysUnRbm7Q0lvyv9pQQ

CLI:
  python3 linkedin_unipile.py probe
  python3 linkedin_unipile.py search --keywords "ai agents" --date-posted past_week --limit 5
  python3 linkedin_unipile.py search --url "https://www.linkedin.com/search/results/posts/?keywords=..."
  python3 linkedin_unipile.py comment --social-id "urn:li:activity:7332661864792854528" --text "..."
  python3 linkedin_unipile.py react   --social-id "urn:li:activity:7332661864792854528"
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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


# The live UniPile account lives under matt@mediar.ai. An older (dead) i@m13v.com
# trial shares the same keychain service names, so we must look up the scoped
# entry first; an unscoped `-w` returns whichever the keychain orders first
# (often the stale one). Override with UNIPILE_KEYCHAIN_ACCOUNT.
KEYCHAIN_ACCOUNT = os.environ.get("UNIPILE_KEYCHAIN_ACCOUNT", "matt@mediar.ai")


def _keychain(service, account=None):
    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd += ["-a", account]
    cmd += ["-w"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return (out.stdout.strip() or None)
    except Exception:
        pass
    return None


def _keychain_any(service):
    return _keychain(service, KEYCHAIN_ACCOUNT) or _keychain(service)


def get_config():
    dsn = os.environ.get("UNIPILE_DSN") or _keychain_any("unipile-dsn")
    api_key = os.environ.get("UNIPILE_API_KEY") or _keychain_any("unipile-api-key")
    account_id = (os.environ.get("UNIPILE_ACCOUNT_ID")
                  or _keychain_any("unipile-account-id-linkedin-m13v"))
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
        author_id = _first(author, "id", "provider_id", "public_identifier")
        author_public_id = _first(author, "public_identifier")
    else:
        author_name = _first(item, "author_name", "actor_name")
        author_headline = None
        author_id = None
        author_public_id = None
    social_id = _first(item, "social_id", "share_urn", "urn")
    return {
        "social_id": social_id,
        "id": _first(item, "id"),
        "share_url": _first(item, "share_url", "permalink", "url"),
        "text": _first(item, "text", "commentary", "content"),
        "author_name": author_name,
        "author_headline": author_headline,
        "author_id": author_id,
        "author_public_id": author_public_id,
        "author_followers": None,  # filled by _enrich_followers when requested
        "reaction_count": _first(item, "reaction_counter", "reaction_count",
                                 "reactions_count", "likes"),
        "comment_count": _first(item, "comment_counter", "comment_count",
                                "comments_count"),
        "repost_count": _first(item, "repost_counter", "repost_count",
                               "reposts_count", "shares_count"),
        "is_repost": item.get("is_repost"),
        # parsed_datetime is the authoritative ISO timestamp; `date` is a
        # relative string ("now", "5h", "2d") that's harder to score on.
        "posted_at": _first(item, "parsed_datetime", "date_posted", "created_at"),
        "raw": item,
    }


def search_posts(keywords=None, *, url=None, date_posted=None, sort_by="date",
                 content_type=None, author_keywords=None, limit=10, cursor=None,
                 account_id=None, with_followers=False):
    """Search LinkedIn posts. Pass keywords= (structured) or url= (paste a
    LinkedIn search-results URL). Returns {items, cursor, count, raw}.

    with_followers=True makes one extra GET /users/{id} per distinct author to
    fill author_followers (search alone never returns follower count). UniPile
    is flat-rate, so the extra calls are free; they let the LinkedIn scorer use
    its author-reach multiplier exactly as the browser path did."""
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
    if with_followers:
        _enrich_followers(norm, account_id=query["account_id"])
    return {"items": norm, "cursor": next_cursor, "count": len(norm), "raw": resp}


def get_profile(identifier, *, account_id=None, timeout=30):
    """GET /api/v1/users/{identifier} — returns the LinkedIn profile dict
    (follower_count, connections_count, is_influencer, headline, ...).
    identifier accepts either the public_identifier (e.g. 'mahendraakula') or
    the internal provider id (e.g. 'ACoAA...'). Raises UnipileApiError on non-200."""
    cfg = get_config()
    path = "/api/v1/users/" + urllib.parse.quote(str(identifier), safe="")
    status, resp = _request("GET", path,
                            query={"account_id": account_id or cfg["account_id"]},
                            timeout=timeout)
    if status != 200:
        raise UnipileApiError("profile failed: HTTP %s" % status, status, resp)
    return resp


def _enrich_followers(items, *, account_id=None):
    """Fill author_followers on each normalized item via get_profile. One call
    per DISTINCT author (cached), non-fatal: a failed lookup leaves the field
    None so the scorer falls back to its neutral reach multiplier."""
    cache = {}
    for it in items:
        ident = it.get("author_public_id") or it.get("author_id")
        if not ident:
            continue
        if ident not in cache:
            try:
                prof = get_profile(ident, account_id=account_id)
                cache[ident] = prof.get("follower_count") if isinstance(prof, dict) else None
            except Exception:
                cache[ident] = None
        it["author_followers"] = cache[ident]


def _age_hours_from(posted_at):
    """ISO timestamp → hours since, rounded. None if unparseable."""
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600.0, 2)
    except (ValueError, TypeError):
        return None


def to_pipeline_results(items):
    """Map normalized search items to the candidate shape run-linkedin.sh's
    Phase A envelope + score_linkedin_candidates.py expect. UniPile returns the
    post URN directly in social_id, so post_url/activity_id are always present
    (no click-to-resolve, no namespace guessing like the browser path needs)."""
    results = []
    for it in items:
        social_id = it.get("social_id") or ""
        m = re.search(r"(\d{16,19})", social_id)
        activity_id = m.group(1) if m else None
        post_url = make_our_url(social_id) if social_id else None
        pub = it.get("author_public_id")
        author_profile_url = ("https://www.linkedin.com/in/%s/" % pub) if pub else None
        results.append({
            "post_url": post_url,
            "activity_id": activity_id,
            "all_urns": [activity_id] if activity_id else [],
            "social_id": social_id or None,
            "author_name": it.get("author_name"),
            "author_headline": it.get("author_headline"),
            "author_profile_url": author_profile_url,
            "author_followers": it.get("author_followers"),
            "post_text": it.get("text"),
            "age_hours": _age_hours_from(it.get("posted_at")),
            "reactions": int(it.get("reaction_count") or 0),
            "comments": int(it.get("comment_count") or 0),
            "reposts": int(it.get("repost_count") or 0),
            "is_repost": bool(it.get("is_repost")),
        })
    return results


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


REACTION_TYPES = ("like", "celebrate", "support", "love", "insightful", "funny")


def react_to_post(social_id, *, reaction_type="like", comment_id=None,
                  account_id=None):
    """Add a reaction (default 'like') to a post or one of its comments.

    UniPile endpoint: POST /api/v1/posts/reaction with a flat body carrying
    post_id (the post's social_id), account_id, and reaction_type. Pass
    comment_id to react to a specific comment instead of the post itself.
    Returns {ok, status, response}."""
    if not social_id:
        raise ValueError("social_id required")
    if reaction_type not in REACTION_TYPES:
        raise ValueError("reaction_type must be one of %s (got %r)"
                         % (", ".join(REACTION_TYPES), reaction_type))
    cfg = get_config()
    body = {
        "account_id": account_id or cfg["account_id"],
        "post_id": social_id,
        "reaction_type": reaction_type,
    }
    if comment_id:
        body["comment_id"] = comment_id
    status, resp = _request("POST", "/api/v1/posts/reaction", body=body)
    # LinkedIn happily 201s when you re-like something already liked, so any
    # 2xx (and the non-error object) is success.
    ok = status in (200, 201) and not (isinstance(resp, dict)
                                       and resp.get("object") == "error")
    return {"ok": ok, "status": status, "response": resp}


def comment_on_post(social_id, text, *, comment_id=None, mentions=None,
                    account_id=None, also_like=True):
    """Comment on a post identified by its social_id (urn:li:activity:...).
    comment_id replies to an existing comment; mentions uses {{0}} placeholders.

    also_like=True (default) auto-likes the parent post right after a successful
    comment, mirroring the Twitter proactive-comment path. The like is fail-soft:
    a reaction error can NEVER fail the comment. The outcome is carried back in
    the `liked` / `like_result` keys for the caller to log.

    Returns {ok, status, response, comment_urn, our_url, liked, like_result}."""
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

    # Auto-like the parent post on every successful comment. Wrapped so a like
    # failure can NEVER fail the comment itself; the outcome rides out in
    # like_result for the caller to log.
    like_result = {"ok": False, "error": "not_attempted"}
    if ok and also_like:
        try:
            like_result = react_to_post(social_id, reaction_type="like",
                                        account_id=account_id)
        except Exception as exc:  # noqa: BLE001 - like must never break commenting
            like_result = {"ok": False, "error": str(exc)}

    return {
        "ok": ok,
        "status": status,
        "response": resp,
        "comment_urn": comment_urn,
        "our_url": make_our_url(social_id, comment_urn),
        "liked": bool(like_result.get("ok")),
        "like_result": like_result,
    }


def list_comments(social_id, *, account_id=None, limit=50, cursor=None):
    """GET /api/v1/posts/{social_id}/comments — read a post's comments back.
    Used by Phase B to confirm our just-posted comment actually rendered.
    Returns {items, count, raw}."""
    if not social_id:
        raise ValueError("social_id required")
    cfg = get_config()
    query = {"account_id": account_id or cfg["account_id"], "limit": limit}
    if cursor:
        query["cursor"] = cursor
    status, resp = _request("GET", "/api/v1/posts/" + social_id + "/comments",
                            query=query)
    if status != 200:
        raise UnipileApiError("list comments failed: HTTP %s" % status, status, resp)
    if isinstance(resp, dict):
        items = resp.get("items")
    elif isinstance(resp, list):
        items = resp
    else:
        items = None
    items = items or []
    return {"items": items, "count": len(items), "raw": resp}


def comment_exists(social_id, comment_id, *, account_id=None):
    """True if a comment with comment_id is present on the post. Best-effort
    read-back proof for Phase B; falls back to False on any lookup error."""
    if not comment_id:
        return False
    try:
        res = list_comments(social_id, account_id=account_id)
    except Exception:
        return False
    target = str(comment_id)
    for c in res["items"]:
        if not isinstance(c, dict):
            continue
        for k in ("comment_id", "id", "urn", "social_id"):
            v = c.get(k)
            if v is not None and target in str(v):
                return True
    return False


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
        with_followers=args.with_followers,
    )
    if args.raw:
        print(json.dumps(res["raw"], indent=2))
        return 0
    if args.pipeline:
        # Candidate shape run-linkedin.sh Phase A consumes directly.
        out = {
            "ok": True,
            "query": args.keywords or args.url or "",
            "result_count": res["count"],
            "cursor": res["cursor"],
            "results": to_pipeline_results(res["items"]),
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    slim = [{k: it[k] for k in ("social_id", "author_name", "author_headline",
                                "author_followers", "reaction_count",
                                "comment_count", "repost_count", "posted_at",
                                "share_url", "text")}
            for it in res["items"]]
    print(json.dumps({"count": res["count"], "cursor": res["cursor"],
                      "items": slim}, indent=2, ensure_ascii=False))
    return 0


def _cmd_comment(args):
    res = comment_on_post(args.social_id, args.text, comment_id=args.reply_to,
                          also_like=not args.no_like)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res["ok"] else 1


def _cmd_react(args):
    res = react_to_post(args.social_id, reaction_type=args.reaction_type,
                        comment_id=args.comment_id)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res["ok"] else 1


def _cmd_profile(args):
    prof = get_profile(args.identifier)
    if args.raw:
        print(json.dumps(prof, indent=2, ensure_ascii=False))
        return 0
    keys = ("public_identifier", "first_name", "last_name", "headline",
            "follower_count", "connections_count", "is_influencer",
            "is_creator", "is_premium", "network_distance")
    print(json.dumps({k: prof.get(k) for k in keys}, indent=2, ensure_ascii=False))
    return 0


def _cmd_comments(args):
    if args.contains_id:
        found = comment_exists(args.social_id, args.contains_id)
        print(json.dumps({"social_id": args.social_id,
                          "contains_id": args.contains_id, "found": found}))
        return 0 if found else 1
    res = list_comments(args.social_id, limit=args.limit)
    print(json.dumps({"count": res["count"], "items": res["items"]},
                     indent=2, ensure_ascii=False))
    return 0


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
    s.add_argument("--with-followers", dest="with_followers", action="store_true",
                   help="enrich each hit with author follower_count (extra GET per author)")
    s.add_argument("--pipeline", action="store_true",
                   help="emit run-linkedin.sh Phase A candidate shape")

    c = sub.add_parser("comment", help="comment on a post")
    c.add_argument("--social-id", dest="social_id", required=True,
                   help="urn:li:activity:... (use a post's social_id)")
    c.add_argument("--text", required=True)
    c.add_argument("--reply-to", dest="reply_to",
                   help="comment_id to reply to an existing comment")
    c.add_argument("--no-like", dest="no_like", action="store_true",
                   help="skip the automatic parent-post like (on by default)")

    rx = sub.add_parser("react", help="like/react to a post (or a comment)")
    rx.add_argument("--social-id", dest="social_id", required=True,
                    help="urn:li:activity:... (the post's social_id)")
    rx.add_argument("--reaction-type", dest="reaction_type", default="like",
                    choices=REACTION_TYPES)
    rx.add_argument("--comment-id", dest="comment_id",
                    help="react to a specific comment instead of the post")

    pr = sub.add_parser("profile", help="fetch a LinkedIn profile (follower_count, ...)")
    pr.add_argument("identifier", help="public_identifier or provider id")
    pr.add_argument("--raw", action="store_true", help="print the raw API response")

    cm = sub.add_parser("comments", help="list a post's comments (read-back verify)")
    cm.add_argument("--social-id", dest="social_id", required=True)
    cm.add_argument("--limit", type=int, default=50)
    cm.add_argument("--contains-id", dest="contains_id",
                    help="exit 0 iff a comment with this comment_id is present")

    args = p.parse_args(argv)
    try:
        if args.cmd == "probe":
            return _cmd_probe(args)
        if args.cmd == "search":
            return _cmd_search(args)
        if args.cmd == "comment":
            return _cmd_comment(args)
        if args.cmd == "react":
            return _cmd_react(args)
        if args.cmd == "profile":
            return _cmd_profile(args)
        if args.cmd == "comments":
            return _cmd_comments(args)
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
