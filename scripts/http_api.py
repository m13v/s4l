#!/usr/bin/env python3
"""Shared HTTP helper for the s4l.ai API endpoints.

All Reddit-pipeline (and friends) writes/reads route through here, carrying
either:
  - X-Installation header (default lane, open-source identity), or
  - Authorization: Bearer $AUTOPOSTER_API_KEY when the key is set in env
    (server-internal callers / endpoints that still use requireApiKey).

Both headers are sent on every request when both are available, so a route
that uses resolveAuth picks the install lane while a route that uses
requireApiKey picks the bearer lane. No caller-side branching needed.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Best-effort Sentry init (no-op if sentry-sdk missing or DSN unset). http_api
# is the central HTTP-lane client (~100 pipeline scripts import it), so this one
# hook gives the whole Python pipeline error capture. Mirrors mcp/src/telemetry.ts.
try:
    import sentry_init as _sentry_init

    _sentry_init.init()
except Exception:
    pass


def _build_ssl_context() -> ssl.SSLContext:
    """Pin a known-good trust store, immune to a bad inherited SSL_CERT_FILE.

    The MCP/host app can inject SSL_CERT_FILE / SSL_CERT_DIR pointing at a
    bundle that lacks the right roots, which makes urllib raise
    CERTIFICATE_VERIFY_FAILED only inside the spawned subprocess (TLS works
    fine in a normal shell). We resolve the trust store deliberately here
    instead of trusting whatever env we inherit:

      1. inherited SSL_CERT_FILE, but only if the path exists AND yields a
         context with at least one trusted root;
      2. the platform default store;
      3. certifi, if installed.

    The get_ca_certs() check is what rejects a bad inherited path: an empty
    trust store silently falls through to the next candidate.
    """
    candidates = []
    env_file = os.environ.get("SSL_CERT_FILE")
    if env_file and os.path.exists(env_file):
        candidates.append(env_file)
    candidates.append(None)  # platform default
    for cafile in candidates:
        try:
            ctx = ssl.create_default_context(cafile=cafile)
            if ctx.get_ca_certs():
                return ctx
        except Exception:
            continue
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CONTEXT = _build_ssl_context()

ENV_PATH = os.path.expanduser("~/social-autoposter/.env")


def load_env():
    """Load ~/social-autoposter/.env into os.environ (setdefault, never clobber).

    Generic dotenv loader, not DB-specific: callers need it for keys like
    MOLTBOOK_API_KEY / AUTOPOSTER_API_KEY / AUTOPOSTER_API_BASE. Lives here
    (rather than the now-removed db.py) so HTTP-only scripts have one import.
    """
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def _base_url():
    return os.environ.get("AUTOPOSTER_API_BASE", "https://s4l.ai").rstrip("/")


def _headers():
    from identity import get_identity_header
    headers = {
        "Content-Type": "application/json",
        "X-Installation": get_identity_header(),
    }
    bearer = (os.environ.get("AUTOPOSTER_API_KEY") or "").strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
        headers["x-api-key"] = bearer
    return headers


def _request(method: str, path: str, body: dict | None = None,
             query: dict | None = None, ok_on_conflict: bool = False,
             ok_on_404: bool = False):
    """Generic request runner with retries.

    Returns parsed JSON. Raises SystemExit on terminal failure.

    method: GET | POST | PATCH | DELETE
    body: JSON body for write methods
    query: optional dict for ?k=v query-string (GET / DELETE)
    ok_on_conflict: when True, a 409 body is returned (not raised)
    ok_on_404: when True, a 404 returns {"_not_found": True}
    """
    url = f"{_base_url()}{path}"
    if query:
        # Drop None values so we don't send 'key=None' string.
        qs = urllib.parse.urlencode(
            {k: v for k, v in query.items() if v is not None},
            doseq=True,
        )
        if qs:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{qs}"

    data = None
    if body is not None:
        data = json.dumps(body).encode()

    delays = [1, 3, 9]
    last_err = None
    for attempt, delay in enumerate(delays, 1):
        try:
            req = urllib.request.Request(
                url, data=data, headers=_headers(), method=method,
            )
            with urllib.request.urlopen(
                req, timeout=30, context=_SSL_CONTEXT
            ) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode(errors="replace")
            if e.code == 409 and ok_on_conflict:
                try:
                    return json.loads(body_txt)
                except Exception:
                    return {"error": "conflict"}
            if e.code == 404 and ok_on_404:
                return {"_not_found": True}
            if 400 <= e.code < 500:
                raise SystemExit(
                    f"[http_api] {method} {path} HTTP {e.code}: {body_txt}"
                )
            last_err = e
            print(
                f"[http_api] {method} {path} HTTP {e.code} attempt {attempt}: "
                f"{body_txt[:120]}",
                file=sys.stderr,
            )
        except Exception as e:
            last_err = e
            print(
                f"[http_api] {method} {path} attempt {attempt}: {e}",
                file=sys.stderr,
            )
        if attempt < len(delays):
            time.sleep(delay)
    raise SystemExit(
        f"[http_api] {method} {path} failed after {len(delays)} attempts: "
        f"{last_err}"
    )


def api_get(path: str, query: dict | None = None, ok_on_404: bool = False):
    """GET path with optional query dict. Returns parsed JSON."""
    return _request("GET", path, query=query, ok_on_404=ok_on_404)


def api_post(path: str, body: dict, ok_on_conflict: bool = False, ok_on_404: bool = False):
    """POST body to path. ok_on_conflict=True returns the 409 body;
    ok_on_404=True returns {_not_found: True} on 404 instead of raising."""
    return _request("POST", path, body=body, ok_on_conflict=ok_on_conflict, ok_on_404=ok_on_404)


def api_patch(path: str, body: dict, ok_on_conflict: bool = False, ok_on_404: bool = False):
    """PATCH body to path. ok_on_conflict=True returns the 409 body;
    ok_on_404=True returns {_not_found: True} on 404 instead of raising."""
    return _request("PATCH", path, body=body, ok_on_conflict=ok_on_conflict, ok_on_404=ok_on_404)


def api_delete(path: str, query: dict | None = None):
    """DELETE path. Optional query string."""
    return _request("DELETE", path, query=query)
