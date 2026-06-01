#!/usr/bin/env python3
"""LinkedIn URL helpers: ID extraction, canonicalization, dedup checks.

LinkedIn surfaces the same post under multiple URL shapes:
  /feed/update/urn:li:activity:<19-digit-activity-id>/[?commentUrn=...]
  /posts/<author-slug>_<keywords>-activity-<19-digit-id>-<5-char-suffix>
  /posts/<author-slug>_<keywords>-share-<19-digit-id>-<5-char-suffix>
  /posts/<author-slug>_<keywords>-ugcPost-<19-digit-id>-<5-char-suffix>

The activity URN, share URN, and ugcPost URN for the same logical post are
DIFFERENT numbers, so canonicalizing to one form by string transform is not
possible. The pragmatic fix: extract every 16-19 digit ID from a URL and
treat the SET of IDs as the post identity. Two URLs collide if any ID
overlaps. (Across our DB this matches because the comment-permalink
captured after posting always carries the activity URN, so day-2 logging
under /posts/...-share-<X>-... still has our_url=/feed/update/...activity:<Y>
where Y matches day-1's stored thread_url ID.)

CLI:
    python3 scripts/linkedin_url.py --extract URL
    python3 scripts/linkedin_url.py --canonicalize URL
    python3 scripts/linkedin_url.py --check-engaged URL
        Exits 0 if the URL has any ID overlap with an existing
        platform='linkedin' row. Prints JSON with {engaged, ids, match}.
    python3 scripts/linkedin_url.py --check-self-author URL_OR_SLUG
        Exits 0 if the author profile URL/slug matches one of our own
        LinkedIn accounts (we should never comment on our own posts).
        Exits 1 otherwise. Prints JSON with {input, slug, self}.
"""

import argparse
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get

ID_RE = re.compile(r"\b(\d{16,19})\b")
ACTIVITY_URN_RE = re.compile(r"urn:li:activity:(\d{16,19})", re.IGNORECASE)

# LinkedIn public profile slugs we own. Author URL match against this set
# means "this is our own post; skip". Add any future account here.
SELF_LINKEDIN_SLUGS = {"m13v"}


def extract_slug(author_url_or_slug):
    """Pull the public profile slug from a LinkedIn author identifier.

    Accepts any of:
      'https://www.linkedin.com/in/m13v/'
      'https://www.linkedin.com/in/m13v'
      '/in/m13v/'
      'm13v'
    Returns the lowercase slug, or '' if nothing parseable.
    """
    if not author_url_or_slug:
        return ""
    s = urllib.parse.unquote(author_url_or_slug.strip()).lower().rstrip("/")
    m = re.search(r"/in/([a-z0-9\-_]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-z0-9\-_]+", s):
        return s
    return ""


def is_self_author(author_url_or_slug):
    """True if the given author URL/slug is one of our own LinkedIn
    accounts. Used to skip posts authored by us during pipeline discovery."""
    return extract_slug(author_url_or_slug) in SELF_LINKEDIN_SLUGS


def extract_ids(url):
    """Return ordered, deduped list of 16-19 digit IDs found in the URL.

    Catches activity URNs, share URNs, ugcPost URNs, and comment URNs
    regardless of where they sit in the path or query string. Decodes
    percent-encoded URNs first so commentUrn=urn%3Ali%3Aactivity%3A...
    contributes its IDs too.
    """
    if not url:
        return []
    decoded = urllib.parse.unquote(url)
    seen = []
    for m in ID_RE.finditer(decoded):
        v = m.group(1)
        if v not in seen:
            seen.append(v)
    return seen


def canonicalize(url):
    """Return a canonical /feed/update/urn:li:activity:<id>/ form when we
    can find an explicit activity URN in the URL. Otherwise return the URL
    with query+fragment stripped. Used for the our_url column so the
    activity-comment permalink doesn't drift between runs."""
    if not url:
        return url
    decoded = urllib.parse.unquote(url)
    m = ACTIVITY_URN_RE.search(decoded)
    if m:
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/"
    # Strip query+fragment as a fallback — keeps /posts/... slugs stable but
    # drops tracking params.
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def find_existing_engagement(ids):
    """Given a list of LinkedIn IDs, return the first existing posts row
    that mentions any of them in posts.urns (primary, GIN-indexed) OR in
    thread_url / our_url (fallback for any row missed by backfill).

    The urns array path catches the case where the same logical post
    surfaces under different URN forms: search-page DOM exposes the
    ugcPost URN, but our DB stored only the activity URN. Storing every
    URN we ever see for a post into posts.urns means a single
    ``urns && ARRAY[...]`` overlap query catches the collision regardless
    of which URN form the candidate page rendered.

    Returns None if no overlap, else a dict with keys post_id, posted_at,
    thread_url, our_url, our_account.

    Migrated 2026-06-01 to the s4l.ai HTTP API
    (GET /api/v1/linkedin-engaged?ids=...). The collision query (urns
    overlap + thread_url/our_url substring) now runs server-side; no
    DATABASE_URL needed.
    """
    if not ids:
        return None
    resp = api_get("/api/v1/linkedin-engaged", {"ids": ",".join(ids)})
    data = resp.get("data") or {}
    if not data.get("engaged"):
        return None
    return data.get("match")


def get_engaged_ids():
    """Return a sorted list of every LinkedIn ID we've engaged with
    (anything 16-19 digits found in thread_url or our_url for
    platform='linkedin'). Used to brief the LLM in run-linkedin.sh.

    Migrated 2026-06-01: the API returns the raw (thread_url, our_url)
    pairs; the canonical ID extraction (extract_ids) stays single-sourced
    here in Python rather than being re-implemented as a Postgres regexp.
    """
    resp = api_get("/api/v1/linkedin-engaged", {"list_urls": 1})
    rows = (resp.get("data") or {}).get("urls") or []
    ids = set()
    for row in rows:
        for v in extract_ids(row.get("thread_url") or ""):
            ids.add(v)
        for v in extract_ids(row.get("our_url") or ""):
            ids.add(v)
    return sorted(ids)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract", help="Print all IDs found in URL")
    parser.add_argument("--canonicalize", help="Print the canonical form of URL")
    parser.add_argument("--check-engaged", help="Check if URL collides with any "
                        "existing linkedin row. Exits 0 on collision, 1 otherwise.")
    parser.add_argument("--check-engaged-ids", help="Comma- or whitespace-separated "
                        "list of LinkedIn URN IDs (16-19 digits each) extracted "
                        "from a candidate post's DOM. Pre-comment dedup primary path: "
                        "the URL bar may only carry the share URN while our DB rows "
                        "store the activity URN, so the browser-side script must "
                        "walk componentkey/data-testid for ALL URNs and pipe them in. "
                        "Exits 0 on collision, 1 otherwise.")
    parser.add_argument("--list-engaged-ids", action="store_true",
                        help="Print every linkedin ID we've engaged with, one per line.")
    parser.add_argument("--check-self-author", help="Author profile URL or "
                        "public-ID slug from a candidate post. Exits 0 if it "
                        "matches one of our own LinkedIn accounts (skip the "
                        "post), 1 otherwise (proceed). Pre-comment guard so "
                        "the pipeline doesn't comment on Matthew's own posts "
                        "when search results surface them.")
    args = parser.parse_args()

    if args.extract:
        print(json.dumps(extract_ids(args.extract)))
        return
    if args.canonicalize:
        print(canonicalize(args.canonicalize))
        return
    if args.check_engaged:
        import db as dbmod
        dbmod.load_env()
        conn = dbmod.get_conn()
        ids = extract_ids(args.check_engaged)
        match = find_existing_engagement(conn, ids)
        conn.close()
        out = {"url": args.check_engaged, "ids": ids, "engaged": bool(match)}
        if match:
            out["match"] = {
                "post_id": match[0],
                "posted_at": str(match[1]),
                "thread_url": match[2],
                "our_url": match[3],
                "our_account": match[4],
            }
        print(json.dumps(out, indent=2))
        sys.exit(0 if match else 1)
    if args.check_engaged_ids:
        import db as dbmod
        dbmod.load_env()
        conn = dbmod.get_conn()
        # Accept comma, whitespace, or newline separation. Filter to 16-19
        # digit numeric IDs so we don't pollute with ad campaign mcid values
        # or random noise the browser-side walker might pick up.
        raw = re.split(r"[,\s]+", args.check_engaged_ids.strip())
        ids = [v for v in raw if re.fullmatch(r"\d{16,19}", v or "")]
        match = find_existing_engagement(conn, ids)
        conn.close()
        out = {"ids": ids, "engaged": bool(match)}
        if match:
            out["match"] = {
                "post_id": match[0],
                "posted_at": str(match[1]),
                "thread_url": match[2],
                "our_url": match[3],
                "our_account": match[4],
            }
        print(json.dumps(out, indent=2))
        sys.exit(0 if match else 1)
    if args.check_self_author:
        slug = extract_slug(args.check_self_author)
        matched = slug in SELF_LINKEDIN_SLUGS
        print(json.dumps({
            "input": args.check_self_author,
            "slug": slug,
            "self": matched,
        }))
        sys.exit(0 if matched else 1)
    if args.list_engaged_ids:
        import db as dbmod
        dbmod.load_env()
        conn = dbmod.get_conn()
        for v in get_engaged_ids(conn):
            print(v)
        conn.close()
        return
    parser.print_help()
    sys.exit(2)


if __name__ == "__main__":
    main()
