#!/usr/bin/env python3
"""twitter_cookie_mirror.py - local 0600 mirror of the managed X session cookies.

Why this exists (Gap B, 2026-06-02)
-----------------------------------
On a persistent (non-VM) machine the server-side session store
(social_accounts.session_cookies) is SKIPPED during connect_x because there is no
social_accounts row to attach the cookies to. That left Chrome's own encrypted
Cookies SQLite as the ONLY thing keeping the X session across a Chrome relaunch.

That store is not durable on a headless / SSH box: macOS encrypts Chrome cookies
with the per-app `Chrome Safe Storage` key, which lives in the login keychain.
When the keychain re-locks (idle ~5 min) between the import and the next Chrome
launch, the freshly-launched Chrome cannot read the Safe Storage key, cannot
decrypt the existing blobs, and reinitializes the Cookies DB to an empty schema.
The imported session silently evaporates between `connect_x` and the first cycle.

This module is the keychain-independent durability layer. On a successful import
connect_x writes the validated x.com/twitter.com cookies (CDP-shaped, straight
from Network.getAllCookies) here as plaintext JSON, and the cycle preflight
(restore_twitter_session.py, invoked from skill/lib/twitter-backend.sh) re-injects
them via CDP whenever the live session comes up logged out. A keychain re-lock or
a wiped Cookies DB is therefore no longer fatal — the next cycle restores.

Security
--------
The file grants access to the X account: it is exactly as sensitive as the Chrome
profile itself, and is written 0600 (owner read/write only). Treat it like a
token. It is intentionally NOT encrypted — the whole point is to survive a locked
keychain, so adding a keychain-derived key would reintroduce the dependency this
file exists to remove. On a multi-user host, restrict the home directory.

CLI (debug / doctor):
    python3 twitter_cookie_mirror.py count   # prints the mirrored cookie count
    python3 twitter_cookie_mirror.py path    # prints the mirror file path
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Sibling of the harness profile dir, NOT inside it: a VM profile reseed wipes
# the profile but a persistent machine keeps this file across Chrome relaunches.
# (On a VM the server-side store is the durable path; the mirror just stays empty
# there and restore_twitter_session falls through to the API.)
MIRROR_PATH = (
    Path.home() / ".claude" / "browser-profiles" / "browser-harness.x-cookies.json"
)


def save_cookies(cookies, handle: str | None = None) -> int:
    """Write the given CDP-shaped cookies to the 0600 mirror. Returns count saved.

    Atomic (temp file + os.replace) so a crash mid-write never leaves a partial
    JSON that the reader would choke on. No-op (returns 0) on an empty list."""
    clean = [c for c in (cookies or []) if isinstance(c, dict) and c.get("name")]
    if not clean:
        return 0
    # Never downgrade a previously-resolved @handle to null. Live handle
    # resolution (_resolve_live_handle) is best-effort and races React hydration,
    # so a re-import can legitimately arrive with handle=None even though we
    # already knew the account. Clobbering it would drop the handle the dashboard
    # + account_resolver rely on. Carry the prior handle forward in that case.
    if not handle:
        prev = (_read().get("handle") or "") if MIRROR_PATH.exists() else ""
        if prev:
            handle = prev
    MIRROR_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"handle": handle, "saved_at": int(time.time()), "cookies": clean}
    tmp = MIRROR_PATH.with_name(MIRROR_PATH.name + ".tmp")
    # Create with 0600 from the start so the secret is never briefly world-readable.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, MIRROR_PATH)
    try:
        os.chmod(MIRROR_PATH, 0o600)
    except OSError:
        pass
    return len(clean)


def _read() -> dict:
    try:
        with open(MIRROR_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_cookies() -> list:
    """Return the mirrored CDP-shaped cookies, or [] if no/invalid mirror."""
    cks = _read().get("cookies")
    return cks if isinstance(cks, list) else []


def load_meta() -> dict:
    """Return {handle, saved_at, count} for the mirror, or {} if absent."""
    data = _read()
    if not data:
        return {}
    return {
        "handle": data.get("handle"),
        "saved_at": data.get("saved_at"),
        "count": len(data.get("cookies") or []),
    }


def _cli(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "count"
    if cmd == "path":
        print(MIRROR_PATH)
        return 0
    if cmd == "count":
        print(len(load_cookies()))
        return 0
    if cmd == "meta":
        print(json.dumps(load_meta()))
        return 0
    print(f"usage: {Path(sys.argv[0]).name} [count|path|meta]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
