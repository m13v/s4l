#!/usr/bin/env python3
"""Reply state mutations for the engage bots.

All write paths (processing/replied/skipped/skip_batch/set_project) route
through the public HTTPS endpoint /api/v1/replies/{id} on $AUTOPOSTER_API_BASE
(default https://s4l.ai), carrying the X-Installation header from
scripts/identity.py. The retry loop in _http_patch handles transient s4l.ai
blips (DNS, timeout, 5xx) so a single curl FAIL does not strand a row in
'processing'. 4xx fast-fails because retrying a deterministic client error
just burns the budget.

The 'status' command is the per-cycle heartbeat for skill/engage*.sh — prints
counts grouped by reply status. As of 2026-05-12 this also routes through
HTTP (/api/v1/replies/counts) so there is no remaining direct-SQL path in
the Reddit pipeline.
"""
import sys, json, os
sys.path.insert(0, os.path.dirname(__file__))
from version import read_version as read_autoposter_version
try:
    from account_resolver import resolve as _resolve_account
except Exception:
    def _resolve_account(_platform):  # type: ignore[unused-arg]
        return None

CLAUDE_SESSION_ID = os.environ.get("CLAUDE_SESSION_ID") or None
API_BASE = (os.environ.get("AUTOPOSTER_API_BASE") or "https://s4l.ai").rstrip("/")
AUTOPOSTER_VERSION = read_autoposter_version()
# Resolved once at import time from config.json. Used ONLY as a fallback when
# we have no URL to derive the live handle from (e.g. Reddit, where the
# permalink doesn't carry the author). For Twitter we prefer _handle_from_url
# below because playwright-extension attaches to the user's running Chrome and
# whichever account is logged in there at post time may not match config.json
# (persona drift bug surfaced 2026-05-27: 97 MacBook rows stamped m13v_/NULL
# while x.com URL said matt_diak).
OUR_ACCOUNT = _resolve_account("twitter")


def _handle_from_url(url):
    """Extract the canonical posting handle from an our_reply_url, or None.

    Twitter/X URLs are shaped `https://x.com/<handle>/status/<id>` and
    `https://twitter.com/<handle>/status/<id>`. The handle in the URL is the
    ground truth for which account actually posted (X mints the URL after the
    POST succeeds against the logged-in session), so it beats config.json /
    AUTOPOSTER_TWITTER_HANDLE env var, which can disagree with the live Chrome
    when playwright-extension is attached.

    Reddit, LinkedIn, and GitHub URLs don't include the author in the path
    shape we use, so this returns None for them and the caller falls back to
    the module-level OUR_ACCOUNT (config.json) — which is fine on those
    platforms because they don't have the playwright-extension multi-account
    drift problem.
    """
    if not url or not isinstance(url, str):
        return None
    import re
    m = re.match(r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/([^/?#]+)/status/", url)
    if not m:
        return None
    handle = m.group(1).strip()
    if handle.startswith("@"):
        handle = handle[1:]
    return handle or None


def _http_patch(rid: int, body: dict) -> None:
    """PATCH /api/v1/replies/{rid} with body, attaching X-Installation header.

    Drops keys whose values are None so the server's COALESCE-style endpoint
    preserves existing column values.

    Retries on transient failures (network errors, HTTP 5xx) up to 3 attempts
    with exponential backoff (1s, 3s, 9s) so a brief s4l.ai blip does not
    strand a row in 'processing'. 4xx responses are deterministic client
    errors and fail fast without retry. Raises SystemExit on final failure
    so the calling shell sees a non-zero exit.
    """
    import urllib.request, urllib.error, time
    from identity import get_identity_header  # local module

    payload = {k: v for k, v in body.items() if v is not None}
    data = json.dumps(payload).encode("utf8")
    url = f"{API_BASE}/api/v1/replies/{rid}"

    attempts = 3
    backoff_s = [1, 3, 9]
    last_err = None
    for i in range(attempts):
        req = urllib.request.Request(
            url,
            data=data,
            method="PATCH",
            headers={
                "content-type": "application/json",
                "x-installation": get_identity_header(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            return  # success
        except urllib.error.HTTPError as e:
            # 4xx is deterministic (bad payload, missing row, auth); never
            # going to succeed on retry, so fail fast with the server body.
            if 400 <= e.code < 500:
                body_txt = ""
                try:
                    body_txt = e.read().decode("utf8", errors="ignore")
                except Exception:
                    pass
                raise SystemExit(f"http {e.code} from PATCH {url}: {body_txt}")
            # 5xx: transient (502/503/504 from upstream). Retry.
            last_err = f"http {e.code}"
        except urllib.error.URLError as e:
            # Network-level failure: DNS resolution, connection refused,
            # socket timeout. All worth retrying.
            last_err = f"network error {e}"
        if i < attempts - 1:
            print(
                f"[reply_db] PATCH {url} attempt {i+1}/{attempts} failed: "
                f"{last_err}; retrying in {backoff_s[i]}s",
                file=sys.stderr,
            )
            time.sleep(backoff_s[i])
    raise SystemExit(
        f"PATCH {url} failed after {attempts} attempts: {last_err}"
    )


cmd = sys.argv[1]
if cmd == "processing":
    # reply_db.py processing ID
    # Mark as in-progress BEFORE browser action to prevent re-processing on crash
    rid = int(sys.argv[2])
    _http_patch(rid, {"status": "processing"})
    print(f"ok {rid}")
elif cmd == "replied":
    # reply_db.py replied ID "content" [url] [engagement_style] [is_recommendation]
    # is_recommendation is "1" / "true" to mark this reply as a project mention;
    # anything else (or absent) leaves the column at its default FALSE. Style
    # and is_recommendation are independent: style is TONE, is_recommendation
    # is INTENT. Do not pass style="recommendation" — that value is deprecated.
    rid, content = int(sys.argv[2]), sys.argv[3]
    url = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
    style = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None
    is_rec_arg = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] else None
    is_rec = is_rec_arg is not None and is_rec_arg.lower() in ("1", "true", "yes")
    body = {
        "status": "replied",
        "our_reply_content": content,
        "our_reply_url": url,
        "engagement_style": style,
        "claude_session_id": CLAUDE_SESSION_ID,
        # autoposter_version: stamp on the replied transition so we can
        # attribute reply engagement back to the release that produced
        # this comment. None when package.json + env are both missing.
        "autoposter_version": AUTOPOSTER_VERSION,
        # our_account: stamp the persona on every transition. Prefer the
        # handle baked into our_reply_url because X mints that URL against
        # the actually-logged-in session, so it's the ground truth for which
        # account posted. Fall back to OUR_ACCOUNT (config.json / env) only
        # when the URL is missing or non-Twitter-shaped. Server uses COALESCE
        # so subsequent transitions don't overwrite an earlier good stamp.
        "our_account": _handle_from_url(url) or OUR_ACCOUNT,
    }
    # Server uses COALESCE for is_recommendation: only send TRUE so we
    # never accidentally clobber an existing TRUE flag back to FALSE.
    if is_rec:
        body["is_recommendation"] = True
    _http_patch(rid, body)
    print(f"ok {rid}")
elif cmd == "skipped":
    # reply_db.py skipped ID "reason"
    rid, reason = int(sys.argv[2]), sys.argv[3]
    _http_patch(rid, {
        "status": "skipped",
        "skip_reason": reason,
        "claude_session_id": CLAUDE_SESSION_ID,
    })
    print(f"ok {rid}")
elif cmd == "skip_batch":
    # reply_db.py skip_batch '{"ids":[1,2,3],"reason":"..."}'
    data = json.loads(sys.argv[2])
    for rid in data["ids"]:
        _http_patch(rid, {
            "status": "skipped",
            "skip_reason": data["reason"],
            "claude_session_id": CLAUDE_SESSION_ID,
        })
    print(f"ok {len(data['ids'])}")
elif cmd == "set_project":
    # reply_db.py set_project ID "project_name"
    # Used by engage_reddit.py to attribute a posted reply to a recommended
    # project after the fact. Routes through the same PATCH endpoint as the
    # other status mutations (no SQL injection risk: project name travels
    # as a JSON body field, not interpolated into a shell command).
    rid, project = int(sys.argv[2]), sys.argv[3]
    _http_patch(rid, {"project_name": project})
    print(f"ok {rid}")
elif cmd == "status":
    # Per-cycle heartbeat used by skill/engage*.sh. Routes through the
    # /api/v1/replies/counts aggregate endpoint so this module has zero
    # direct-SQL paths.
    from http_api import api_get
    platform = sys.argv[2] if len(sys.argv) > 2 else None
    query = {"platform": platform} if platform else None
    resp = api_get("/api/v1/replies/counts", query=query)
    counts = ((resp or {}).get("data") or {}).get("counts") or []
    for row in counts:
        print(f"{row.get('status', '')} {row.get('count', 0)}")
elif cmd == "blocklist":
    # reply_db.py blocklist <subcmd> ...
    #
    # The escape hatch for the engagement-loop / bot defense. The Twitter,
    # LinkedIn, and GitHub engage prompts call:
    #   blocklist add <platform> <handle> --reason "<one-line judgment>"
    #     [--classification bot|engagement_loop] [--severity hard|soft]
    #     [--source-reply-id N]
    # when the model identifies a handle that should be permanently
    # filtered. Future candidates from the same handle are dropped silently
    # at /api/v1/replies POST time (server-side gate). See
    # migrations/2026-05-27_author_blocklist.sql for the full design.
    #
    # Also exposes:
    #   blocklist list [platform]   -> print active blocks for the install
    #   blocklist remove <platform> <handle>
    #   blocklist check <platform> <handle>  -> exit 0 if blocked, 1 if not
    from http_api import api_get, api_post
    sub = sys.argv[2] if len(sys.argv) > 2 else None
    if sub == "add":
        # blocklist add <platform> <handle> --reason "..." [opts]
        platform = sys.argv[3]
        handle = sys.argv[4]
        # naive arg parsing: --flag value pairs after position 5
        opts = {}
        i = 5
        while i < len(sys.argv):
            key = sys.argv[i]
            if key.startswith("--") and i + 1 < len(sys.argv):
                opts[key[2:].replace("-", "_")] = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        body = {
            "platform": platform,
            "handle": handle,
            "reason": opts.get("reason") or "engage prompt flagged",
            "classification": opts.get("classification", "bot"),
            "severity": opts.get("severity", "hard"),
            "added_by": opts.get("added_by", "engage_llm"),
            "source_session_id": CLAUDE_SESSION_ID,
        }
        if opts.get("source_reply_id"):
            try:
                body["source_reply_id"] = int(opts["source_reply_id"])
            except (TypeError, ValueError):
                pass
        if opts.get("project"):
            body["project"] = opts["project"]
        resp = api_post("/api/v1/blocklist", body=body)
        data = (resp or {}).get("data") or {}
        action = data.get("action", "?")
        row = data.get("row") or {}
        print(f"ok blocklist {action} {row.get('platform', platform)}/{row.get('handle', handle)} severity={row.get('severity', '?')}")
        # Stable stderr marker so log_run.py / grep-based observability can
        # count escape-hatch firings without re-querying the DB. Velocity-gate
        # auto-blocks land directly via the route.ts SQL path and do NOT pass
        # through here, so this marker is specific to LLM-judgment-driven
        # adds (or manual operator adds).
        classification = body["classification"]
        source = body.get("source_reply_id", "")
        reason_safe = (body.get("reason") or "").replace("\n", " ").replace("|", "/")[:200]
        print(
            f"[escape_hatch] platform={body['platform']} handle={body['handle']} "
            f"classification={classification} severity={body['severity']} "
            f"source_reply_id={source} action={action} reason=\"{reason_safe}\"",
            file=sys.stderr,
        )
    elif sub == "list":
        platform = sys.argv[3] if len(sys.argv) > 3 else None
        query = {"platform": platform} if platform else None
        resp = api_get("/api/v1/blocklist", query=query)
        rows = ((resp or {}).get("data") or {}).get("rows") or []
        if not rows:
            print("(no active blocks)")
        for r in rows:
            print(
                f"{r.get('platform','')} @{r.get('handle','')} "
                f"sev={r.get('severity','?')} "
                f"cls={r.get('classification','?')} "
                f"by={r.get('added_by','?')} "
                f"hits={r.get('hit_count', 0)} "
                f"reason={(r.get('reason') or '')[:80]}"
            )
    elif sub == "remove":
        import urllib.request, urllib.parse
        from identity import get_identity_header
        platform = sys.argv[3]
        handle = sys.argv[4].lstrip("@").lower()
        url = f"{API_BASE}/api/v1/blocklist/{urllib.parse.quote(platform)}/{urllib.parse.quote(handle)}"
        req = urllib.request.Request(
            url,
            method="DELETE",
            headers={"x-installation": get_identity_header()},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            print(f"ok removed {platform}/{handle}")
        except Exception as e:
            raise SystemExit(f"DELETE {url} failed: {e}")
    elif sub == "check":
        platform = sys.argv[3]
        handle = sys.argv[4].lstrip("@").lower()
        resp = api_get(
            "/api/v1/blocklist",
            query={"platform": platform},
        )
        rows = ((resp or {}).get("data") or {}).get("rows") or []
        match = next(
            (r for r in rows if (r.get("handle") or "").lower() == handle and r.get("severity") == "hard"),
            None,
        )
        if match:
            print(f"BLOCKED {platform}/{handle} cls={match.get('classification','?')} reason={(match.get('reason') or '')[:100]}")
            sys.exit(0)
        else:
            print(f"not blocked {platform}/{handle}")
            sys.exit(1)
    else:
        raise SystemExit(
            "usage: reply_db.py blocklist {add|list|remove|check} ..."
        )
