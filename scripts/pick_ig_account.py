#!/opt/homebrew/bin/python3.11
"""Pick which Instagram account should post next.

Mirrors scripts/pick_project.py: inverse-recent-share weighting over
enabled `instagram.accounts` entries in config.json. Effective weight =
config_weight / (1 + posts in the last `recent_window_days`). An account
that has been posting heavily damps toward under-posted ones; never
selected above its raw weight; settles toward the configured weight ratio
over time.

Usage:
    pick_ig_account.py                  # print chosen username
    pick_ig_account.py --json           # full account record as JSON
    pick_ig_account.py --account NAME   # force a specific account (must be enabled)
    pick_ig_account.py --show-weights   # diagnostic table of weights vs recent posts
    pick_ig_account.py --list           # all enabled accounts, JSON array

Exit codes:
    0  picked successfully
    2  no enabled accounts (returns the legacy single-account default if any
       account is found, else exits 2)
    3  --account requested an unknown / disabled account
    4  config / DB error
"""

import argparse
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get

CONFIG_PATH = Path.home() / "social-autoposter" / "config.json"
ENV_PATH = Path.home() / "social-autoposter" / ".env"


def load_env():
    env = {}
    if ENV_PATH.exists():
        for ln in ENV_PATH.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def load_ig_cfg():
    cfg = json.loads(CONFIG_PATH.read_text())
    ig = cfg.get("instagram") or {}
    accounts = ig.get("accounts") or []
    window_days = int(ig.get("recent_window_days", 7))
    return accounts, window_days


def recent_posts_by_account(window_days):
    """Return {target_account: post count over last `window_days`} from
    media_posts WHERE status='posted' AND posted_urls ? 'instagram'.

    media_posts is the per-platform table for IG; we don't need the unified
    `posts` table here because all IG posts route through this pipeline.
    Served via the HTTP API (account-post-counts) so no DATABASE_URL is needed.
    """
    resp = api_get(
        "/api/v1/media-posts/account-post-counts",
        query={"window_days": int(window_days)},
    )
    return (resp.get("data") or {}).get("account_counts") or {}


def pick_account(accounts, window_days):
    """Inverse-recent-share weighted draw from enabled accounts.

    Effective weight is `posts_per_day * weight` (defaults: posts_per_day=
    global instagram.posts_per_account_per_day, weight=1). Dividing by
    (1 + recent_posts) damps over-posting toward the target rate.
    """
    enabled = [a for a in accounts if a.get("enabled") and float(a.get("weight", 0)) > 0]
    if not enabled:
        return None, {}, {}
    counts = recent_posts_by_account(window_days)
    # Pull global default for posts_per_day fallback.
    cfg = json.loads(CONFIG_PATH.read_text())
    global_ppd = int((cfg.get("instagram") or {}).get("posts_per_account_per_day", 5))
    effective = {
        a["username"]: (
            float(a["weight"]) * float(a.get("posts_per_day", global_ppd))
        ) / (1 + counts.get(a["username"], 0))
        for a in enabled
    }
    names = list(effective.keys())
    ws = [effective[n] for n in names]
    chosen_name = random.choices(names, weights=ws, k=1)[0]
    chosen = next(a for a in enabled if a["username"] == chosen_name)
    return chosen, counts, effective


def main():
    ap = argparse.ArgumentParser(description="Pick next IG account to post for")
    ap.add_argument("--json", action="store_true", help="emit full account record")
    ap.add_argument("--account", help="force a specific account (must be enabled)")
    ap.add_argument("--show-weights", action="store_true", help="diagnostic table")
    ap.add_argument("--list", action="store_true", help="list enabled accounts as JSON array")
    args = ap.parse_args()

    accounts, window_days = load_ig_cfg()

    if args.list:
        enabled = [a for a in accounts if a.get("enabled")]
        print(json.dumps(enabled, indent=2))
        return

    if args.show_weights:
        counts = recent_posts_by_account(window_days)
        cfg = json.loads(CONFIG_PATH.read_text())
        global_ppd = int((cfg.get("instagram") or {}).get("posts_per_account_per_day", 5))
        print(f"{'Account':25} {'Enabled':>8} {'Weight':>7} {'PPD':>5} {'Recent':>7} {'Effective':>10}")
        print("-" * 70)
        for a in accounts:
            ppd = int(a.get("posts_per_day", global_ppd))
            eff = (
                (float(a.get("weight", 0)) * ppd) / (1 + counts.get(a["username"], 0))
                if a.get("enabled") else 0
            )
            print(
                f"{a['username']:25} {str(a.get('enabled', False)):>8} "
                f"{a.get('weight', 0):>7} {ppd:>5} {counts.get(a['username'], 0):>7} "
                f"{eff:>10.3f}"
            )
        return

    if args.account:
        match = next(
            (a for a in accounts if a.get("username", "").lower() == args.account.lower()),
            None,
        )
        if not match:
            sys.stderr.write(f"unknown account: {args.account}\n")
            sys.exit(3)
        if not match.get("enabled"):
            sys.stderr.write(f"account disabled: {args.account}\n")
            sys.exit(3)
        chosen = match
    else:
        chosen, _, _ = pick_account(accounts, window_days)
        if chosen is None:
            # Legacy fallback: if config has no enabled accounts but the
            # single-account env vars exist, fall back to matt_diak so a
            # misconfigured config doesn't take the pipeline down. Exit 2
            # signals the caller it was a fallback so the harness can log.
            env = load_env()
            if env.get("IG_USER_ID") and env.get("IG_LONG_TOKEN"):
                sys.stderr.write(
                    "no enabled accounts in config; falling back to legacy IG_USER_ID/IG_LONG_TOKEN as 'matt_diak'\n"
                )
                chosen = {
                    "username": "matt_diak",
                    "ig_user_id_env": "IG_USER_ID",
                    "ig_long_token_env": "IG_LONG_TOKEN",
                    "weight": 1,
                    "enabled": True,
                }
            else:
                sys.stderr.write("no enabled accounts and no legacy env vars\n")
                sys.exit(2)

    if args.json:
        print(json.dumps(chosen, indent=2))
    else:
        print(chosen["username"])


if __name__ == "__main__":
    main()
