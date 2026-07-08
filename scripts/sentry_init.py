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
_install_id_cache: str | None = None


def _install_id() -> str | None:
    """Cached install_id lookup, used to fingerprint events per-install (see
    capture_exception/capture_message). Best-effort: "" (not None) is cached on
    failure so a broken identity.py doesn't re-attempt the read on every call."""
    global _install_id_cache
    if _install_id_cache is not None:
        return _install_id_cache or None
    try:
        from identity import get_identity

        ident = get_identity() or {}
        _install_id_cache = str(ident.get("install_id") or "")
    except Exception:
        _install_id_cache = ""
    return _install_id_cache or None


def init() -> None:
    """Initialize Sentry once per process. Idempotent and exception-safe."""
    global _initialized
    if _initialized:
        return
    # Set early so a failure below never re-attempts on every http_api import.
    _initialized = True

    dsn = os.environ.get("S4L_SENTRY_DSN") or _EMBEDDED_DSN
    if not dsn:
        return
    try:
        import sentry_sdk
    except Exception:
        return  # sentry-sdk not installed in this runtime; stay silent
    try:
        env = "development" if os.environ.get("S4L_ENV") == "development" else "production"
        sentry_sdk.init(
            dsn=dsn,
            environment=env,
            release=os.environ.get("S4L_VERSION") or None,
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
        with sentry_sdk.push_scope() as scope:
            for k, v in (tags or {}).items():
                scope.set_tag(str(k), str(v))
            # Mix install_id into the grouping key so the SAME exception on two
            # different customer boxes lands in two different issues, not one
            # conflated issue whose "latest event" can silently belong to a
            # different install than the one you filtered for (default grouping
            # is by exception type + location, which ignores tags entirely).
            iid = _install_id()
            if iid:
                scope.fingerprint = ["{{ default }}", iid]
            sentry_sdk.capture_exception(err)
    except Exception:
        return


def capture_message(message, level="error", tags=None, extra=None) -> None:
    """Report a handled, non-exception condition to Sentry (e.g. a post that
    failed gracefully and returned a reason instead of raising). The global
    excepthook only catches UNHANDLED exceptions, so operational failures that
    are caught + reported as a result count would otherwise never reach Sentry.
    Safe to call even if init() never ran or sentry-sdk is missing (no-op).

    `extra` is a dict of larger structured values (e.g. the scheduled-task
    registry summary) attached as event extras — tags are capped at 200 chars
    and would truncate them. Added 2026-07-06: the "needs attention: missing"
    events carried no registry detail, which cost hours on the Karol case.

    Fingerprint: mixed with install_id (see capture_exception) — capture_message
    events group by the message TEXT by default, so two installs hitting the
    identically-worded stall alert (e.g. "autopilot stalled... sustained N
    checks") would otherwise merge into one issue and hide per-install history."""
    if not _initialized:
        return
    try:
        import sentry_sdk
    except Exception:
        return
    try:
        with sentry_sdk.push_scope() as scope:
            for k, v in (tags or {}).items():
                scope.set_tag(str(k), str(v))
            for k, v in (extra or {}).items():
                scope.set_extra(str(k), v)
            iid = _install_id()
            if iid:
                scope.fingerprint = ["{{ default }}", iid]
            sentry_sdk.capture_message(str(message), level=level)
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
