"""Single source of truth for the posting account on every platform.

Resolution order for each platform (first non-empty wins):

  1. Env var `AUTOPOSTER_<PLATFORM>_HANDLE` (used by the VM / per-account
     systemd or launchd units to override config.json without rewriting the
     checked-in file). Twitter retains the legacy `AUTOPOSTER_TWITTER_HANDLE`
     name as an alias.
  2. The matching field in `config.json` -> `accounts.<platform>.<field>`.
  3. The platform's durable identity store, when it has one. Today only
     twitter does: the keychain-independent cookie mirror stamps the live
     @handle at connect time (see twitter_cookie_mirror). This is the SAME
     ground truth the session posts through, so it's a safe last resort where
     a hardcoded fallback would not be. It exists because config.json is not a
     reliable single home for the handle: onboarding populated the mirror but
     not always config (the "posts land 0/N, no_account_configured" bug), so
     making config the ONLY source made it a single point of failure. There is
     deliberately NO hardcoded default anywhere in this resolver; a missing
     handle returns None and callers refuse rather than impersonate an account.

The handle is normalized:
  - leading `@` is stripped (twitter)
  - leading `u/` is stripped (reddit)
  - surrounding whitespace is stripped
So both `@matt_diak` and `matt_diak` resolve to `matt_diak`, both
`u/Deep_Ad1959` and `Deep_Ad1959` resolve to `Deep_Ad1959`, matching the
canonical shape stored in `posts.our_account` after the 2026-05-20 migration.

Returns None if neither source has a value. Callers should treat None as
"unknown account" and decline to scope per-account work that needs a handle
(e.g. dedupe filters).

Platform key map:
    twitter   -> accounts.twitter.handle           (env: AUTOPOSTER_TWITTER_HANDLE)
    reddit    -> accounts.reddit.username          (env: AUTOPOSTER_REDDIT_USERNAME)
    linkedin  -> accounts.linkedin.name            (env: AUTOPOSTER_LINKEDIN_NAME)
    github    -> accounts.github.username          (env: AUTOPOSTER_GITHUB_USERNAME)
    moltbook  -> accounts.moltbook.username        (env: AUTOPOSTER_MOLTBOOK_USERNAME)
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional

_PLATFORM_CONFIG_FIELD = {
    "twitter":  ("twitter",  "handle"),
    "x":        ("twitter",  "handle"),   # alias for the canonical post-platform
    "reddit":   ("reddit",   "username"),
    "linkedin": ("linkedin", "name"),
    "github":   ("github",   "username"),
    "moltbook": ("moltbook", "username"),
}

_PLATFORM_ENV_NAME = {
    "twitter":  "AUTOPOSTER_TWITTER_HANDLE",
    "x":        "AUTOPOSTER_TWITTER_HANDLE",
    "reddit":   "AUTOPOSTER_REDDIT_USERNAME",
    "linkedin": "AUTOPOSTER_LINKEDIN_NAME",
    "github":   "AUTOPOSTER_GITHUB_USERNAME",
    "moltbook": "AUTOPOSTER_MOLTBOOK_USERNAME",
}


def normalize(handle: Optional[str]) -> Optional[str]:
    """Canonicalize a raw account handle.

    Drops leading `@` (twitter) and `u/` (reddit) plus surrounding
    whitespace. Returns None for empty input.
    """
    if not handle:
        return None
    h = handle.strip()
    if h.startswith("@"):
        h = h[1:]
    elif h.lower().startswith("u/"):
        h = h[2:]
    h = h.strip()
    return h or None


@lru_cache(maxsize=1)
def _load_config() -> dict:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg_path = os.path.join(repo_root, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _durable_handle(key: str) -> Optional[str]:
    """Best-effort read of the platform's durable identity store, or None.

    The durable store is written once at connect time and survives a fresh
    install / keychain re-lock / Cookies-DB wipe, so it's the ground-truth last
    resort when neither env nor config carries the handle. Only twitter has one
    today (the cookie mirror); other platforms return None (env + config only).
    Never raises: an unimportable/empty store must not break resolution.
    """
    if key in ("twitter", "x"):
        try:
            import twitter_cookie_mirror  # optional dep; scripts-dir sibling
            return normalize((twitter_cookie_mirror.load_meta() or {}).get("handle"))
        except Exception:
            return None
    return None


def resolve(platform: str) -> Optional[str]:
    """Return the normalized posting handle for `platform`, or None."""
    key = (platform or "").strip().lower()
    if key not in _PLATFORM_CONFIG_FIELD:
        return None

    env_name = _PLATFORM_ENV_NAME[key]
    env_value = normalize(os.environ.get(env_name))
    if env_value:
        return env_value

    section, field = _PLATFORM_CONFIG_FIELD[key]
    cfg = _load_config()
    accounts = cfg.get("accounts") or {}
    block = accounts.get(section) or {}
    config_value = normalize(block.get(field))
    if config_value:
        return config_value

    # Last resort: the durable identity store (twitter cookie mirror). Keeps
    # config.json from being a single point of failure without ever guessing.
    return _durable_handle(key)


def require(platform: str) -> str:
    """Like resolve() but raises if no handle is configured."""
    h = resolve(platform)
    if not h:
        section, field = _PLATFORM_CONFIG_FIELD.get(
            (platform or "").lower(), ("?", "?")
        )
        env_name = _PLATFORM_ENV_NAME.get(
            (platform or "").lower(), f"AUTOPOSTER_{platform.upper()}_HANDLE"
        )
        raise RuntimeError(
            f"No account configured for platform={platform!r}. "
            f"Set env {env_name} or accounts.{section}.{field} in config.json."
        )
    return h


# Backwards-compatible shim so the existing twitter-only call site keeps
# working without churn. `from twitter_account import resolve_handle` will
# continue to work; new code should call `account_resolver.resolve('twitter')`.
def resolve_handle() -> Optional[str]:
    return resolve("twitter")


def require_handle() -> str:
    return require("twitter")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        plat = sys.argv[1]
    else:
        plat = "twitter"
    h = resolve(plat)
    if h:
        sys.stdout.write(h + "\n")
        sys.exit(0)
    sys.stderr.write(f"no handle configured for platform={plat}\n")
    sys.exit(1)
