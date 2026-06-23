#!/usr/bin/env python3
"""Best-effort Sentry init for the social-autoposter Python posting pipeline.

Imported once from scripts/http_api.py (the central HTTP-lane client, pulled in
by ~100 pipeline scripts), so any pipeline process that talks to the s4l.ai API
gets Sentry error capture. Mirrors the Node .mcpb telemetry (mcp/src/telemetry.ts)
and the Fazm app. Sentry org: `mediar-n5`.

HARD RULE: this must NEVER raise into the caller. If sentry-sdk is missing or
the DSN is unset, init() is a silent no-op so the pipeline runs unchanged.
"""
from __future__ import annotations

import os

# Client-side DSN: safe to embed, same posture as the Node telemetry and Fazm's
# hardcoded Swift DSN. Overridable via env for dev. Empty -> Sentry disabled.
_EMBEDDED_DSN = "https://4d44ac907262c6545cf8681703528d04@o4507617161314304.ingest.us.sentry.io/4511598804336640"

_initialized = False


def init() -> None:
    """Initialize Sentry once per process. Idempotent and exception-safe."""
    global _initialized
    if _initialized:
        return
    # Set early so a failure below never re-attempts on every http_api import.
    _initialized = True

    dsn = os.environ.get("SAPS_SENTRY_DSN") or _EMBEDDED_DSN
    if not dsn:
        return
    try:
        import sentry_sdk
    except Exception:
        return  # sentry-sdk not installed in this runtime; stay silent
    try:
        env = "development" if os.environ.get("SAPS_ENV") == "development" else "production"
        sentry_sdk.init(
            dsn=dsn,
            environment=env,
            release=os.environ.get("SAPS_VERSION") or None,
            traces_sample_rate=0.0,  # errors only, no performance tracing
            send_default_pii=False,
        )
        _tag_install(sentry_sdk)
    except Exception:
        return


def _tag_install(sentry_sdk) -> None:
    """Attach the stable install_id so events are attributable + cross-ref the
    install-lane digest. Best-effort; identity.py is standalone (no http_api
    import) so there is no import cycle."""
    try:
        from identity import get_identity

        ident = get_identity() or {}
        iid = ident.get("install_id")
        host = ident.get("hostname")
        if iid:
            sentry_sdk.set_tag("install_id", str(iid))
        if host:
            sentry_sdk.set_tag("hostname", str(host))
    except Exception:
        pass


def capture_exception(err, tags=None) -> None:
    """Explicitly report an exception to Sentry with optional tags. Safe to call
    even if init() was never run or sentry-sdk is missing (silent no-op). Use for
    swallowed/handled errors that would otherwise never reach Sentry (the global
    excepthook only catches UNHANDLED ones)."""
    if not _initialized:
        return
    try:
        import sentry_sdk
    except Exception:
        return
    try:
        if tags:
            with sentry_sdk.push_scope() as scope:
                for k, v in tags.items():
                    scope.set_tag(str(k), str(v))
                sentry_sdk.capture_exception(err)
        else:
            sentry_sdk.capture_exception(err)
    except Exception:
        return


def flush(timeout: float = 2.0) -> None:
    """Block until queued events are sent (best-effort). Call before a short-lived
    or about-to-crash process exits so a just-captured event isn't dropped on
    teardown."""
    if not _initialized:
        return
    try:
        import sentry_sdk

        sentry_sdk.flush(timeout)
    except Exception:
        return
