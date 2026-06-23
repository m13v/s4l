#!/usr/bin/env python3
"""Log a posted comment/reply to the database.

Single tool for all platforms. Enforces:
  - status='active' for successful posts
  - our_url must start with http for successful posts (validated)
  - dedup on thread_url per platform

Usage (INSERT — default mode):
    python3 scripts/log_post.py \\
        --platform reddit \\
        --thread-url URL \\
        --our-url URL \\
        --our-content TEXT \\
        --project PROJECT \\
        --thread-author AUTHOR \\
        --thread-title TITLE \\
        [--account ACCOUNT] \\
        [--engagement-style STYLE] \\
        [--language LANG]

Usage (REJECTED — record a server-rejected attempt):
    python3 scripts/log_post.py --rejected \\
        --platform linkedin \\
        --thread-url URL \\
        --our-content TEXT \\
        --project PROJECT \\
        [--rejection-reason TEXT] \\
        [--network-response TEXT]

    Inserts with status='rejected_by_platform'. Skips our_url validation
    (no permalink exists). Counts toward dedup so we don't retry the same
    thread. rejection-reason and network-response go into source_summary.

Usage (UPDATE — record a self-reply / link follow-up on an existing post):
    python3 scripts/log_post.py --mark-self-reply \\
        --post-id 12345 \\
        --self-reply-url URL \\
        --self-reply-content TEXT

    Writes to posts.link_edited_at / link_edit_content so the
    link-edit-* sweeps skip this row on the next pass.

Output (JSON):
    {"logged": true, "post_id": 12345}
    {"rejected": true, "post_id": 12345}
    {"marked": true, "post_id": 12345}
    {"error": "DUPLICATE_THREAD", ...}
    {"error": "INVALID_URL", ...}
    {"error": "POST_NOT_FOUND", ...}
"""

import argparse
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import http_api

# --- API error-envelope helpers (2026-06-02) -------------------------------
# The API returns failures as a NESTED object: {"ok": false, "error": {"code",
# "message", "details"}} (see social-autoposter-website response.ts). http_api
# may also surface a FLAT {"error": "conflict"} on a 409 it couldn't parse. The
# old `resp.get("error") in (...)` string check missed the nested shape, so a
# duplicate_thread 409 fell through and printed {"logged": true, "post_id":
# null} -- a false success that looked like a logging gap. These helpers read
# either shape so dedups are recognized and reported correctly.
def _api_error_code(resp):
    e = (resp or {}).get("error")
    if isinstance(e, dict):
        return e.get("code")
    return e  # flat string or None

def _api_error_detail(resp, key):
    e = (resp or {}).get("error")
    if isinstance(e, dict):
        d = e.get("details")
        if isinstance(d, dict) and d.get(key) is not None:
            return d.get(key)
    return (resp or {}).get(key)

import linkedin_url as li_url
from db import load_env
from twitter_account import resolve_handle as resolve_twitter_handle
from version import read_version as read_autoposter_version

# Engagement-style enforcement (2026-05-31 LinkedIn alignment): the LinkedIn
# post path goes straight through log_post.py (no candidate/plan pipeline like
# Twitter's twitter_post_plan.py), so the picker-coercion engine has to live
# here. When the caller passes --assigned-style/--assigned-mode (sourced from
# saps_pick_style in run-linkedin.sh), we call validate_or_register exactly
# like twitter_post_plan.py::post_one so (a) USE-mode drift coerces back to the
# assigned style and (b) INVENT-mode inventions land in
# engagement_styles_registry via the /api/v1/engagement-styles/registry POST.
# Soft import so the post path still runs if the module is unavailable; we fall
# back to the raw --engagement-style string in that case.
try:
    from engagement_styles import validate_or_register  # noqa: E402
except Exception:
    validate_or_register = None  # type: ignore[assignment]

URN_ID_RE = re.compile(r"\b(\d{16,19})\b")


def parse_urn_ids(*sources):
    """Extract all 16-19-digit URN IDs from the given strings, dedupe,
    preserve insertion order. Used to merge --urns CLI input with IDs
    found in thread_url / our_url so we always store the full URN set
    we know about for a LinkedIn post."""
    seen = []
    for s in sources:
        if not s:
            continue
        for m in URN_ID_RE.finditer(s):
            v = m.group(1)
            if v not in seen:
                seen.append(v)
    return seen

VALID_PLATFORMS = ("reddit", "twitter", "linkedin", "github_issues", "moltbook")

# Maps log_post's platform strings to account_resolver's platform keys.
# (log_post uses "github_issues"; account_resolver uses "github".)
_RESOLVER_PLATFORM = {
    "twitter": "twitter",
    "reddit": "reddit",
    "linkedin": "linkedin",
    "github_issues": "github",
    "moltbook": "moltbook",
}


def _resolve_default_account(platform: str) -> str:
    """Return the configured account handle for `platform` on this machine.

    Resolved ONLY from env (`AUTOPOSTER_<PLATFORM>_*`) or config.json
    (`accounts.<platform>.*`) via account_resolver. There are NO hardcoded
    handle fallbacks: a misconfigured install must never silently post under
    another person's identity. The old per-platform defaults
    (m13v_/Deep_Ad1959/Matthew Diakonov/m13v/matthew-autoposter) did exactly
    that, stamping every unconfigured install's rows with the repo owner's
    handle and polluting the shared DB across accounts.

    Returns "" when nothing is configured; the caller's
    `args.account or _resolve_default_account(...)` chain still lets an
    explicit `--account` flag win, and an empty value surfaces the misconfig
    instead of impersonating someone.
    """
    try:
        import account_resolver
        return account_resolver.resolve(
            _RESOLVER_PLATFORM.get(platform, platform)
        ) or ""
    except Exception:
        return ""


def coerce_engagement_style(args):
    """Run the picker-coercion engine and return the style to log.

    Shared by INSERT mode and --rejected mode so a server-rejected attempt
    records the same coerced/assigned style as a successful one (otherwise
    INVENT-mode model names leak onto rejected rows and pollute the per-style
    report). When the caller passed --assigned-style/--assigned-mode (from
    saps_pick_style in run-linkedin.sh), call validate_or_register exactly
    like twitter_post_plan.py::post_one:
      - USE-mode drift coerces back to the assigned name
      - INVENT-mode + well-formed --new-style registers in the registry
    Falls back to the raw --engagement-style on any error / missing module so
    a registry hiccup never blocks the write. Returns the style string (or
    None) to use for this row.
    """
    raw_style = (args.engagement_style or "").strip() or None
    if validate_or_register is None or not raw_style:
        return raw_style
    if not (args.assigned_style or args.assigned_mode):
        return raw_style

    new_style_block = None
    if args.new_style:
        try:
            parsed = json.loads(args.new_style)
            if isinstance(parsed, dict):
                new_style_block = parsed
        except json.JSONDecodeError as e:
            print(json.dumps({
                "warning": "NEW_STYLE_PARSE_FAILED",
                "message": f"could not parse --new-style JSON: {e}",
            }), file=sys.stderr)

    decision = {
        "engagement_style": raw_style,
        **({"new_style": new_style_block} if new_style_block else {}),
    }
    try:
        coerced_style, action = validate_or_register(
            decision,
            source_post={
                "platform": args.platform,
                "post_url": getattr(args, "our_url", None) or args.thread_url,
                "post_id": None,
                "model": None,
            },
            assigned_style=(args.assigned_style or None),
            assigned_mode=(args.assigned_mode or None),
        )
    except Exception as e:
        print(f"[log_post] validate_or_register raised {e!r}; "
              f"falling back to raw style={raw_style!r}", file=sys.stderr)
        return raw_style

    if action == "coerced" and coerced_style != raw_style:
        print(f"[log_post] engagement_style coerced {raw_style!r} -> "
              f"{coerced_style!r} (assigned={args.assigned_style!r})",
              file=sys.stderr)
    elif action == "registered":
        print(f"[log_post] registered new engagement_style "
              f"{coerced_style!r} into engagement_styles_registry",
              file=sys.stderr)
    # coerced_style is None only on "rejected" (unknown style, no usable
    # new_style). Keep the raw style so the row still logs a non-null style.
    return (coerced_style or raw_style or "").strip() or None


def mark_self_reply(args):
    if args.post_id is None or not args.self_reply_url or args.self_reply_content is None:
        print(json.dumps({
            "error": "MISSING_ARGS",
            "message": "--mark-self-reply requires --post-id, --self-reply-url, --self-reply-content",
        }))
        sys.exit(1)
    if not args.self_reply_url.startswith("http"):
        print(json.dumps({
            "error": "INVALID_URL",
            "message": f"self-reply-url must start with http, got: {args.self_reply_url[:50]}",
        }))
        sys.exit(1)

    load_env()
    http_api.api_patch(f"/api/v1/posts/{args.post_id}", {
        "self_reply_url": args.self_reply_url,
        "self_reply_content": args.self_reply_content,
    })
    print(json.dumps({"marked": True, "post_id": args.post_id}))


def log_rejected(args):
    """Record a comment attempt that the platform rejected server-side.

    Writes status='rejected_by_platform' so dedup blocks retries on the same
    thread, and stashes the rejection reason + network response in
    source_summary for diagnostics.
    """
    missing = [f for f in ("platform", "thread_url", "our_content", "project")
               if getattr(args, f) is None]
    if missing:
        print(json.dumps({
            "error": "MISSING_ARGS",
            "message": f"--rejected requires: {', '.join('--' + m.replace('_', '-') for m in missing)}",
        }))
        sys.exit(1)

    account = args.account or _resolve_default_account(args.platform)

    # Engagement-style enforcement (2026-05-31 LinkedIn alignment): coerce the
    # model's style back to the picker assignment (USE) or register the
    # invention (INVENT) before the INSERT, so server-rejected rows record the
    # same canonical style as successful ones instead of leaking one-off
    # invented names into the per-style report. See coerce_engagement_style().
    args.engagement_style = coerce_engagement_style(args)

    summary_parts = []
    if args.rejection_reason:
        summary_parts.append(f"REASON: {args.rejection_reason}")
    if args.network_response:
        summary_parts.append(f"NETWORK: {args.network_response}")
    summary = "\n".join(summary_parts) if summary_parts else "rejected_by_platform"

    load_env()
    claude_session_id = os.environ.get("CLAUDE_SESSION_ID") or None

    urn_ids = []
    if args.platform == "linkedin":
        urn_ids = parse_urn_ids(args.urns, args.thread_url, args.network_response)

    body = {
        "platform": args.platform,
        "thread_url": args.thread_url,
        "our_content": args.our_content,
        "project": args.project,
        "status": "rejected_by_platform",
        "thread_author": args.thread_author or "",
        "thread_title": args.thread_title or "",
        "thread_content": args.thread_content or "",
        "our_account": account,
        "source_summary": summary,
    }
    if args.engagement_style:
        body["engagement_style"] = args.engagement_style
    if args.search_topic:
        body["search_topic"] = args.search_topic
    if args.language:
        body["language"] = args.language
    if claude_session_id:
        body["claude_session_id"] = claude_session_id
    if urn_ids:
        body["urns"] = urn_ids
    # autoposter_version: stamped on every write so we can attribute
    # engagement back to the release of the autoposter code that produced
    # this row. None when package.json + env are both missing; API stores
    # NULL in that case (doesn't block the insert).
    autoposter_version = read_autoposter_version()
    if autoposter_version:
        body["autoposter_version"] = autoposter_version

    resp = http_api.api_post("/api/v1/posts", body, ok_on_conflict=True)
    if resp and _api_error_code(resp) in ("duplicate_thread", "conflict"):
        print(json.dumps({
            "error": "DUPLICATE_THREAD",
            "message": "Already have a row for this thread",
            "existing_post_id": _api_error_detail(resp, "existing_post_id"),
        }))
        return
    # See note in main() about the resp.data.post.id shape.
    data = (resp or {}).get("data") or {}
    post_obj = data.get("post") or (resp or {}).get("post") or {}
    post_id = post_obj.get("id")
    print(json.dumps({"rejected": True, "post_id": post_id, "urns": urn_ids}))


def main():
    parser = argparse.ArgumentParser(description="Log a posted comment to the database")
    parser.add_argument("--mark-self-reply", action="store_true",
                        help="UPDATE mode: mark link_edited_at on an existing post. "
                             "Requires --post-id, --self-reply-url, --self-reply-content.")
    parser.add_argument("--rejected", action="store_true",
                        help="REJECTED mode: record a server-rejected attempt with "
                             "status='rejected_by_platform'. Skips our_url validation. "
                             "Use when the platform silently swallowed the comment.")
    parser.add_argument("--rejection-reason", default=None,
                        help="Brief reason text (e.g. 'TOAST: comment could not be created'). "
                             "Goes into source_summary.")
    parser.add_argument("--network-response", default=None,
                        help="Captured XHR response from the comment-create endpoint. "
                             "Goes into source_summary (truncated to 4000 chars).")
    parser.add_argument("--post-id", type=int, default=None,
                        help="posts.id to update (only with --mark-self-reply)")
    parser.add_argument("--self-reply-url", default=None,
                        help="URL of the self-reply that carries the project link")
    parser.add_argument("--self-reply-content", default=None,
                        help="Text of the self-reply (goes into link_edit_content)")
    parser.add_argument("--platform", choices=VALID_PLATFORMS)
    parser.add_argument("--thread-url")
    parser.add_argument("--our-url",
                        help="Permalink to our posted comment (must start with http)")
    parser.add_argument("--our-content")
    parser.add_argument("--project")
    parser.add_argument("--thread-author", default="")
    parser.add_argument("--thread-title", default="")
    parser.add_argument("--thread-content", default="",
                        help="Body text of the original thread/post we're "
                             "replying to. Stored in posts.thread_content and "
                             "surfaced on the public dashboard so visitors see "
                             "the conversation context our comment lives in. "
                             "Capped at 4000 chars by the API.")
    parser.add_argument("--account", default=None,
                        help="Override default account for the platform")
    parser.add_argument("--engagement-style", default=None,
                        help="Tone style (e.g. critic, storyteller). Separate from "
                             "--is-recommendation, which is intent.")
    parser.add_argument("--assigned-style", default=None,
                        help="The engagement style the programmatic picker "
                             "(saps_pick_style / pick_style_for_post) assigned for "
                             "this post. When present alongside --assigned-mode, "
                             "log_post runs validate_or_register so USE-mode drift "
                             "is coerced back to this name and INVENT-mode names "
                             "register in engagement_styles_registry. Mirrors "
                             "Twitter's --assigned-style on log_draft.py. Empty on "
                             "INVENT mode (picker assigns no concrete name).")
    parser.add_argument("--assigned-mode", default=None,
                        help="Picker mode for this post: 'use' (a concrete style "
                             "was assigned, drift coerces back) or 'invent' (model "
                             "creates a new snake_case style + --new-style block). "
                             "Drives validate_or_register's enforcement branch.")
    parser.add_argument("--new-style", default=None,
                        help="JSON object describing a model-invented style, REQUIRED "
                             "iff --assigned-mode=invent and --engagement-style is a "
                             "new name not in the registry. Shape mirrors "
                             "engagement_styles.py::_REQUIRED_NEW_STYLE_FIELDS: "
                             "{description, example, why_existing_didnt_fit, "
                             "note?}. Passed through to validate_or_register so the "
                             "invention lands in engagement_styles_registry.")
    parser.add_argument("--search-topic", default=None,
                        help="Topic seed from the project's search_topics list "
                             "(or a model-invented variant) that surfaced this "
                             "thread. Stamped on posts.search_topic so "
                             "top_search_topics.py can aggregate per-topic "
                             "conversion. For Twitter this should be copied "
                             "from twitter_candidates.search_topic; Reddit and "
                             "GitHub already populate this field via their own "
                             "log-post wrappers.")
    parser.add_argument("--is-recommendation", action="store_true",
                        help="Mark this post as a project mention/recommendation. "
                             "Composes with --engagement-style; tone and intent are "
                             "independent dimensions.")
    parser.add_argument("--language", default=None,
                        help="ISO 639-1 language code (e.g. en, ja, zh, es)")
    parser.add_argument("--link-source", default=None,
                        help="How the link in our_content was sourced: "
                             "seo_page | plain_url_ab_skip | plain_url_no_lp | "
                             "plain_url_fallback:<reason> | empty[_*]. "
                             "Used to A/B compare engagement between the "
                             "page-gen and plain-URL lanes on Twitter.")
    parser.add_argument("--tail-link-variant", default=None,
                        help="Tail-link AB test arm for Twitter posts: "
                             "'link' (reply includes bridge sentence + URL) or "
                             "'no_link' (reply posted without any link tail). "
                             "NULL for non-Twitter posts and rows pre-dating "
                             "the experiment. Stored in posts.tail_link_variant.")
    parser.add_argument("--target-chars", type=int, default=None,
                        help="Snapshot of the assigned engagement style's "
                             "target comment length (chars) at post time. "
                             "Frozen onto posts.target_chars so "
                             "style_length_report can compare realized-vs-target "
                             "length immune to later registry drift. Resolved by "
                             "the caller (twitter_post_plan.py) from the final "
                             "coerced style via the registry. NULL leaves the "
                             "column empty; the report falls back to the live "
                             "registry target for NULL rows.")
    parser.add_argument("--length-arm", default=None,
                        help="Historical Twitter length-control A/B arm. The live "
                             "experiment concluded 2026-06-04 and production no "
                             "longer passes this flag; keep it for old rows and "
                             "manual/backfill writes to posts.length_arm. Expected "
                             "values: 'treatment' or 'control'.")
    parser.add_argument("--urns", default=None,
                        help="LinkedIn-only: comma- or whitespace-separated list "
                             "of 16-19 digit URN IDs that identify this post "
                             "(activity, ugcPost, share). Pass everything you "
                             "captured from the createComment network response. "
                             "log_post.py merges these with IDs extracted from "
                             "thread_url and our_url before INSERT, so dedup "
                             "via posts.urns catches future cross-URN collisions.")
    parser.add_argument("--generation-trace", default=None,
                        help="Path to a JSON file with the few-shot context "
                             "Claude saw before drafting this post. Stored in "
                             "posts.generation_trace JSONB so a later audit "
                             "can reconstruct 'which examples produced this "
                             "output?'. Pass the file path (NOT inline JSON) "
                             "to keep argv short and avoid shell-escape pain. "
                             "Capped at 64 KB by the API. See "
                             "migrations/2026-05-12_generation_trace.sql.")
    parser.add_argument("--thread-engagement", default=None,
                        help="JSON string snapshot of the original thread's "
                             "engagement at scrape time. Shape: "
                             "{\"likes\":N,\"retweets\":N,\"replies\":N,"
                             "\"views\":N,\"bookmarks\":N,\"snapshot_at\":\"...\"}. "
                             "Stored verbatim in posts.thread_engagement (TEXT). "
                             "No live refresh, no extra API calls; whatever the "
                             "candidate row already had under *_t0 is what gets "
                             "recorded. Capped at 2 KB by the API.")
    parser.add_argument("--thread-media", default=None,
                        help="JSON array snapshot of the original thread's media "
                             "([{\"url\":...,\"alt\":...,\"type\":\"image|video|gif|card\"}]) "
                             "captured at draft time. Stored in posts.thread_media "
                             "(JSONB) as the immutable record of what the thread "
                             "visually showed when we replied. An empty array [] is "
                             "valid (captured-none). Omitted/None leaves the column "
                             "NULL (never captured). 2026-06-03 thread-media feature.")
    args = parser.parse_args()

    if args.mark_self_reply:
        mark_self_reply(args)
        return

    if args.rejected:
        log_rejected(args)
        return

    # INSERT mode — enforce required fields that argparse can't conditionally require.
    missing = [f for f in ("platform", "thread_url", "our_url", "our_content", "project")
               if getattr(args, f) is None]
    if missing:
        print(json.dumps({
            "error": "MISSING_ARGS",
            "message": f"INSERT mode requires: {', '.join('--' + m.replace('_', '-') for m in missing)}",
        }))
        sys.exit(1)

    # Validate our_url
    if not args.our_url.startswith("http"):
        print(json.dumps({
            "error": "INVALID_URL",
            "message": f"our_url must start with http, got: {args.our_url[:50]}",
        }))
        sys.exit(1)

    account = args.account or _resolve_default_account(args.platform)

    # Engagement-style enforcement (2026-05-31 LinkedIn alignment): coerce the
    # model's style back to the picker assignment (USE) or register the
    # invention (INVENT) before the INSERT. See coerce_engagement_style().
    args.engagement_style = coerce_engagement_style(args)

    # LinkedIn: same post surfaces under multiple URL shapes (/feed/update/
    # vs /posts/...-share-...) with different numeric URNs. Canonicalize
    # our_url to /feed/update/urn:li:activity:<id>/ so the comment-permalink
    # captured after posting drops its commentUrn query string.
    urn_ids = []
    if args.platform == "linkedin":
        # Preserve a ?commentUrn= query (it identifies OUR engagement-comment)
        # across canonicalization. canonicalize() runs ACTIVITY_URN_RE over the
        # whole URL and, when the URL carries
        #   ?commentUrn=urn:li:comment:(activity:<parent>,<cid>)
        # it matches the INNER parent activity and collapses the entire URL to
        # /feed/update/urn:li:activity:<parent>/, dropping both the base post
        # URN and our comment id. That breaks the stats matcher
        # (update_linkedin_stats_from_feed.py keys on the numeric comment id
        # inside commentUrn). Fix: canonicalize the PATH-ONLY base, then
        # re-attach the original commentUrn so the stored our_url keeps it.
        _split = urllib.parse.urlsplit(args.our_url or "")
        _qs = urllib.parse.parse_qs(_split.query)
        _comment_urn = (_qs.get("commentUrn") or [None])[0]
        _base = urllib.parse.urlunsplit(
            (_split.scheme, _split.netloc, _split.path, "", "")
        )
        _canon = li_url.canonicalize(_base)
        if _comment_urn:
            _sep = "&" if "?" in _canon else "?"
            args.our_url = (
                _canon + _sep + "commentUrn="
                + urllib.parse.quote(_comment_urn, safe="")
            )
        else:
            args.our_url = _canon
        # Build the full URN-ID set for this post: --urns input plus
        # everything we can extract from thread_url and our_url. Stored in
        # posts.urns so future dedup queries catch any URN form (activity,
        # ugcPost, share) regardless of which one the candidate-page DOM
        # renders. Without this, the search-page only exposes the ugcPost
        # URN while we stored only the activity URN, so the cross-URN
        # collision check missed and we double-posted.
        urn_ids = parse_urn_ids(args.urns, args.thread_url, args.our_url)

    load_env()
    claude_session_id = os.environ.get("CLAUDE_SESSION_ID") or None

    body = {
        "platform": args.platform,
        "thread_url": args.thread_url,
        "our_url": args.our_url,
        "our_content": args.our_content,
        "project": args.project,
        "thread_author": args.thread_author or "",
        "thread_title": args.thread_title or "",
        "thread_content": args.thread_content or "",
        "our_account": account,
        "is_recommendation": bool(args.is_recommendation),
    }
    if args.engagement_style:
        body["engagement_style"] = args.engagement_style
    if args.search_topic:
        body["search_topic"] = args.search_topic
    if args.language:
        body["language"] = args.language
    if claude_session_id:
        body["claude_session_id"] = claude_session_id
    if urn_ids:
        body["urns"] = urn_ids
    if args.link_source:
        body["link_source"] = args.link_source
    if args.tail_link_variant:
        body["tail_link_variant"] = args.tail_link_variant
    if args.target_chars:
        body["target_chars"] = args.target_chars
    if args.length_arm:
        body["length_arm"] = args.length_arm
    if args.thread_engagement:
        body["thread_engagement"] = args.thread_engagement
    # Thread media snapshot (2026-06-03): the media of the thread we replied to,
    # frozen onto posts.thread_media as an immutable audit record. Read from the
    # candidate row by twitter_post_plan.py and forwarded here as a JSON array
    # string. Parse defensively: a malformed value must NOT block the post, so on
    # any parse error we skip the field (column stays NULL) rather than failing.
    if args.thread_media is not None:
        try:
            parsed_media = json.loads(args.thread_media)
            if isinstance(parsed_media, list):
                body["thread_media"] = parsed_media
        except (TypeError, ValueError) as e:
            print(json.dumps({
                "warning": "THREAD_MEDIA_PARSE_FAILED",
                "message": f"could not parse --thread-media: {e}",
            }), file=sys.stderr)
    # autoposter_version: stamped on every write so we can attribute
    # engagement back to the release of the autoposter code that produced
    # this row. None when package.json + env are both missing.
    autoposter_version = read_autoposter_version()
    if autoposter_version:
        body["autoposter_version"] = autoposter_version
    # Generation trace: read the JSON file and pass as-is. We do NOT
    # validate the inner shape here; the API enforces the 64 KB cap and
    # rejects non-object payloads. If the file is missing or unparseable
    # we skip the field silently rather than failing the post — losing
    # the audit row for one post is preferable to losing the post itself.
    if args.generation_trace:
        try:
            with open(args.generation_trace, "r", encoding="utf-8") as tf:
                body["generation_trace"] = json.load(tf)
        except (OSError, json.JSONDecodeError) as e:
            print(json.dumps({
                "warning": "GENERATION_TRACE_LOAD_FAILED",
                "message": f"could not load {args.generation_trace}: {e}",
            }), file=sys.stderr)

    resp = http_api.api_post("/api/v1/posts", body, ok_on_conflict=True)
    if resp and _api_error_code(resp) in ("duplicate_thread", "conflict"):
        print(json.dumps({
            "error": "DUPLICATE_THREAD",
            "message": "Already posted in this thread",
            "existing_post_id": _api_error_detail(resp, "existing_post_id"),
            "content_preview": _api_error_detail(resp, "content_preview"),
        }))
        return
    # API response shape is {"ok":true,"data":{"post":{"id":N,...}}}.
    # Earlier code looked at resp["post"]["id"] which silently returns None
    # against the current API, causing twitter_post_plan.py to drop into the
    # log_post_no_id branch even when the row was successfully inserted.
    # Accept both shapes for backwards compat.
    data = (resp or {}).get("data") or {}
    post_obj = data.get("post") or (resp or {}).get("post") or {}
    post_id = post_obj.get("id")
    print(json.dumps({"logged": True, "post_id": post_id, "urns": urn_ids}))


if __name__ == "__main__":
    main()
