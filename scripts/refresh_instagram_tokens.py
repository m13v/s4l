#!/usr/bin/env python3
"""Refresh Instagram Graph API long-lived tokens before they expire.

Instagram long-lived user tokens are valid for ~60 days. Each call to the
refresh_access_token endpoint extends the lifetime by another 60 days. The
token must be at least 24 hours old to be refreshable, and Meta recommends
refreshing well before expiry (we use a 14-day buffer).

This script:
  1. Iterates over every account in config.json -> instagram.accounts[].
  2. Reads the current token + expiry from ~/instagram-graph-api/.env via the
     ig_long_token_env / derived IG_TOKEN_EXPIRES_<suffix> key.
  3. If the token expires within REFRESH_BUFFER_DAYS, calls the Graph API
     refresh_access_token endpoint and rewrites the .env file in place
     (atomic: write to tempfile then os.replace).
  4. Prints a machine-readable SUMMARY line for the wrapper to log via
     scripts/log_run.py.

The .env file is the SINGLE source of truth — update_instagram_stats.py and
scan_instagram_comments.py both read it on every invocation, so a refreshed
token is picked up by the next pipeline run with no daemon-restart needed.

Usage:
    python3 scripts/refresh_instagram_tokens.py [--quiet] [--force] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

IG_ENV_PATH = Path.home() / "instagram-graph-api" / ".env"
GRAPH = "https://graph.instagram.com"
SA_CONFIG = Path(__file__).resolve().parent.parent / "config.json"

# Refresh tokens that expire within this many days. 14 days gives us 2 weeks
# of headroom for cron failures, network outages, or attention lapses.
REFRESH_BUFFER_DAYS = 14
# Meta requires tokens to be at least 24h old before they can be refreshed.
MIN_TOKEN_AGE_HOURS = 24


def load_env_lines() -> list[str]:
    """Return the .env file as a list of raw lines (preserving comments +
    blank lines), so we can rewrite individual keys without reformatting."""
    if not IG_ENV_PATH.exists():
        return []
    return IG_ENV_PATH.read_text().splitlines()


def env_dict_from_lines(lines: list[str]) -> dict[str, str]:
    env = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def write_env_atomic(lines: list[str]):
    """Rewrite the .env file from `lines`. Atomic via temp-file + os.replace
    so a Ctrl-C or crash mid-write can't truncate the file."""
    dir_ = IG_ENV_PATH.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.tmp.", dir=str(dir_))
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines))
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, IG_ENV_PATH)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def expires_key_for(token_key: str) -> str:
    """Derive the IG_TOKEN_EXPIRES env-var name from the IG_LONG_TOKEN one.

    IG_LONG_TOKEN -> IG_TOKEN_EXPIRES
    IG_LONG_TOKEN_MATTHEWHEARTFUL -> IG_TOKEN_EXPIRES_MATTHEWHEARTFUL
    IG_LONG_TOKEN_OMIDOTME -> IG_TOKEN_EXPIRES_OMIDOTME
    """
    if not token_key.startswith("IG_LONG_TOKEN"):
        return ""
    return "IG_TOKEN_EXPIRES" + token_key[len("IG_LONG_TOKEN"):]


def parse_expires(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    # Accept both "2026-07-05T23:06:44Z" and "2026-07-05T23:06:44+00:00".
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def format_expires(dt: datetime) -> str:
    """Match the existing .env convention: ISO-8601 UTC with trailing Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh_token(long_token: str) -> dict:
    qs = urllib.parse.urlencode({
        "grant_type": "ig_refresh_token",
        "access_token": long_token,
    })
    url = f"{GRAPH}/refresh_access_token?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RefreshError(f"HTTP {e.code}: {body[:300]}") from e


class RefreshError(Exception):
    pass


def update_line(lines: list[str], key: str, value: str) -> list[str]:
    """Return a new list with the line `<key>=<old>` replaced by `<key>=<value>`.
    If the key isn't present, appends `<key>=<value>` at the end."""
    out = []
    found = False
    prefix = f"{key}="
    for line in lines:
        if line.strip().startswith(prefix) or line.startswith(prefix):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="Refresh every token regardless of expiry buffer")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be refreshed but don't call the API")
    parser.add_argument("--account", default=None,
                        help="Only refresh this account (default: all accounts)")
    args = parser.parse_args()

    def log(msg: str):
        if not args.quiet:
            print(msg)

    if not IG_ENV_PATH.exists():
        print(f"[refresh-ig-tokens] env file missing: {IG_ENV_PATH}")
        print("SUMMARY:REFRESHED=0 SKIPPED=0 FAILED=0 ACCOUNTS=0")
        sys.exit(0)

    try:
        cfg = json.loads(SA_CONFIG.read_text())
    except FileNotFoundError:
        cfg = {}
    accounts_cfg = ((cfg.get("instagram") or {}).get("accounts") or [])

    if args.account:
        accounts_cfg = [a for a in accounts_cfg
                        if a.get("username", "").lower() == args.account.lower()]
    if not accounts_cfg:
        print("[refresh-ig-tokens] no instagram accounts in config")
        print("SUMMARY:REFRESHED=0 SKIPPED=0 FAILED=0 ACCOUNTS=0")
        sys.exit(0)

    lines = load_env_lines()
    env = env_dict_from_lines(lines)
    now = datetime.now(timezone.utc)
    buffer_secs = REFRESH_BUFFER_DAYS * 86400

    refreshed = 0
    skipped = 0
    failed = 0

    for account_cfg in accounts_cfg:
        username = account_cfg.get("username", "")
        token_key = account_cfg.get("ig_long_token_env", "IG_LONG_TOKEN")
        exp_key = expires_key_for(token_key)
        if not exp_key:
            log(f"[refresh-ig-tokens] {username}: cannot derive expires key from {token_key}; skipping")
            skipped += 1
            continue

        cur_token = env.get(token_key)
        if not cur_token:
            log(f"[refresh-ig-tokens] {username}: no value for {token_key}; skipping")
            skipped += 1
            continue

        cur_exp_raw = env.get(exp_key)
        cur_exp = parse_expires(cur_exp_raw)
        if cur_exp is None and not args.force:
            log(f"[refresh-ig-tokens] {username}: {exp_key} unparseable ({cur_exp_raw!r}); skipping (use --force to refresh anyway)")
            skipped += 1
            continue

        if cur_exp is not None and not args.force:
            remaining = (cur_exp - now).total_seconds()
            if remaining > buffer_secs:
                days_left = remaining / 86400
                log(f"[refresh-ig-tokens] {username}: {days_left:.1f}d remaining (> {REFRESH_BUFFER_DAYS}d buffer); skipping")
                skipped += 1
                continue
            if remaining < 0:
                log(f"[refresh-ig-tokens] {username}: EXPIRED {(-remaining)/86400:.1f}d ago; attempting refresh anyway (Meta may reject)")

        if args.dry_run:
            log(f"[refresh-ig-tokens] {username}: DRY-RUN would refresh {token_key} (exp {cur_exp_raw})")
            refreshed += 1
            continue

        log(f"[refresh-ig-tokens] {username}: refreshing {token_key} (current exp {cur_exp_raw})")
        try:
            resp = refresh_token(cur_token)
        except RefreshError as e:
            log(f"[refresh-ig-tokens] {username}: REFRESH FAILED: {e}")
            failed += 1
            continue
        except Exception as e:
            log(f"[refresh-ig-tokens] {username}: REFRESH FAILED (unexpected): {e}")
            failed += 1
            continue

        new_token = resp.get("access_token")
        expires_in = resp.get("expires_in")
        if not new_token or not expires_in:
            log(f"[refresh-ig-tokens] {username}: refresh response missing fields: {resp}")
            failed += 1
            continue

        new_exp_dt = datetime.now(timezone.utc).fromtimestamp(time.time() + expires_in, tz=timezone.utc)
        new_exp_str = format_expires(new_exp_dt)

        lines = update_line(lines, token_key, new_token)
        lines = update_line(lines, exp_key, new_exp_str)
        env[token_key] = new_token
        env[exp_key] = new_exp_str

        log(f"[refresh-ig-tokens] {username}: OK, new expiry {new_exp_str} (~{expires_in/86400:.0f}d)")
        refreshed += 1

    if refreshed and not args.dry_run:
        write_env_atomic(lines)
        log(f"[refresh-ig-tokens] wrote {IG_ENV_PATH}")

    print(
        f"SUMMARY:REFRESHED={refreshed} SKIPPED={skipped} FAILED={failed} "
        f"ACCOUNTS={len(accounts_cfg)}"
    )


if __name__ == "__main__":
    main()
