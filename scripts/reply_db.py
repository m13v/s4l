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

CLAUDE_SESSION_ID = os.environ.get("CLAUDE_SESSION_ID") or None
API_BASE = (os.environ.get("AUTOPOSTER_API_BASE") or "https://s4l.ai").rstrip("/")


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
