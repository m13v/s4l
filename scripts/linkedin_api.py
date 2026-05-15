#!/usr/bin/env python3
"""LinkedIn API wrapper for Social Autoposter.

Replaces browser automation for posting, commenting, replying, and reacting.
Browser is still needed for discovery (notifications, search) since LinkedIn
has no content discovery API.

Usage:
    # Post a comment on a LinkedIn post
    python3 linkedin_api.py comment <activity_id> "comment text"

    # Reply to a comment on a LinkedIn post
    python3 linkedin_api.py reply <activity_id> <parent_comment_urn> "reply text"

    # Create a new post
    python3 linkedin_api.py post "post text"

    # Like a post
    python3 linkedin_api.py like <activity_id>

    # Get user profile info
    python3 linkedin_api.py whoami

Environment:
    LINKEDIN_ACCESS_TOKEN - OAuth 2.0 access token (w_member_social scope)
    LINKEDIN_PERSON_URN   - Optional. Auto-detected from token if not set.
"""

import json
import os
import re
import sys
import time
import urllib.parse

import requests


# ---- URL-wrap + post_links backfill ----------------------------------------
#
# Optional --project flag turns on the wrap; without it linkedin_api.py runs
# in legacy unwrapped mode (backward-compat for existing call sites that
# haven't been updated yet). When --project is set, every URL in the text
# gets minted into post_links and rewritten to https://<project.website>/r/<code>
# before the API call. After a 201 response, if --reply-id or --post-id was
# also passed, the minted codes get backfilled with that id so click
# attribution lands on the right replies/posts row.
#
# The minted_session UUID is ALWAYS surfaced in the success JSON envelope so
# Claude-driven prompts (engage-linkedin.sh, run-linkedin.sh) can pass it to
# `python3 scripts/dm_short_links.py backfill-{reply,post}` themselves if they
# don't have the row id at the time of the linkedin_api.py call.

def _parse_optional_flags(argv):
    """Scan argv for --project, --reply-id, --post-id, --post-urn flags.

    Returns dict with optional keys: project, reply_id, post_id. Removes
    consumed flags from argv in-place so the caller's positional indexing
    isn't disturbed (the main() routing uses sys.argv[2], [3], [4] directly).
    """
    flags = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--project" and i + 1 < len(argv):
            flags["project"] = argv[i + 1]
            del argv[i:i + 2]
            continue
        if argv[i] == "--reply-id" and i + 1 < len(argv):
            try:
                flags["reply_id"] = int(argv[i + 1])
            except ValueError:
                pass
            del argv[i:i + 2]
            continue
        if argv[i] == "--post-id" and i + 1 < len(argv):
            try:
                flags["post_id"] = int(argv[i + 1])
            except ValueError:
                pass
            del argv[i:i + 2]
            continue
        i += 1
    return flags


def _wrap_if_project(text, project):
    """If project is set, mint short links for every URL in text and return
    (wrapped_text, minted_session). Otherwise pass through (text, None).

    Wrap failures are logged to stderr and fall back to the unwrapped text;
    losing attribution on a single LinkedIn comment is preferable to dropping
    a reply we already drafted."""
    if not project:
        return text, None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from dm_short_links import wrap_text_for_post, utm_only_text
        res = wrap_text_for_post(text=text, platform="linkedin", project_name=project)
        if res.get("ok"):
            if res.get("codes"):
                print(f"[linkedin_api] wrapped {len(res['codes'])} URL(s): "
                      f"{res['codes']}", file=sys.stderr)
            return res.get("text", text), res.get("minted_session")
        print(f"[linkedin_api] WARNING: URL wrap failed "
              f"({res.get('error')}); falling back to UTM-only", file=sys.stderr)
        return utm_only_text(text=text, platform="linkedin", project_name=project), None
    except Exception as e:
        print(f"[linkedin_api] WARNING: URL wrap raised ({e}); falling back to UTM-only",
              file=sys.stderr)
        try:
            from dm_short_links import utm_only_text
            return utm_only_text(text=text, platform="linkedin", project_name=project), None
        except Exception as ee:
            print(f"[linkedin_api] WARNING: UTM-only fallback also failed ({ee}); "
                  f"posting unwrapped", file=sys.stderr)
    return text, None


def _backfill_after_success(minted_session, reply_id=None, post_id=None):
    """Stamp post_links.{reply_id,post_id} for codes minted under
    minted_session. Caller passes exactly one of reply_id / post_id (or
    neither, in which case this is a no-op and Claude-side scripting is
    responsible for backfill via the dm_short_links.py CLI)."""
    if not minted_session:
        return
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from dm_short_links import backfill_post_id, backfill_reply_id
        if reply_id is not None:
            backfill_reply_id(minted_session=minted_session, reply_id=reply_id)
        elif post_id is not None:
            backfill_post_id(minted_session=minted_session, post_id=post_id)
    except Exception as e:
        print(f"[linkedin_api] WARNING: backfill failed ({e})", file=sys.stderr)


def get_env():
    """Load .env if needed and return token + person URN."""
    env_path = os.path.expanduser("~/social-autoposter/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)

    token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
    if not token:
        print("ERROR: LINKEDIN_ACCESS_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return token


def get_person_urn(token):
    """Get the authenticated user's person URN."""
    cached = os.environ.get("LINKEDIN_PERSON_URN")
    if cached:
        return cached
    r = requests.get(
        "https://api.linkedin.com/v2/userinfo",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    sub = r.json()["sub"]
    return f"urn:li:person:{sub}"


def rest_headers(token):
    """Headers for /rest/ endpoints (versioned)."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": "202503",
    }


def v2_headers(token):
    """Headers for /v2/ endpoints (unversioned)."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }


def handle_rate_limit(response):
    """Check for 429 rate limit. If detected, write cooldown and exit."""
    if response.status_code == 429:
        error_text = response.text
        print(json.dumps({
            "ok": False,
            "status": 429,
            "error": error_text,
            "rate_limited": True,
        }))
        # Write 2-hour cooldown
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            from linkedin_cooldown import set_cooldown
            from datetime import datetime, timedelta, timezone
            reason = "429 API rate limit"
            if "fuse limit" in error_text.lower():
                reason = "429 CommentCreatePermission fuse limit"
            set_cooldown(reason, datetime.now(timezone.utc) + timedelta(hours=2))
        except Exception:
            pass
        sys.exit(2)


def post_with_retry(method, url, headers, json_data=None, max_retries=2):
    """Make an API request with retry on 5xx and rate limit detection on 429."""
    for attempt in range(max_retries + 1):
        if method == "POST":
            r = requests.post(url, headers=headers, json=json_data)
        elif method == "DELETE":
            r = requests.delete(url, headers=headers)
        else:
            r = requests.get(url, headers=headers)

        handle_rate_limit(r)

        if r.status_code >= 500 and attempt < max_retries:
            wait = 5 * (attempt + 1)
            print(f"Server error {r.status_code}, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue

        return r
    return r


def create_post(token, person_urn, text, project=None, post_id=None):
    """Create a new LinkedIn post. Returns the post URN.

    If project is set, URLs in text are wrapped via post_links before send.
    If post_id is also passed, post_links.post_id is backfilled after a
    successful 201; otherwise minted_session is returned for the caller to
    backfill out-of-band."""
    text, minted_session = _wrap_if_project(text, project)
    data = {
        "author": person_urn,
        "lifecycleState": "PUBLISHED",
        "visibility": "PUBLIC",
        "commentary": text,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
    }
    r = requests.post(
        "https://api.linkedin.com/rest/posts",
        headers=rest_headers(token),
        json=data,
    )
    if r.status_code == 201:
        post_urn = r.headers.get("x-restli-id", "")
        _backfill_after_success(minted_session, post_id=post_id)
        print(json.dumps({"ok": True, "post_urn": post_urn,
                          "minted_session": minted_session}))
        return post_urn
    else:
        print(json.dumps({"ok": False, "status": r.status_code, "error": r.text}))
        sys.exit(1)


def resolve_post_urn(identifier):
    """Convert an activity ID or share ID to the appropriate URN for API calls.

    The pipeline extracts activity IDs from browser data-urn attributes.
    LinkedIn's socialActions API accepts both urn:li:activity: and urn:li:share: URNs.
    """
    if identifier.startswith("urn:li:"):
        return identifier
    return f"urn:li:activity:{identifier}"


def extract_post_urn_parts(resp, fallback_id):
    """Extract (namespace, id) from a comment response's $URN field.

    Comment URN shape: urn:li:comment:(urn:li:<ns>:<id>,<comment_id>)
    where <ns> is 'activity', 'ugcPost', or 'share'. The activity, share,
    and ugcPost numeric IDs for the same post are DIFFERENT (LinkedIn
    namespaces, not prefixes), so we must echo back the namespace
    LinkedIn used. If we hard-code 'activity' but the response carried
    'share', the constructed permalink stores a share-namespace ID
    masquerading as an activity URN, which then 404s on every namespace-
    sensitive read (Unipile, /v2/socialActions, etc.) and gets misflagged
    as deleted. Falls back to ('activity', fallback_id) only when the
    response has no parseable URN at all.
    """
    urn = resp.get("$URN", "")
    m = re.search(r"urn:li:(activity|ugcPost|share):(\d+)", urn)
    if m:
        return m.group(1), m.group(2)
    return "activity", fallback_id


def extract_activity_id_from_response(resp, fallback_id):
    """Deprecated. Use extract_post_urn_parts; kept for any external caller."""
    return extract_post_urn_parts(resp, fallback_id)[1]


def comment_on_post(token, person_urn, activity_id, text, project=None, reply_id=None, post_id=None):
    """Comment on a LinkedIn post. Returns the comment URN.

    Accepts activity IDs (from browser data-urn), share IDs, or full URNs.
    If urn:li:activity fails with 404, retries with urn:li:share.

    Optional URL-wrap + post_links attribution: see module-level
    _wrap_if_project / _backfill_after_success docstrings. reply_id wins
    over post_id when both are passed (a comment is naturally a reply).
    """
    text, minted_session = _wrap_if_project(text, project)
    post_urn = resolve_post_urn(activity_id)
    encoded_urn = urllib.parse.quote(post_urn, safe="")
    data = {
        "actor": person_urn,
        "message": {"text": text},
    }
    r = post_with_retry(
        "POST",
        f"https://api.linkedin.com/v2/socialActions/{encoded_urn}/comments",
        headers=v2_headers(token),
        json_data=data,
    )
    # If activity URN fails, try alternative URN formats
    if r.status_code == 400 and "actual threadUrn" in r.text:
        # Extract the real URN from error: "actual threadUrn: urn:li:ugcPost:NNNN"
        m = re.search(r"actual threadUrn:\s*(urn:li:\w+:\d+)", r.text)
        if m:
            real_urn = m.group(1)
            encoded_real = urllib.parse.quote(real_urn, safe="")
            r = post_with_retry(
                "POST",
                f"https://api.linkedin.com/v2/socialActions/{encoded_real}/comments",
                headers=v2_headers(token),
                json_data=data,
            )
    if r.status_code == 404 and not activity_id.startswith("urn:li:"):
        share_urn = f"urn:li:share:{activity_id}"
        encoded_share = urllib.parse.quote(share_urn, safe="")
        r = post_with_retry(
            "POST",
            f"https://api.linkedin.com/v2/socialActions/{encoded_share}/comments",
            headers=v2_headers(token),
            json_data=data,
        )
    if r.status_code == 201:
        resp = r.json()
        comment_id = resp.get("id", "")
        ns, real_id = extract_post_urn_parts(resp, activity_id)
        comment_urn = resp.get("$URN", f"urn:li:comment:(urn:li:{ns}:{real_id},{comment_id})")
        # 2026-05-11: embed ?commentUrn=... so log_post.py stores a URL that
        # uniquely identifies OUR comment, not just the parent post. Before
        # this fix, `our_url` was the bare parent post URL, which made the
        # post-stats pipeline scrape the parent post's reactions (e.g., 188)
        # instead of OUR comment's (e.g., 1). Mirrors the URL shape that
        # reply_to_comment already produces (line ~414).
        our_url = (
            f"https://www.linkedin.com/feed/update/urn:li:{ns}:{real_id}/"
            f"?commentUrn={urllib.parse.quote(comment_urn, safe='')}"
        )
        _backfill_after_success(minted_session, reply_id=reply_id, post_id=post_id)
        print(json.dumps({"ok": True, "comment_urn": comment_urn, "our_url": our_url,
                          "post_namespace": ns, "post_id": real_id,
                          "activity_id": real_id,
                          "minted_session": minted_session}))
        return comment_urn
    else:
        print(json.dumps({"ok": False, "status": r.status_code, "error": r.text}))
        sys.exit(1)


def normalize_comment_urn(urn):
    """Normalize comment URN to the format LinkedIn API expects.

    Pipeline stores: urn:li:comment:(activity:ID,COMMENT_ID)
    API expects:     urn:li:comment:(urn:li:activity:ID,COMMENT_ID)
    """
    import re
    # If it has (activity:ID without urn:li: prefix, add it
    urn = re.sub(
        r"\(activity:(\d+)",
        r"(urn:li:activity:\1",
        urn,
    )
    # If it has (ugcPost:ID without urn:li: prefix, add it
    urn = re.sub(
        r"\(ugcPost:(\d+)",
        r"(urn:li:ugcPost:\1",
        urn,
    )
    return urn


def reply_to_comment(token, person_urn, activity_id, parent_comment_urn, text,
                      project=None, reply_id=None, post_id=None):
    """Reply to a specific comment on a LinkedIn post.

    Optional URL-wrap + post_links attribution: see module-level
    _wrap_if_project / _backfill_after_success docstrings."""
    text, minted_session = _wrap_if_project(text, project)
    post_urn = resolve_post_urn(activity_id)
    encoded_urn = urllib.parse.quote(post_urn, safe="")
    data = {
        "actor": person_urn,
        "message": {"text": text},
        "parentComment": normalize_comment_urn(parent_comment_urn),
    }
    r = post_with_retry(
        "POST",
        f"https://api.linkedin.com/v2/socialActions/{encoded_urn}/comments",
        headers=v2_headers(token),
        json_data=data,
    )
    if r.status_code == 201:
        resp = r.json()
        # NOTE: shadowed `reply_id` here is LinkedIn's API-returned comment id
        # (string), distinct from the function param of the same name above
        # which is our internal replies.id (int). The post_id/reply_id
        # backfill block below uses the function-scope param (the int).
        api_reply_id = resp.get("id", "")
        ns, real_id = extract_post_urn_parts(resp, activity_id)
        reply_urn = resp.get("$URN", f"urn:li:comment:(urn:li:{ns}:{real_id},{api_reply_id})")
        permalink = (
            f"https://www.linkedin.com/feed/update/urn:li:{ns}:{real_id}"
            f"?commentUrn={urllib.parse.quote(reply_urn, safe='')}"
        )
        _backfill_after_success(minted_session, reply_id=reply_id, post_id=post_id)
        print(json.dumps({"ok": True, "reply_urn": reply_urn, "permalink": permalink,
                          "minted_session": minted_session}))
        return reply_urn
    else:
        print(json.dumps({"ok": False, "status": r.status_code, "error": r.text}))
        sys.exit(1)


def like_post(token, person_urn, activity_id):
    """Like/react to a LinkedIn post."""
    post_urn = resolve_post_urn(activity_id)
    encoded_urn = urllib.parse.quote(post_urn, safe="")
    data = {"actor": person_urn}
    r = post_with_retry(
        "POST",
        f"https://api.linkedin.com/v2/socialActions/{encoded_urn}/likes",
        headers=v2_headers(token),
        json_data=data,
    )
    if r.status_code == 404 and not activity_id.startswith("urn:li:"):
        share_urn = f"urn:li:share:{activity_id}"
        encoded_share = urllib.parse.quote(share_urn, safe="")
        r = post_with_retry(
            "POST",
            f"https://api.linkedin.com/v2/socialActions/{encoded_share}/likes",
            headers=v2_headers(token),
            json_data=data,
        )
    if r.status_code == 201:
        print(json.dumps({"ok": True, "activity_id": activity_id}))
    else:
        print(json.dumps({"ok": False, "status": r.status_code, "error": r.text}))
        sys.exit(1)


def delete_post(token, post_urn):
    """Delete a LinkedIn post."""
    encoded = urllib.parse.quote(post_urn, safe="")
    r = requests.delete(
        f"https://api.linkedin.com/rest/posts/{encoded}",
        headers=rest_headers(token),
    )
    if r.status_code == 204:
        print(json.dumps({"ok": True, "deleted": post_urn}))
    else:
        print(json.dumps({"ok": False, "status": r.status_code, "error": r.text}))
        sys.exit(1)


def whoami(token):
    """Print authenticated user info."""
    r = requests.get(
        "https://api.linkedin.com/v2/userinfo",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    info = r.json()
    print(json.dumps({"ok": True, "name": info.get("name"), "email": info.get("email"), "sub": info.get("sub")}))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    # Strip optional flags (--project, --reply-id, --post-id) out of argv
    # FIRST so the positional indexing below (sys.argv[2], [3], [4]) keeps
    # working for legacy callers that don't pass any flags. New callers
    # (engage-linkedin.sh / run-linkedin.sh prompts) put --project NAME
    # anywhere after the subcommand and get URL wrapping + post_links
    # attribution for free.
    flags = _parse_optional_flags(sys.argv)

    cmd = sys.argv[1]
    token = get_env()
    person_urn = get_person_urn(token)

    if cmd == "comment":
        if len(sys.argv) < 4:
            print("Usage: linkedin_api.py comment <activity_id> <text> "
                  "[--project NAME] [--reply-id N] [--post-id N]", file=sys.stderr)
            sys.exit(1)
        comment_on_post(token, person_urn, sys.argv[2], sys.argv[3],
                        project=flags.get("project"),
                        reply_id=flags.get("reply_id"),
                        post_id=flags.get("post_id"))

    elif cmd == "reply":
        if len(sys.argv) < 5:
            print("Usage: linkedin_api.py reply <activity_id> <parent_comment_urn> <text> "
                  "[--project NAME] [--reply-id N] [--post-id N]", file=sys.stderr)
            sys.exit(1)
        reply_to_comment(token, person_urn, sys.argv[2], sys.argv[3], sys.argv[4],
                         project=flags.get("project"),
                         reply_id=flags.get("reply_id"),
                         post_id=flags.get("post_id"))

    elif cmd == "post":
        if len(sys.argv) < 3:
            print("Usage: linkedin_api.py post <text> "
                  "[--project NAME] [--post-id N]", file=sys.stderr)
            sys.exit(1)
        create_post(token, person_urn, sys.argv[2],
                    project=flags.get("project"),
                    post_id=flags.get("post_id"))

    elif cmd == "like":
        if len(sys.argv) < 3:
            print("Usage: linkedin_api.py like <activity_id>", file=sys.stderr)
            sys.exit(1)
        like_post(token, person_urn, sys.argv[2])

    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("Usage: linkedin_api.py delete <post_urn>", file=sys.stderr)
            sys.exit(1)
        delete_post(token, sys.argv[2])

    elif cmd == "whoami":
        whoami(token)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
